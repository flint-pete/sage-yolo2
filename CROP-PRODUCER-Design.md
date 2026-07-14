# sage-yolo2 — Crop-Producer Extension Design (v2.1.0)

Status: **DESIGN LOCKED** (2026-07-14) — ready for staged implementation (§5).
Additive, off-by-default extension to the LOCKED v2.0.0 consumer design
(`V2-Design.md`). Nothing here changes the counting/consuming behavior verified
on H00F — every new capability is gated behind new, default-off flags.

Locked decisions: all 9 in §3 as drafted; OPEN DECISION resolved to **(B)
vendored copy** (matches the `save_match.py` precedent, no cross-repo change);
plus a **`--crop-min-px`** floor (skip crops smaller than N px on the short side
— a distant/tiny box is useless to a classifier; skip rather than upscale).

## 0. Thesis in one line

sage-yolo2 gains a **producer role**: when a detection matches a rule, it crops
the bounding box and writes that crop into a **new `/local-cache` stream** as a
proper v2 frame, so a downstream classifier (BioCLIP) can consume each detected
object for species-level identification — a detect→classify cascade mediated
entirely by the shared cache, with **no cross-plugin triggering code**.

```
image-sampler2        sage-yolo2 (detect + CROP-PRODUCE)          bioclip (classify)
 PRODUCER              CONSUMER + PRODUCER                          CONSUMER
 camera -> cache  -->  read frame, YOLO detect                 -->  read each crop
 <cam> stream          count/publish (unchanged)                    species classify
                       for each matching detection:                 + annotate/upload
                         crop bbox -> write v2 frame
                         into <cam>-crop stream  ------------------>
```

## 1. Why this design

- **Reuses the proven cache contract.** image-sampler2 -> yolo2 already works on
  H00F; this just makes yolo2 ALSO write into the same kind of cache stream.
- **BioCLIP needs zero changes** IF crops are written in the identical v2 format
  it already consumes (same filename scheme + EXIF/UserComment metadata).
- **Replaces fragile system-level gating.** The old "detector-continuous +
  classifier-gated-in-the-watcher" coupling (Resolution B) becomes a clean
  file-mediated handoff: yolo2 drops crops, bioclip picks them up on its own
  schedule. No watcher logic, no cross-plugin RPC.
- **Frame-anchored all the way down.** A species result traces back through the
  crop's metadata to the exact parent frame + bounding box + capture instant.

## 2. The v2 format a crop MUST satisfy (so BioCLIP can read it)

From image-sampler2 (`metadata.py`, `cache.py`) — the crop producer must emit
byte-compatible frames:

- **Filename:** `<capture_ts_ns>-v2-<vsn>-<camera>.jpg` (parsed by
  `metadata.parse_v2_name`, anchored on the `-v2-` marker).
- **EXIF standard tags:** Make=`Sage/Waggle`, Model=`<vsn>`, Software=`<plugin>`,
  DateTime/DateTimeOriginal = capture time, ImageDescription = human string.
- **UserComment:** full JSON blob of the v2 field set (schema_version, vsn,
  node_id, job, task, plugin, camera, capture_timestamp_ns, upload_timestamp_ns,
  unique_id, object_name, lat, lon, acquisition_path).
- **ImageUniqueID:** SHA256 of the final injected JPEG bytes.
- **Ring write:** atomic `.tmp` -> rename into `<root>/<cache-name>/<camera>/`,
  eviction bounded by count and/or MB (oldest by capture-ts prefix).

## 3. Design decisions (leans adopted for this draft; each is a review point)

| # | Decision | Adopted lean | Rationale |
|---|---|---|---|
| 1 | Crop `capture_ts` | inherit the SOURCE frame's `capture_ts_ns` | keeps species result frame-anchored to when the photo was taken |
| 2 | Crop camera/stream name | `<camera>-crop` (own stream, distinct from raw) | consumers subscribe to crops without seeing raw frames |
| 3 | Multi-detection disambiguation | append a `detection_index` to `object_name`; crops share capture_ts but differ by index | N birds in one frame -> N distinct cache entries |
| 4 | Which detections to crop | NEW flag `--crop-match "bird:0.5"` (same grammar as save-match), default empty=OFF | cropping-for-classification is a different confidence decision than saving-for-humans |
| 5 | Output cache location | `--crop-cache-name` (default `<job>-crops`) + `--crop-max-count` / `--crop-max-mb` ring bounds | own bounded ring under the shared root |
| 6 | Crop geometry | padded: `--crop-padding 0.15` (fraction of bbox, clamped to image) | classifiers do better with a little context than a tight box |
| 7 | Crop provenance metadata | add `source_class`, `source_confidence`, `source_bbox`, `source_unique_id` to the crop's v2 JSON | gives BioCLIP YOLO context + full traceability to parent frame/box |
| 8 | Measurement on crop | publish `env.crop.count` per frame, frame-anchored | crop activity observable in the data plane without node access |
| 9 | Version | **2.1.0** (minor, additive, off-by-default) | never disturbs the validated 2.0.0 consumer path |

