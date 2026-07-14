# sage-yolo2 — YOLO11x Object Counter (pywaggle2 Cache Consumer)

## Science

Real-time object detection and counting from camera sensors underpins
agriculture analytics, wildlife monitoring, traffic engineering, and
infrastructure management. Running inference at the edge — instead of
streaming raw video to the cloud — cuts bandwidth, latency, and privacy
exposure: only compact measurement records (per-class counts and, optionally,
annotated frames) leave the node.

sage-yolo2 pushes that edge-first idea one step further. Rather than every
analysis plugin opening its own camera, a single **producer** captures each
frame once and writes it to a shared on-node cache; any number of **consumers**
— sage-yolo2 among them — read the same frame. **One camera open, one decode,
many consumers.** This removes the N-plugins-means-N-camera-connections
bottleneck that limits how many models a node can run against one sensor.

## Model

This plugin ships **YOLO11x** — the largest Ultralytics v11 variant (56.9 M
parameters, 54.7 % mAP on COCO val2017) — an accuracy/throughput sweet spot for
GPU-equipped edge nodes. Smaller variants (`yolo11n`, `yolo11s`) can be
selected at runtime via `--model` to trade accuracy for speed. The model
recognises all 80 COCO classes and can be filtered at runtime to count only
classes of interest (e.g. `--classes person,car,bird`).

## Architecture: producer → shared cache → consumer

In production sage-yolo2 does **not open a camera**. A producer plugin
(`image-sampler2`) opens the camera once and writes self-describing JPEG frames
into a shared `/local-cache` directory provided by the `wes-local-cache-manager`
WES component. sage-yolo2 is pointed at a per-stream directory in that cache,
selects frames, runs YOLO inference, and publishes counts.

```
  image-sampler2 (PRODUCER)                         sage-yolo2 (CONSUMER)
  - opens camera once                               - NO camera
  - writes <ts>-v2-<vsn>-<cam>.jpg   --/local-cache--> reads committed -v2- frames
    into <root>/<cache-name>/<cam>/                 - get_node_info() for vsn/gps
  - bounded ring (Layer-1)                          - YOLO11x -> per-class counts
       |                                            - publish counts (+vsn, +gps)
  wes-local-cache-manager bounds the disk (Layer-2) - optional annotated upload
```

Three standalone fallbacks remain for nodes without a provisioned cache:
`--source stream` and `--source snapshot` open a live camera directly, and
`--source image-dir` runs against a local test folder. **Cache is the intended
production path.**

## Frame-anchored measurements (observation time = capture time)

The decisive property of the consumer model: a detection's timestamp is **when
the photo was taken, not when YOLO ran**. sage-yolo2 reads each frame's embedded
capture metadata and publishes every record with `observation_ts = capture_ts`.
A backlog processed minutes later still lands on the science timeline at the
true observation instant. Node identity (`vsn`, `node_id`) and geolocation are
resolved from the frame and the node-info injection, and attached to each
record; GPS is **omitted, never fabricated**, when unavailable.

| Topic                     | Type  | Description                                        |
|---------------------------|-------|----------------------------------------------------|
| `env.count.<class_name>`  | int   | Count of each detected class                       |
| `env.count.total`         | int   | Total objects in frame — published EVERY cycle, even at 0 (heartbeat) |
| `upload` (annotated JPEG) | image | Annotated frame, uploaded selectively — see `--save-match` |

Record `meta` carries `camera`, `model`, `classes`, `num_classes`, and — in
cache mode — `vsn`, `node_id`, `lat`, `lon`, `location_source`.

## Selection, batching, and dedup

A consumer wakes on a batch clock (`--every`) and, per wake, selects frames by a
capture-time stride (`--select-every`), a newest-K cap (`--max-frames`), or the
full unseen backlog (`--all-unseen`). A **durable seen-store** (keyed on each
frame's `unique_id` SHA-256, kept in the cache's reserved `.state` area) ensures
a frame is processed exactly once, and **survives pod restarts** — a re-launched
consumer skips frames it already handled and processes only genuinely new ones.
`--consumer-id` lets multiple instances cooperatively divide one cache.

## Saving images: `--save-match`

Counting (always published) is separated from saving (selective, expensive).
`--save-match` takes a comma-separated OR-list of `Class:confidence` rules; the
annotated frame is uploaded when any detection matches any rule (class matched
case-insensitively and exactly against the COCO name). Examples:
`--save-match "bird:0.5,cat:0.6"` or `--save-match "*:0.5"` (any detection
≥ 0.5). Omit it (and set `--upload-image N`) to publish counts/heartbeat only.

## Deployment note (GPU / NVIDIA base)

sage-yolo2 is built `FROM nvcr.io/nvidia/pytorch` (CUDA). The ECR portal build
cross-compiles arm64 under QEMU, which crashes on CUDA base images, so the
working deployment path on Thor/Jetson nodes is a **native on-node build +
k3s side-load** (see `DOCKER-BUILD.md`). In cache mode the plugin requires the
`/local-cache` mount from `wes-local-cache-manager` and **fails fast** if it is
absent.

## Testing

`make test` runs a fully offline suite (self-bootstrapping venv, ~120 tests):
pure-logic unit tests for the consumer, frame selection, seen-store dedup, and
node-identity resolution, plus an integration test that drives the cache wake
loop end-to-end with stubbed GPU libraries. A GPU integration path
(`tests/run-tests.sh`) exercises the real model on committed images.

## Example use cases

- **Bird counting at feeders** — `--source cache --classes bird` against frames
  a shared producer already captured, with any other consumer reading the same
  frames concurrently.
- **Urban traffic monitoring** — count vehicles, pedestrians, cyclists from a
  shared intersection feed.
- **Parking occupancy** — count `car` from a fixed-view lot camera.
- **Construction-site safety** — detect `person` in restricted zones.
