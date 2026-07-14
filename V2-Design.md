# sage-yolo2 — V2 Design

Status: DESIGN LOCKED (reviewed 2026-07-13) — ready for staged implementation (§9).
This document maps how to evolve **sage-yolo2** from the standalone sage-yolo (v0.3.1,
copied in verbatim as the starting point) into the **first exemplar plugin built on
the new core WES / pywaggle2 features**.

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

> **FLAG-NAME NOTE:** §§1–5 were written before the CLI was consolidated and use the
> first-draft flag names (`--from-cache`, `--stream`, `--image-dir`, `--continuous`,
> `--interval`). **§10 (CLI redesign) is authoritative** for the actual flags
> (`--source {cache,stream,snapshot,image-dir}` + `--input`, `--every`,
> `--select-every`, …). The *semantics* below are current; only the spelling changed.

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

### 7.1 Metadata authority & reconstruction (decided during Stage-1 implementation)

Reading the producer's actual `metadata.py` surfaced four points that pin down how
sage-yolo2 must read metadata. These are pre-decided for Stage 2:

1. **Filename `vsn`/`camera` is best-effort; EXIF/JSON is authoritative.** The v2 name
   `<ts>-v2-<vsn>-<camera>.jpg` is split on the first `-` after the marker, which is
   only correct when `vsn` has no hyphen. Sage VSNs are hyphenless (`W123`, `H00F`),
   but `build_v2_name` *permits* hyphens, so the split can be wrong in principle. This
   is harmless because the **filename is used ONLY for selection ordering** (which
   needs the capture_ts, and the ts is all-digits so it is never ambiguous). The
   authoritative `vsn`/`camera` come from the frame's EXIF/UserComment (§7.2), never
   from the filename split. (Implemented in Stage 1: `parse_v2_name` returns the ts
   authoritatively and vsn/camera best-effort.)

2. **GPS: UserComment JSON is AUTHORITATIVE; GPS EXIF is the tool-friendly view.**
   The producer cannot store negative lat/lon in EXIF (piexif raises `struct.error`),
   so standard GPS EXIF stores `abs(degrees)` as DMS rationals plus a separate
   `GPSLatitudeRef`/`GPSLongitudeRef` (`S`/`W` ⇒ negative). The UserComment JSON
   carries `lat`/`lon` as **plain signed decimal floats**. Therefore:
   - sage-yolo2 reads GPS **from the UserComment JSON** (signed floats, no DMS/ref
     reconstruction, no hemisphere ambiguity) — this is the authoritative source.
   - The GPS EXIF tags are provided so the wide ecosystem of **image browsers, photo
     managers, and mapping tools** (the ones that drop a pin on a map from a photo's
     EXIF) work out-of-the-box on a bare downloaded JPEG. The EXIF is expected to be
     correct; the JSON is authoritative for disambiguation.
   - **Docs obligation:** state this authority split explicitly in the plugin docs
     (README/overview) so downstream consumers know to trust the JSON `lat`/`lon` as
     the source of truth and treat GPS EXIF as the convenience/tooling view.

3. **GPS is optional — never fabricate.** The producer writes a GPS block only when
   both `lat` and `lon` are non-None; a node without a fix yields no GPS EXIF and
   `lat/lon: null` in the JSON. "Frame has no location" is a first-class case: omit
   location from the published record entirely — never invent it. (Ties into the §2.2
   cross-check with `get_node_info()` at Stage 3: frame-GPS and pod-GPS can each be
   present or absent independently.)

4. **capture_ts disagreement → warn and prefer the filename ts.** The filename prefix
   and the UserComment `capture_timestamp_ns` are written by the same producer from
   the same value and should be identical. On the rare mismatch (a corrupted or
   hand-edited file), **log a warning and prefer the filename ts** — it is the ordering
   key selection already committed to (§8.5), so preferring it keeps ordering and the
   published observation_ts consistent. Do not skip the frame on a ts mismatch.

### 7.2 Read order for the authoritative fields

