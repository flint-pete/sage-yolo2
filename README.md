# sage-yolo2

**A YOLO11x object-counter plugin for Sage/Waggle, re-architected as a pywaggle2 cache consumer.**

sage-yolo2 runs YOLO11x (56.9M params, 54.7% mAP COCO) to count objects per COCO class,
publishes per-class counts, and optionally uploads annotated images. Unlike its
predecessor, its production path does **not open a camera**: it consumes image frames
that a producer plugin (`image-sampler2`) wrote into a shared on-node cache. One camera
open, one decode, many consumers — that is the architectural win.

---

## 1. What it does / the architecture

In production, sage-yolo2 is a **consumer** of an already-produced set of frames. The
producer (`image-sampler2`) opens the camera once and writes self-describing JPEG frames
into a shared `/local-cache` directory provided by the `wes-local-cache-manager` WES
component. sage-yolo2 is *pointed at* a per-stream directory in that cache, selects
frames, runs inference, and publishes counts anchored to the frame's capture metadata.

```
  image-sampler2 (PRODUCER)                         sage-yolo2 (CONSUMER)
  - opens camera once                               - NO camera
  - writes <ts>-v2-<vsn>-<cam>.jpg   --/local-cache--> reads committed -v2- frames
    into <root>/<cache-name>/<cam>/                 - get_node_info() for vsn/gps
  - bounded ring (Layer-1)                          - YOLO11x -> per-class counts
       |                                            - publish counts (+vsn, +gps)
  wes-local-cache-manager bounds the disk (Layer-2) - optional annotated upload
```

One camera open, one decode, many consumers. Instead of every analysis plugin
re-opening the camera (N plugins = N camera connections = N decode paths), the producer
writes once and any number of consumers read the shared frame.

sage-yolo2 still keeps standalone live-camera modes (`stream`, `snapshot`) as a fallback
for a stock node with no cache provisioned, plus an `image-dir` mode for local testing —
but **cache is the intended production path.**

---

## 2. Quick start / usage examples

```bash
# Production: consume frames image-sampler2 wrote to the shared cache (NO camera)
python3 app.py --source cache --input /local-cache/hummingcam/top --classes bird

# Local testing: a directory of images, no node/cache/camera
python3 app.py --source image-dir --input ./tests/test-images --every 0

# Standalone fallback: live camera (stock node, no cache provisioned)
python3 app.py --source stream --input bottom_camera --every 30s

# Standalone fallback: HTTP snapshot camera (creds via the URL / a Secret)
python3 app.py --source snapshot --input "http://IP:PORT/cgi-bin/api.cgi?cmd=Snap&user=U&password=P" --every 0
```

---

## 3. CLI reference

`--source` and `--input` are **required**. Their meaning is coupled — `--input` is the
argument to whichever `--source` was chosen.