RESOLVED (locked 2026-07-14): **(B) vendored copy.** The v2 writer
(`metadata.py` + `cache.py` ring/eviction logic) is copied into yolo2 as a
vendored module (like `save_match.py` is kept byte-identical across repos) — no
image-sampler2 change, isolated single-repo feature. Manual sync is the accepted
tradeoff. (A) shared module remains the cleaner long-term option if these ever
diverge enough to warrant a shared package.

## 4. Where the crop logic wires in (surgical, one call site)

In `_process_cache_wake` (`app.py`), immediately after `detect()` and alongside
the existing publish/upload calls (app.py ~L463-472):

```python
detections = detector.detect(img, target_classes)
_publish_detections(...)          # unchanged
_maybe_upload(...)                # unchanged (full annotated frame, --save-match)
_maybe_produce_crops(plugin, args, detections, img, meta, identity,
                     crop_rules=crop_rules)   # NEW, no-op when --crop-match empty
seen.mark(meta.unique_id)         # unchanged
```

`_maybe_produce_crops`: for each detection passing `--crop-match`, compute the
padded/clamped bbox, `img[y1:y2, x1:x2]`, encode JPEG, build v2 metadata
(inheriting parent capture_ts + identity, adding the `source_*` provenance
fields and `detection_index`), inject EXIF, and ring-write into
`<root>/<crop-cache-name>/<camera>-crop/`. Publish `env.crop.count`.

The image-dir and live (stream/snapshot) paths get the same call for parity, so
crop-production works in test and standalone modes too.

## 5. Staged implementation gates (nothing built until §3 + the OPEN decision are locked)

- **Stage 0 — THIS design doc.** Review + lock the decisions.
- **Stage 1 — v2 cache-writer available to yolo2** (shared module or vendored
  copy per the OPEN decision). Unit tests: v2-name build/parse, EXIF/UserComment
  round-trip, ring eviction by count+MB.
- **Stage 2 — crop logic** (`--crop-match`, `--crop-padding`, per-detection crop,
  ring-write to crop stream) wired into all three source paths. Unit tests: crop
  geometry + clamping, multi-detection -> N crops, metadata correctness,
  off-by-default no-op.
- **Stage 3 — `env.crop.count` publish + `source_*` provenance metadata.**
- **Stage 4 — offline e2e:** a test image with 2 birds -> assert 2 valid v2 crops
  land in a temp cache and are readable by the SAME `consumer.read_frame_metadata`
  that BioCLIP uses.
- **Stage 5 — on-node e2e (H00F):** live hummingcam -> yolo2 crops -> a REAL
  BioCLIP consumer classifies each crop -> species records in the data API. Full
  cascade verified end-to-end (data-API proof, not just logs).
- **Stage 6 — docs (README/DESIGN/DOCKER-BUILD/CHANGELOG) + bump to 2.1.0 +
  commit.**

## 6. Risks / things to watch

- **Crop cache growth.** Each matching detection is a new file; a busy scene
  could churn the ring fast. The `--crop-max-count`/`--crop-max-mb` bounds and a
  sane `--crop-match` confidence floor mitigate this. Size the crop ring for
  BioCLIP's consume cadence (it must drain faster than yolo2 fills, or crops
  evict before classification — same producer/consumer rate rule as the raw cache).
- **Tiny crops.** A distant bird may yield a sub-32px box; consider a minimum
  crop-size floor (skip or upscale) — decide in Stage 2 (candidate flag
  `--crop-min-px`).
- **BioCLIP dedup.** BioCLIP's seen-store keys on the crop's `unique_id` (SHA256
  of the crop bytes) — distinct per crop, so multiple crops from one frame are
  each processed once. Confirm in Stage 5.
- **GPU sharing.** yolo2 (crop producer) + bioclip (crop consumer) both use the
  GPU. On Thor this is a non-issue (unified 122 GB; see DOCKER-BUILD.md "GPU
  sharing & memory contention"). On a small-VRAM node, stagger via windowing.