| Field | Authoritative source | EXIF role |
|---|---|---|
| capture_ts | filename prefix (warn+prefer on JSON mismatch, §7.1.4) | `DateTimeOriginal` (human view) |
| unique_id (SHA256) | UserComment JSON `unique_id` = EXIF `ImageUniqueID` (agree by construction) | `ImageUniqueID` |
| vsn / node_id | UserComment JSON | `Model` (vsn only) |
| lat / lon | **UserComment JSON (signed floats)** | GPS tags (tool-friendly, abs+ref) |
| camera | UserComment JSON | — |
| acquisition_path | UserComment JSON | `Make` encodes path hint |

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

> **NOTE:** the flag NAMES below are the first draft. They were later consolidated
> for a cleaner CLI — see **§10 (CLI redesign)**, which is authoritative for flag
> names/spelling. The *semantics* in §8.1–8.8 are unchanged; only the surface (e.g.
> `--batch-interval`→`--every`, `--select`+`--select-stride`→`--select-every`,
> `--from-cache`→`--source cache --input`) changed. Read §8 for behavior, §10 for
> the actual flags.

```
--from-cache <dir>          # per-stream cache dir (root/<cache-name>/<camera>); required in cache mode
--batch-interval <dur>      # A: how often to wake and process a batch (e.g. 1h). 0 = single-shot.
--select <policy>           # B: which frames to pick from the batch window. See 8.3.
--select-stride <dur>       # for --select stride: spacing between picked frames (e.g. 15m)
--max-batch <int>           # safety cap: max frames processed per wake (0 = unlimited)
--seen-store <path>         # override the composite seen-store path (default: auto, see 8.4)
--consumer-id <id>          # override the <consumer-id> key segment (default: job+task; see 8.4)
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
- A tiny append-only **seen-store** records what has been processed. **Format decision
  (2026-07-13): newline-delimited hex SHA256s** — one `unique_id` per line, plain
  text. Simple, append-only (crash-safe: a torn final line is just skipped), trivially
  greppable/debuggable, prune by rewrite. (Sqlite was considered and rejected as
  over-engineered for a bounded, append-mostly set.) `all-unseen` and dedup load it
  into a set; every processed frame is appended after successful inference+publish.
- **The seen-store MUST be node-persistent** — it has to survive pod restarts, or a
  one-shot scheduled run (fresh pod each fire) starts blank and re-processes the whole
  cache. `/tmp` is pod-ephemeral and wrong. **Decision (2026-07-13):** it lives in the
  WES cache's **reserved state area** — `/local-cache/.state/...` — which
  `wes-local-cache-manager` v0.2.0+ **never counts or evicts** (`RESERVED_STATE_DIRNAME`,
  default `.state`). This makes `/local-cache` the single durable home for both frames
  and consumer state, no extra mount.
- **Seen-store path is a COMPOSITE key (multi-instance safe).** Keying by plugin name
  alone collides when two YOLOs run (redundant instances, different class filters,
  different cameras). The store path is:
  ```
  /local-cache/.state/<plugin>/<consumer-id>/<cache-name>/<camera>/seen
  ```
  where each segment answers a distinct question:
  - `<plugin>` — which plugin family (e.g. `sage-yolo2`).
  - `<consumer-id>` — which *instance*. Derived (in order) from
    `WAGGLE_JOB_NAME` + `WAGGLE_TASK_NAME` — **stable across restarts of the same
    scheduled instance** (so one-shot memory persists), yet **distinct between two
    different scheduled instances** (so they don't clobber each other). Falls back to
    `WAGGLE_APP_ID` (pod UID) only if job/task are unset — with a WARNING, because a
    UID changes every pod and thus loses cross-restart memory. Overridable via
    `--consumer-id` when the operator wants explicit control (e.g. two instances that
    SHOULD share, or a rename that should keep memory).
  - `<cache-name>/<camera>` — which producer stream it consumes (already the cache's
    own namespacing; keeps memory separate when one YOLO watches two caches).
  Rationale: identity = (which instance) × (what it consumes). `WAGGLE_JOB_NAME`/
  `WAGGLE_TASK_NAME` are the same env image-sampler2 uses for provenance, so the
  identifier is consistent across the producer/consumer pair.
- **Consuming is NON-destructive** — the frame stays in the cache (Layer-2 manager
  owns eviction). Seen-memory is the consumer's private bookmark, not a delete.
- **Fail soft** if the reserved area isn't writable (older manager, no mount): warn
  and fall back to in-memory (dedup within the run only — no cross-restart memory).
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
- **Two instances on one cache (§8.4 composite key):** by default they have SEPARATE
  seen-stores (distinct job/task → distinct `<consumer-id>`), so each independently
  processes every frame — correct for "two different analyses of the same stream."
  To make N identical instances COOPERATIVELY divide one cache (each frame processed
  once, whichever grabs it first), give them a SHARED `--consumer-id`; then treat the
  seen-store as shared and accept a benign race (a frame processed twice at most on
  concurrent wakes — dedup is advisory, not a lock). No cross-instance locking in v1.

### 8.7 Defaults for the exemplar (v1)

`--select newest`, `--batch-interval 0` (single-shot per scheduled run),
`--max-batch 1`, seen-store on, `--reprocess false`. This reproduces "one scheduled
run → process the newest unseen frame → publish → exit," the simplest correct
consumer, matching §6. The stride/all-unseen machinery is present and tested but not
the default.

### 8.8 Worked example: two consumers, one cache

Two YOLO instances reading the SAME image-sampler2 stream
(`/local-cache/hummingcam/top`), doing different jobs at different cadences:

```
# Count people every 15 minutes
app.py --from-cache /local-cache/hummingcam/top --consumer-id human \
       --classes person --batch-interval 15m --select newest

