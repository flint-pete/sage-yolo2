"""
YOLO Object Counter Plugin for Sage/Waggle
Captures frames from node cameras, runs YOLO11x inference (54.7% mAP COCO),
publishes per-class object counts and uploads annotated images.

Default model: yolo11x.pt (56.9M params, best accuracy in YOLO family).
Requires ~4-5 GB GPU memory at 1080p. Fits easily in 128GB unified memory
on DGX Spark / Sage Thor nodes.

Measurement topics:
  env.count.<class_name>   — integer count per detected class
  env.count.total          — total detections across all classes
  upload                   — annotated JPEG with bounding boxes
"""
import argparse
import logging
import os
import time
import tempfile
import urllib.request
import urllib.error

import cv2
import numpy as np
import torch
from ultralytics import YOLO

from waggle.plugin import Plugin
from waggle.data.vision import Camera

from save_match import parse_save_match, should_save, SaveMatchError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("yolo-object-counter")


# ── detector ────────────────────────────────────────────────────────
class YOLODetector:
    """Thin wrapper around Ultralytics YOLO for Sage plugins."""

    def __init__(self, model_name: str, conf_thres: float = 0.25, iou_thres: float = 0.45,
                 imgsz: int = 640, half: bool = False, max_det: int = 300,
                 augment: bool = False, agnostic_nms: bool = False):
        self.model_name = model_name
        self.conf_thres = conf_thres
        self.iou_thres = iou_thres
        self.imgsz = imgsz
        self.half = half
        self.max_det = max_det
        self.augment = augment
        self.agnostic_nms = agnostic_nms
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = None  # loaded lazily via load() so the model construction
                           # can be timed inside the Plugin context
                           # (plugin.duration.loadmodel)

    def load(self):
        """Construct + move the model to device. Separated from __init__ so
        callers can wrap it in plugin.timeit('plugin.duration.loadmodel')."""
        logger.info("Loading %s on %s (conf=%.2f, iou=%.2f, imgsz=%d)",
                     self.model_name, self.device, self.conf_thres,
                     self.iou_thres, self.imgsz)
        self.model = YOLO(self.model_name)
        self.model.to(self.device)
        logger.info("Model loaded — %d classes available", len(self.model.names))

    def detect(self, frame: np.ndarray, target_classes: list[str] | None = None):
        """
        Run inference on a BGR numpy frame.
        Returns list of dicts: [{class, confidence, bbox:[x1,y1,x2,y2]}, ...]
        """
        results = self.model(
            frame,
            conf=self.conf_thres,
            iou=self.iou_thres,
            imgsz=self.imgsz,
            half=self.half,
            max_det=self.max_det,
            augment=self.augment,
            agnostic_nms=self.agnostic_nms,
            verbose=False,
        )
        detections = []
        for r in results:
            for box in r.boxes:
                cls_name = r.names[int(box.cls[0])]
                if target_classes and cls_name.lower() not in target_classes:
                    continue
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy().astype(int)
                detections.append({
                    "class": cls_name,
                    "confidence": float(box.conf[0]),
                    "bbox": [int(x1), int(y1), int(x2), int(y2)],
                })
        return detections


def draw_boxes(frame: np.ndarray, detections: list[dict]) -> np.ndarray:
    """Draw bounding boxes + labels on a copy of the frame."""
    annotated = frame.copy()
    for det in detections:
        x1, y1, x2, y2 = det["bbox"]
        label = f"{det['class']} {det['confidence']:.2f}"
        color = (0, 255, 0)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 1)
        cv2.rectangle(annotated, (x1, y1 - th - 6), (x1 + tw, y1), color, -1)
        cv2.putText(annotated, label, (x1, y1 - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 0), 1)
    return annotated


# ── image sources ────────────────────────────────────────────────────

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}


def iter_image_dir(directory: str):
    """
    Yield (image_path, frame_bgr, timestamp_ns) for every image in a
    directory.  Used for local testing without a live camera.
    """
    from pathlib import Path

    dir_path = Path(directory)
    if not dir_path.is_dir():
        raise FileNotFoundError(f"Image directory not found: {directory}")

    files = sorted(
        p for p in dir_path.iterdir()
        if p.suffix.lower() in IMAGE_EXTENSIONS and p.is_file()
        and not p.name.startswith(".")
    )
    if not files:
        raise FileNotFoundError(
            f"No image files found in {directory}. "
            f"Supported extensions: {', '.join(sorted(IMAGE_EXTENSIONS))}"
        )

    logger.info("Found %d test images in %s", len(files), directory)
    for img_path in files:
        frame = cv2.imread(str(img_path))
        if frame is None:
            logger.warning("Skipping unreadable file: %s", img_path.name)
            continue
        yield str(img_path), frame, time.time_ns()


