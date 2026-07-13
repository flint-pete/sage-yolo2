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

## 5. Decisions (locked) & remaining open questions

### 5.1 Locked decisions (2026-07-13)

1. **Keep standalone camera modes as fallback; `--from-cache` is the production
   path.** `--stream`/`--image-dir` stay so sage-yolo2 still runs on a stock node for
   testing, but `--from-cache` is the documented, intended production input. (Matches
   the "standalone plugins deployable now" principle.)
2. **Vendor `read_node_info()`, don't pip-depend.** Vendor `node_info_env.py` (single
   file, no deps) byte-identical to `pywaggle2-nodeinfo`, and note the sync
   obligation — until pywaggle2 is pip-installable upstream. Keeps the plugin
   self-contained, matching image-sampler2's pattern.
3. **Explicit, config-driven cache path — NO discovery for v1.** sage-yolo2 is told
   where the cache is (`--from-cache <root>/<cache-name>/<camera>`, or
   `--cache-name` + `--camera`); the producer and consumer agree on `<cache-name>` by
   job config. **Fail fast** if the cache dir is not present/provisioned (i.e. the
   prototype pywaggle2/WES `wes-local-cache-manager` mount is absent) — no silent
   fallback, no auto-discovery. Cache discovery/announcement is explicitly DEFERRED
   (tracked as IS-5 in sage-design-planning/plugin-improvements.md); convention
   suffices for the exemplar.

### 5.2 Remaining open questions (decide during staging)

1. **Consume rate vs. produce rate.** If the consumer runs faster than the producer,
   it re-reads the same newest frame. Lean: track last-seen capture_ts, skip
   re-inference on an unchanged newest frame.
2. **Selection window** — "newest single frame" for v1, or newest-K for burst
   inference? Lean: newest-single for v1; pin the option in the API.

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

---

## 7. Scaffolding features to leverage explicitly (beyond "an image exists")

The new WES/pywaggle2 scaffolding means a cached frame is **self-describing** — it
carries maximum metadata the camera + producer could provide. sage-yolo2 should USE
that, not just decode pixels. Concretely, every cached `-v2-` JPEG carries (verified
from image-sampler2/metadata.py):

- **Standard EXIF** any tool reads: `DateTimeOriginal` (capture time), `Make`
  (camera or acquisition path), `Model` (VSN), `Software` (producing plugin+version),
  `ImageUniqueID` (SHA256 of the ORIGINAL frame bytes), and **GPS** tags when known.
- **A full JSON blob in `UserComment`** — the lossless machine round-trip:
  `schema_version, vsn, node_id, job, task, plugin, camera, capture_timestamp_ns,
  upload_timestamp_ns, unique_id, object_name, lat, lon, acquisition_path`.

**What sage-yolo2 should do with it (explicit features, not just pixels):**

1. **Trust the frame's own metadata over re-deriving it.** Read `capture_timestamp_ns`
   from the frame (filename prefix, corroborated by EXIF/JSON) and use it as the
   **observation time** of the published detection — NOT `now()`. A detection is about
   *when the photo was taken*, not when YOLO happened to run.
2. **`unique_id` (SHA256) = the stable identity of a capture.** Use it as the
   dedup/seen key (see §8), and echo it into the published record so a detection is
   traceable to the exact source frame (provenance chain: frame → detection).
3. **Node identity: prefer the frame's, cross-check with `get_node_info()`.** The
   frame's JSON already has `vsn/node_id/lat/lon` as captured. sage-yolo2 also calls
   `get_node_info()` for its OWN pod identity. On a correct node they agree; log a
   warning on mismatch (e.g. a stale/mis-labeled cache). Attribute the published
   detection with the frame's captured identity (it's what the pixels correspond to),
   falling back to `get_node_info()` only if the frame lacks it.
4. **`acquisition_path`** (`native-raw` vs `opencv-reencoded`) → attach as a
   data-quality tag on the detection, so downstream can weight native-raw higher.
5. **`camera` + `object_name`** → carry through so a detection points back to the
   producer's exact cache object (cross-plugin provenance, cross-ref IS-5 discovery
   later).

Net: the published detection record is **frame-anchored** — capture_ts, source
unique_id, node identity, and acquisition quality all inherited from the producer,
plus the YOLO counts. That is the payoff of "maximum metadata in the cache."

---

## 8. Consumer runtime & batching semantics (the core design)

