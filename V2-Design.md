# sage-yolo2 — V2 Design (DRAFT / discussion)

Status: DRAFT. This document maps how to evolve **sage-yolo2** from the standalone
sage-yolo (v0.3.1, copied in verbatim as the starting point) into the **first
exemplar plugin built on the new core WES / pywaggle2 features**.

The thesis in one line: sage-yolo2 stops opening its own camera and instead
**consumes frames that image-sampler2 produced into the shared `/local-cache`**, and
it **geotags/attributes its output via `get_node_info()`** instead of publishing
counts with no location.

---

## 0. What "the new features" are (the pywaggle2 trilogy)

sage-yolo2 is the consumer-side proof that these three shipped pieces work together:

| Feature | Provided by | What sage-yolo2 uses it for |
|---|---|---|
| Node identity in every pod | `wes-nodeinfo-injection` (WES) | reads its own VSN/GPS/mobility from env |
| `get_node_info()` reader | `pywaggle2-nodeinfo` (library) | clean `NodeInfo` (sentinel-normalized) |
| Bounded shared `/local-cache` | `wes-local-cache-manager` (WES) | the disk it reads producer frames from |
| Producer of frames | `image-sampler2` (plugin) | the upstream that fills the cache |

---

## 1. Where we start (sage-yolo v0.3.1, as copied)

The current plugin (in `app.py`) acquires images three ways, all **self-sourced**:
- `--stream <name|rtsp>` — opens a camera/RTSP itself (default `bottom_camera`)
- `--snapshot-url <url>` — HTTP snapshot (Reolink etc.), **camera creds in the URL**
- `--image-dir <dir>` — a directory of test images (local testing)

It then runs YOLO11x, counts classes, publishes counts, and optionally uploads an
annotated image. Two gaps vs. the new architecture:
1. **It is its own producer.** Every consumer re-opens the camera → N plugins = N
   camera connections, N decode paths, no shared frame. The cache exists precisely
   to break this.
2. **No node identity.** Published counts carry no VSN/location; annotated uploads
   aren't attributed to a node via the injected identity.

---

## 2. Where we're going (sage-yolo2, the consumer)

### 2.1 New primary input: `--from-cache`
A new acquisition mode that reads the **newest** frame(s) image-sampler2 wrote to a
per-stream cache dir, instead of opening a camera. This mirrors image-sampler2's own
`--from-cache` consumer path (already implemented there — reuse the pattern).

