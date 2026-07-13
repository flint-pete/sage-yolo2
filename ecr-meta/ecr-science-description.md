# YOLO Object Counter for Edge AI

## Science

Real-time object detection and counting from camera sensors is foundational
to agriculture analytics, wildlife monitoring, traffic engineering, and
infrastructure management.  Traditional approaches require streaming raw
video to the cloud for post-hoc analysis — incurring high bandwidth costs,
latency, and privacy concerns.  By deploying state-of-the-art YOLO
(You Only Look Once) models directly on edge nodes, we perform inference in
milliseconds at the point of data collection, publishing only compact
measurement records (per-class counts and optional annotated images) to the
Sage data store.

## Model

This plugin ships **YOLO11x** — the largest variant in the Ultralytics v11
family (56.9 M parameters, 54.7 % mAP on COCO val2017).  YOLO11x provides
an excellent balance between accuracy and throughput for edge deployments on
GPU-equipped nodes.  Smaller variants (yolo11n, yolo11s) can be selected at
runtime via the `--model` flag to trade accuracy for speed.  The model
recognises all 80 COCO object classes and can be filtered at runtime to
count only specific classes of interest (e.g. `--classes person,car,bird`).

## Architecture

The plugin captures frames from any Waggle camera abstraction (named camera,
RTSP stream, or static image file) at a configurable interval, runs YOLO
inference on the GPU, draws bounding-box annotations on the image, and
publishes per-class counts via `plugin.publish()` and annotated images via
`plugin.upload_file()`.  A single iteration completes in under 50 ms on
NVIDIA Blackwell (GB10), leaving the GPU available for concurrent workloads.

## Runtime Modes

By default (`--continuous Y`) the plugin loops indefinitely, capturing and
publishing every `--interval` seconds.  With `--continuous N` it performs a
single shot and exits.

| Argument         | Default | Description                                                                 |
|------------------|---------|-----------------------------------------------------------------------------|
| `--continuous`   | `Y`     | `Y` = loop every `--interval` seconds; `N` = single-shot then exit.         |
| `--interval`     | `30`    | Seconds between captures (camera mode only).                                |
| `--max-runtime`  | `0`     | When in continuous mode, self-exit after this many seconds (`0` = run forever). Lets a scheduled job behave like one long bounded single-shot. Ignored when `--continuous N`. |

## Windowed GPU Sharing

Some edge nodes carry a **single GPU** that must be shared between multiple
always-on continuous plugins — which cannot truly co-run without contending
for GPU memory and compute.  The `--max-runtime` flag turns YOLO into a
*bounded* continuous job that occupies the GPU for a fixed window and then
voluntarily releases it.

For example, on a node where YOLO shares one GPU with the BioCLIP plugin, a
scheduler (cron) starts each plugin at a fixed minute and each runs a bounded
10-minute window:

- **:00 — YOLO** runs `--continuous Y --max-runtime 600 --interval 15`,
  sampling roughly every 15 s (~40 frames) for 10 minutes, then self-exits.
- **:20 — BioCLIP** takes the next window after a 10-minute guard-band that
  guarantees YOLO has fully exited and freed the GPU.

The 10-minute guard-bands between windows (e.g. :10–:20) prevent overlap from
slow model teardown, leaving the GPU free at the boundaries.  Total GPU
occupancy is roughly 20 minutes per hour, allowing both workloads to coexist
on one device without a dedicated GPU each.

> **Subtle behavior — `--max-runtime` is WALL-CLOCK, not inference time.** The
> timer starts when the process starts, so model load, the first camera
> connection, and any startup overhead all count against the window. YOLO's
> model is small (~seconds), so the effective sampling window is close to the
> full `--max-runtime`. But the practical implication is general: the actual
> time spent sampling is `--max-runtime` *minus* startup, not the full value.
> If you need a guaranteed amount of *inference* time, size `--max-runtime`
> above your target to absorb the cold start, and keep the guard-band wide
> enough that a slow start can't push the self-exit into the next plugin's
> window.

