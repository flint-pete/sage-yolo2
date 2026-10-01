# YOLO Object Counter — Plugin Overview

A comprehensive tutorial for understanding, configuring, and deploying the
YOLO Object Counter plugin on the Sage Continuum edge computing platform.

---

## Table of Contents

1. [What This Plugin Does](#what-this-plugin-does)
2. [File Layout](#file-layout)
3. [How It Works (Internals)](#how-it-works-internals)
4. [Configuration Reference](#configuration-reference)
5. [Local Testing (No Node Required)](#local-testing)
6. [Deployment on a Sage Node](#deployment-on-a-sage-node)
7. [Data Published](#data-published)
8. [Querying Your Data](#querying-your-data)
9. [Choosing the Right Model](#choosing-the-right-model)
10. [Key Issues and Pitfalls](#key-issues-and-pitfalls)
11. [Example Scenarios](#example-scenarios)

---

## 1. What This Plugin Does

The YOLO Object Counter captures frames from a camera attached to a Sage
edge node, runs the YOLO11x object detection model on the GPU, counts how
many of each object class appear in the frame, and publishes those counts
to the Sage data store.  Optionally, it uploads an annotated image with
bounding boxes drawn around each detected object.

In concrete terms: every N seconds the node says "I see 3 people, 2 cars,
and 1 bicycle" — and that becomes a queryable time-series record in the Sage
data API.

**Why at the edge?**  Streaming raw video to the cloud costs bandwidth
(~5 Mbps per 1080p stream), introduces latency, and raises privacy concerns.
YOLO inference takes <50 ms on the GPU, so we can publish compact counts
instead of raw frames.

---

## 2. File Layout

```
yolo-object-counter/
├── app.py                          # Main application (~260 lines)
├── Dockerfile                      # Container build instructions
├── requirements.txt                # Python dependencies
├── sage.yaml                       # ECR metadata — name, version, inputs
├── overview.md                     # This file
├── ecr-meta/                       # ECR submission metadata
│   ├── README                      # Instructions for ECR submission
│   ├── ecr-science-description.md  # Science narrative (displayed on ECR portal)
│   ├── ecr-credits-license.txt     # Authors, funding, license
│   ├── ecr-project-keywords.txt    # Search keywords / ontology terms
│   └── ecr-project-url.txt         # URL to project science page
│   (ecr-icon.jpg)                  # 512x512 icon — create before submission
│   (ecr-science-image.jpg)         # 1920x1080 science image — create before submission
├── jobs/                           # Sample Sage job specifications
│   └── yolo-counter-job.yaml       # Deploy on W097 counting people & cars
└── tests/                          # Self-contained test suite
    ├── run-tests.sh                # Run all tests for this plugin
    ├── test_yolo_local.py          # Local validation on your own images
    ├── test_harness.py             # Pywaggle test harness library
    └── test-images/                # Test images (committed, real photos)
```

**What each file does:**

| File | Purpose |
|------|---------|
| `app.py` | The entire plugin — model loading, camera capture, inference, publishing |
| `Dockerfile` | Builds the container image from NVIDIA PyTorch base, installs deps |
| `requirements.txt` | pip dependencies: pywaggle, ultralytics, torch, opencv, numpy, pillow |
| `sage.yaml` | ECR registry metadata: name, version, inputs, ontology, architecture |
| `ecr-meta/` | Supplementary metadata displayed on the ECR portal page |

---

## 3. How It Works (Internals)

### Startup Sequence

```
1. Parse command-line arguments (argparse)
2. Create YOLODetector instance
   └── Load YOLO model weights (auto-downloads if not cached)
   └── Move model to GPU (CUDA) or CPU
3. Open camera stream via pywaggle Camera abstraction
4. Enter main loop
```

### Main Loop (one iteration)

```
┌─────────────────────────────────────────────────────┐
│ 1. cam.snapshot()                                   │
│    └── Captures a single BGR numpy frame            │
│                                                     │
│ 2. detector.detect(frame, target_classes)            │
│    └── Runs YOLO inference on GPU                   │
│    └── Filters by confidence threshold              │
│    └── Applies NMS (non-maximum suppression)        │
│    └── Optionally filters to target classes only    │
│    └── Returns list of {class, confidence, bbox}    │
│                                                     │
│ 3. Count detections per class                       │
│    └── dict accumulation on class names            │
│                                                     │
│ 4. plugin.publish() for each class                  │
│    └── "env.count.person" → 3                       │
│    └── "env.count.car" → 2                          │
│    └── "env.count.total" → 5                        │
│    └── meta: model, confidence threshold, camera     │
│                                                     │
│ 5. If --upload-image Y:                              │
│    └── draw_boxes() — annotates frame with boxes     │
│    └── Save to temp JPEG                             │
│    └── plugin.upload_file() — sends to object store  │
│                                                     │
│ 6. Sleep(interval) and repeat (if continuous=Y)      │
└─────────────────────────────────────────────────────┘
```

### Key Classes

**`YOLODetector`** (lines 36–73)
- Wraps the Ultralytics YOLO API
- `__init__`: loads model, moves to CUDA, stores thresholds
- `detect(frame, target_classes)`: runs inference, returns list of dicts
- Each detection dict: `{"class": "person", "confidence": 0.87, "bbox": [x1, y1, x2, y2]}`

**`draw_boxes()`** (lines 76–88)
- Takes the raw frame + detections list
- Draws green rectangles and labels using OpenCV
- Returns a new annotated frame (does not modify the original)

### pywaggle Integration

The plugin uses two pywaggle abstractions:

1. **`Camera(stream)`** — abstracts camera access.  The `stream` argument
   can be:
   - A named camera: `"bottom_camera"`, `"top_camera"` (resolved by node config)
   - An RTSP URL: `"rtsp://192.168.1.100:554/stream"`
   - A file path: `"/path/to/test-image.jpg"` (for local testing)

2. **`Plugin()`** — handles data publishing.  Used as a context manager:
   - `plugin.publish(topic, value, meta={...})` — publishes a measurement
   - `plugin.upload_file(path)` — uploads a file to the object store
   - When `PYWAGGLE_LOG_DIR` is set, writes to local files instead of Beehive

**Critical rule**: All `meta={}` values must be strings.  `{"count": str(n)}`
not `{"count": n}`.  pywaggle's `valid_meta()` enforces this and will raise
ValueError on non-string values.

---

## 4. Configuration Reference

All parameters are passed as command-line arguments:

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--stream` | string | `bottom_camera` | Camera source name or RTSP URL |
| `--image-dir` | string | (none) | Directory of test images — replaces `--stream` for local batch testing |
| `--model` | string | `yolo11x.pt` | YOLO model name (see Section 9 for options) |
| `--interval` | int | `30` | Seconds between captures (camera mode only, ignored with `--image-dir`) |
| `--conf-thres` | float | `0.25` | Minimum detection confidence (0.0–1.0) |
| `--iou-thres` | float | `0.45` | Non-maximum suppression IoU threshold (0.0–1.0) |
| `--imgsz` | int | `640` | Input image size — images are resized to this before inference. Larger values detect smaller objects but use more memory and are slower |
| `--half` | flag | off | FP16 half-precision inference (faster, slightly less accurate) |
| `--max-det` | int | `300` | Maximum detections per image. Lower this for scenes with few objects |
| `--augment` | flag | off | Test-time augmentation (TTA) — runs inference at multiple scales/flips for better accuracy at ~3x speed cost |
| `--agnostic-nms` | flag | off | Class-agnostic NMS — treats all classes as one during suppression. Useful for overlapping objects of different classes |
| `--classes` | string | `""` (all) | Comma-separated COCO class names to count (empty = all 80) |
| `--continuous` | string | `Y` | `Y` = loop forever, `N` = process once and exit |
| `--upload-image` | string | `Y` | `Y` = upload annotated image with bounding boxes each cycle |

> **Ultralytics documentation**: The inference parameters `--conf-thres`,
> `--iou-thres`, `--imgsz`, `--half`, `--max-det`, `--augment`, and
> `--agnostic-nms` are passed directly to the Ultralytics YOLO predict API.
> For detailed explanations, trade-offs, and advanced tuning, see:
> **https://docs.ultralytics.com/modes/predict/#inference-arguments**

### The `--classes` Filter

YOLO11x recognises 80 COCO classes.  By default all are counted.  To count
only specific classes, pass a comma-separated list:

```bash
--classes "person,car,truck,bus,bicycle"     # traffic monitoring
--classes "bird"                              # avian counting
--classes "dog,cat"                           # pet detection
```

Class names must match COCO names exactly (lowercase).  Common classes:
`person`, `bicycle`, `car`, `motorcycle`, `bus`, `truck`, `bird`, `cat`,
`dog`, `horse`, `sheep`, `cow`, `backpack`, `umbrella`, `handbag`,
`suitcase`, `bottle`, `chair`, `bench`, `potted plant`.

### Confidence Threshold

The `--conf-thres` parameter controls the minimum confidence for a detection
to be included.  Lower values detect more objects but increase false
positives:

| Threshold | Use Case |
|-----------|----------|
| 0.10 | Maximum recall — detect everything (noisy) |
| 0.25 | Default — good balance |
| 0.50 | High precision — only confident detections |
| 0.70 | Very conservative — few false positives |

---

## 5. Local Testing (No Node Required)

pywaggle supports local testing by redirecting all publish/upload calls to
local files.  No Sage node, no Docker, no credentials needed.  There are
three complementary approaches, from quick smoke-test to full validation.

### 5a. Single-Image Quick Test (app.py --stream)

The fastest way to verify the plugin works on a single image:

```bash
# 1. Set up a Python virtual environment
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Tell pywaggle to write output locally
export PYWAGGLE_LOG_DIR=./test-output

# 3. Run single-shot against one test image
python3 app.py --stream test-image.jpg --continuous N

# 4. Check results
cat test-output/data.ndjson          # Published measurements (one JSON per line)
ls test-output/uploads/              # Annotated images
```

### 5b. Directory Mode (app.py --image-dir)

Process every image in a directory in one run — ideal for batch evaluation
on a curated set of test photos.  The plugin iterates through all images
(JPG, PNG, WEBP, BMP) alphabetically, runs inference on each, publishes
per-class counts and totals, and uploads annotated frames.

```bash
# Create a directory of test images
mkdir test-photos
cp  my-street-photo.jpg  test-photos/
cp  my-bird-photo.jpg    test-photos/

# Run on every image in the directory
export PYWAGGLE_LOG_DIR=./test-output
python3 app.py --image-dir test-photos --continuous N

# Filter to specific classes
python3 app.py --image-dir test-photos --classes bird,person --continuous N

# Adjust confidence threshold
python3 app.py --image-dir test-photos --conf-thres 0.5 --continuous N
```

Each image is processed exactly once.  The source filename replaces the
camera name in published metadata, so you can trace which results came
from which image.  The `--continuous N` flag is implied (directory mode
always runs once), but it's good practice to pass it explicitly.

### 5c. Local Test Runner (test_yolo_local.py)

The repository includes a standalone local test runner that invokes app.py
against the committed test images, validates pywaggle output, and prints a
detailed per-image report.  This is the go-to test for verifying detection
quality on your own images before deploying to a Sage node.

**Test image directory**: `tests/test-images/`  
Images in this directory are committed to the repo and shared across the
team.  Drop any JPG/PNG/WEBP image here — photos of streets, animals,
indoor scenes, or anything containing COCO objects.

```bash
cd Sage-agents/plugins/yolo-object-counter
source ../../tests/.venv/bin/activate

# Default: detect all 80 COCO classes in tests/test-images/
python tests/test_yolo_local.py

# Filter to specific classes
python tests/test_yolo_local.py --classes person,car,bird

# High-confidence only
python tests/test_yolo_local.py --confidence 0.7

# Verbose — show all plugin log output
python tests/test_yolo_local.py -v
```

**What the runner does:**
1. Discovers all images in `tests/test-images/`
2. Invokes `app.py --image-dir ...` with your chosen parameters
3. Parses the pywaggle `data.ndjson` output
4. Prints per-image class counts with visual bars
5. Saves a machine-readable `tests/output/yolo-local/report.json`
6. Exits 0 on success, 1 on failure (CI-friendly)

**Sample output:**

```
======================================================================
  YOLO LOCAL TEST
======================================================================
  Test images:  tests/test-images
  Image count:  1
    - test1.jpg  (971 KB)
  Confidence:   0.25
  IoU:          0.45
  Classes:      all (80 COCO classes)
  Output:       tests/output/yolo-local
======================================================================

  Image 1: test1.jpg
    Total detections: 3
    Classes detected: 2
      bird                    1  [###############---------------]
      vase                    2  [##############################]
    Annotated image:  uploaded ✓

======================================================================
  SUMMARY
======================================================================
  Images processed:    1/1
  Total detections:    3
  Unique classes:      2
  Inference time:      0.45s
  Annotated uploads:   1
  NDJSON records:      4

  PASSED — all images detected successfully
  Report: tests/output/yolo-local/report.json
======================================================================
```

### 5d. Test Runner CLI Options

The test runner (`test_yolo_local.py`) accepts these command-line flags:

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--confidence` | float | `0.25` | Minimum detection confidence |
| `--iou` | float | `0.45` | IoU threshold for NMS |
| `--classes` | string | (all) | Comma-separated class filter |
| `--add-no-detect-text` | flag | on | Save "detected no objects" copy for images with zero detections |
| `--no-add-no-detect-text` | flag | off | Disable the above |
| `--verbose`, `-v` | flag | off | Show all plugin log output |

```bash
# Run the test suite
cd Sage-agents/plugins/yolo-object-counter
source ../../tests/.venv/bin/activate

# Default: all 80 COCO classes, conf=0.25
python3 tests/test_yolo_local.py

# Filter to birds and people, high confidence
python3 tests/test_yolo_local.py --classes bird,person --confidence 0.7

# Verbose — see all plugin log messages
python3 tests/test_yolo_local.py -v

# Run all plugins (from project root)
bash tests/run-all-tests.sh
```

### What `PYWAGGLE_LOG_DIR` Does

When this environment variable is set, pywaggle intercepts all
`plugin.publish()` and `plugin.upload_file()` calls and writes them locally:

- **`data.ndjson`** — Newline-delimited JSON, one record per publish call:
  ```json
  {"timestamp":"2025-06-12T10:30:00Z","name":"env.count.person","value":3,"meta":{"model":"yolo11x.pt","camera":"test-image.jpg"}}
  ```
- **`uploads/`** — Uploaded files saved as `{timestamp}-{filename}`

All three testing approaches (single image, directory mode, local runner)
use this mechanism.  The test runner and integration test set it
automatically — you only need to export it manually for ad-hoc app.py runs.

---

## 6. Deployment on a Sage Node

### Building the Docker Image

Deploy by building natively on Thor and side-loading into k3s — the ECR portal
build does **not** work for this NVIDIA-base plugin yet (QEMU cross-build crash,
Infra #3; see DOCKER-BUILD.md). For a quick local smoke test first:

```bash
docker build --no-cache -t yolo-object-counter:0.3.1 .
docker run --rm --runtime=nvidia -e PYWAGGLE_LOG_DIR=/tmp/out \
    yolo-object-counter:0.3.1 --stream bottom_camera --continuous N
```

The image is served to SES by side-loading it into the node's k3s containerd and
registering an ECR catalog *metadata* record; the registry tag is:
`registry.sagecontinuum.org/beckman/yolo-object-counter:0.3.1`

See **DOCKER-BUILD.md** for the full build → register → side-load → submit workflow.

### Submitting a Job

Create a job YAML (see `jobs/yolo-counter-job.yaml` for a ready-to-use example):

```yaml
name: yolo-bird-counter
plugins:
  - name: yolo-object-counter
    pluginSpec:
      image: registry.sagecontinuum.org/beckman/yolo-object-counter:0.3.1
      args:
        - "--stream"
        - "bottom_camera"
        - "--classes"
        - "bird"
        - "--interval"
        - "60"
        - "--conf-thres"
        - "0.30"
nodes:
  W097:
scienceRules:
  - "schedule(yolo-object-counter): cronjob('count-birds', '*/10 * * * *')"
successcriteria:
  - WallClock('7day')
```

Submit (note actual sesctl flags — `create -f`, `submit -j <numeric-id>`):

```bash
export SES_HOST=https://es.sagecontinuum.org
export SES_USER_TOKEN=<your-token>
sesctl --server "$SES_HOST" --token "$SES_USER_TOKEN" create -f yolo-bird-counter-job.yaml
sesctl --server "$SES_HOST" --token "$SES_USER_TOKEN" submit -j <job-id>
```

### On-Node Testing with pluginctl

```bash
ssh waggle-dev-node-V032
pluginctl build .
pluginctl run --name yolo-counter \
    registry.sagecontinuum.org/beckman/yolo-object-counter:0.3.1 \
    -- --stream bottom_camera --classes bird --continuous N
pluginctl logs yolo-counter
```

---

## 7. Data Published

Every inference cycle publishes these measurements:

```
env.count.person     → 3      meta: {model: "yolo11x.pt", camera: "bottom_camera"}
env.count.car        → 2      meta: {model: "yolo11x.pt", camera: "bottom_camera"}
env.count.total      → 5      meta: {model: "yolo11x.pt", camera: "bottom_camera"}
```

Plus one uploaded JPEG (if `--upload-image Y`): the original frame with
bounding boxes and confidence labels drawn on it.

---

## 8. Querying Your Data

After deployment, retrieve counts using the Sage data API:

```python
import sage_data_client

df = sage_data_client.query(
    start="-1h",
    filter={"name": "env.count.*", "vsn": "W097", "plugin": "*yolo*"}
)
print(df[["timestamp", "name", "value"]].to_string())
```

Or via curl:

```bash
curl -s -X POST https://data.sagecontinuum.org/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{"start":"-1h","filter":{"name":"env.count.bird","vsn":"W097"}}' \
  | python3 -m json.tool
```

---

## 9. Choosing the Right Model

| Model | Params | mAP (COCO) | Inference (GB10) | Use Case |
|-------|--------|------------|------------------|----------|
| yolo11n.pt | 2.6M | 39.5% | ~5 ms | Maximum speed, low power |
| yolo11s.pt | 9.4M | 47.0% | ~8 ms | Fast + decent accuracy |
| yolo11m.pt | 20.1M | 51.5% | ~14 ms | Balanced |
| yolo11x.pt | 56.9M | 54.7% | ~21 ms | **Default — best accuracy** |
| yolov8x.pt | 68.2M | 53.9% | ~25 ms | Alternative large model |

The model auto-downloads on first use.  To pre-download, add a `RUN` in the
Dockerfile:

```dockerfile
RUN python3 -c "from ultralytics import YOLO; YOLO('yolo11x.pt')"
```

---

## 10. Key Issues and Pitfalls

### Meta Values Must Be Strings

```python
# WRONG — will raise ValueError
plugin.publish("env.count.person", 3, meta={"confidence": 0.25})

# RIGHT
plugin.publish("env.count.person", 3, meta={"confidence": "0.25"})
```

pywaggle's `valid_meta()` enforces `isinstance(v, str)` on every meta value.

### Model Download on First Run

YOLO models are downloaded from Ultralytics on first use (~130 MB for
yolo11x.pt).  On edge nodes without internet, the model must be baked into
the Docker image at build time (see the Dockerfile `RUN` download line).

### Camera Stream Names

Camera names like `bottom_camera` and `top_camera` are resolved by the
node's local configuration.  Not every node has every camera.  Check node
capabilities via `pluginctl` or the Sage portal before deploying.

### GPU vs CPU

The plugin auto-detects CUDA availability.  On CPU-only nodes, inference
is ~10x slower (200–500 ms vs 20 ms).  For CPU nodes, use a smaller model
like `yolo11n.pt`.

### OpenCV and Headless Mode

The plugin uses `opencv-python-headless` (no GUI dependencies) which is
the correct choice for headless edge nodes.  If you need GUI functions
like `cv2.imshow()` for local debugging, switch to `opencv-python` in
`requirements.txt`.

### NMS and Overlapping Detections

The `--iou-thres` parameter controls non-maximum suppression.  Lower values
are more aggressive at removing overlapping boxes.  If you see duplicate
detections of the same object, lower this from 0.45 to 0.30.

---

## 11. Example Scenarios

### Scenario A: Bird Counting at a Wildlife Station

```bash
python3 app.py \
    --stream bottom_camera \
    --model yolo11x.pt \
    --classes bird \
    --interval 60 \
    --conf-thres 0.30 \
    --upload-image Y
```

This captures a frame every 60 seconds, counts only birds, and uploads
annotated images.  The higher confidence threshold (0.30) reduces false
positives from distant or partially occluded birds.

### Scenario B: Urban Traffic Monitoring

```bash
python3 app.py \
    --stream rtsp://192.168.1.100:554/traffic \
    --classes "person,car,truck,bus,bicycle,motorcycle" \
    --interval 30 \
    --conf-thres 0.25
```

### Scenario C: Single-Shot Testing

```bash
export PYWAGGLE_LOG_DIR=./test-output
python3 app.py \
    --stream /path/to/parking-lot.jpg \
    --classes car \
    --continuous N
cat test-output/data.ndjson
```

---

## Appendix: Performance Benchmarks (DGX Spark / Sage Thor)

Measured on NVIDIA GB10 (Blackwell), 128 GB unified memory, aarch64:

| Metric | Value |
|--------|-------|
| Model load time | 2.71 s |
| Warmup inference | 0.74 s |
| Average inference | 20.6 ms |
| GPU memory | ~1.2 GB |

These benchmarks show that YOLO11x is extremely lightweight relative to the
128 GB available on Thor nodes, leaving ample room for concurrent plugins
(BioCLIP, vLLM).

---

## Appendix: Further Reading — Beyond Object Counting

This plugin performs **single-frame object counting** using the 80 pretrained
COCO classes.  That's a solid starting point, but YOLO11 and the Sage
platform support much more.  This section outlines advanced directions for
students who want to go further.

### Custom-Trained Models for Specific Species

The pretrained COCO model recognises generic classes like `bird`, `cat`, and
`dog` — it cannot distinguish a red-tailed hawk from a Cooper's hawk.  For
species-level detection, you can fine-tune YOLO on a custom dataset:

1. Collect labeled images of your target species (e.g. using
   [Roboflow](https://roboflow.com/) or
   [Label Studio](https://labelstud.io/))
2. Train a custom model:
   ```python
   from ultralytics import YOLO
   model = YOLO("yolo11x.pt")      # start from pretrained weights
   model.train(data="my-birds.yaml", epochs=100, imgsz=640)
   ```
3. Deploy the resulting `best.pt` with `--model best.pt` — no plugin code
   changes needed

For details on training, see:
**https://docs.ultralytics.com/modes/train/**

### Object Tracking Across Frames

This plugin treats each frame independently — it counts objects but has no
concept of identity or movement over time.  A single frame of an animal
lying down is ambiguous (resting or in distress?).  Tracking the same
animal across frames reveals behavioral patterns: did it lie down slowly
after unusual movements?

YOLO11 supports built-in object tracking via `model.track()`, which assigns
persistent IDs to objects across video frames.  Building a Sage plugin
around `model.track()` instead of `model()` would enable:

- **Individual animal identification** (count unique visitors, not just objects)
- **Behavioral anomaly detection** (sudden changes in movement patterns)
- **Dwell-time analysis** (how long a person or vehicle stays in a zone)

For details on tracking, see:
**https://docs.ultralytics.com/modes/track/**

### Other YOLO Tasks (Segmentation, Pose, Classification)

This plugin uses `task=detect` (bounding boxes + class labels).  YOLO11
supports additional tasks that could be built as separate Sage plugins:

| Task | What It Does | Use Case |
|------|-------------|----------|
| `detect` | Bounding boxes + class labels | **This plugin** — object counting |
| `segment` | Pixel-level masks per object | Vegetation cover, water body area |
| `pose` | Skeleton keypoints per object | Animal gait analysis, posture monitoring |
| `classify` | Whole-image classification | Scene type (urban/rural/water/forest) |
| `obb` | Oriented bounding boxes | Aerial/satellite imagery, angled objects |

Each task uses a different model variant (e.g. `yolo11x-seg.pt` for
segmentation, `yolo11x-pose.pt` for pose estimation).

For an overview of tasks, see:
**https://docs.ultralytics.com/tasks/**

### Ultralytics Blog: Animal Monitoring with YOLO

For a broader perspective on how YOLO is used in livestock management,
wildlife conservation, and veterinary research — including non-invasive
drone-based monitoring and behavioral AI — see:
**https://www.ultralytics.com/blog/role-of-computer-vision-and-ultralytics-yolo11-in-animal-monitoring**
