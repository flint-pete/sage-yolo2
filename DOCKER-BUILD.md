# Building and Testing the YOLO Plugin Docker Image

> **Status (2026-07):** The ECR portal build does **NOT** work for this plugin
> yet. The buildkit `/proc/acpi` runc bug (Infra #2) is fixed, so `RUN` steps now
> start — but this is an **NVIDIA/CUDA-base** plugin, and the pipeline still
> cross-builds `linux/arm64` under **QEMU on x86**, which crashes on the CUDA base
> (`qemu: uncaught target signal 6 (Aborted)` / exit 134). This is Infra #3 — a
> **native arm64 builder does not exist yet** (verified 2026-07-10: the ECR build
> of tag v0.3.1 failed at exactly this step). **Deploy path remains: build
> natively on Thor + side-load into k3s** (see below). CPU-only plugins like
> birdnet DO build in ECR now; NVIDIA-base ones (yolo, bioclip) do not.
> Platform blockers tracked in `~/AI-projects/sage-design-planning/Infra-problems-to-fix.md` (#2 fixed,
> #3 open).

How to build the Docker image, test it locally, and deploy it to
a Sage node.

> **Quick deploy (side-load — the working path).** Because ECR can't build this
> plugin yet (Infra #3, above), deployment is scripted. From the repo root **on
> the Thor node**, one command builds natively, imports into k3s, and registers
> the ECR catalog record — version read straight from `sage.yaml`, nothing
> hardcoded:
>
> ```bash
> export SAGE_TOKEN=...            # Sage portal token (for the catalog register step)
> scripts/deploy-sideload.sh                                    # build → import → register
> scripts/deploy-sideload.sh --submit jobs/yolo-hummingcam-h00f.yaml   # + create/submit SES job
> ```
>
> Add `--dry-run` to preview every step without executing. `--submit` also needs
> `SES_USER_TOKEN` (write-scoped). Run `scripts/deploy-sideload.sh -h` for all
> flags (`--version`, `--from-version`, `--skip-build`, `--skip-register`). The
> manual equivalents are documented in full under **"Deploy path: build natively
> on Thor + side-load into k3s"** below — the script just automates them.


## v2 cache-consumer deploy (VERIFIED end-to-end on H00F, 2026-07-14)

This is the actual v2 production shape: a **producer** (`image-sampler2`) writes
frames to the shared cache, and sage-yolo2 **consumes** them (`--source cache`).
Both are side-loaded and run with `sudo pluginctl run`. The full e2e below was
run on H00F and confirmed via the data API (frame-anchored `env.count.total`,
`vsn=H00F`, seen-store dedup persisting across restarts).

Key facts learned on the node (all baked into the commands):
- **`--selector zone=core`** is required whenever you use `-v` (volume mount):
  pluginctl refuses a mount without a node selector, and `--node <hostname>`
  does NOT schedule (no `vsn` label on the node) — use the `zone=core` label.
- **`--resource limit.memory=16Gi,request.memory=4Gi`** is required or the pod
  is **OOMKilled (exit 137)** the instant YOLO11x starts inference. Do NOT pass
  a gpu resource — `--resource resource.gpu=true` is invalid (quantities only);
  GPU access is automatic via Thor's NVIDIA runtime.
- Camera creds go in a **root-only env file** consumed by `--env-from`, never on
  argv. `image-sampler2` reads `CAMERA_{HOST,PORT,CHANNEL,USER,PASSWORD}`.
- The cache host path is `/media/plugin-data/local-cache` (from
  `wes-local-cache-manager`); mount it to `/local-cache` in both pods.

```bash
# --- 0. camera creds -> root-only env file (NOT argv) ---
printf 'CAMERA_HOST=10.107.0.221\nCAMERA_PORT=10000\nCAMERA_CHANNEL=0\nCAMERA_USER=USER\nCAMERA_PASSWORD=PASS\n' \
  | ssh beckman@node-H00F.sage 'sudo tee /root/cam.env >/dev/null && sudo chmod 600 /root/cam.env'

# --- 1. PRODUCER: image-sampler2 --continuous writes frames to the shared cache ---
sudo pluginctl run --name hummingcam-producer \
  --selector zone=core \
  --env-from /root/cam.env \
  -v /media/plugin-data/local-cache:/local-cache \
  localhost/image-sampler2:0.3.0-rc -- \
  --continuous 10 --stream top_camera --name top \
  --cache-root /local-cache --cache-name hummingcam --cache-max-count 20 --vsn H00F

# --- 2. CONSUMER: sage-yolo2 --source cache reads those frames ---
sudo pluginctl run --name sage-yolo2-consumer \
  --selector zone=core \
  --resource limit.memory=16Gi,request.memory=4Gi \
  -v /media/plugin-data/local-cache:/local-cache \
  -e WAGGLE_JOB_NAME=stage7 -e WAGGLE_TASK_NAME=sage-yolo2 \
  registry.sagecontinuum.org/beckman/sage-yolo2:2.0.0 -- \
  --source cache --input /local-cache/hummingcam/top \
  --every 0 --all-unseen --max-frames 5 \
  --model yolo11x.pt --conf-thres 0.25 --classes bird --save-match "bird:0.4"

# --- 3. teardown ---
sudo pluginctl rm hummingcam-producer sage-yolo2-consumer
ssh beckman@node-H00F.sage 'sudo rm -f /root/cam.env'
```

Capturing a one-shot consumer's FULL logs before GC (pluginctl detaches its log
stream early and the pod GCs fast): launch detached, then poll `sudo kubectl
logs <name> -n default -c <name>` into a file until a marker
(`no detections` / `Published` / a terminated-state reason) appears. Verify the
publish reached the cloud via the data API:

```bash
curl -s -X POST https://data.sagecontinuum.org/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"start":"-15m","filter":{"vsn":"H00F","name":"env.count.total"}}'
# record timestamp == frame CAPTURE time (frame-anchored); meta.vsn=H00F,
# meta.plugin=.../beckman/sage-yolo2:2.0.0, meta.task=sage-yolo2-consumer
```

> NOTE on run mode: `pluginctl run` bypasses the ECR-catalog gate, so it needs
> **no** registration — ideal for dev/test round-trips. The production SES path
> (`sesctl create/submit`) DOES validate against the ECR catalog and would need
> a first-time catalog record for `beckman/sage-yolo2` (deferred — the plugin
> ships as side-load-run for now). The canonical producer+consumer SES pair is
> `jobs/sage-yolo2-hummingcam-h00f.yaml`.

---

## GPU sharing & memory contention (co-running with BioCLIP et al.)

A recurring question: does a **continuous/self-sleep** consumer (one long-lived
pod that processes a batch, `sleep`s ~10 min, repeats) block other GPU plugins
(BioCLIP, etc.) while it sleeps? The answer depends on separating **two
independent kinds of GPU contention** — and conflating them leads to wrong
deploy decisions.

**1. GPU COMPUTE (SM time) — time-sliced, NOT exclusive.** The CUDA driver
interleaves kernels from all processes on the GPU. A plugin only contends for
compute *while it is actually issuing kernels* (during inference, a few seconds
per batch here). While a self-sleep consumer is in `time.sleep(600)` it issues
**zero** kernels — it is not on the GPU computationally at all, and a co-tenant
gets 100% of the compute. **A sleeping consumer does not lock out another
plugin's compute.** True hard exclusion only happens if a process explicitly
takes the GPU exclusively (e.g. `nvidia-smi -c EXCLUSIVE_PROCESS`, or MPS in a
mode that serializes) — which we do NOT do.

**2. GPU MEMORY (VRAM) — the only real steady-state cost, and it's node-dependent.**
A loaded model + CUDA context stay resident for the pod's whole life, including
the sleep window (YOLO11x ≈ 5 GB; BioCLIP ViT-H ≈ 28 GB). Whether that blocks a
co-tenant is **entirely a function of the node's memory budget:**

| Node profile | Memory | Two resident heavy models? |
|---|---|---|
| **Thor (H00F)** — Jetson, **unified** memory | **~122 GB** (~69 GB free) | Trivial. 5 GB + 28 GB fit with tens of GB to spare. **No contention.** |
| NX / Xavier / small discrete GPU | ~8–16 GB VRAM | Two resident heavy models **exhaust VRAM** → the second OOMs / can't load. **Real contention.** |

**CONSEQUENCE for the deploy decision on Thor:** a self-sleep consumer costs
~5 GB of 122 GB and zero sleep-time compute, so **it does not meaningfully
impede BioCLIP or any other GPU plugin.** GPU sharing is therefore a
**non-issue** on Thor — the self-sleep-vs-SES-cron choice reduces to
**reliability/operability** (see below), not resource contention.

On a **memory-constrained node** the calculus flips: a resident-but-sleeping
model genuinely blocks a co-tenant, and you must either (a) run one-shot SES cron
so VRAM is freed between ticks, or (b) use bounded **windowing**
(`--max-runtime <sec>`) so the continuous pod releases VRAM between windows
(the classic single-small-GPU duty-cycle pattern).

### Self-sleep (continuous) vs SES one-shot cron — the reliability tradeoff (Thor)

Since GPU contention is off the table on Thor, choose on operability:

| | Self-sleep (continuous pod) | SES one-shot cron (`*/10 * * * *`) |
|---|---|---|
| Model cold start | Once (warm thereafter) | Every wake (~2–5 s reload) |
| Reboot survival | No (pluginctl pod won't return) | Yes (scheduler respawns) |
| Crash blast radius | Kills ALL future wakes | One tick dies; next tick is clean (**self-healing**) |
| Scheduler visibility | None | Full (restart policy, accounting) |
| ECR catalog record | Not needed | **Required** (first-time registration) |

Lean for a long-period / short-batch / shared-Thor workload: **SES one-shot
cron** (self-healing + reboot survival; the seen-store makes one-shot batches
equivalent to self-sleep batches). Lean for a **dedicated**-GPU or
cold-start-sensitive workload: self-sleep. Either way on Thor, both share the
GPU equally well.

---

## Prerequisites

- A build machine with internet access, Docker, and an NVIDIA GPU
- SSH access to a Sage Thor node (for on-node testing)
- NVIDIA Container Toolkit configured for Docker (see below)

### NVIDIA Container Toolkit Setup

Docker needs the NVIDIA Container Toolkit to pass GPUs into
containers. The toolkit may already be **installed** but not
**configured** — both steps are required.

**Check if it's already working:**

```bash
docker run --rm --runtime=nvidia nvidia/cuda:12.9.0-base-ubuntu24.04 nvidia-smi
```

If that prints your GPU info, you're set. If it fails:

```bash
# Step 1: Install (if not already)
# https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html

# Step 2: Configure Docker to use the nvidia runtime
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

# Step 3: Verify
docker info | grep -i runtime
#  Runtimes: runc io.containerd.runc.v2 nvidia   ← nvidia must appear
```

This is a one-time setup per machine.


## Base Image

The Dockerfile uses `nvcr.io/nvidia/pytorch:25.08-py3`:

| Component | Version |
|-----------|---------|
| CUDA | 13.0 |
| PyTorch | 2.8 |
| Python | 3.12 |
| Ubuntu | 24.04 |
| Min driver | R575+ |

This image supports both Blackwell GPU variants natively:
- **DGX Spark** (GB10, sm_121)
- **Thor nodes** (NVIDIA Thor / Jetson Thor, sm_110)

Previous base image (25.04-py3, CUDA 12.9) lacked sm_110 cubins
and would fail on Thor with "CUDA capability sm_110 is not
compatible" warnings.


## Building the Image

### Option A: Build on a Machine with Internet (DGX Spark)

```bash
cd ~/sage-yolo
docker build --no-cache -t yolo-object-counter:0.3.1 .
```

Then transfer to Thor (see "Transfer to Thor" below).

### Option B: Build Directly on Thor

If the Thor node has outbound internet access, you can clone
and build directly — no transfer step needed:

```bash
# One-time setup
git clone https://github.com/flint-pete/sage-yolo.git ~/sage-yolo

# Build (sudo required for Docker on Thor)
cd ~/sage-yolo
sudo docker build --no-cache -t yolo-object-counter:0.3.1 .
```

To rebuild after code changes:

```bash
cd ~/sage-yolo
git pull
sudo docker build --no-cache -t yolo-object-counter:0.3.1 .
```

This is the fastest iteration loop — edit code, `git push` from
your dev machine, `git pull && sudo docker build` on Thor.

Build time: ~5 minutes (the YOLO model is downloaded during build).

### Dockerfile: OpenCV Fix

The Dockerfile includes this fix to use `opencv-python-headless`
(no GUI) instead of the base image's `opencv-python`:

```dockerfile
RUN pip uninstall -y opencv-python opencv-python-headless 2>/dev/null; \
    rm -rf /usr/local/lib/python3.*/dist-packages/cv2* && \
    pip install --no-cache-dir opencv-python-headless>=4.8.0
```

The `rm -rf cv2*` clears stale files that `pip uninstall`
sometimes leaves behind.


## Testing the Image Locally

Before deploying, verify the image works with GPU:

### Quick sanity check

```bash
# Verify the image exists and app.py runs
sudo docker run --rm --runtime=nvidia yolo-object-counter:0.3.1 --help
```

### Batch test with test images

Run against the committed test images — same as the QA test,
but inside Docker:

```bash
mkdir -p ~/yolo-test-output

sudo docker run --rm --runtime=nvidia \
    -e PYWAGGLE_LOG_DIR=/output \
    -v ~/yolo-test-output:/output \
    -v ~/sage-yolo/tests/test-images:/images:ro \
    yolo-object-counter:0.3.1 \
    --image-dir /images --continuous N

# Check results
cat ~/yolo-test-output/data.ndjson | python3 -m json.tool
ls -la ~/yolo-test-output/uploads/
```

### Test with an RTSP camera (e.g. Reolink)

```bash
mkdir -p ~/yolo-camera-test

sudo docker run --rm --runtime=nvidia \
    -e PYWAGGLE_LOG_DIR=/output \
    -v ~/yolo-camera-test:/output \
    yolo-object-counter:0.3.1 \
    --stream "rtsp://admin:PASSWORD@CAMERA_IP:554/h264Preview_01_sub" \
    --interval 30 --continuous Y

# In another terminal, watch results:
tail -f ~/yolo-camera-test/data.ndjson
ls -la ~/yolo-camera-test/uploads/
```

Use the sub stream (`h264Preview_01_sub`, 640x360) rather than
the main stream — YOLO resizes to 640px anyway, so 4K frames
waste bandwidth.

Press Ctrl-C to stop.

### Test with an HTTP snapshot camera

For cameras behind a port-mapped router (e.g. Reolink with
only HTTP port forwarded, no RTSP):

```bash
mkdir -p ~/yolo-camera-test

# One-shot test (--continuous N)
sudo docker run --rm --runtime=nvidia \
    -e PYWAGGLE_LOG_DIR=/output \
    -v ~/yolo-camera-test:/output \
    yolo-object-counter:0.3.1 \
    --snapshot-url "http://IP:PORT/cgi-bin/api.cgi?cmd=Snap&channel=0&rs=snap&user=USER&password=PASS&width=640&height=360" \
    --continuous N

# Check results
cat ~/yolo-camera-test/data.ndjson
ls -la ~/yolo-camera-test/uploads/
```

The `&width=640&height=360` parameters request a low-resolution
snapshot (~12KB vs ~445KB at full 4K). This saves significant
bandwidth on LTE-connected cameras while giving YOLO exactly
the resolution it needs (it resizes to 640px anyway).

To verify the camera is reachable before running YOLO:

```bash
curl -o /tmp/test.jpg "http://IP:PORT/cgi-bin/api.cgi?cmd=Snap&channel=0&rs=test&user=USER&password=PASS"
file /tmp/test.jpg   # Should say "JPEG image data"
```


## Transfer to Thor (Option A only)

If you built on DGX Spark (not on Thor), transfer the image:

```bash
# On the build machine
docker save yolo-object-counter:0.3.1 | gzip > /tmp/yolo-object-counter.tar.gz
scp /tmp/yolo-object-counter.tar.gz beckman@thor-node:~/

# On Thor — load into Docker
sudo docker load < ~/yolo-object-counter.tar.gz
```


## Deploy via pluginctl (Sage Workflow)

> **v1 HISTORICAL — the examples below use the retired v1 camera CLI**
> (`--stream/--interval/--continuous/--snapshot-url`), which **no longer exists**
> in v2's `app.py`. For the current v2 deploy use the verified
> **"v2 cache-consumer deploy"** section near the top of this file. This section
> is kept only as a record of the v1 side-load mechanics (import, resource
> limits, monitoring), which are still accurate.

For running on a Thor node with the Sage infrastructure:

### Step 1: Import the image into k3s

pluginctl uses k3s/containerd, not Docker. The image must be
imported even if it was built locally with Docker:

```bash
sudo docker save yolo-object-counter:0.3.1 | sudo k3s ctr images import -
```

This takes ~6 minutes for the full image. Verify:

```bash
sudo k3s ctr images ls | grep yolo
```

### Step 2: Deploy

**With an RTSP camera (named or URL):**

```bash
sudo pluginctl deploy -n yolo-hummingcam \
    --resource 'memory=8Gi,limit.memory=16Gi' \
    docker.io/library/yolo-object-counter:0.3.1 \
    -- --stream bottom_camera --interval 60 --continuous Y
```

**With an HTTP snapshot camera (e.g. Reolink via port-mapped router):**

```bash
sudo pluginctl deploy -n yolo-hummingcam \
    --resource 'memory=8Gi,limit.memory=16Gi' \
    docker.io/library/yolo-object-counter:0.3.1 \
    -- --snapshot-url "http://IP:PORT/cgi-bin/api.cgi?cmd=Snap&channel=0&rs=snap&user=USER&password=PASS&width=640&height=360" \
       --interval 60 --continuous Y --upload-image Y
```

The `&width=640&height=360` fetches the sub-stream resolution
(~12KB vs ~445KB at 4K) — YOLO resizes to 640px anyway.
Critical for LTE-connected cameras.

**Important:** The `--resource 'memory=8Gi,limit.memory=16Gi'`
flag is required. Without it, the default k3s memory limit is
too low for YOLO11x and the pod gets OOMKilled (exit code 137).

### Step 3: Monitor

```bash
# Check the pod is running
sudo pluginctl ps

# Watch logs (live inference output)
sudo pluginctl logs yolo-hummingcam

# Follow logs continuously (Ctrl-C to stop watching)
sudo pluginctl logs -f yolo-hummingcam

# Check pod status (Running, Failed, etc.)
sudo kubectl get pod yolo-hummingcam
```

Note: `pluginctl logs` requires `sudo` on Thor (k3s kubeconfig
is root-only). If the pod shows `Failed`, check for OOMKilled:

```bash
sudo kubectl get pod yolo-hummingcam -o jsonpath='{.status.containerStatuses[0].state}' && echo ''
```

### Step 4: Stop

```bash
sudo pluginctl rm yolo-hummingcam
```

### Step 5: Rebuild and redeploy (after code changes)

```bash
cd ~/sage-yolo
git pull
sudo docker build -t yolo-object-counter:0.3.1 .

# If only app.py changed, skip --no-cache (uses cached layers, ~5 seconds)
# If Dockerfile or requirements.txt changed, use --no-cache

# Re-import into k3s
sudo docker save yolo-object-counter:0.3.1 | sudo k3s ctr images import -

# Remove old deployment and redeploy
sudo pluginctl rm yolo-hummingcam
sudo pluginctl deploy -n yolo-hummingcam \
    --resource 'memory=8Gi,limit.memory=16Gi' \
    docker.io/library/yolo-object-counter:0.3.1 \
    -- --snapshot-url "..." --interval 60 --continuous Y --upload-image Y
```


## Production: Scheduled SES Jobs on Thor (arm64)

This is the production deployment path — a scheduler-managed job that
survives reboots and is visible to the scheduler, instead of a hand-deployed
`pluginctl` pod. There are **two modes**, and choosing the right one matters
a lot for what you're observing:

### Continuous vs One-shot vs Windowed — choose before you deploy

| | **Windowed** (default for birds) | **Continuous** (always-on) | **One-shot** (cron) |
|---|---|---|---|
| Job file | `jobs/yolo-hummingcam-h00f.yaml` | (git history / hand-edit) | `jobs/yolo-hummingcam-h00f-oneshot.yaml` |
| Args | `--continuous Y --interval 15 --max-runtime 600` | `--continuous Y --interval 60` | `--continuous N` |
| Science rule | `cronjob(..., '0 * * * *')` | `schedule(...): True` | `cronjob(..., '*/10 * * * *')` |
| Sampling | every 15 s for 10 min/hour, then self-exit | every 60 s, forever | once per cron tick |
| GPU | ~10 min/hour (shares with other plugins) | held 24/7 | freed between ticks |
| Best for | birds on a **single-GPU node** shared with another model | birds on a node with a dedicated GPU | slow scenes: clouds, snow, occupancy |

**Why Windowed is the default on Thor (single-GPU sharing).** Thor has ONE GPU,
and two always-on continuous plugins cannot co-run — a held GPU blocks the
second pod from scheduling at all. So YOLO and BioCLIP each take a bounded
10-minute window per hour instead of holding the GPU 24/7:

```
:00–:10  YOLO     (--max-runtime 600, samples every 15s)
:10–:20  guard-band
:20–:30  BioCLIP  (--max-runtime 600, samples every 15s)
:30–:00  guard-band
```

The **`--max-runtime`** flag (added in 0.2.1) is what makes this work: in
`--continuous Y` mode the plugin loops every `--interval` seconds, then
self-exits after `--max-runtime` seconds — behaving like one long bounded
single-shot and freeing the GPU. A cron starts each window; the plugin ends it.
The 10-minute guard-bands absorb any model-load overrun so the two never collide
on the single GPU. Net GPU use: ~20 min/hour (~1/3) for both plugins combined.

**Why this matters — a real failure we hit:** when the hummingbird cam ran
as a `*/10` one-shot cron, bird detections collapsed from ~15/day to ~0. A
hummingbird visits the feeder for only a few seconds, so sampling once every
10 minutes almost never catches one in-frame. Windowed mode samples every 15s
*within* its window, restoring detection coverage while still sharing the GPU.
**Rule of thumb:** brief/unpredictable subject + shared GPU → windowed; brief
subject + dedicated GPU → continuous; slowly-changing scene → one-shot.

To switch modes, just deploy the other job file (see "Create + submit" below).

### Why the ECR portal build does NOT work for this plugin (yet)

The standard Sage workflow is "Create App → Register and Build," where the ECR
portal builds the image from your GitHub repo. **For this NVIDIA-base plugin that
build still fails**, verified 2026-07-10 on tag v0.3.1:

- The buildkit `/proc/acpi` runc bug (Infra #2) is **fixed** — `RUN` steps now
  start (pip downloads, etc. run for a while).
- BUT the ECR pipeline runs on **x86_64** and cross-builds `linux/arm64` under
  **QEMU emulation** (`buildctl ... --opt platform=linux/arm64`). The NVIDIA base
  (`nvcr.io/nvidia/pytorch:25.08-py3`) contains aarch64 binaries QEMU cannot
  emulate; `pip install` crashes with `qemu: uncaught target signal 6 (Aborted)`,
  build **exit 134**. This is **Infra #3**, still open — there is **no native
  arm64 builder** yet.

CPU-only plugins (birdnet, `python:3.12-slim`) DO build in ECR now — they have
native wheels and no CUDA, so QEMU handles them. GPU/NVIDIA plugins (yolo,
bioclip) do not. Until a native arm64 build node is added, use side-load below.

### Deploy path: build natively on Thor + side-load into k3s (WORKING)

Because SES pods on Thor use **`imagePullPolicy: IfNotPresent`**, the scheduler
uses a locally-cached image if one is present in k3s containerd under the exact
registry-qualified name — it never has to pull. So we build natively on Thor
(arm64, no QEMU), import into k3s, and register only the catalog *metadata* so SES
validation passes. No registry push, no portal build.

> **`scripts/deploy-sideload.sh` automates Steps 1–3 below** (and Step 4 with
> `--submit`). Prefer it — the manual steps here are the reference it wraps, kept
> for when you need to run a step by hand. The script reads the version from
> `sage.yaml` (no hardcoded tags) and guards against job-YAML tag drift.

**Step 1 — build natively on Thor + import into k3s:**

```bash
cd ~/sage-yolo && git pull
# Full registry-path tag so it matches the job YAML's image: field exactly:
sudo docker build -t registry.sagecontinuum.org/beckman/yolo-object-counter:0.3.1 .
sudo docker save registry.sagecontinuum.org/beckman/yolo-object-counter:0.3.1 \
  | sudo k3s ctr images import -
sudo k3s ctr images ls | grep yolo-object-counter   # expect io.cri-containerd.image=managed
```

**Step 2 — register the ECR catalog metadata record** (SES validates the job's
image against the catalog; without a record, `sesctl submit` fails with
`[...yolo-object-counter:0.3.1 does not exist in ECR]`). This registers metadata
only — it does NOT need the portal build to succeed:

```bash
python3 scripts/register-ecr-version.py \
    --namespace beckman --name yolo-object-counter \
    --from-version 0.3.0 --version 0.3.1 \
    --git-url https://github.com/flint-pete/sage-yolo.git \
    --token "$SAGE_TOKEN"
```

Make the app **public**, or SES returns `registry ... does not exist in ECR`.

**Step 3 — create + submit the SES job** (needs a write-scoped SES token). **Pick
the job file for your mode** (see "Continuous vs One-shot" above):

- Continuous (default, for hummingbirds): `jobs/yolo-hummingcam-h00f.yaml`
- One-shot cron (slow scenes): `jobs/yolo-hummingcam-h00f-oneshot.yaml`

```bash
sesctl --server https://es.sagecontinuum.org --token "$SES_USER_TOKEN" \
    create -f jobs/yolo-hummingcam-h00f.yaml      # returns a numeric job ID
sesctl --server https://es.sagecontinuum.org --token "$SES_USER_TOKEN" \
    submit -j <job-id>
```

> `create` uses `-f`/`--file-path`; `submit` takes `-j <numeric-id>`. `rm -s <id>`
> suspends, `rm <id>` removes. To switch modes: suspend + remove the old job, then
> create + submit the other job file.

**Step 4 — verify it fires and publishes** via the data API (the pod events show
*"already present on machine"*, confirming the side-loaded image was used):

```bash
curl -s -X POST https://data.sagecontinuum.org/api/v1/query \
  -H 'Content-Type: application/json' \
  -d '{"start":"-15m","filter":{"vsn":"H00F","name":"env.count.total"}}'
```

Record metadata identifies the job/image: `"job": "yolo-object-counter-<id>"` and
`"plugin": "registry.sagecontinuum.org/beckman/yolo-object-counter:0.3.1"`.

### Re-deploying after a code change (new version)

Bump the version everywhere (`sage.yaml`, `Makefile`, job YAML), then repeat
build → side-load with the new tag + register the catalog record. Because the tag
changes, k3s uses the new local image on the next tick; update the job YAML's
`image:` and re-submit.

### The durable fix (escalate to the ECR/cyberinfra team)

Add a **native arm64 build node** to the Jenkins/ECR pipeline (Infra #3) so the
portal "Register and Build" path produces arm64 NVIDIA images without QEMU. That
removes the manual side-load step for every Thor-targeted GPU plugin (yolo,
bioclip). The `/proc/acpi` fix (#2) already landed; #3 is the remaining blocker.

See: https://sagecontinuum.org/docs/tutorials/edge-apps/publishing-to-ecr


## Troubleshooting

**"unknown or invalid runtime name: nvidia"**
  → NVIDIA Container Toolkit not configured. Run:
  ```
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
  ```

**"No CUDA GPUs are available" inside container**
  → Missing `--runtime=nvidia` flag on docker run.

**numpy.core.multiarray failed to import**
  → OpenCV fix didn't run. Rebuild with `--no-cache`.

**"permission denied" on docker commands (Thor)**
  → Use `sudo docker ...` — Thor's Docker socket is root-only.

**Image too large to transfer**
  → The image is ~8-10 GB compressed. Use a fast network, or
  build directly on Thor (Option B) to skip the transfer entirely.

**RTSP stream timeout or black frames**
  → Verify the camera is reachable: `ffprobe rtsp://admin:PASS@IP:554/...`
  → Check firewall rules between the container and camera network.
  → Try the sub stream instead of main stream.

**OOMKilled (exit code 137) via pluginctl**
  → Default k3s memory limits are too low for YOLO11x (~4-5GB GPU
  + several GB system memory). Add `--resource 'memory=8Gi,limit.memory=16Gi'`
  to the `pluginctl deploy` command.

**HTTP snapshot returns HTML instead of JPEG**
  → The URL path is wrong. For Reolink cameras, the snapshot
  endpoint is `/cgi-bin/api.cgi?cmd=Snap&channel=0&...`, not `/`.
  Check credentials and verify with curl first:
  `curl -o test.jpg "http://IP:PORT/cgi-bin/api.cgi?cmd=Snap&channel=0&rs=test&user=USER&password=PASS"`

**pluginctl logs says "image can't be pulled"**
  → The image exists in Docker but not in k3s containerd.
  Import it: `sudo docker save IMAGE | sudo k3s ctr images import -`