## Measurements Published

| Topic                     | Type  | Description                        |
|---------------------------|-------|------------------------------------|
| `env.count.<class_name>`  | int   | Count of each detected class       |
| `env.count.total`         | int   | Total objects detected in frame (published EVERY cycle, even at 0 — heartbeat) |
| `upload` (annotated JPEG) | image | Annotated frame with bounding boxes, uploaded selectively — see Saving Images |

## Saving Images: `--save-match`

The plugin separates *what it counts* (always published) from *what frames it
saves* (selective). Counts and the `env.count.total` heartbeat publish every
cycle; uploading an annotated JPEG is the expensive part, so it is governed
separately:

- **`--save-match`** (preferred) — a comma-separated OR-list of `Class:confidence`
  rules. The annotated frame is uploaded when ANY detection matches ANY rule.
  Class is matched **case-insensitively** and **exactly** against the COCO class
  name (no substring). Examples:

  ```
  --save-match "bird:0.5,cat:0.6"   # save when a bird >=0.5 OR a cat >=0.6 is seen
  --save-match "*:0.5"               # save any frame with a detection >=0.5
  ```

  When `--save-match` is set it REPLACES the legacy upload-every-cycle behavior.

- **`--upload-image`** (deprecated, back-compat) — only consulted when
  `--save-match` is omitted. `Y` (default) uploads every cycle that has
  detections (legacy behavior); `N` never uploads.

To save selectively, prefer `--save-match`. To save nothing (counts/heartbeat
only), set `--upload-image N` and omit `--save-match`.

### Performance Telemetry

Following the standard Sage convention (as used by `avian-diversity-monitoring`
and other production plugins on TAFT nodes), every cycle publishes nanosecond
timing for the three execution phases, making cold-start cost and per-cycle
latency observable from the data plane (e.g. whether a bounded GPU window is
spent on model load vs. inference):

| Topic | Unit | Frequency | Description |
|-------|------|-----------|-------------|
| `plugin.duration.loadmodel` | ns | once | Load + move the YOLO model to device |
| `plugin.duration.input`     | ns | per cycle | Capture/snapshot + decode the frame |
| `plugin.duration.inference` | ns | per cycle | Run YOLO detection |

These publish every cycle regardless of detections, so they also serve as a
liveness/heartbeat signal on empty scenes.

## Testing

The plugin ships two complementary, self-contained test suites (no node,
network, or Beehive access required):

**1. Local detection tests** (`tests/test_yolo_local.py`) — run the real YOLO11x
model against committed test images (`tests/test-images/`) through the pywaggle
test harness, validating per-class counts and annotated-image output. Results
(report + annotated JPEGs) land under `tests/output/`.

**2. Save-match unit tests** (`tests/test_save_match.py`) — 29 pure-Python unit
tests (no model load) covering the `--save-match` rule grammar and matching in
`save_match.py`: rule parsing, the `*` wildcard, case-insensitive exact matching
against COCO class names, the OR-of-rules semantics, the no-substring rule,
malformed-rule fail-fast (bad confidence / empty name → clear error, non-zero
exit), and out-of-range confidence rejection.

```bash
python3 tests/test_save_match.py    # => "29 passed, 0 failed (29 total)"
```

> **Note on `save_match.py`:** the matcher module and its test file are kept
> **byte-identical** across the sage-yolo, birdnet, and sage-bioclip repos (the
> three plugins do not share a Python package yet). When changing matcher
> behavior, update all three copies together so they cannot drift.

## Example Use Cases

- **Urban traffic monitoring** — count vehicles, pedestrians, and cyclists
  at intersections every 30 seconds.
- **Bird counting at feeders** — `--classes bird --interval 60` on
  camera-equipped nodes near wildlife stations.
- **Parking occupancy** — count `car` in a fixed-view parking lot camera.
- **Construction site safety** — detect `person` in restricted zones.