# Count hummingbirds every 2 minutes
app.py --from-cache /local-cache/hummingcam/top --consumer-id fast-hummers \
       --classes bird --batch-interval 2m --select newest
```

Resulting seen-stores (separate → no clobbering):
```
/local-cache/.state/sage-yolo2/human/hummingcam/top/seen
/local-cache/.state/sage-yolo2/fast-hummers/hummingcam/top/seen
```

The three knobs are independent: `--consumer-id` = *identity* (whose memory),
`--batch-interval` = *how often it wakes*, `--classes` = *what it computes*. The
`--consumer-id` is a human-readable name the operator picks — clearer in a cache
tree / logs than an auto-derived job id, and stable across renames. If omitted, the
default `<consumer-id>` (job+task) still keeps two separate Sage jobs distinct
automatically; the explicit name is the recommended override for readability.

To instead run N identical workers that COOPERATIVELY divide one cache (each frame
processed once), give them a SHARED `--consumer-id` (see §8.6).

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
- **Stage 2 — frame-anchored metadata (§7, §7.1–7.2).** Read EXIF/UserComment JSON;
  publish with observation_ts=capture_ts, unique_id, vsn/gps from the frame. GPS from
  the UserComment JSON (signed floats — authoritative), NOT reconstructed from GPS
  EXIF (abs+ref); GPS optional → omit-never-fabricate; capture_ts JSON-vs-filename
  mismatch → warn + prefer filename ts. Docs: state the EXIF-is-tool-view /
  JSON-is-authoritative GPS split. Gate: tests asserting the published record inherits
  frame metadata; mismatch-warning path; no-GPS path; signed-lat/lon round-trip.
- **Stage 3 — node identity (§2.2).** Vendor the pywaggle2 reader at REPO ROOT as
  `node_info.py` (content byte-identical to pywaggle2-nodeinfo; VENDORED.md notes the
  sync obligation) — NOT under `waggle/`, which would shadow installed pywaggle and
  break `from waggle.plugin import Plugin` (same collision image-sampler2 avoided via
  `nodemeta.py`). Wire `get_node_info()`, cross-check vs frame. Gate: identity
  attribution + never-fabricate-location tests.
- **Stage 4 — seen-memory (§8.4).** Seen-store read/add/prune keyed on unique_id;
  `--reprocess`. Gate: dedup across wakes, corrupt/missing store tolerated, prune
  horizon.
- **Stage 5 — batching & selection (§8.2–8.5, §10).** `--every`, `--select-every`,
  `--max-frames`, `--all-unseen`; the wake loop. Gate: each selection case over a
  synthetic multi-frame cache; edge cases in 8.6; `--max-frames` cap; empty-window
  sleep.
- **Stage 6 — jobs + docs + Docker.** New `jobs/` YAML running image-sampler2
  (producer) + sage-yolo2 (consumer) as a pair mounting `/local-cache`; overview/
  README rewrite for the consumer model; Dockerfile deps. Gate: docs consistent;
  image builds.
- **Stage 7 — on-node e2e (H00F).** The §6 success criteria on real hardware, incl.
  the cross-user cache read. Gate: producer writes, consumer processes without a
  camera, published detections carry VSN+GPS, loop bounded by the manager.

---

## 10. CLI redesign — a coherent parameter surface

Adding cache mode on top of the inherited sage-yolo flags produced ~25 `--flags`
with three kinds of confusion. Because sage-yolo2 is a NEW repo, we fix the surface
now rather than carry the ambiguity. **Decision (2026-07-13): adopt the clean set
below; the old CLI compatibility is intentionally dropped.**

### 10.1 The three confusions we're removing

1. **Four overlapping timing flags.** `--interval` (self-capture spacing — meaningless
   without a camera), `--continuous Y/N` (loop vs once), `--max-runtime` (wall bound),
   and the new `--batch-interval` (wake cadence) all fought over "timing."
   `--batch-interval 0` already *means* single-shot, so `--continuous` was redundant.
2. **Four inferred-mode input flags.** `--stream`/`--snapshot-url`/`--image-dir`/
   `--from-cache` are mutually exclusive sources, but the mode was *inferred* by
   precedence — set two, get a silent surprise.
3. **Conditionally-valid flags.** `--select-stride` only meant something with
   `--select stride`; `--max-batch` meant different things per policy. Invisible in
   `--help`.

### 10.2 The clean set

**Source — one explicit selector (fixes confusion #2):**
```
--source {cache,stream,snapshot,image-dir}   # REQUIRED. names the acquisition mode.
--input <value>                              # the source's argument, interpreted per --source:
                                             #   cache     -> <root>/<cache-name>/<camera> dir
                                             #   stream    -> camera name or RTSP URL
                                             #   snapshot  -> HTTP snapshot URL
                                             #   image-dir -> directory of test images