| Flag | Meaning |
|---|---|
| `--source {cache,stream,snapshot,image-dir}` | **(required)** Acquisition mode. `cache` = consume producer frames from the shared WES cache (production); `stream`/`snapshot` = live camera (standalone fallback); `image-dir` = local test folder. |
| `--input <value>` | **(required)** The source's argument: cache dir (`<root>/<cache-name>/<camera>`) \| camera name/RTSP URL \| HTTP snapshot URL \| directory of test images. |
| `--every <dur>` | Wake cadence (batch clock): how often to process a batch. `0` = single-shot (run once, exit). Accepts `s`/`m`/`h` (e.g. `1h`). Default `0`. |
| `--select-every <dur>` | Sampling stride: one frame per this much CAPTURE-time. `0` = the single newest unseen frame. Accepts `s`/`m`/`h` (e.g. `15m`). Default `0`. |
| `--max-frames <int>` | Cap frames processed per wake (`0` = unlimited). With `--select-every 0` this means the K NEWEST frames. Default `1`. |
| `--all-unseen` | Backlog mode: process EVERY not-yet-seen frame in the cache (capped by `--max-frames` per wake). Overrides `--select-every`. |
| `--max-runtime <sec>` | Overall wall-clock bound in seconds (`0` = forever). Default `0`. |
| `--consumer-id <id>` | Override the seen-store `<consumer-id>` segment (default: `WAGGLE_JOB_NAME`+`WAGGLE_TASK_NAME`). Give two instances the SAME id to make them cooperatively divide one cache. |
| `--seen-store <path>` | Override the full seen-store path (default: auto, under the cache's reserved `.state` area). |
| `--reprocess` | Ignore the seen-store (process regardless of memory). Still records what it processes. |
| `--model <name>` | YOLO model name/path (e.g. `yolo11x.pt`, `yolo11n.pt`). Default `yolo11x.pt`. |
| `--classes <list>` | Comma-separated classes to count (empty = all). |
| `--conf-thres <float>` | Confidence threshold. Default `0.25`. |
| `--iou-thres <float>` | NMS IoU threshold. Default `0.45`. |
| `--imgsz <int>` | Inference image size. Default `640`. |
| `--half` | Use FP16 inference. |
| `--max-det <int>` | Maximum detections per image. Default `300`. |
| `--augment` | Enable test-time augmentation. |
| `--agnostic-nms` | Class-agnostic NMS. |
| `--upload-image {Y,N}` | `Y` = allow annotated-image uploads, `N` = never. Governed by `--save-match` when set. Default `Y`. |
| `--save-match <rules>` | OR-list of `Class:confidence` rules (e.g. `bird:0.5`) or `*:0.5`. Upload the annotated frame when ANY detection matches ANY rule. |
| `--crop-match <rules>` | **Crop-producer (OFF by default).** Same grammar as `--save-match`. For EACH matching detection, crop its bbox and write it as a v2 frame into a `<camera>-crop` cache stream for a downstream classifier. Empty = off. |
| `--crop-padding <float>` | Fraction of bbox size to pad each crop on all sides (clamped to image). Default `0.15`. |
| `--crop-min-px <int>` | Skip crops whose short side (after padding+clamp) is below this many px — skip, don't upscale. Default `32`. |
| `--crop-cache-name <name>` | Cache name for the crop ring under the shared cache root. Default `<job>-crops`. |
| `--crop-max-count <int>` | Max crops per stream ring; oldest evicted. Default `500`. |
| `--crop-max-mb <float>` | Max MB (decimal 10⁶) per crop stream ring. Default `500`. |

---

## 4. Sources explained

| Source | Role | `--input` is… |
|---|---|---|
| `cache` | **Production consumer.** Reads committed `-v2-` frames from the shared WES cache. No camera opened. | The per-stream cache dir `<root>/<cache-name>/<camera>`. |
| `stream` | Standalone live-camera fallback (stock node, no cache). | A camera name or RTSP URL. |
| `snapshot` | Standalone live-camera fallback via HTTP snapshot (Reolink-style API, etc.). | An HTTP snapshot URL (credentials may be embedded / provided via a Secret). |
| `image-dir` | Local test — a folder of images, no node/cache/camera. | A directory of test images. |

`cache` is the intended production path; the others exist for testing and for stock nodes
that have no producer/cache provisioned.

---

## 5. Published data

**Topics**

| Topic | Value | Notes |
|---|---|---|
| `env.count.<class_name>` | int | One record per detected COCO class (class name sanitized: spaces/hyphens → `_`). |
| `env.count.total` | int | Total detections across all classes; also the empty-scene heartbeat (value `0`). |
| `env.crop.count` | int | Crops produced from a frame (only when `--crop-match` is set and ≥1 crop was written). Frame-anchored. Meta: `camera`, `cache_name`. |

**Meta on every record**

- `camera` — the camera / stream the frame came from
- `model` — the YOLO model in use

**Meta on `env.count.total` (additionally)**

- `classes` — a `class:count,…` summary (or `none`)
- `num_classes` — number of distinct classes detected

**Meta in `cache` mode (when the frame carries it)**

- `vsn`, `node_id` — node identity
- `lat`, `lon` — geolocation (never fabricated; omitted when unknown)
- `location_source` — `frame` or `node` (which identity supplied the location)

**Frame-anchored observation time.** In cache mode the published record's timestamp is
`observation_ts = capture_ts` — the moment the **photo was taken**, read from the frame's
metadata, *not* the moment YOLO happened to run. A detection is about when the scene
existed, not when it was analyzed.

**Uploads.** When enabled (see `--upload-image` / `--save-match`), sage-yolo2 uploads the
annotated JPEG (bounding boxes + labels) with meta `camera`, `detections`, `top_class`,
`confidence`.

---

## 5b. Crop-producer (detect→classify cascade, off by default)

With `--crop-match`, sage-yolo2 also acts as a **producer**: for each detection matching
the rule it crops the bounding box and writes that crop as a new `-v2-` frame into a
**crop cache stream**, so a downstream classifier (e.g. BioCLIP) can consume each detected
object for species-level ID — a detect→classify cascade mediated entirely by the shared
cache, with **no cross-plugin triggering code**. When `--crop-match` is empty (default),
none of this runs; the count/upload path is unchanged.

```
image-sampler2        sage-yolo2 (count + CROP-PRODUCE)         classifier (e.g. BioCLIP)
 camera → cache   →   read frame, YOLO detect, count/publish  →  read each crop, classify
 <cam> stream         for each --crop-match detection:             + annotate/upload
                        crop bbox → write v2 frame
                        into <cam>-crop-<idx> stream  ─────────────→
```

- **Where crops go:** `<cache-root>/<crop-cache-name>/<camera>-crop-<idx>/` (own bounded
  ring per detection index, so N objects in one frame → N distinct streams/entries).
- **Frame-anchored:** each crop inherits the **parent frame's `capture_ts`**, so a species
  result traces back to when the photo was taken.
- **Provenance:** each crop's `UserComment` JSON carries a nested `source` object —
  `source_class`, `source_confidence`, `source_bbox`, `source_unique_id`,
  `detection_index` — giving the classifier YOLO context + full traceability to the parent
  frame and box.
- **Geometry:** `--crop-padding` adds context around the box (clamped to the image);
  `--crop-min-px` skips boxes too small to classify (skip, never upscale).
- **Bounded:** `--crop-max-count` / `--crop-max-mb` cap the ring (evict-on-either, oldest
  first). Size it so the classifier drains faster than yolo2 fills, or crops evict before
  classification (same producer/consumer rate rule as the raw cache).

Example — count birds AND feed a BioCLIP-style classifier:

```bash
python3 app.py --source cache --input /local-cache/hummingcam/top \
  --classes bird --conf-thres 0.25 --every 10m \
  --save-match "bird:0.4" \
  --crop-match "bird:0.5" --crop-padding 0.15 --crop-cache-name hummingcam-crops
```

Crops are compatible with the same `consumer.read_frame_metadata` API sage-yolo2 itself
uses, verified offline end-to-end by `tests/test_crop_e2e.py`. See
`CROP-PRODUCER-Design.md` for the full design.

---

## 6. Metadata & provenance

Cached `-v2-` frames are **self-describing**: `image-sampler2` embeds a full JSON blob in
the EXIF `UserComment` tag (schema version, vsn, node_id, job, task, plugin, camera,
`capture_timestamp_ns`, `unique_id`, lat/lon, `acquisition_path`, …) plus standard EXIF
tags. sage-yolo2 trusts the frame's own metadata over re-deriving it, so a published
detection is anchored to the exact source frame.

sage-yolo2 has **two identity views**:

- The **frame's captured identity** (from the `UserComment` JSON) — what the pixels
  actually correspond to; authoritative for attribution.
- The **pod's own identity** via a vendored `get_node_info()` reading the WES-injected
  `WAGGLE_NODE_*` env.

It attributes with the frame's identity, cross-checks against the pod's, warns on a `vsn`
mismatch (a stale/mislabeled cache), and falls back to the pod's identity only for fields
the frame lacks. Location is **never fabricated** — if neither frame nor pod has a fix,
the record simply carries no location.

### GPS metadata authority: UserComment JSON vs GPS EXIF

**The `UserComment` JSON `lat`/`lon` are the authoritative source of truth. GPS EXIF is
the tool-friendly convenience view.**

Each cached frame carries geolocation in *two* forms:

1. **UserComment JSON** — `lat`/`lon` as plain **signed decimal-degree floats**. This is
   the **authoritative** source. sage-yolo2 reads geolocation from here.
2. **Standard GPS EXIF tags** — provided so the wide ecosystem of image browsers, photo
   managers, and mapping tools that drop a pin on a map from a photo's EXIF work
   out-of-the-box on a bare downloaded JPEG.

The GPS EXIF is expected to be correct, but EXIF **cannot store a negative lat/lon
directly** — it stores an absolute value (DMS rationals) plus a separate
`GPSLatitudeRef`/`GPSLongitudeRef` (`S`/`W` ⇒ negative). Because of this abs-value +
reference encoding, the **UserComment JSON is authoritative for disambiguation**: it
carries the sign directly with no hemisphere ambiguity or DMS reconstruction.

**Downstream consumers should trust the JSON `lat`/`lon` as the source of truth and treat
GPS EXIF as the tooling/convenience view.**

(See `V2-Design.md` §7.1–7.2 for the full authority/read-order table.)

---

## 7. Seen-memory / dedup

A consumer must remember which frames it has already processed so a re-scan — or a fresh
one-shot pod each scheduled fire — does not re-infer the whole cache.

- **Key.** Dedup is keyed on the frame's `unique_id` (SHA256 of the *original* frame
  bytes), read from the frame metadata. This is stable across producer restarts,
  re-scans, and mtime changes — the only correct identity.
