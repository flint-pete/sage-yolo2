# Testing on Thor Nodes

How to test the YOLO Object Counter plugin on a Sage Thor node.


## Quick Start: Build on Thor and Test

The fastest workflow — clone, build, and test all on Thor:

```bash
# One-time: clone the repo
git clone https://github.com/flint-pete/sage-yolo.git ~/sage-yolo

# Build the Docker image
cd ~/sage-yolo
sudo docker build --no-cache -t yolo-object-counter:0.3.1 .

# Run against test images
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

To iterate after code changes:

```bash
cd ~/sage-yolo
git pull
sudo docker build --no-cache -t yolo-object-counter:0.3.1 .
```

For the full Docker build reference (base image, OpenCV fix,
NVIDIA Container Toolkit setup), see **DOCKER-BUILD.md**.


## Testing with an RTSP Camera

To test against a network-attached IP camera (e.g. Reolink),
pass the RTSP URL directly to `--stream`:

```bash
mkdir -p ~/yolo-camera-test

sudo docker run --rm --runtime=nvidia \
    -e PYWAGGLE_LOG_DIR=/output \
    -v ~/yolo-camera-test:/output \
    yolo-object-counter:0.3.1 \
    --stream "rtsp://admin:PASSWORD@CAMERA_IP:554/h264Preview_01_sub" \
    --interval 30 --continuous Y
```

This captures a frame every 30 seconds, runs YOLO inference, and
saves results to `~/yolo-camera-test/`:

- `data.ndjson` — per-class object counts (one JSON record per frame)
- `uploads/` — annotated images with bounding boxes

Monitor results in another terminal:

```bash
tail -f ~/yolo-camera-test/data.ndjson
ls -la ~/yolo-camera-test/uploads/
```

Press Ctrl-C to stop.

### RTSP URL formats (Reolink)

| Stream | Resolution | URL |
|--------|-----------|-----|
| Main | 4K (3840x2160) | `rtsp://admin:PASS@IP:554/h264Preview_01_main` |
| Sub | 640x360 | `rtsp://admin:PASS@IP:554/h264Preview_01_sub` |

Use the **sub stream** for testing — YOLO resizes to 640px
anyway, so 4K wastes bandwidth.

### Useful options for camera testing

```bash
--interval 30              # Seconds between captures (default: 30)
--classes "person,car"     # Only count specific classes
--conf-thres 0.30          # Higher confidence = fewer false positives
--upload-image Y           # Save annotated images (default: Y)
--continuous Y             # Keep running (default: Y)
```


## Testing via pluginctl (Sage Infrastructure)

For testing with the full Sage stack (data publishing, scheduler):

```bash
# If image was transferred from another machine:
sudo k3s ctr images import ~/yolo-object-counter.tar.gz

# Deploy
sudo pluginctl deploy -n yolo-counter \
    docker.io/library/yolo-object-counter:0.3.1 \
    -- --stream bottom_camera --interval 30 --continuous Y

# Monitor
sudo pluginctl ps
pluginctl logs -f yolo-counter

# Stop
sudo pluginctl rm yolo-counter
```


## Inspecting Output

All output from Docker testing is captured locally via
`PYWAGGLE_LOG_DIR`:

```bash
# Pretty-print published measurements
cat ~/yolo-test-output/data.ndjson | python3 -m json.tool

# View uploaded images
ls -la ~/yolo-test-output/uploads/

# Copy annotated images to your dev machine
scp beckman@thor-node:~/yolo-test-output/uploads/* ./
```

Each line in `data.ndjson` looks like:

```json
{"timestamp":"2025-06-17T10:30:00Z","name":"env.count.person","value":3,"meta":{"model":"yolo11x.pt","camera":"rtsp://..."}}
```


## Clean Up

```bash
# Remove test output
rm -rf ~/yolo-test-output ~/yolo-camera-test

# Remove the Docker image (to free disk space)
sudo docker rmi yolo-object-counter:0.3.1

# Remove the cloned repo
rm -rf ~/sage-yolo
```


## Troubleshooting

**"permission denied" on docker commands**
  → Use `sudo docker ...` — Thor's Docker socket is root-only.

**"No CUDA GPUs are available"**
  → Missing `--runtime=nvidia` on the docker run command.

**RTSP stream timeout or black frames**
  → Verify camera reachability: `ffprobe rtsp://admin:PASS@IP:554/...`
  → Check that the container can reach the camera network.
  → Try the sub stream URL instead of main.

**torch.cuda.is_available() returns False (direct execution)**
  → Your user is not in the `video` group. `/dev/nvmap` is owned
  by `root:video`. Use Docker instead (containers get GPU access
  automatically via `--runtime=nvidia`).
