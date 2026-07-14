# Changelog

All notable changes to the `sage-yolo2` Sage plugin.

## 2.1.0 — 2026-07-14

Additive, off-by-default **crop-producer** extension: sage-yolo2 gains a producer
role. When a detection matches a rule, it crops the bounding box and writes that
crop as a v2 frame into a new cache stream, so a downstream classifier (e.g.
BioCLIP) can consume each detected object for species-level ID — a detect→classify
cascade mediated entirely by the shared cache, with no cross-plugin triggering
code. The validated 2.0.0 count/upload path is untouched when the feature is off.
See `CROP-PRODUCER-Design.md`.

### Added
- **`crop_writer.py`** — vendored v2 cache-writer (WRITE side of the contract:
  EXIF/UserComment embed + per-stream bounded ring with evict-on-either-cap and
  atomic tmp→replace), from image-sampler2's `metadata.py` + `cache.py`. Kept
  compatible with sage-yolo2's own `consumer.py` reader. Registered in VENDORED.md.
- **Crop flags** (all OFF by default): `--crop-match` (Class:confidence rules,
  same grammar as `--save-match`), `--crop-padding` (0.15), `--crop-min-px` (32),
  `--crop-cache-name` (`<job>-crops`), `--crop-max-count` (500), `--crop-max-mb`
  (500). Surfaced in `sage.yaml` inputs.
- **`_maybe_produce_crops`** wired into all three source paths (cache/image-dir/
  live): pad+clamp bbox, min-px floor, per-detection crop → `<camera>-crop-<idx>`
  stream, with a nested `source{}` provenance blob (source_class/confidence/bbox/
  unique_id + detection_index) inheriting the parent capture_ts. Fail-soft per crop.
- **`env.crop.count`** measurement (frame-anchored); `env.crop.*` added to the
  sage.yaml ontology.
- **`piexif`** added to `requirements.txt` (crop_writer dependency).
- **Tests**: `tests/test_crop_writer.py` (14 — v2-name, embed round-trip,
  crop-readable-by-consumer, provenance, ring eviction/E3), `tests/test_app_crops.py`
  (7 — geometry/clamp, off-by-default no-op, N-crops, match filtering, min-px),
  `tests/test_crop_e2e.py` (2 — offline detect→crop→consume with pixel/geometry +
  provenance proof). Full suite: **141 passed**.

## Unreleased — 2026-07-14

Repo-metadata and documentation sync after the on-node verification (no plugin
code change; still version 2.0.0).

### Changed
- **`sage.yaml` → v2.0.0** — was stale from v1 (name `yolo-object-counter`,
  version `0.3.1`, url `sage-yolo`). Now `name: sage-yolo2`, `version: 2.0.0`,
  `url: .../sage-yolo2.git`, `branch: master`, with the v2 `--source/--input/...`
  inputs and `pytest` test command.
- **`ecr-meta/` refreshed for v2** — `ecr-science-description.md` rewritten around
  the producer→cache→consumer architecture and frame-anchored measurements (was
  the v1 camera/`--interval` story); keywords add cache-consumer / producer-consumer
  / pywaggle2 / frame-anchored.
- **Deploy docs — GPU sharing & memory contention.** DOCKER-BUILD.md gains a
  "GPU sharing & memory contention" section: separates GPU COMPUTE (time-sliced,
  a sleeping plugin issues zero kernels → does not exclude a co-tenant) from GPU
  MEMORY (resident model cost; a NON-issue on Thor's ~122 GB unified memory, but
  real on small-VRAM NX/Xavier nodes) from the WES scheduler `resource.gpu`
  PLACEMENT gate. Concludes the self-sleep-vs-SES-cron choice on Thor is a
  reliability decision, not a GPU-contention one. Corrects an earlier
  overstatement that a sleeping GPU plugin "locks out" others.
- **Deploy docs** — DOCKER-BUILD.md gains a verified "v2 cache-consumer deploy"
  section (producer + consumer `pluginctl run` recipe); v1 `pluginctl deploy`
  examples flagged historical. THOR-TESTING.md gains a verified-on-H00F status
  banner.

