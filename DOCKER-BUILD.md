# Building and deploying the sage-yolo2 image

The canonical, verified end-to-end recipe is the media-sampler3 install guide:
[INSTALLING-MEDIA-SAMPLER3.md](https://github.com/flint-pete/media-sampler3/blob/master/INSTALLING-MEDIA-SAMPLER3.md).
Step 5 builds this image, and Steps 6b–6g run and test it. This page explains
the build itself and the GPU constraints behind those steps.

The previous long-form version of this page is kept in
[docs/history/DOCKER-BUILD-v1-and-2.0.md](docs/history/DOCKER-BUILD-v1-and-2.0.md).
It covers the v1 camera CLI, the DGX build-and-transfer flow, and windowed SES
scheduling.

---

## Why the image is built on the node (side-load)

The Sage ECR portal ("Register and Build") can build Thor/arm64 images, including
this CUDA-based one. But this image has not been published to the registry yet,
so the stack builds it natively **on the Thor** and imports it straight into k3s
containerd. Pods run that local image without any registry pull. The cost: a
side-loaded image is not reboot-durable the way a registry image is. Publishing
through ECR removes that cost. See
[media-sampler3 REBOOT-RECOVERY.md](https://github.com/flint-pete/media-sampler3/blob/master/REBOOT-RECOVERY.md).

## Build and side-load (one command)

From the repo root, **on the Thor node**:

```bash
scripts/deploy-sideload.sh --dry-run          # show the plan
scripts/deploy-sideload.sh --skip-register    # sudo docker build + import into k3s
```

- **Tag.** The script reads `name`, `namespace` and `version` from `sage.yaml`,
  so the tag is `registry.sagecontinuum.org/beckman/sage-yolo2:<version>`. That's
  only the local image name; nothing is pushed. The registry, ECR and SES URLs are
  constants at the top of the script.
- **`--skip-register`** skips writing an ECR catalog record. `pluginctl run`
  doesn't need one; an SES job (`--submit`) does, and that also needs
  `SAGE_TOKEN`/`SES_USER_TOKEN`.
- **Size and time.** The image is about 10 GiB and takes tens of minutes to
  build. The import takes about 7 minutes and keeps the disk busy (SSH may lag);
  this is normal.
- **Drift check.** The script warns if any `image:` line for this plugin in
  `jobs/*.yaml` doesn't match the `sage.yaml` version.

## What the Dockerfile does

- **Base:** `nvcr.io/nvidia/pytorch:25.08-py3` (CUDA 13.0, PyTorch 2.8,
  Python 3.12, Ubuntu 24.04). It has native kernels for both Thor (sm_110) and
  DGX Spark (sm_121). The older 25.04 base lacked sm_110 and failed on Thor.
- **OpenCV fix:** it swaps the base image's `opencv-python` for
  `opencv-python-headless`, and removes stray `cv2*` files first. The leftover
  files otherwise cause `numpy.core.multiarray failed to import`.
- **Model baked in:** `yolo11x.pt` is downloaded at build time, so the pod needs
  no network at runtime.

## Running it on a Thor

Use `sudo pluginctl-nodeinfo run` with the flags in install guide Step 6c. Three of them
are required:

| Flag | Why |
|------|-----|
| `--resource limit.memory=16Gi,request.memory=4Gi` | Without it the pod is OOMKilled (exit 137) the moment YOLO11x starts. Don't request a GPU resource; Thor's NVIDIA runtime gives GPU access automatically. |
| `-v /media/plugin-data/local-cache:/local-cache` | Mounts the shared cache. |
| `--selector zone=core` | `pluginctl` refuses a volume mount without a node selector. `--node <hostname>` doesn't work because the node has no `vsn` label. |

Set `-e WAGGLE_JOB_NAME=… -e WAGGLE_TASK_NAME=…` too. They name the seen-store,
so it survives a relaunch (see [README](README.md) §7).

## GPU sharing on Thor

Two things could make GPU plugins compete, and they behave differently:

- **Compute is time-sliced, not exclusive.** A consumer that is sleeping between
  wakes (`--every 5m`) issues no GPU kernels, so another plugin gets all the
  compute meanwhile.
- **Memory stays resident.** YOLO11x holds about 5 GB, and BioCLIP ViT-H about
  28 GB, for the pod's whole life. Thor has about 122 GB of unified memory, so both
  fit easily and **GPU sharing is not an issue on Thor**. On a small-VRAM node
  (8–16 GB) two resident models would not fit. There you'd use bounded windows
  (`--max-runtime`) or one-shot scheduling so memory is freed between runs.

On Thor, choosing between a long-lived pod and SES one-shot scheduling is about
operability (reboot survival, crash isolation), not about resources.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Pod `OOMKilled` / exit 137 | Add `--resource limit.memory=16Gi,request.memory=4Gi`. |
| `ErrImageNeverPull` / image can't be pulled | The image isn't in k3s containerd (for example, lost after a reboot). Re-run `scripts/deploy-sideload.sh --skip-register`. |
| `numpy.core.multiarray failed to import` | The OpenCV fix didn't run; rebuild without the build cache. |
| `permission denied` from docker | Use `sudo`; Thor's docker socket is root-only (the script already does). |
| Cache mode exits immediately with a "cache missing" error | `/local-cache` isn't mounted or `--input` is wrong. Check the `-v` flag and that the producer is writing to that directory. |
| Runs, but no crops and no `env.count.bird` | Probably no birds in the frames. Use the seeded-bird test (install guide Steps 6b–6g). |