The old sage-yolo was a **self-sampler**: `--interval` seconds between its own
captures, `--max-runtime` to bound a scheduled run (e.g. 10 min/hr). sage-yolo2 is a
**consumer of an already-produced set** — a fundamentally different model. It does
NOT capture; it SELECTS from what image-sampler2 already put in the cache. The
semantics below make "how often it runs, and how many frames it processes" tight and
unambiguous.

### 8.1 The two independent clocks (do not conflate)

A consumer has **two** rates that must be kept separate:

| | Term | Meaning | Old-yolo analog |
|---|---|---|---|
| **A** | **batch cadence** | how often the plugin WAKES to do work | ~ the hourly job schedule |
| **B** | **sampling stride** | which produced frames it PICKS once awake | ~ `--interval` |

Example from the brief: "consume an image every 15 min, batching at 1 hr" =
**batch cadence B_cadence = 1 h**, **sampling stride = 15 min**. Every hour the
plugin fires, then looks back and selects the 15-min-spaced frames produced since it
last ran, and processes those.

### 8.2 The parameter set (first draft)

```
--from-cache <dir>          # per-stream cache dir (root/<cache-name>/<camera>); required in cache mode
--batch-interval <dur>      # A: how often to wake and process a batch (e.g. 1h). 0 = single-shot.
--select <policy>           # B: which frames to pick from the batch window. See 8.3.
--select-stride <dur>       # for --select stride: spacing between picked frames (e.g. 15m)
--max-batch <int>           # safety cap: max frames processed per wake (0 = unlimited)
--seen-store <path>         # memory file of processed unique_ids (default under cache or state dir)
--reprocess <bool>          # if true, ignore seen-store (process regardless of memory). default false
--max-runtime <sec>         # overall job wall-clock bound (0 = forever) — unchanged from v1
```

Durations accept `s/m/h` suffixes (e.g. `15m`, `1h`). `--interval` from old-yolo is
**renamed** to avoid the capture-vs-consume ambiguity (it becomes `--batch-interval`
for A; sampling is `--select`/`--select-stride` for B).

### 8.3 `--select` policies (what to pick from a batch window)

The **batch window** = frames produced since the last successful wake (bounded by
`--batch-interval` on the first run, or by seen-memory thereafter). Within it:

- **`newest`** — the single newest unseen frame. (v1 default; the §2 exemplar path.)
- **`newest-k` (with `--max-batch K`)** — the K newest unseen frames.
- **`stride` (with `--select-stride D`)** — walk the window oldest→newest, pick one
  frame every D of capture-time (the "every 15 min" case). Anchored to capture_ts,
  not wall-clock, so it's deterministic and reproducible.
- **`all-unseen`** — every frame in the cache not yet in the seen-store. (The
  "process everything I haven't examined" case; `--max-batch` still caps a single
  wake to avoid a thundering backlog.)

### 8.4 Seen-memory (what "already examined" means)

- The **seen key is `unique_id` (SHA256 of the original frame)** — stable across
  producer restarts, re-scans, and mtime changes; the only correct identity.
- A tiny append-only **seen-store** (newline-delimited unique_ids, or a small
  sqlite/JSON) records what has been processed. `all-unseen` and dedup consult it;
  every processed frame is added after successful inference+publish.
- **Consuming is NON-destructive** — the frame stays in the cache (Layer-2 manager
  owns eviction). Seen-memory is the consumer's private bookmark, not a delete.
