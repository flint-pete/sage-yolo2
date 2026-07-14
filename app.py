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
import crop_writer
import consumer
import selection
import seenstore

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


# ── cache consumer helpers (v2) ──────────────────────────────────────
def resolve_consumer_id(override=None):
    """The <consumer-id> seen-store segment (V2-Design §8.4).

    Must be BOTH stable across restarts of the same scheduled instance (so one-shot
    memory persists) AND distinct between different instances (so they don't clobber).
    Order: --consumer-id override > WAGGLE_JOB_NAME+WAGGLE_TASK_NAME > WAGGLE_APP_ID
    (pod UID, WITH warning -- it changes every pod, losing cross-restart memory).
    """
    if override:
        return override
    job = os.environ.get("WAGGLE_JOB_NAME", "").strip()
    task = os.environ.get("WAGGLE_TASK_NAME", "").strip()
    if job or task:
        return "%s-%s" % (job or "job", task or "task")
    app_id = os.environ.get("WAGGLE_APP_ID", "").strip()
    if app_id:
        logger.warning("no WAGGLE_JOB_NAME/TASK_NAME; using WAGGLE_APP_ID (%s) as "
                       "consumer-id -- pod UID changes each restart, so cross-restart "
                       "seen-memory will NOT persist. Set --consumer-id to fix.", app_id)
        return app_id
    logger.warning("no consumer identity in env; using 'default' consumer-id")
    return "default"


def parse_cache_input(input_path, cache_root):
    """Split --input (<root>/<cache-name>/<camera>) into (cache_name, camera).

    Used to build the composite seen-store path. Best-effort: takes the last two
    path segments; if the input isn't under cache_root the segments still work for
    keying (they just describe the stream). Returns (cache_name, camera).
    """
    norm = os.path.normpath(input_path).rstrip("/")
    parts = norm.split(os.sep)
    camera = parts[-1] if parts else "camera"
    cache_name = parts[-2] if len(parts) >= 2 else "cache"
    return cache_name, camera