The contract sage-yolo2 must honor (verified from image-sampler2's `cache.py` /
`metadata.py`):
- **Cache root:** `/local-cache` (default), overridable via env; provided by
  `wes-local-cache-manager`. Fail fast with a clear message if absent (mirror
  image-sampler2's `assert_cache_root_available` behavior — no silent fallback).
- **Per-stream dir:** `<cache-root>/<cache-name>/<camera>/` — the producer chooses
  `<cache-name>` (e.g. the image-sampler2 job's cache name) and `<camera>`.
  sage-yolo2 is *pointed at* that dir; it does not invent the layout.
- **Filenames:** `<capture_ts_ns>-v2-<vsn>-<camera>.jpg`. The capture timestamp is
  the authoritative ordering key (NOT mtime). "Newest" = largest `capture_ts_ns`.
- **Selection:** default = the single newest frame; option for newest-K or a
  time-window (deferred — pin the API now, implement newest-first first).
- **In-flight writes:** ignore `*.tmp` (the producer writes tmp then atomically
  renames). Only fully-committed `-v2-` files are consumable.
- **EXIF (bonus):** each frame already carries capture-time + node identity + a
  SHA256 of the original bytes. sage-yolo2 CAN read EXIF for provenance, but the
  filename alone gives it capture_ts + vsn + camera.

### 2.2 Node identity via `get_node_info()`
Replace any ad-hoc identity with the pywaggle2 reader:
```python
from waggle.data.node_info_env import read_node_info   # vendored or pip
ni = read_node_info()   # NodeInfo(vsn, node_id, lat, lon, mobility, vsn_is_placeholder)
```
Use it to:
- **Attribute published counts** with `vsn`/`node_id` (so the data is self-describing
  even before Beehive routing).
- **Geotag** the annotated upload / add lat/lon to the record when `ni.lat/lon` are
  not None (respect the never-fabricate rule: omit location when None).
- Optionally **cross-check** the frame's own `-v2-<vsn>-` against `ni.vsn` (they
  should match on a correctly-configured node; log a warning if not).

### 2.3 What stays the same
- The YOLO11x model, class counting, the publish topics, the annotate+upload path,
  `--continuous`/`--max-runtime` scheduling, heartbeat conventions.
- Local testing via `--image-dir` (keep it — it's how we test without a cache/node).

### 2.4 What's removed / demoted
- `--stream` camera-opening becomes secondary (kept for standalone fallback, but the
  cache is the intended production path). Decision needed (see §5).
- `--snapshot-url` with creds-in-URL: the whole point of the cache path is that
  sage-yolo2 no longer touches the camera, so it no longer needs camera creds at all
  in cache mode. (Standalone snapshot mode, if retained, still has the cleartext-cred
  problem — tracked in sage-design-planning/Infra-problems-to-fix.md.)

---

## 3. Data flow (target)

```
  image-sampler2 (PRODUCER)                         sage-yolo2 (CONSUMER)
  ─ opens camera once                               ─ NO camera
  ─ writes <ts>-v2-<vsn>-<cam>.jpg   ──/local-cache──▶ reads newest -v2- frame
    into <root>/<cache-name>/<cam>/                 ─ get_node_info() for vsn/gps
  ─ bounded ring (Layer-1)                          ─ YOLO11x → counts
       │                                            ─ publish counts (+vsn, +gps)
  wes-local-cache-manager bounds the disk (Layer-2) ─ optional annotated upload
```

One camera open, one decode, many consumers. That is the architectural win.

---

## 4. Concrete change list (to refine into a staged plan)

1. **Add `--from-cache <dir>` acquisition mode** — scan the per-stream dir, pick the
   newest committed `-v2-` frame, load it. (New helper; mirror image-sampler2.)
2. **Vendor or depend on `read_node_info()`** — decide vendoring vs. requirements
   (see §5). Wire `NodeInfo` into publish + upload attribution + geotag.
3. **Cache-root resolution + fail-fast** — reuse image-sampler2's resolve/assert
   semantics (no silent fallback).
4. **Filename/timestamp parsing** — parse `<ts>-v2-<vsn>-<cam>` to get capture_ts +
   vsn + camera; use capture_ts as the record's observation time (not now()).
5. **Selection policy** — newest-first now; pin the newest-K / time-window API.
6. **Publish contract** — add vsn/node_id/lat/lon to the record; keep existing count
   topics.
7. **Docs + jobs** — a new `jobs/` YAML that mounts `/local-cache` and runs the pair
   (image-sampler2 producing + sage-yolo2 consuming); update overview/README.
8. **Tests** — feed a synthetic cache dir of `-v2-` frames; assert newest selection,
   ts parsing, node-info attribution, fail-fast on missing cache.

---

## 5. Open questions (decide before staging)

1. **Keep standalone camera modes, or cache-only?** Lean: KEEP `--stream`/
   `--image-dir` as fallback so sage-yolo2 still runs on a stock node for testing,
   but make `--from-cache` the documented production path. (Matches the "standalone
   plugins deployable now" principle.)
2. **`read_node_info()` — vendor a copy or pip-depend?** image-sampler2 vendors its
   own identity reader today. Lean: VENDOR `node_info_env.py` (single file, no deps)
   for now, byte-identical to `pywaggle2-nodeinfo`, and note the sync obligation —
   until pywaggle2 is pip-installable upstream. Keeps the plugin self-contained.
3. **How does sage-yolo2 learn the producer's `<cache-name>`?** By convention (job
   config passes the same cache-name to both), or discovery? Lean: explicit
   `--from-cache <root>/<cache-name>/<camera>` (or `--cache-name` + `--camera`),
   config-driven — no magic discovery for v1.
4. **Consume rate vs. produce rate.** If the consumer runs faster than the producer,
   it re-reads the same newest frame. Dedup by capture_ts (skip if unchanged)?
   Lean: track last-seen capture_ts, skip re-inference on an unchanged newest frame.
5. **Selection window** — is "newest single frame" enough for v1, or do we need
   newest-K for burst inference? Lean: newest-single for v1; pin the option.

---

## 6. Success criteria (the exemplar bar)

sage-yolo2 v1 is "done as an exemplar" when, on H00F:
- image-sampler2 produces `-v2-` frames into `/local-cache`,
- sage-yolo2 consumes the newest frame WITHOUT opening a camera,
- publishes counts attributed with the injected VSN and (when known) GPS,
- and the whole loop is bounded by `wes-local-cache-manager` —
proving the pywaggle2 producer/consumer + node-info story end-to-end on real
hardware, with a second plugin reading a producer's cache across the shared mount
(the cross-user-read gap noted in wes-local-cache-manager/HANDOFF.md).