- **Bounded memory:** prune the seen-store to a horizon (e.g. keep last N ids or ids
  newer than the cache's own retention) so it can't grow unbounded — the cache is
  bounded, so the useful seen-set is bounded too.

### 8.5 The wake loop (semantics in one place)

Two windowing models, chosen by policy — kept distinct on purpose:
- **time-windowed** (`newest`, `newest-k`, `stride`): look only at frames with
  `capture_ts > last_wake_ts` (what the producer made since the last wake).
- **backlog** (`all-unseen`): look at the WHOLE cache dir, ignoring last_wake_ts.

Seen-store dedup is the **universal safety net** applied after either window, so no
frame is ever processed twice regardless of policy.

```
every --batch-interval (or once, if 0), until --max-runtime:
    if --select == all-unseen:
        window = all frames in --from-cache                 # backlog model
    else:
        window = frames with capture_ts > last_wake_ts       # time-windowed model
    candidates = apply --select policy to window             # newest / newest-k / stride / all-unseen
    if not --reprocess: candidates = [c for c in candidates if c.unique_id not in seen]
    candidates = candidates[: --max-batch or all]            # oldest→newest order
    for frame in candidates:
        detections = yolo(frame)
        publish(detections, observation_ts=frame.capture_ts, vsn=frame.vsn, ...)   # §7
        seen.add(frame.unique_id)
    last_wake_ts = now()
```

Note `last_wake_ts` advances every wake regardless of policy; `all-unseen` simply
doesn't use it for windowing (dedup carries it), so switching policies mid-life is
safe.

### 8.6 Edge & corner cases (the tight semantics)

- **Empty cache dir present but no frames yet:** valid — process nothing, sleep to
  next wake. (Distinct from cache-root ABSENT, which is fail-fast per §5.1.)
- **Cache root absent / not provisioned:** FAIL FAST (§5.1) — no silent skip.
- **Consumer faster than producer (newest re-reads same frame):** seen-store dedup
  skips it; a wake can legitimately process zero frames.
- **Producer restarted / capture_ts monotonic reset:** unique_id (not ts) is the
  seen key, so no double-processing; ts still orders within a batch.
- **Backlog on first run (`all-unseen` over a full cache):** `--max-batch` caps the
  first wake; subsequent wakes drain the rest — never a single unbounded storm.
- **Frame evicted by Layer-2 between selection and read:** treat as vanished (skip,
  like image-sampler2 handles mid-scan disappearance); don't crash.
- **`*.tmp` in-flight producer writes:** never candidates (only committed `-v2-`).
- **Duplicate unique_id across cameras (shouldn't happen):** key on
  `(camera, unique_id)` if we ever consume multiple cameras; single-camera v1 keys on
  unique_id alone.
- **Seen-store missing/corrupt:** treat as empty (worst case = reprocess once);
  never block inference on bookmark I/O.

### 8.7 Defaults for the exemplar (v1)

`--select newest`, `--batch-interval 0` (single-shot per scheduled run),
`--max-batch 1`, seen-store on, `--reprocess false`. This reproduces "one scheduled
run → process the newest unseen frame → publish → exit," the simplest correct
consumer, matching §6. The stride/all-unseen machinery is present and tested but not
the default.

---

## 9. Staged implementation plan

Each stage ends GREEN (offline tests pass) before the next. Mirrors how
image-sampler2 was built. Code lives in `app.py` + new small modules; keep KISS/DRY.

- **Stage 0 — baseline & scaffolding.** DONE: verbatim sage-yolo copy + this design.
  Add a `consumer.py` module stub + test harness wiring. Rename repo self-references
  sage-yolo → sage-yolo2 (docs/jobs). Gate: existing tests still pass.
- **Stage 1 — cache read + fail-fast.** `--from-cache` dir scan, parse
  `<ts>-v2-<vsn>-<cam>` names, resolve+assert cache root (reuse image-sampler2
  semantics). Select `newest`. Gate: unit tests over a synthetic cache dir (newest
  pick, ts parse, empty-dir vs absent-root, ignore `*.tmp`).
- **Stage 2 — frame-anchored metadata (§7).** Read EXIF/UserComment JSON; publish
  with observation_ts=capture_ts, unique_id, vsn/gps from the frame. Gate: tests
  asserting the published record inherits frame metadata; mismatch-warning path.
- **Stage 3 — node identity (§2.2).** Vendor `node_info_env.py` (byte-identical to
  pywaggle2-nodeinfo; note sync); wire `get_node_info()`, cross-check vs frame. Gate:
  identity attribution + never-fabricate-location tests.
- **Stage 4 — seen-memory (§8.4).** Seen-store read/add/prune keyed on unique_id;
  `--reprocess`. Gate: dedup across wakes, corrupt/missing store tolerated, prune
  horizon.
- **Stage 5 — batching & select policies (§8.2–8.5).** `--batch-interval`,
  `--select {newest,newest-k,stride,all-unseen}`, `--select-stride`, `--max-batch`;
  the wake loop. Gate: each policy over a synthetic multi-frame cache; edge cases in
  8.6; `--max-batch` cap; empty-window sleep.
- **Stage 6 — jobs + docs + Docker.** New `jobs/` YAML running image-sampler2
  (producer) + sage-yolo2 (consumer) as a pair mounting `/local-cache`; overview/
  README rewrite for the consumer model; Dockerfile deps. Gate: docs consistent;
  image builds.
- **Stage 7 — on-node e2e (H00F).** The §6 success criteria on real hardware, incl.
  the cross-user cache read. Gate: producer writes, consumer processes without a
  camera, published detections carry VSN+GPS, loop bounded by the manager.