def fetch_snapshot(url: str) -> np.ndarray:
    """
    Fetch a JPEG snapshot from an HTTP URL and return as a BGR numpy array.

    Works with Reolink's HTTP API:
      http://IP:PORT/cgi-bin/api.cgi?cmd=Snap&channel=0&rs=abc&user=USER&password=PASS

    Also works with any URL that returns a JPEG image (MJPEG snapshot
    endpoints, generic IP camera snapshot URLs, etc.).
    """
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=15) as resp:
            img_bytes = resp.read()
    except urllib.error.URLError as e:
        raise ConnectionError(f"Failed to fetch snapshot from {url}: {e}") from e

    img_array = np.frombuffer(img_bytes, dtype=np.uint8)
    frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError(
            f"Could not decode image from {url} "
            f"({len(img_bytes)} bytes received)"
        )
    return frame


# ── main loop ───────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="YOLO Object Counter for Sage",
        epilog="""
Examples:
  # Normal mode — capture from camera on a Sage node
  python3 app.py --stream bottom_camera --classes bird --interval 60

  # Local testing — detect objects in all images in a directory
  export PYWAGGLE_LOG_DIR=./test-output
  python3 app.py --image-dir ./test-images --continuous N

  # Local testing — single image via --stream (legacy)
  python3 app.py --stream /path/to/photo.jpg --continuous N

  # Filter to specific COCO classes
  python3 app.py --image-dir ./test-images --classes "person,car,truck" --continuous N

  # HTTP snapshot camera (e.g. Reolink via port-mapped router)
  python3 app.py --snapshot-url "http://IP:PORT/cgi-bin/api.cgi?cmd=Snap&channel=0&rs=snap&user=USER&password=PASS" --continuous N
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--stream", default="bottom_camera",
                        help="Camera stream name or RTSP URL (ignored if --image-dir is set)")
    parser.add_argument("--image-dir", default=None,
                        help="Directory of test images (replaces camera input for local testing)")
    parser.add_argument("--snapshot-url", default=None,
                        help="HTTP URL that returns a JPEG snapshot (e.g. Reolink CGI API). "
                             "Overrides --stream. Credentials go in the URL query string. "
                             "Example: http://IP:PORT/cgi-bin/api.cgi?cmd=Snap&channel=0"
                             "&rs=snap&user=USER&password=PASS")
    parser.add_argument("--model", default="yolo11x.pt",
                        help="YOLO model name/path (e.g. yolo11x.pt, yolov8x.pt, yolo11n.pt)")
    parser.add_argument("--interval", type=int, default=30,
                        help="Seconds between captures (camera mode only)")
    parser.add_argument("--conf-thres", type=float, default=0.25,
                        help="Confidence threshold (0.0-1.0, default: 0.25)")
    parser.add_argument("--iou-thres", type=float, default=0.45,
                        help="IoU threshold for NMS (0.0-1.0, default: 0.45)")
    parser.add_argument("--imgsz", type=int, default=640,
                        help="Input image size for inference — images are resized to this "
                             "before YOLO processes them (default: 640). Larger values "
                             "detect smaller objects but use more GPU memory and are slower. "
                             "See: https://docs.ultralytics.com/modes/predict/#inference-arguments")
    parser.add_argument("--half", action="store_true",
                        help="Use FP16 half-precision inference (faster, slightly less accurate). "
                             "See: https://docs.ultralytics.com/modes/predict/#inference-arguments")
    parser.add_argument("--max-det", type=int, default=300,
                        help="Maximum detections per image (default: 300). Lower this if you "
                             "only expect a few objects per frame.")
    parser.add_argument("--augment", action="store_true",
                        help="Enable test-time augmentation (TTA) — runs inference at multiple "
                             "scales/flips for better accuracy at the cost of ~3x slower speed. "
                             "See: https://docs.ultralytics.com/modes/predict/#inference-arguments")
    parser.add_argument("--agnostic-nms", action="store_true",
                        help="Class-agnostic NMS — treats all classes as one during NMS. "
                             "Useful when overlapping objects of different classes cause duplicates.")
    parser.add_argument("--classes", default="",
                        help="Comma-separated classes to count (empty = all)")
    parser.add_argument("--continuous", default="Y",
                        help="Y = loop, N = single-shot")
    parser.add_argument("--max-runtime", type=int, default=0,
                        help="When in continuous mode (--continuous Y), exit after this "
                             "many seconds (0 = run forever). Lets a scheduled job behave "
                             "like one long bounded single-shot: e.g. --max-runtime 600 "
                             "--interval 15 samples every 15s for ~10 min then self-exits, "
                             "freeing the GPU for other plugins. Ignored when --continuous N.")
    parser.add_argument("--upload-image", default="Y",
                        help="DEPRECATED gate kept for back-compat. Y = allow image "
                             "uploads, N = never upload. Actual saving is governed by "
                             "--save-match; with --upload-image Y and no --save-match, "
                             "every cycle with detections is uploaded (legacy behavior).")
    parser.add_argument("--save-match", default="",
                        help="When to SAVE (upload) the annotated frame. Comma-separated "
                             "OR-list of 'Class:confidence' rules, e.g. "
                             "\"bird:0.5,cat:0.6\". Class is matched case-insensitively "
                             "and EXACTLY against the COCO class name. Use \"*:0.5\" to "
                             "save any frame with a detection >=0.5. The frame is saved "
                             "if ANY detection matches ANY rule. When set, it REPLACES "
                             "the legacy upload-every-cycle behavior. Omit to keep legacy "
                             "behavior (governed by --upload-image).")
    args = parser.parse_args()

    target_classes = None
    if args.classes:
        target_classes = [c.strip().lower() for c in args.classes.split(",")]
        logger.info("Filtering to classes: %s", target_classes)

    # Parse --save-match up front and FAIL FAST on a malformed spec.
    try:
        save_rules = parse_save_match(args.save_match)
    except SaveMatchError as e:
        logger.error("Invalid --save-match: %s", e)
        raise SystemExit(2)
    if save_rules:
        logger.info("Image save rules (--save-match): %s",
                    ", ".join(f"{'*' if r.is_wildcard else r.name}>={r.min_confidence}"
                              for r in save_rules))
    elif args.upload_image == "Y":
        logger.info("No --save-match rules: using legacy behavior — upload every "
                    "cycle that has detections (--upload-image Y).")
    else:
        logger.info("No --save-match rules and --upload-image N: images will NOT "
                    "be saved (counts + heartbeat still publish).")

    detector = YOLODetector(args.model, args.conf_thres, args.iou_thres,
                            imgsz=args.imgsz, half=args.half,
                            max_det=args.max_det, augment=args.augment,
                            agnostic_nms=args.agnostic_nms)

    # ── Choose image source ──────────────────────────────────────────
    using_image_dir = args.image_dir is not None
    using_snapshot_url = args.snapshot_url is not None

    if using_image_dir:
        # Local testing mode: read images from a directory
        image_source = iter_image_dir(args.image_dir)
        source_label = f"image-dir:{args.image_dir}"
    elif using_snapshot_url:
        # HTTP snapshot mode: fetch JPEG from URL each cycle
        source_label = args.snapshot_url.split("?")[0]  # log URL without query params
    else:
        # Production mode: capture from camera (RTSP or named)
        camera = Camera(args.stream)
        source_label = args.stream

    with Plugin() as plugin:
        logger.info("Plugin started — source=%s, interval=%ds, model=%s",
                     source_label, args.interval, args.model)

        # Load the model, timed as plugin.duration.loadmodel (nanoseconds) —
        # the standard Sage telemetry convention (see avian-diversity-monitoring
        # / TAFT). Makes cold-start cost observable for GPU-window sizing.
        with plugin.timeit("plugin.duration.loadmodel"):
            detector.load()

        if not using_image_dir:
            logger.info("Capture interval: %ds", args.interval)

        # Bounded continuous mode: in --continuous Y, optionally self-exit after
        # --max-runtime seconds so a scheduled job runs like one long single-shot
        # and frees the GPU for other plugins. deadline=None means run forever.
        deadline = None
        if args.continuous == "Y" and args.max_runtime > 0 and not using_image_dir:
            deadline = time.monotonic() + args.max_runtime
            logger.info("Max runtime: %ds — will self-exit at the end of the window",
                        args.max_runtime)

        while True:
            try:
                # Acquire input, timed as plugin.duration.input (nanoseconds) —
                # standard Sage phase metric, published every cycle (even on
                # empty scenes) so it doubles as a liveness signal.
                with plugin.timeit("plugin.duration.input"):
                    if using_image_dir:
                        # Get next image from directory iterator
                        try:
                            img_path, frame, timestamp = next(image_source)
                        except StopIteration:
                            logger.info("All test images processed")
                            break
                        source_name = os.path.basename(img_path)
                        logger.info("Processing: %s (%dx%d)",
                                    source_name, frame.shape[1], frame.shape[0])
                    elif using_snapshot_url:
                        frame = fetch_snapshot(args.snapshot_url)
                        timestamp = time.time_ns()
                        source_name = "http-snapshot"
                        logger.info("Snapshot: %dx%d from %s",
                                    frame.shape[1], frame.shape[0], source_label)
                    else:
                        sample = camera.snapshot()
                        frame = sample.data  # numpy BGR
                        timestamp = sample.timestamp
                        source_name = args.stream

                # Run inference, timed as plugin.duration.inference (nanoseconds).
                with plugin.timeit("plugin.duration.inference"):
                    detections = detector.detect(frame, target_classes)

                # Aggregate counts per class
                counts: dict[str, int] = {}
                for det in detections:
                    counts[det["class"]] = counts.get(det["class"], 0) + 1

                # Publish per-class counts
                for cls_name, count in counts.items():
                    # Sanitize class name for pywaggle topic (a-z0-9_ only)
                    safe_name = cls_name.replace(" ", "_").replace("-", "_")
                    topic = f"env.count.{safe_name}"
                    plugin.publish(
                        topic, count,
                        timestamp=timestamp,
                        meta={"camera": source_name, "model": args.model},
                    )
                    logger.info("Published %s = %d", topic, count)

                # Build a self-describing classes summary for the total record.
                # Format: "bottle:2,person:1" — all classes and counts in one field
                # so you can read a single record without cross-referencing.
                classes_summary = ",".join(
                    f"{c}:{n}" for c, n in sorted(counts.items())
                )
                total = sum(counts.values())

                # Publish total (includes full class breakdown in meta)
                plugin.publish(
                    "env.count.total",
                    total,
                    timestamp=timestamp,
                    meta={
                        "camera": source_name,
                        "model": args.model,
                        "classes": classes_summary if classes_summary else "none",
                        "num_classes": str(len(counts)),
                    },
                )

                # SAVE (selective): decide whether to upload the annotated frame.
                # - With --save-match rules: upload only when a detection matches
                #   a rule (any rule x any detection). This is the new behavior.
                # - Without rules: fall back to legacy --upload-image Y (upload
                #   every cycle that has detections).
                if save_rules:
                    do_upload = should_save(save_rules, detections, name_keys=["class"])
                else:
                    do_upload = args.upload_image == "Y" and bool(detections)

                if do_upload and detections:
                    annotated = draw_boxes(frame, detections)
                    stem = os.path.splitext(source_name)[0]
                    tmp_path = os.path.join(tempfile.gettempdir(),
                                            f"{stem}-annotated.jpg")
                    cv2.imwrite(tmp_path, annotated)
                    top = max(detections, key=lambda d: d["confidence"])
                    plugin.upload_file(tmp_path, timestamp=timestamp,
                                       meta={"camera": source_name,
                                             "detections": str(len(detections)),
                                             "top_class": str(top["class"]),
                                             "confidence": str(top["confidence"])})
                    if os.path.exists(tmp_path):
                        os.unlink(tmp_path)
                    why = "save-match matched" if save_rules else "legacy upload"
                    logger.info("Uploaded annotated image (%d detections, %s)",
                                len(detections), why)

                if not detections:
                    logger.info("No detections this cycle")

            except Exception:
                logger.exception("Inference error")

            if args.continuous != "Y" and not using_image_dir:
                break
            # Bounded-window self-exit: stop before sleeping if the next cycle
            # would start at/after the deadline.
            if deadline is not None and time.monotonic() + args.interval >= deadline:
                logger.info("Max runtime reached — self-exiting to free the GPU")
                break
            if not using_image_dir:
                time.sleep(args.interval)


if __name__ == "__main__":
    main()
