# HANDOFF — sage-yolo2 2.0.0

Stopping point after Stage 7 (on-node e2e). This records the plugin state, the
verified deploy path, and — importantly — the changes made **outside this repo**
(live H00F node state) that are not captured by git.

## Plugin state
- Version **2.0.0**, branch `master`, tree clean. `make test` → 120 passed
  (offline: consumer / selection / seenstore / identity units + cache-wake
  integration + save-match).
- Architecture: **pywaggle2 cache consumer**. Production path `--source cache`
  reads `-v2-` frames a producer (`image-sampler2`) wrote to the shared WES
  `/local-cache`; standalone `stream`/`snapshot`/`image-dir` remain as fallbacks.
  Full design in `V2-Design.md`; usage in `README.md`.
- Metadata is current for v2: `sage.yaml` (name `sage-yolo2`, v2.0.0, v2 inputs)
  and `ecr-meta/` (science description + keywords rewritten for the consumer
  model).

## Deploy path (IMPORTANT — ECR portal build does NOT work for this plugin)
- Base image is `nvcr.io/nvidia/pytorch` (CUDA). ECR/Jenkins cross-builds arm64
  under QEMU, which crashes on CUDA bases (`signal 6` / exit 134 — Infra #3, open).
- **Working path = native on-node build + k3s side-load + `pluginctl run`.**
  `scripts/deploy-sideload.sh` automates build→import→(register). The verified
  producer+consumer `pluginctl run` recipe is in
  **DOCKER-BUILD.md → "v2 cache-consumer deploy"**.
- `pluginctl run` needs NO ECR catalog record. The **SES** path
  (`sesctl create/submit`, job `jobs/sage-yolo2-hummingcam-h00f.yaml`) DOES
  validate against the ECR catalog, which for `beckman/sage-yolo2` has **not**
  been created yet (deferred; run mode is side-load-run for now).

## Verified on H00F (Thor/arm64), 2026-07-14
Live image-sampler2 producer → shared cache → sage-yolo2 consumer. Data API
confirmed `env.count.total` published **frame-anchored** (record ts = frame
CAPTURE ts, not inference ts), `meta.vsn=H00F`, `meta.node=00004cbb4701d16c`,
`meta.plugin=.../beckman/sage-yolo2:2.0.0`. Seen-store persisted 5 `unique_id`s;
a re-run skipped them and processed only new frames (dedup across restart).

## Changes made OUTSIDE this repo (live H00F state — not git-tracked)
These were required to run Stage 7 on the node and are the answer to "did
anything change that wasn't in sage-yolo2?":

1. **Three SES jobs SUSPENDED** (to free the GPU; owner beckman) — left suspended
   per Pete's instruction at this stopping point:
   - `yolo-hummingcam` (job 5679)
   - `bioclip-hummingcam` (job 5667)
   - `insect-bioclip` (job 5668)
   Resume with `sesctl --server https://es.sagecontinuum.org --token <SES_TOKEN>
   submit -j <id>` for each (run from the node).
   NOTE: a 4th GPU vision job `sage-vision-detect-bioclip-h00f` (job 5606) is
   owned by **another user (`plebbyd`)** and was **left running** — stopping it
   needs `sesctl rm --override --suspend 5606` (cross-user), which was not done.

2. **Image side-loaded into H00F's k3s containerd:**
   `registry.sagecontinuum.org/beckman/sage-yolo2:2.0.0` (10.7 GiB, arm64,
   `io.cri-containerd.image=managed`). Persists on the node; a future SES/pluginctl
   run uses it without pulling (imagePullPolicy=IfNotPresent).

3. **Transient, already cleaned:** the e2e test cache
   (`/media/plugin-data/local-cache/hummingcam` + `.state`) and the root-only
   camera env file (`/root/hummingcam.env`) were removed after the test.

No sibling repo (image-sampler2, wes-local-cache-manager, wes-nodeinfo-injection,
pywaggle2-nodeinfo) needed a code change for this work — they were already at
their committed heads and functioned as-is. (Credential note, corrected: an
earlier draft claimed image-sampler2 logs the camera password — it does NOT.
`acquire.py::_redact()` replaces the `password=` value with `***` before logging
the snapshot URL, and the password is env-only (`CAMERA_PASSWORD`), never on argv.
The `&password=***` seen in its pod logs is the plugin's OWN redaction. The real,
separate cleartext-cred exposure is in the old v1 `flint-pete/sage-yolo` job YAMLs
`--snapshot-url` arg — a different plugin's v1 files, not image-sampler2.)