```
One `--source` + one `--input` replaces four inferred flags. The mode is explicit,
self-documenting, and mutually exclusive by construction. (`cache` is the production
default in docs/jobs; `image-dir` for local testing; `stream`/`snapshot` are the
standalone fallback of §5.1.)

**`cache` vs `image-dir` — the distinction is CONTRACT, not mechanism.** Both read
JPEGs from a directory, so they look nearly identical. They are opposites:

| | `--source cache` | `--source image-dir` |
|---|---|---|
| What it is | consume a producer neighbor's output via the shared WES cache | read an arbitrary local folder of images |
| Purpose | **production** consumer path (the whole point of v2) | **local test harness** (laptop, no node) |
| Provenance | frame-anchored: capture_ts, unique_id, VSN, GPS, EXIF/JSON (§7) | none — files carry no metadata; observation ts = `now()` |
| Lifecycle | live, shared, bounded: producer writes concurrently, Layer-2 evicts, `.tmp` in-flight, newest changes between wakes | static, private fixture: nobody else writes, nothing evicts |
| Selection/memory | full consumer machinery: seen-store, `--select-every`, `--all-unseen`, wake loop | process the folder once; no seen-store, no batching |
| Filename shape | requires `<capture_ts_ns>-v2-<vsn>-<camera>.jpg` | any `*.jpg` |

They are kept as **separate sources on purpose** — NOT merged with filename
auto-detection. Choosing `cache` is a promise "these are real v2 cache frames, give
me full semantics"; choosing `image-dir` is "these are just test images, don't
expect provenance." Merging and switching behavior on filename shape would
reintroduce exactly the silent inference this redesign removed: a user pointing at a
plain folder would get either parse errors or silently-degraded behavior with no
signal why. Explicit source = explicit contract.

**Timing — two orthogonal knobs (fixes confusion #1):**
```
--every <dur>        # how often to wake and process a batch. 0 = single-shot (run once, exit).
--max-runtime <dur>  # overall wall-clock bound for the whole job. 0 = no bound.
```
`--interval` and `--continuous` are GONE. `--every 0` is single-shot (was
`--continuous N`); `--every 15m` loops every 15 min (was the `--continuous Y` +
scheduling combo). `--every` renames `--batch-interval` — shorter, and no longer
ambiguous now that `--interval` is retired. Both accept `s/m/h` durations.

**Selection — one knob + one cap (fixes confusion #3):**
```
--select-every <dur>   # pick one frame per <dur> of CAPTURE-time across the window.
                       #   0 (default) = just the single newest frame.