- **Store format.** A plain newline-delimited list of hex SHA256s: append-only
  (crash-safe — a torn final line is simply skipped), greppable, pruned by rewrite to a
  bounded horizon.
- **Location.** The store lives in the WES cache's **reserved `.state` area** — which
  `wes-local-cache-manager` never counts or evicts — so it is node-persistent and
  **survives pod restarts**. No extra mount required.
- **Composite path** (multi-instance safe):
  ```
  <root>/.state/<plugin>/<consumer-id>/<cache-name>/<camera>/seen
  ```
  `<consumer-id>` defaults to `WAGGLE_JOB_NAME`+`WAGGLE_TASK_NAME` — stable across
  restarts of the same scheduled instance, yet distinct between different instances.
- **`--consumer-id`.** By default two different Sage jobs get *separate* seen-stores, so
  each independently processes every frame ("two different analyses of one stream"). Give
  N identical workers the **same** `--consumer-id` to have them cooperatively divide one
  cache (each frame processed once).
- **Fail-soft.** A missing/corrupt/unwritable store degrades to "nothing seen" (worst
  case = reprocess once) and never blocks inference. `--reprocess` ignores the store
  entirely while still recording what it processes.

---

## 8. Testing

- **`make test`** — the offline unit + integration suite (consumer, metadata, identity,
  seen-store, selection, app-cache path, save-match). Pure-stdlib logic — no GPU, cv2, or
  YOLO required. It **self-bootstraps a throwaway venv** (`.venv-test`) with pytest,
  Pillow, piexif, and numpy, so it runs out-of-the-box on a clean checkout. Clean up with
  `make clean`.
- **`tests/run-tests.sh`** — the GPU integration test (real YOLO11x inference over
  `tests/test-images/`). Requires a GPU and the shared project venv.

---

## 9. Requirements / deployment

- **Cache mode** requires the `/local-cache` mount provided by the
  `wes-local-cache-manager` WES component, plus a producer (`image-sampler2`) filling a
  per-stream directory. If the target cache directory is absent or unreadable, sage-yolo2
  **fails fast** with a clear message rather than silently doing nothing — a missing
  cache means the node lacks the component, the producer never ran, or the volume was not
  mounted. The cache root defaults to `/local-cache` and is overridable via the
  `IS2_CACHE_ROOT` env var (kept in sync with the producer).
- **YOLO11x** needs ~4–5 GB GPU memory at 1080p. It fits easily in the 128 GB unified
  memory on DGX Spark / Sage Thor nodes.
- `stream`/`snapshot`/`image-dir` modes do **not** require the cache mount (standalone /
  local-test fallbacks).

---

## Contact

Pete Beckman — pete.beckman@northwestern.edu