# ── main loop ───────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="sage-yolo2 — YOLO11x object counter, pywaggle2 CACHE CONSUMER",
        epilog="""
Examples:
  # Production: consume frames image-sampler2 wrote to the shared cache (NO camera)
  python3 app.py --source cache --input /local-cache/hummingcam/top --classes bird

  # Local testing: a directory of images, no node/cache/camera
  python3 app.py --source image-dir --input ./tests/test-images --every 0

  # Standalone fallback: live camera (stock node, no cache provisioned)
  python3 app.py --source stream --input bottom_camera --every 30s

  # Standalone fallback: HTTP snapshot camera (creds via the URL / a Secret)
  python3 app.py --source snapshot --input "http://IP:PORT/cgi-bin/api.cgi?cmd=Snap&user=U&password=P" --every 0
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── source (explicit, mutually exclusive by construction — §10) ──
    parser.add_argument("--source", required=True,
                        choices=["cache", "stream", "snapshot", "image-dir"],
                        help="Acquisition mode. cache=consume producer frames from the "
                             "shared WES cache (production); stream/snapshot=live camera "
                             "(standalone fallback); image-dir=local test folder.")
    parser.add_argument("--input", required=True,
                        help="The source's argument: cache dir "
                             "(<root>/<cache-name>/<camera>) | camera name/RTSP URL | "
                             "HTTP snapshot URL | directory of test images.")

    # ── timing (two orthogonal clocks — 8.1/10) ──
    parser.add_argument("--every", default="0",
                        help="Wake cadence (batch clock): how often to process a batch. "
                             "0 = single-shot (run once, exit). Accepts s/m/h (e.g. 1h).")
    parser.add_argument("--select-every", default="0",
                        help="Sampling stride: one frame per this much CAPTURE-time. "
                             "0 = the single newest unseen frame. Accepts s/m/h (e.g. 15m).")
    parser.add_argument("--max-frames", type=int, default=1,
                        help="Cap frames processed per wake (0 = unlimited). With "
                             "--select-every 0 this means the K NEWEST frames.")
    parser.add_argument("--all-unseen", action="store_true",
                        help="Backlog mode: process EVERY not-yet-seen frame in the "
                             "cache (capped by --max-frames per wake). Overrides "
                             "--select-every.")
    parser.add_argument("--max-runtime", type=int, default=0,
                        help="Overall wall-clock bound in seconds (0 = forever).")

    # ── seen-memory (cache mode) ──
    parser.add_argument("--consumer-id", default=None,
                        help="Override the seen-store <consumer-id> segment (default: "
                             "WAGGLE_JOB_NAME+WAGGLE_TASK_NAME). Give two instances the "
                             "SAME id to make them cooperatively divide one cache.")
    parser.add_argument("--seen-store", default=None,
                        help="Override the full seen-store path (default: auto, under "
                             "the cache's reserved .state area).")
    parser.add_argument("--reprocess", action="store_true",
                        help="Ignore the seen-store (process regardless of memory). "
                             "Still records what it processes.")

    # ── model / inference (unchanged from v1) ──
    parser.add_argument("--model", default="yolo11x.pt",
                        help="YOLO model name/path (e.g. yolo11x.pt, yolo11n.pt)")
    parser.add_argument("--classes", default="",
                        help="Comma-separated classes to count (empty = all)")
    parser.add_argument("--conf-thres", type=float, default=0.25)
    parser.add_argument("--iou-thres", type=float, default=0.45)
    parser.add_argument("--imgsz", type=int, default=640)
    parser.add_argument("--half", action="store_true")
    parser.add_argument("--max-det", type=int, default=300)
    parser.add_argument("--augment", action="store_true")
    parser.add_argument("--agnostic-nms", action="store_true")

    # ── upload / save (unchanged from v1) ──
    parser.add_argument("--upload-image", default="Y",
                        help="Y = allow annotated-image uploads, N = never. Governed by "
                             "--save-match when set.")
    parser.add_argument("--save-match", default="",
                        help="OR-list of 'Class:confidence' rules (e.g. bird:0.5) or "
                             "'*:0.5'. Upload the annotated frame when ANY detection "
                             "matches ANY rule.")

    # ── crop-producer (v2.1.0, additive, OFF by default) ──────────────────
    parser.add_argument("--crop-match", default="",
                        help="OR-list of 'Class:confidence' rules (same grammar as "
                             "--save-match). For EACH detection that matches, crop its "
                             "bbox and write it as a v2 frame into a <camera>-crop cache "
                             "stream for a downstream classifier (e.g. BioCLIP). Empty "
                             "(default) = crop production OFF.")
    parser.add_argument("--crop-padding", type=float, default=0.15,
                        help="Fraction of bbox size to pad each crop on all sides "
                             "(clamped to image bounds). Default 0.15.")
    parser.add_argument("--crop-min-px", type=int, default=32,
                        help="Skip crops whose SHORT side (after padding+clamp) is "
                             "below this many pixels — a tiny box is useless to a "
                             "classifier; skip rather than upscale. Default 32.")
    parser.add_argument("--crop-cache-name", default="",
                        help="Cache name for the crop ring (default: <job>-crops).")
    parser.add_argument("--crop-max-count", type=int, default=500,
                        help="Max crops per stream ring (evict oldest). Default 500.")
    parser.add_argument("--crop-max-mb", type=float, default=500.0,
                        help="Max MB (decimal 10^6) per crop stream ring. Default 500.")
    args = parser.parse_args()

    target_classes = None
    if args.classes:
        target_classes = [c.strip().lower() for c in args.classes.split(",")]
        logger.info("Filtering to classes: %s", target_classes)

    try:
        save_rules = parse_save_match(args.save_match)
        crop_rules = parse_save_match(args.crop_match)   # same grammar; empty = OFF
    except SaveMatchError as e:
        logger.error("Invalid --save-match/--crop-match: %s", e)
        raise SystemExit(2)
    if crop_rules:
        logger.info("crop-producer ON: rules=%s padding=%.2f min-px=%d "
                    "cache=%s caps=[count=%d, mb=%.1f]",
                    args.crop_match, args.crop_padding, args.crop_min_px,
                    args.crop_cache_name or "<job>-crops",
                    args.crop_max_count, args.crop_max_mb)

    try:
        every_s = selection.parse_duration(args.every)
        select_every_s = selection.parse_duration(args.select_every)
    except ValueError as e:
        logger.error("Invalid duration: %s", e)
        raise SystemExit(2)

    detector = YOLODetector(args.model, args.conf_thres, args.iou_thres,
                            imgsz=args.imgsz, half=args.half,
                            max_det=args.max_det, augment=args.augment,
                            agnostic_nms=args.agnostic_nms)

    is_cache = args.source == "cache"
    is_image_dir = args.source == "image-dir"

    seen = None
    if is_cache:
        try:
            consumer.assert_cache_available(args.input)
        except consumer.CacheError as e:
            logger.error("%s", e)
            raise SystemExit(2)
        cache_root = consumer.resolve_cache_root()
        cache_name, camera = parse_cache_input(args.input, cache_root)
        consumer_id = resolve_consumer_id(args.consumer_id)
        store_path = args.seen_store or seenstore.seen_store_path(
            cache_root, consumer_id, cache_name, camera)
        seen = seenstore.SeenStore(store_path, reprocess=args.reprocess)
        logger.info("cache consumer: input=%s consumer-id=%s seen-store=%s (%d known)",
                    args.input, consumer_id, store_path, len(seen))

    with Plugin() as plugin:
        logger.info("sage-yolo2 started — source=%s input=%s model=%s every=%ds",
                    args.source, args.input, args.model, every_s)
        with plugin.timeit("plugin.duration.loadmodel"):
            detector.load()

        deadline = None
        if every_s > 0 and args.max_runtime > 0:
            deadline = time.monotonic() + args.max_runtime
            logger.info("Max runtime: %ds — will self-exit at end of window",
                        args.max_runtime)

        last_wake_ts_ns = 0
        img_iter = iter_image_dir(args.input) if is_image_dir else None

        while True:
            try:
                if is_cache:
                    _process_cache_wake(plugin, detector, args, target_classes,
                                        save_rules, seen, last_wake_ts_ns,
                                        select_every_s, crop_rules=crop_rules)
                    last_wake_ts_ns = time.time_ns()
                elif is_image_dir:
                    if not _process_image_dir(plugin, detector, args, target_classes,
                                              save_rules, img_iter,
                                              crop_rules=crop_rules):
                        break
                else:
                    _process_live(plugin, detector, args, target_classes, save_rules,
                                  crop_rules=crop_rules)
            except Exception:
                logger.exception("wake error")

            if every_s == 0 and not is_image_dir:
                break
            if deadline is not None and time.monotonic() + every_s >= deadline:
                logger.info("Max runtime reached — self-exiting to free the GPU")
                break
            if not is_image_dir:
                time.sleep(every_s)


def _publish_detections(plugin, args, detections, *, timestamp, camera, identity=None):
    """Publish per-class counts + total, frame-anchored (observation_ts=capture_ts,
    vsn/gps from identity when available). Shared by all sources."""
    counts = {}
    for det in detections:
        counts[det["class"]] = counts.get(det["class"], 0) + 1

    base_meta = {"camera": camera, "model": args.model}
    if identity is not None:
        if identity.vsn:
            base_meta["vsn"] = identity.vsn
        if identity.node_id:
            base_meta["node_id"] = identity.node_id
        if identity.has_location:            # never fabricated
            base_meta["lat"] = str(identity.lat)
            base_meta["lon"] = str(identity.lon)
            base_meta["location_source"] = identity.location_source

    for cls_name, count in counts.items():
        safe = cls_name.replace(" ", "_").replace("-", "_")
        plugin.publish("env.count.%s" % safe, count, timestamp=timestamp,
                       meta=dict(base_meta))
        logger.info("Published env.count.%s = %d", safe, count)

    classes_summary = ",".join("%s:%d" % (c, n) for c, n in sorted(counts.items()))
    total_meta = dict(base_meta)
    total_meta["classes"] = classes_summary or "none"
    total_meta["num_classes"] = str(len(counts))
    plugin.publish("env.count.total", sum(counts.values()), timestamp=timestamp,
                   meta=total_meta)
    return counts


def _maybe_upload(plugin, args, detections, frame, *, timestamp, camera, save_rules):
    """Upload the annotated frame per --save-match / legacy --upload-image."""
    if save_rules:
        do_upload = should_save(save_rules, detections, name_keys=["class"])
    else:
        do_upload = args.upload_image == "Y" and bool(detections)
    if not (do_upload and detections):
        return
    annotated = draw_boxes(frame, detections)
    tmp_path = os.path.join(tempfile.gettempdir(), "%s-annotated.jpg" % camera)
    cv2.imwrite(tmp_path, annotated)
    top = max(detections, key=lambda d: d["confidence"])
    plugin.upload_file(tmp_path, timestamp=timestamp,
                       meta={"camera": camera, "detections": str(len(detections)),
                             "top_class": str(top["class"]),
                             "confidence": str(top["confidence"])})
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
    logger.info("Uploaded annotated image (%d detections)", len(detections))


def _pad_clamp_bbox(bbox, w, h, padding):
    """Pad a [x1,y1,x2,y2] box by `padding` fraction of its size, clamp to image.

    Returns (x1,y1,x2,y2) ints with 0<=x1<x2<=w and 0<=y1<y2<=h, or None if the
    (clamped) box is degenerate (zero area)."""
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    px, py = int(round(bw * padding)), int(round(bh * padding))
    x1, y1 = max(0, x1 - px), max(0, y1 - py)
    x2, y2 = min(w, x2 + px), min(h, y2 + py)
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)


def _maybe_produce_crops(plugin, args, detections, frame, *, timestamp, camera,
                         crop_rules, identity=None, source_uid=""):
    """Crop each matching detection and ring-write it as a v2 frame (crop-producer).

    No-op when crop_rules is empty (default). For each detection passing
    --crop-match: pad+clamp the bbox, apply the --crop-min-px floor, encode JPEG,
    embed v2 metadata (inheriting the parent capture_ts + identity, adding source_*
    provenance and detection_index), and ring-write into <crop-cache-name>/
    <camera>-crop-<idx>/. Publishes env.crop.count (frame-anchored). Fail-soft:
    a per-crop error is logged and skipped, never crashing the wake."""
    if not crop_rules:
        return 0
    h, w = frame.shape[:2]
    vsn = (identity.vsn if identity and identity.vsn else "unknown")
    node_id = (identity.node_id if identity and identity.node_id else "")
    lat = identity.lat if (identity and identity.has_location) else None
    lon = identity.lon if (identity and identity.has_location) else None
    job = os.environ.get("WAGGLE_JOB_NAME", "").strip() or "sage"
    task = os.environ.get("WAGGLE_TASK_NAME", "").strip() or "sage-yolo2"
    plugin_str = _plugin_id_string()
    cache_root = consumer.resolve_cache_root()   # crops share the raw cache root
    cache_name = args.crop_cache_name or ("%s-crops" % job)

    produced = 0
    for idx, det in enumerate(detections):
        if not should_save(crop_rules, [det], name_keys=["class"]):
            continue
        clamped = _pad_clamp_bbox(det["bbox"], w, h, args.crop_padding)
        if clamped is None:
            continue
        x1, y1, x2, y2 = clamped
        if min(x2 - x1, y2 - y1) < args.crop_min_px:      # tiny-crop floor
            logger.info("skip crop (short side %dpx < --crop-min-px %d): %s",
                        min(x2 - x1, y2 - y1), args.crop_min_px, det["class"])
            continue
        try:
            crop_img = frame[y1:y2, x1:x2]
            ok, buf = cv2.imencode(".jpg", crop_img)
            if not ok:
                logger.warning("crop encode failed: %s idx=%d", det["class"], idx)
                continue
            crop_cam = "%s-crop-%d" % (camera, idx)
            source = {
                "source_class": det["class"],
                "source_confidence": float(det["confidence"]),
                "source_bbox": [int(v) for v in det["bbox"]],
                "source_unique_id": source_uid or "",
                "detection_index": idx,
            }
            final, _uid = crop_writer.embed_all(
                buf.tobytes(), vsn=vsn, node_id=node_id, job=job, task=task,
                plugin=plugin_str, camera=crop_cam, capture_ts_ns=int(timestamp),
                upload_ts_ns=None, lat=lat, lon=lon,
                acquisition_path="opencv-reencoded", source=source)
            sdir = crop_writer.stream_dir(cache_root, cache_name, crop_cam)
            name = crop_writer.build_v2_name(int(timestamp), vsn, crop_cam)
            res = crop_writer.write_frame(final, sdir, name,
                                          max_count=args.crop_max_count,
                                          max_mb=args.crop_max_mb)
            for wmsg in res.warnings:
                logger.warning("crop ring: %s", wmsg)
            if res.written:
                produced += 1
        except Exception:
            logger.exception("crop production failed: %s idx=%d", det["class"], idx)

    if produced:
        plugin.publish("env.crop.count", produced, timestamp=timestamp,
                       meta={"camera": camera, "cache_name": cache_name})
        logger.info("Produced %d crop(s) into %s/%s-crop-*", produced,
                    cache_name, camera)
    return produced


def _plugin_id_string():
    """'<registry.../name>:<version>' from WES env, best-effort."""
    name = os.environ.get("WAGGLE_PLUGIN_NAME", "").strip()
    version = os.environ.get("WAGGLE_PLUGIN_VERSION", "").strip()
    if name and version:
        return "%s:%s" % (name, version)
    return name or "sage-yolo2"


def _process_cache_wake(plugin, detector, args, target_classes, save_rules, seen,
                        last_wake_ts_ns, select_every_s, *, crop_rules=None):
    """One cache wake: scan -> select -> per frame read metadata + identity, infer,
    publish frame-anchored, mark seen (8.5)."""
    frames = consumer.scan_frames(args.input)
    selected = selection.select_frames(
        frames, last_wake_ts_ns=last_wake_ts_ns,
        select_every_ns=select_every_s * 1_000_000_000,
        all_unseen=args.all_unseen, max_frames=args.max_frames,
        seen=seen, reprocess=args.reprocess,
        uid_of=_frame_uid)
    if not selected:
        logger.info("cache wake: 0 frames to process")
        return
    node_info = consumer.get_node_info()
    for frame in selected:
        meta = consumer.read_frame_metadata(frame)
        identity = consumer.resolve_identity(meta, node_info=node_info)
        img = cv2.imread(frame.path)
        if img is None:                      # evicted between select and read (8.6)
            logger.warning("frame vanished before read: %s", frame.name)
            continue
        with plugin.timeit("plugin.duration.inference"):
            detections = detector.detect(img, target_classes)
        ts = meta.capture_ts_ns              # observation time = capture time (7)
        cam = meta.camera or frame.camera
        _publish_detections(plugin, args, detections, timestamp=ts, camera=cam,
                            identity=identity)
        _maybe_upload(plugin, args, detections, img, timestamp=ts, camera=cam,
                      save_rules=save_rules)
        _maybe_produce_crops(plugin, args, detections, img, timestamp=ts,
                             camera=cam, crop_rules=crop_rules, identity=identity,
                             source_uid=meta.unique_id or "")
        if meta.unique_id:
            seen.mark(meta.unique_id)
        if not detections:
            logger.info("no detections: %s", frame.name)


def _frame_uid(frame):
    """unique_id for dedup: the frame's metadata SHA256 (falls back to name)."""
    meta = consumer.read_frame_metadata(frame)
    return meta.unique_id or frame.name


