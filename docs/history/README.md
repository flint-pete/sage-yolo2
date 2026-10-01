# docs/history — design and deployment record (not current instructions)

These files are kept for context and are **not** maintained. Where they disagree
with the current docs ([README](../../README.md), [DOCKER-BUILD](../../DOCKER-BUILD.md),
and the [media-sampler3 install guide](https://github.com/flint-pete/media-sampler3/blob/master/INSTALLING-MEDIA-SAMPLER3.md)),
the current docs win. Cross-component overview:
[media-sampler3 DESIGN-PATH.md](https://github.com/flint-pete/media-sampler3/blob/master/DESIGN-PATH.md).

| File | What it is |
|------|-----------|
| `overview.md` | v1 tutorial (sage-yolo / yolo-object-counter, which opened the camera itself) |
| `THOR-TESTING.md` | v1 on-Thor testing notes |
| `DOCKER-BUILD-v1-and-2.0.md` | The previous long build/deploy guide: v1 camera CLI, DGX build+transfer, windowed SES scheduling, the 2.0.0 H00F recipe |
| `CROP-PRODUCER-STATUS.md`, `HANDOFF.md` | July 2026 status logs for 2.0.0 / 2.1.0 |
| `overnight-deploy-2.1.0-plan.md`, `morning-check.sh` | The H00F 2.1.0 deploy plan and its next-morning check |
| `jobs/` | v1-era SES job files (snapshot/stream camera modes) |
| `tests/` | The v1 GPU integration script (uses the removed `--image-dir` CLI) |
