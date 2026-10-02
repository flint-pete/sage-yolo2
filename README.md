# sage-yolo2

**A YOLO11x object-counter plugin for Sage/Waggle, re-architected as a pywaggle2 cache consumer.**

sage-yolo2 runs YOLO11x (56.9M params, 54.7% mAP COCO) to count objects per COCO class,
publishes per-class counts, and optionally uploads annotated images. Unlike its
predecessor, its production path does **not open a camera**: it consumes image frames
that a producer plugin (`media-sampler3`) wrote into a shared on-node cache. One camera
open, one decode, many consumers — that is the architectural win.

## Where this fits

sage-yolo2 is one of the three **test consumers** in the media-sampler3 stack
(with sage-bioclip2 and the audio consumer sage-birdnet2). It's also the example
to copy when you write a new cache consumer.

```
camera ─▶ media-sampler3 ─▶ /local-cache/camera/top/ ─▶ sage-yolo2 ─▶ env.count.* ─▶ Beehive
                                                            │
                                                            └▶ /local-cache/camera-crops/top-crop-N/ ─▶ sage-bioclip2 ─▶ env.species.*
wes-local-cache-manager bounds /local-cache · wes-nodeinfo-injection + pywaggle2-nodeinfo supply node identity
```

| Related | Role |
|---|---|
| [media-sampler3](https://github.com/flint-pete/media-sampler3) | The producer whose frames this reads. Also the hub repo: [install guide](https://github.com/flint-pete/media-sampler3/blob/master/INSTALLING-MEDIA-SAMPLER3.md), [REBOOT-RECOVERY.md](https://github.com/flint-pete/media-sampler3/blob/master/REBOOT-RECOVERY.md), [HOW-IT-WORKS.md](https://github.com/flint-pete/media-sampler3/blob/master/docs/HOW-IT-WORKS.md) |
| [sage-bioclip2](https://github.com/flint-pete/sage-bioclip2) | Reads the crops this writes and classifies species |
| [sage-birdnet2](https://github.com/flint-pete/sage-birdnet2) | The audio consumer; reuses this repo's consumer modules plus a sidecar reader |
| [wes-local-cache-manager](https://github.com/flint-pete/wes-local-cache-manager) | Provides and bounds `/local-cache`; never evicts `.state/`, where the seen-store lives |
| [pywaggle2-nodeinfo](https://github.com/flint-pete/pywaggle2-nodeinfo) | Copied (vendored) here as `node_info.py` for pod identity |

**Code map**

| File | What it does | Origin |
|---|---|---|
| `app.py` | CLI, wake loop (`--every`), YOLO inference, publishing, uploads, crop-producer wiring | this repo |
| `consumer.py` | Read side of the v2 cache contract: scans and parses `-v2-` filenames, reads EXIF/UserComment metadata, fails fast if the cache is missing, resolves identity (frame first, pod env as fallback) | this repo (copied into sage-bioclip2) |
| `selection.py` | Decides which frames each wake processes (`--select-every`, `--all-unseen`, `--max-frames`) | this repo (copied into sage-bioclip2) |
| `seenstore.py` | Durable dedup memory under `/local-cache/.state/` | this repo (copied into sage-bioclip2) |
| `crop_writer.py` | Write side: builds a self-describing v2 JPEG crop and commits it into a bounded ring | copied from media-sampler3 `metadata.py` + `cache.py` (see VENDORED.md) |
| `save_match.py` | `Class:confidence` rule parsing for `--save-match` / `--crop-match` | shared copy across the plugin family |
| `node_info.py` | Pod identity from `WAGGLE_NODE_*` env | copied from pywaggle2-nodeinfo (VENDORED.md) |

---

## 1. What it does / the architecture

In production, sage-yolo2 is a **consumer** of an already-produced set of frames. The
producer (`media-sampler3`) opens the camera once and writes self-describing JPEG frames
into a shared `/local-cache` directory provided by the `wes-local-cache-manager` WES
component. sage-yolo2 is *pointed at* a per-stream directory in that cache, selects
frames, runs inference, and publishes counts anchored to the frame's capture metadata.

```
  media-sampler3 (PRODUCER)                         sage-yolo2 (CONSUMER)
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

**On a Thor node** (side-loaded image, shared cache), the verified command is
Step 6c of the install guide. Build the image first with
`scripts/deploy-sideload.sh --skip-register` (see [DOCKER-BUILD.md](DOCKER-BUILD.md)).

```bash
sudo pluginctl-nodeinfo run --name sage-yolo2-consumer --selector zone=core \
  --resource limit.memory=16Gi,request.memory=4Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-yolo2 \
  registry.sagecontinuum.org/beckman/sage-yolo2:2.1.0 -- \
  --source cache --input /local-cache/camera/top \
  --every 5m --all-unseen --max-frames 0 \
  --model yolo11x.pt --conf-thres 0.25 --classes bird \
  --crop-match "bird:0.4" --crop-padding 0.15 --crop-cache-name camera-crops
```

> **Pod identity.** `pluginctl-nodeinfo` is the patched `pluginctl` from install
> Step 3 (same flags). Its pods get the node's VSN, id and GPS, which the consumer
> uses as a cross-check and a GPS fallback, so records carry lat/lon even when
> the frame has none (`location_source: node`). With the stock `pluginctl`, the
> pod has no identity env and only the frame's EXIF counts. Verified on H039, Oct 2026.

Running `app.py` directly, inside the image or in a Python env with the
requirements installed:

```bash
# Production: consume frames media-sampler3 wrote to the shared cache (NO camera)
python3 app.py --source cache --input /local-cache/camera/top --classes bird

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
| `env.count.<class_name>` | int | One record per detected COCO class (class name sanitized: spaces/hyphens → `_`). Published **only** for classes with at least one detection, so there is never an `env.count.bird = 0`. |
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
media-sampler3        sage-yolo2 (count + CROP-PRODUCE)         classifier (e.g. BioCLIP)
 camera → cache   →   read frame, YOLO detect, count/publish  →  read each crop, classify
 <cam> stream         for each --crop-match detection:             + annotate/upload
                        crop bbox → write v2 frame
                        into <cam>-crop-<idx> stream  ─────────────→
```

- **Where crops go:** `<cache-root>/<crop-cache-name>/<camera>-crop-<idx>/` (own bounded
  ring per detection index, so N objects in one frame → N distinct streams/entries).
  - `<camera>` is the frame's EXIF `camera` field. That's media-sampler3's `--name`
    (e.g. `top`), or the filename's source if the field is missing.
  - `<idx>` is the detection's position in that frame's class-filtered detection
    list, so the numbers can have gaps.
  - **A consumer watching one directory sees only that index.** The standard
    sage-bioclip2 setup reads `top-crop-0`, so only the first bird of each frame is
    classified.
- **Counts vs crops:** counting uses `--conf-thres` (e.g. 0.25), but cropping uses
  the `--crop-match` threshold (e.g. `bird:0.4`). So `env.count.bird` can exceed
  the number of crops.
- **Why crops go back into the cache** instead of being handed straight to the
  classifier: the two stages stay decoupled.
  - Each runs on its own schedule and GPU budget.
  - The classifier gets its own seen-store and dedup.
  - Any number of classifiers can read the same crops.
  - If one side is down, nothing breaks; the ring holds the backlog.
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
python3 app.py --source cache --input /local-cache/camera/top \
  --classes bird --conf-thres 0.25 --every 10m \
  --save-match "bird:0.4" \
  --crop-match "bird:0.5" --crop-padding 0.15 --crop-cache-name camera-crops
```

Crops are compatible with the same `consumer.read_frame_metadata` API sage-yolo2 itself
uses, verified offline end-to-end by `tests/test_crop_e2e.py`. See
`CROP-PRODUCER-Design.md` for the full design.

---

## 6. Metadata & provenance

Cached `-v2-` frames are **self-describing**: `media-sampler3` embeds a full JSON blob in
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
  `<consumer-id>` defaults to `<WAGGLE_JOB_NAME>-<WAGGLE_TASK_NAME>`. That stays the
  same across restarts of one scheduled instance, but differs between instances.
  - `pluginctl run` doesn't set these variables, so pass them yourself:
    `-e WAGGLE_JOB_NAME=camera -e WAGGLE_TASK_NAME=sage-yolo2` gives
    `/local-cache/.state/sage-yolo2/camera-sage-yolo2/camera/top/seen`.
  - Without them, the id falls back to the per-pod `WAGGLE_APP_ID` (with a
    warning), and the memory is lost on every relaunch.
- **To process only the newest frames,** use `--select-every 0 --max-frames K`
  (the K newest) with a new `--consumer-id`, so an existing seen-store or backlog
  doesn't get in the way.
- **Frames without a `unique_id` are never marked seen.** That means any JPEG not
  written by media-sampler3 or crop_writer, such as the hand-seeded
  `tests/test-images/bird-cardinal-sample.jpg`. They are **reprocessed on every
  wake** until the ring evicts them, so delete seeded test frames after use. This
  is a known limitation; a fix would key such frames on a computed hash.
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
- **Real-model check (GPU, on a node):** the deterministic cascade test in the
  install guide (Steps 6b–6g). It seeds `tests/test-images/bird-cardinal-sample.jpg`, a
  public-domain Northern Cardinal confirmed to detect as `bird`, into the cache.
  It then checks for `env.count.bird`, the crop, and bioclip2's species result.
  The other `tests/test-images/*` files are camera-sized scenes with no guaranteed
  bird. The old v1 GPU script is kept in `docs/history/tests/`.

---

## 9. Requirements / deployment

- **Cache mode** requires the `/local-cache` mount provided by the
  `wes-local-cache-manager` WES component, plus a producer (`media-sampler3`) filling a
  per-stream directory. If the target cache directory is absent or unreadable, sage-yolo2
  **fails fast** with a clear message rather than silently doing nothing — a missing
  cache means the node lacks the component, the producer never ran, or the volume was not
  mounted. The cache root defaults to `/local-cache` and is overridable via the
  `IS2_CACHE_ROOT` env var, kept in sync with the producer. The `IS2_` prefix is a
  legacy name from media-sampler3's image-sampler2 lineage, kept for
  compatibility.
- **YOLO11x** needs ~4–5 GB GPU memory at 1080p. It fits easily in the 128 GB unified
  memory on DGX Spark / Sage Thor nodes.
- **GPU or CPU:** the model runs on CUDA when the pod can see the GPU, otherwise on the CPU
  (same results, slower). Check the startup log line `Loading ... on cuda|cpu`. On Thor
  nodes with no GPU device plugin and a non-NVIDIA default container runtime (H039,
  Oct 2026), `pluginctl` pods get **CPU**. See the hub guide, Step 6c,
  "Is it using the GPU?". On CPU, YOLO11x took about 2 s per frame on H039.
- `stream`/`snapshot`/`image-dir` modes do **not** require the cache mount (standalone /
  local-test fallbacks).

---

## Docs in this repo

- [DOCKER-BUILD.md](DOCKER-BUILD.md): building and side-loading, GPU constraints, troubleshooting.
- [V2-Design.md](V2-Design.md) and [CROP-PRODUCER-Design.md](CROP-PRODUCER-Design.md): design records explaining why it works this way.
- [VENDORED.md](VENDORED.md): which files are copied from where, and what must stay in sync.
- `jobs/sage-yolo2-camera.yaml`: an **untested** SES template (producer + consumer).
- [docs/history/](docs/history/): v1 docs, status logs and deploy plans (not maintained).

## Contact

Pete Beckman — pete.beckman@northwestern.edu