def _process_image_dir(plugin, detector, args, target_classes, save_rules, img_iter,
                       *, crop_rules=None):
    """One image from the local test directory. Returns False when exhausted."""
    with plugin.timeit("plugin.duration.input"):
        try:
            img_path, frame, timestamp = next(img_iter)
        except StopIteration:
            logger.info("All test images processed")
            return False
    camera = os.path.splitext(os.path.basename(img_path))[0]
    with plugin.timeit("plugin.duration.inference"):
        detections = detector.detect(frame, target_classes)
    _publish_detections(plugin, args, detections, timestamp=timestamp, camera=camera)
    _maybe_upload(plugin, args, detections, frame, timestamp=timestamp,
                  camera=camera, save_rules=save_rules)
    _maybe_produce_crops(plugin, args, detections, frame, timestamp=timestamp,
                         camera=camera, crop_rules=crop_rules)
    if not detections:
        logger.info("No detections: %s", os.path.basename(img_path))
    return True


def _process_live(plugin, detector, args, target_classes, save_rules,
                  *, crop_rules=None):
    """One live frame (stream or snapshot standalone fallback)."""
    with plugin.timeit("plugin.duration.input"):
        if args.source == "snapshot":
            frame = fetch_snapshot(args.input)
            camera = "http-snapshot"
        else:
            frame = Camera(args.input).snapshot().data
            camera = args.input
    timestamp = time.time_ns()
    with plugin.timeit("plugin.duration.inference"):
        detections = detector.detect(frame, target_classes)
    _publish_detections(plugin, args, detections, timestamp=timestamp, camera=camera)
    _maybe_upload(plugin, args, detections, frame, timestamp=timestamp,
                  camera=camera, save_rules=save_rules)
    _maybe_produce_crops(plugin, args, detections, frame, timestamp=timestamp,
                         camera=camera, crop_rules=crop_rules)


if __name__ == "__main__":
    main()
