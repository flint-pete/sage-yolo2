# Crop-Producer — Status & Next Steps

Companion to `CROP-PRODUCER-Design.md` (the locked design). This is the living
"where we are / what's left" record for the crop-producer **extension to the
sage-yolo2 producer**. Not a CI-team handoff — this is our own feature work.

Last updated: 2026-07-14. Released as **v2.1.0** (tag `v2.1.0`, commit `a356824`).

## What the feature IS (one paragraph)

sage-yolo2 already counts objects from cached frames. This extension gives it a
second, **optional** job: when a detection matches `--crop-match`, crop that
bounding box and write it as a self-describing v2 frame into a **crop cache
stream** on the shared `/local-cache`. That's the whole feature on the producer
side — *crop and add to local-cache*. A separate **consumer** (e.g. BioCLIP)
would then pull those clipped images from the local-cache — reading crops instead
of full camera frames — and classify each one. The two sides never call each
other; the shared cache is the only coupling. Off by default: no `--crop-match`,
no behavior change.

## Where we are — DONE and shipped (v2.1.0)

All producer-side work is complete, tested offline, and released.

| Piece | State | Where |
|---|---|---|
| Design locked (9 decisions + vendored copy + `--crop-min-px`) | ✅ | `CROP-PRODUCER-Design.md` |
| v2 cache-writer (EXIF embed + bounded ring, vendored from image-sampler2) | ✅ | `crop_writer.py` |
| Crop logic wired into all 3 source paths (cache/image-dir/live), off by default | ✅ | `app.py` `_maybe_produce_crops` |
| Crop flags surfaced for adopters | ✅ | `sage.yaml` inputs + README CLI table |
| `env.crop.count` measurement (frame-anchored) | ✅ | `app.py`; `env.crop.*` in sage.yaml ontology |
| `source_*` provenance (class/conf/bbox/parent-uid + detection_index) | ✅ | nested `source{}` in each crop's UserComment JSON |
| Offline e2e (2-bird → 2 crops → read back by the consumer API) | ✅ | `tests/test_crop_e2e.py` |
| Docs + version 2.1.0 + build wiring (Dockerfile COPY, piexif dep) | ✅ | README §5b, CHANGELOG, VENDORED.md, Dockerfile, requirements.txt |

Verification: `make test` → **141 passed** (offline; no GPU). 23 of those are new
crop tests. Tree clean, tagged `v2.1.0`, pushed to `github.com/flint-pete/sage-yolo2`.

**Key design properties to remember:**
- Crops go to `<cache-root>/<crop-cache-name>/<camera>-crop-<idx>/`, one bounded
  ring per detection index (N objects in a frame → N distinct entries).
- Each crop inherits the **parent frame's `capture_ts`** → frame-anchored; a
  species result traces back to when the photo was taken + the exact bbox.
- Crops are byte-compatible with the same v2 read API sage-yolo2's own
  `consumer.py` uses — proven by `test_crop_readable_by_consumer` + the e2e.
- Ring sizing rule: the consumer must drain faster than yolo2 fills, or crops
  evict before they're classified (same producer/consumer rate rule as the raw
  cache).

## What's left to COMPLETE the extension (not yet done)

The producer half is finished. To realize the actual detect→classify cascade
end-to-end on a node, two things remain — both **outside this producer's code**:

### 1. Deploy sage-yolo2 2.1.0 to the node (producer side)
- Build the arm64 image on the Thor node natively (ECR/Jenkins portal build fails
  for this plugin — CUDA base + QEMU cross-build crash, Infra #3), then k3s
  side-load + `pluginctl run`. The v2.0.0 deploy path in `HANDOFF.md` still
  applies; the only delta is the new image tag and adding the crop flags to the
  job spec, e.g.:
  ```
  --crop-match "bird:0.5" --crop-padding 0.15 --crop-cache-name hummingcam-crops
  ```
- GPU pluginctl pods need `--resource limit.memory=16Gi` (else OOMKilled 137).
- Verify: `env.crop.count` appears in the data API, and crop frames accumulate in
  `<crop-cache-name>/<camera>-crop-*/` on the node (tar-over-ssh to inspect, cache
  is root-owned).

### 2. A consumer that reads crops from the cache (the other half — separate work)
This is a **separate plugin/effort**, not part of sage-yolo2. A classifier such as
BioCLIP would:
- Run as a `--source cache`-style consumer pointed at the crop stream
  (`<crop-cache-name>/<camera>-crop-<idx>`) instead of a camera.
- Reuse the v2 read contract (`scan_frames` + `read_frame_metadata`, or the
  equivalent) — crops are already in the exact format that API expects, so no
  producer change is needed to support it.
- Read the nested `source{}` provenance to know the YOLO class/confidence/parent
  frame for each crop.
- Dedup on each crop's `unique_id` (SHA256 of the crop bytes — distinct per crop,
  so multiple crops from one frame are each classified once).
- Publish species records (frame-anchored to the inherited `capture_ts`).

When that consumer exists, the on-node cascade (design "Stage 5") can be verified
end-to-end: live hummingcam → yolo2 crops → BioCLIP → species in the data API.

## Open / deferred decisions

- **Vendored `crop_writer.py` will drift** from image-sampler2's `metadata.py` +
  `cache.py` if those change. There's no auto-diff (it's a curated merge); the
  crop tests are the contract guard. See VENDORED.md sync obligation. Long-term,
  a shared package (design decision option A) is cleaner if it ever diverges.
- **Crop ring sizing** on a real node is untuned — pick `--crop-max-count`/`-mb`
  once the consumer's real drain cadence is known.