### Verified (on H00F, Thor/arm64)
- Built natively + side-loaded `registry.sagecontinuum.org/beckman/sage-yolo2:2.0.0`
  (10.7 GiB) — ECR portal build is N/A for this CUDA base (QEMU cross-build crash,
  Infra #3). Ran a live **image-sampler2 producer → shared cache → sage-yolo2
  consumer** pair. Data API confirmed `env.count.total` published **frame-anchored**
  (record ts = frame capture ts), `meta.vsn=H00F`, `meta.node=00004cbb4701d16c`,
  `meta.plugin=.../sage-yolo2:2.0.0`. Seen-store persisted 5 `unique_id`s; a second
  run skipped them and processed only new frames (dedup across restart).

## 2.0.0 — 2026-07-13

The v2 rewrite: sage-yolo2 stops opening its own camera and becomes a **pywaggle2
cache consumer**. Instead of being its own producer (N analysis plugins = N camera
opens = N decode paths), it consumes self-describing frames that `image-sampler2`
wrote into the shared WES `/local-cache`. One camera open, one decode, many
consumers. Full design in `V2-Design.md`.

### Changed
- **Re-architected from a standalone camera-opener into a cache consumer.** The
  production path (`--source cache`) reads committed `-v2-` frames from a per-stream
  cache dir (`<root>/<cache-name>/<camera>`) provided by `wes-local-cache-manager`;
  it does not touch the camera. The old self-sourcing modes are kept as standalone
  fallbacks (`stream`, `snapshot`) plus a local-test mode (`image-dir`), but cache is
  the intended production input. YOLO11x model, class counting, publish topics, and
  the annotate+upload path are unchanged.

### Added
- **New `--source`/`--input` CLI (BREAKING CHANGE from v1 flags).** Acquisition is now
  an explicit, mutually-exclusive `--source {cache,stream,snapshot,image-dir}` plus a
  single `--input` whose meaning depends on the source (cache dir | camera name/RTSP |
  HTTP snapshot URL | test-image dir). This replaces v1's `--stream` /
  `--snapshot-url` / `--image-dir`. Consumer timing is now two orthogonal clocks —
  `--every` (wake cadence; `0` = single-shot) and `--select-every` (capture-time
  sampling stride; `0` = newest) — replacing v1's capture-oriented `--interval`.
- **Frame-anchored metadata.** A published detection's observation time is
  `observation_ts = capture_ts` — when the photo was TAKEN, read from the frame — not
  when YOLO ran. Detections inherit the frame's `unique_id` and, in cache mode, its
  `vsn`/`node_id`/GPS, so a count is traceable to the exact source frame.
- **Node identity via a vendored `get_node_info()`, with frame cross-check.** The
  pod's WES-injected identity is read via a vendored pywaggle2 reader and cross-checked
  against the frame's own captured identity; attribution prefers the frame's (it's what
  the pixels correspond to), warns on a `vsn` mismatch (stale/mislabeled cache), and
  falls back to the pod's only for fields the frame lacks.
- **GPS from the authoritative UserComment JSON.** Geolocation is read from the frame's
  `UserComment` JSON as plain signed decimal floats (the source of truth). Standard GPS
  EXIF (abs-value + N/S/E/W ref) is treated as the tool-friendly convenience view for
  image browsers / mapping tools. Location is never fabricated — omitted when unknown.
- **Durable seen-store dedup.** Keyed on `unique_id` (SHA256 of the original frame
  bytes); a newline-delimited, append-only, prune-by-rewrite store living in the cache's
  reserved, never-evicted `.state` area at a composite path
  (`<root>/.state/<plugin>/<consumer-id>/<cache-name>/<camera>/seen`) so it survives pod
  restarts and two instances never clobber each other. `--consumer-id` controls instance
  identity (share it to cooperatively divide one cache); `--seen-store` overrides the
  path; `--reprocess` ignores memory while still recording.
- **Batching / selection controls.** `--every` (wake cadence), `--select-every`
  (capture-time stride), `--max-frames` (per-wake cap; K-newest with stride `0`), and
  `--all-unseen` (backlog mode — drain every not-yet-seen frame, capped per wake).

### Migration
- Replace v1 acquisition flags with the new pair: `--stream X` → `--source stream
  --input X`; `--snapshot-url U` → `--source snapshot --input U`; `--image-dir D` →
  `--source image-dir --input D`. The new production path is `--source cache --input
  <root>/<cache-name>/<camera>`. Replace `--interval N` with `--every` (wake cadence)
  and/or `--select-every` (sampling stride). Cache mode requires the `/local-cache`
  mount from `wes-local-cache-manager` and fails fast if it is absent.

## 0.3.1 — 2026-07-10

### Added
- **`scripts/deploy-sideload.sh` — one-command side-load deploy** (2026-07-11).
  Wraps the 4-step Thor chore (build natively → import into k3s → register ECR
  catalog metadata → opt-in `--submit` SES job) into a single idempotent script.
  Reads name/namespace/version/source.url straight from `sage.yaml` — nothing
  hardcoded, so a version bump needs zero edits to the script (`--version`
  overrides). Auto-detects `--from-version` from the ECR catalog; warns on
  job-YAML image-tag drift and hard-refuses `--submit` on a mismatched tag;
  `--dry-run` previews every step with no tokens/network. Tokens are demanded
  only by the step that uses them (`SAGE_TOKEN` register, `SES_USER_TOKEN`
  submit). See the "Quick deploy (side-load)" banner in `DOCKER-BUILD.md`.

### Fixed
- **deploy-sideload.sh Step-2 SIGPIPE false-fail** (2026-07-11). The post-import
  check `k3s ctr images ls | grep -q "$TAG"` false-reported "image not found" on
  a successful import: `grep -q` exits on first match and SIGPIPEs the still-
  writing `ls`, and under `set -o pipefail` that 141 propagated to `|| die`.
  Fixed by capturing `ls` to a var and matching with pure-bash `[[ == *tag* ]]`
  (no pipe, no SIGPIPE). Caught only by running on live H00F infra.

### Changed
- **Docs/version bookkeeping only — deploy path UNCHANGED (still side-load).**
  The CI team fixed the buildkit `/proc/acpi` runc bug (Infra #2), so `RUN` steps
  now start. BUT this NVIDIA-base plugin STILL cannot build in the ECR portal:
  the pipeline cross-builds `linux/arm64` under QEMU on x86, and the NVIDIA CUDA
  base crashes with `qemu: uncaught target signal 6 (Aborted)` / exit 134 during
  `pip` (Infra #3 — a native arm64 builder does NOT yet exist; verified by the
  failed ECR build of this exact tag, 2026-07-10). So yolo continues to deploy by
  building natively on Thor and side-loading into k3s. Version bumped + image refs
  normalized to `beckman/…:0.3.1`; no plugin code change (byte-identical to 0.3.0
  — verified `git diff 0.3.0..0.3.1 -- app.py save_match.py Dockerfile` is empty).

### Deployed
- **Cut over on H00F 2026-07-11 12:11 UTC** via `deploy-sideload.sh`. Built +
  imported 0.3.1 (10.7 GiB, `io.cri-containerd.image=managed`); catalog record
  for 0.3.1 already registered. Suspended old job **5670** (0.3.0) as a one-command
  rollback point (`sesctl rm -s`), created + submitted job **5679** (0.3.1).
  Verified 0.3.1 publishes to Beehive via a one-shot on the fresh image (record
  `env.count.total`, `meta.task=yolo031-verify`, `vsn=H00F` — negative path, empty
  scene → value 0). First production windowed cycle fires at the next `:00`.

## 0.3.0 — 2026-06-24

### Added
- **`--save-match`: class-aware image saving, decoupled from publishing.**
  The annotated frame is now uploaded only when a detection matches a
  user-supplied OR-list of `Class:confidence` rules (e.g. `"bird:0.5,cat:0.6"`).
  A frame is saved when ANY detection matches ANY rule. Class matching is
  case-insensitive and EXACT against the COCO class name. The wildcard `"*:0.5"`
  saves any frame with a detection ≥0.5. Implemented via the shared
  `save_match.py` helper (29 unit tests, identical copy to bioclip/birdnet).

### Changed
- **Image saving is now selective when `--save-match` is set**, replacing the
  upload-every-cycle behavior. Counts (`env.count.*`) and the `env.count.total`
  heartbeat still publish every cycle regardless. Upload meta now also carries
  `top_class` and `confidence`.
- **`--upload-image` is now a deprecated back-compat gate.** With `--save-match`
  omitted, behavior is unchanged: `--upload-image Y` uploads every cycle that has
  detections (legacy), `N` never uploads. When `--save-match` is provided it takes
  precedence and `--upload-image` is ignored.

### Migration
- To save selectively, add `--save-match` (e.g. `"bird:0.5"` or `"*:0.4"`).
  Omitting it keeps the previous upload-every-cycle behavior via `--upload-image`.

## 0.2.2 — 2026-06-23

### Added
- **Standard `plugin.duration.*` performance telemetry** (matching
  `avian-diversity-monitoring` / TAFT-node convention). Each cycle publishes
  nanosecond phase timings via pywaggle's `plugin.timeit`:
  `plugin.duration.loadmodel` (model load + device move, once),
  `plugin.duration.input` (snapshot/capture + decode, per cycle),
  `plugin.duration.inference` (YOLO detection, per cycle). Makes cold-start cost
  and per-cycle latency observable from the data plane and doubles as a liveness
  signal on empty scenes. Model load refactored into a `load()` method so it can
  be timed inside the Plugin context.

## 0.2.1 — 2026-06-22

### Added
- **`--max-runtime N` flag for windowed GPU sharing.** When combined with
  `--continuous Y`, the plugin loops every `--interval` seconds and then
  self-exits after N seconds — behaving like one long bounded single-shot.
  Default `0` = run forever (previous behavior, unchanged). This lets a single
  GPU be time-shared: on Thor (one GPU) YOLO runs a bounded 10-minute window at
  the top of each hour (`cronjob('0 * * * *')`, `--max-runtime 600 --interval 15`,
  ~40 frames) then frees the GPU for the BioCLIP plugin's :20 window, with
  10-minute guard-bands so the two never contend. ~20 min/hour total GPU use.

### Changed
- H00F hummingcam job converted to windowed mode and class-filtered to
  **`person,bird,fork`**. The `fork` class is a deliberate **sentinel**: a fork
  cannot occur naturally in the scene, so a fork detection unambiguously means a
  human placed one in-frame to demonstrate the trigger end-to-end.
- `DOCKER-BUILD.md` gained a 3-way Continuous / One-shot / Windowed decision
  table with the window-layout diagram.

## 0.2.0 and earlier

- See git history. Core: YOLO11x object counting, per-class
  `env.count.<class>` + `env.count.total` records, annotated-image upload,
  HTTP-snapshot and RTSP/camera sources.

---

### Deployment note (arm64 / Thor)

This NVIDIA-base plugin is built natively on Thor and **side-loaded** into the
node's k3s containerd (`docker save | sudo k3s ctr images import -`), because the
ECR portal build still fails: it cross-builds `linux/arm64` under QEMU on x86 and
the CUDA base crashes (`signal 6` / exit 134). The buildkit `/proc/acpi` bug
(Infra #2) is fixed, but the QEMU-on-NVIDIA crash (Infra #3) is NOT — no native
arm64 builder exists yet (verified 2026-07-10). The ECR **catalog** version is
registered separately via `scripts/register-ecr-version.py` (metadata SES
validates against); SES pods use `imagePullPolicy=IfNotPresent`, so the
side-loaded image serves the pull.

**Use `scripts/deploy-sideload.sh` to run this whole path in one command** (build
→ import → register, plus opt-in `--submit`); it reads the version from
`sage.yaml`, so no hardcoded tags. See `DOCKER-BUILD.md` for the "Quick deploy
(side-load)" banner and the full manual build → register → side-load → submit
reference it automates.