--max-frames <int>     # cap frames processed per wake (0 = unlimited). renamed from --max-batch.
--all-unseen           # flag: process EVERY not-yet-seen frame in the cache (backlog drain).
                       #   overrides --select-every; --max-frames still caps each wake.
```
This collapses `--select {newest,newest-k,stride,all-unseen}` + `--select-stride` +
`--max-batch` into a single continuous knob plus one boolean:
- `--select-every 0` → newest (the old `newest`).
- `--select-every 15m` → one frame per 15 min of capture-time (the old `stride`).
- `--select-every 0 --max-frames K` → K newest (the old `newest-k`).
- `--all-unseen` → drain the backlog (the old `all-unseen`).
No mode enum, no conditionally-meaningless companion flag — `--select-every` is
always meaningful, and `--all-unseen` is a clearly-scoped override.

**Memory — unchanged from §8.4 (already clean):**
```
--consumer-id <id>   # identity of this consumer's memory (default: job+task)
--seen-store <path>  # override the auto composite path
--reprocess          # flag: ignore seen memory this run
```

### 10.3 `--help` grouping (disambiguation aid)

Argument groups make the shape legible at a glance:
`Source` (`--source`, `--input`) · `Schedule` (`--every`, `--max-runtime`) ·
`Selection` (`--select-every`, `--max-frames`, `--all-unseen`) ·
`Memory` (`--consumer-id`, `--seen-store`, `--reprocess`) ·
`Detection` (`--classes`, `--conf-thres`, `--iou-thres`, `--imgsz`, `--model`,
`--max-det`, `--half`, `--augment`, `--agnostic-nms`) ·
`Output` (`--upload-image`, `--save-match`).

### 10.4 Validation (fail fast on incoherent combos)

- `--source` required; `--input` required for every source.
- `--select-every` / `--all-unseen` only meaningful for `--source cache`; warn (not
  fatal) if set for stream/snapshot/image-dir.
- `--all-unseen` + `--select-every` both set → `--all-unseen` wins, warn.
- Cache-mode fail-fast on absent `/local-cache` root stays (§5.1).

### 10.5 The §8.8 worked example, restated in the clean CLI

```
# people every 15 min
app.py --source cache --input /local-cache/hummingcam/top --consumer-id human \
       --classes person --every 15m
# hummingbirds every 2 min
app.py --source cache --input /local-cache/hummingcam/top --consumer-id fast-hummers \
       --classes bird --every 2m
```
v1 exemplar default (§8.7) becomes: `--source cache --every 0 --select-every 0`
(single-shot, newest frame).

### 10.6 Net economy

| Before (inherited + design) | After |
|---|---|
| `--stream --image-dir --snapshot-url --from-cache` (4, inferred) | `--source --input` (2, explicit) |
| `--interval --continuous --batch-interval --max-runtime` (4) | `--every --max-runtime` (2) |
| `--select --select-stride --max-batch` (3 + enum) | `--select-every --max-frames --all-unseen` (2 + 1 flag) |

11 flags → 6, and every remaining flag is always-meaningful and unambiguous. The
detection/output flags are inherited unchanged.

