#!/usr/bin/env python3
"""
YOLO Local Test Runner — Detect objects in real images from a test directory.

Runs the actual YOLO plugin (app.py) against every image in
tests/test-images/, validates the pywaggle output, and prints
a detailed report with per-image detections, class counts, and timing.

This is the go-to test for checking whether YOLO is performing well
on your own images before deploying to a Sage node.

Setup:
    # Activate the test venv (must have ultralytics, pywaggle, torch, etc.)
    source tests/.venv/bin/activate

    # Add your test images — any JPG/PNG/WEBP photos of streets, wildlife,
    # indoor scenes, etc.  The more diverse, the better the test.
    cp  my-street-photo.jpg   tests/test-images/
    cp  my-wildlife-photo.png tests/test-images/

Usage:
    # Default: detect all COCO classes
    python tests/test_yolo_local.py

    # Filter to specific classes (comma-separated)
    python tests/test_yolo_local.py --classes person,car,bird
    python tests/test_yolo_local.py --classes bird

    # Adjust confidence threshold (default: 0.25)
    python tests/test_yolo_local.py --confidence 0.5

    # Adjust IoU threshold for NMS (default: 0.45)
    python tests/test_yolo_local.py --iou 0.3

    # Verbose: show all log output from the plugin
    python tests/test_yolo_local.py --verbose

Output:
    tests/output/yolo-local/             — pywaggle data.ndjson + uploads/
    tests/output/yolo-local/report.json  — machine-readable per-image results
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────
TESTS_DIR = Path(__file__).parent
PLUGIN_DIR = TESTS_DIR.parent
PLUGIN_APP = PLUGIN_DIR / "app.py"
TEST_IMAGES = TESTS_DIR / "test-images"
OUTPUT_DIR = TESTS_DIR / "output" / "yolo-local"


def count_images(directory: Path) -> list[Path]:
    """Find all image files in a directory."""
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}
    return sorted(p for p in directory.iterdir()
                  if p.suffix.lower() in exts and p.is_file()
                  and not p.name.startswith("."))


def run_yolo(confidence: float, iou: float, classes_filter: str,
             verbose: bool) -> tuple[int, str]:
    """
    Run the YOLO plugin against the test image directory.
    Returns (exit_code, combined_output).
    """
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True)

    env = os.environ.copy()
    env["PYWAGGLE_LOG_DIR"] = str(OUTPUT_DIR)

    cmd = [
        sys.executable, str(PLUGIN_APP),
        "--image-dir", str(TEST_IMAGES),
        "--conf-thres", str(confidence),
        "--iou-thres", str(iou),
        "--continuous", "N",
    ]

    if classes_filter:
        cmd.extend(["--classes", classes_filter])

    if verbose:
        print(f"  CMD: {' '.join(cmd)}")
        print(f"  PYWAGGLE_LOG_DIR={OUTPUT_DIR}")

    result = subprocess.run(
        cmd, env=env,
        capture_output=True, text=True, timeout=600,
    )

    output = result.stdout + result.stderr
    return result.returncode, output


def parse_ndjson(output_dir: Path) -> list[dict]:
    """Parse data.ndjson from a run."""
    ndjson = output_dir / "data.ndjson"
    if not ndjson.exists():
        return []
    records = []
    with open(ndjson) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def extract_results(records: list[dict]) -> list[dict]:
    """
    Group ndjson records by image and extract per-image results.
    Returns list of dicts with image name, class counts, total, and upload status.

    The plugin publishes per-class counts as env.count.<class>,
    a total as env.count.total, and an annotated image upload.
    Records for the same image share the same timestamp.
    """
    # Group records by timestamp (each image gets a unique timestamp)
    by_ts: dict[int, list[dict]] = {}
    for r in records:
        ts = r.get("timestamp", 0)
        by_ts.setdefault(ts, []).append(r)

    results = []
    for ts in sorted(by_ts.keys()):
        group = by_ts[ts]
        image_name = "unknown"
        class_counts = {}
        total = 0
        has_upload = False

        for r in group:
            name = r.get("name", "")
            meta = r.get("meta", {})

            if "camera" in meta:
                image_name = meta["camera"]

            if name == "env.count.total":
                total = int(r["value"])
            elif name.startswith("env.count.") and name != "env.count.total":
                cls = name.replace("env.count.", "")
                class_counts[cls] = int(r["value"])
            elif name == "upload":
                has_upload = True

        if class_counts or total > 0:
            results.append({
                "image": image_name,
                "class_counts": class_counts,
                "total_detections": total,
                "uploaded": has_upload,
            })

    return results


def print_image_report(result: dict, idx: int):
    """Print a single image result with confidence bars for class counts."""
    print(f"\n  Image {idx}: {result['image']}")
    total = result["total_detections"]
    counts = result["class_counts"]

    if not counts:
        print("    No detections")
        return

    # Find max count for scaling bars
    max_count = max(counts.values()) if counts else 1

    print(f"    Total detections: {total}")
    print(f"    Classes detected: {len(counts)}")
    for cls_name in sorted(counts.keys()):
        cnt = counts[cls_name]
        bar_len = int((cnt / max_count) * 30) if max_count > 0 else 0
        bar = "#" * bar_len + "-" * (30 - bar_len)
        print(f"      {cls_name:20s}  {cnt:3d}  [{bar}]")

    if result.get("uploaded"):
        print(f"    Annotated image:  uploaded ✓")


def main():
    parser = argparse.ArgumentParser(
        description="YOLO local test runner — detect objects in test images and report results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  # Detect all COCO classes in test images
  python tests/test_yolo_local.py

  # Filter to birds and people only
  python tests/test_yolo_local.py --classes bird,person

  # High-confidence detections only
  python tests/test_yolo_local.py --confidence 0.7

  # Verbose output for debugging
  python tests/test_yolo_local.py -v
""",
    )
    parser.add_argument("--classes", default="",
                        help="Comma-separated class filter (default: all COCO classes)")
    parser.add_argument("--confidence", type=float, default=0.25,
                        help="Minimum detection confidence (default: 0.25)")
    parser.add_argument("--iou", type=float, default=0.45,
                        help="IoU threshold for NMS (default: 0.45)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Show plugin log output")
    parser.add_argument("--add-no-detect-text", action="store_true", default=True,
                        help="For images with no detections, save a copy with "
                             "'detected no objects' text to the uploads directory "
                             "(default: on, use --no-add-no-detect-text to disable)")
    parser.add_argument("--no-add-no-detect-text", action="store_false",
                        dest="add_no_detect_text")
    args = parser.parse_args()

    # ── Preflight checks ─────────────────────────────────────────────
    if not PLUGIN_APP.exists():
        print(f"ERROR: Plugin not found at {PLUGIN_APP}", file=sys.stderr)
        sys.exit(1)

    images = count_images(TEST_IMAGES)
    if not images:
        print(f"ERROR: No test images found in {TEST_IMAGES}", file=sys.stderr)
        print(f"\nAdd images to test:", file=sys.stderr)
        print(f"  cp your-photo.jpg {TEST_IMAGES}/", file=sys.stderr)
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 70)
    print("  YOLO LOCAL TEST")
    print("=" * 70)
    print(f"  Test images:  {TEST_IMAGES}")
    print(f"  Image count:  {len(images)}")
    for img in images:
        size_kb = img.stat().st_size / 1024
        print(f"    - {img.name}  ({size_kb:.0f} KB)")
    print(f"  Confidence:   {args.confidence}")
    print(f"  IoU:          {args.iou}")
    if args.classes:
        print(f"  Classes:      {args.classes}")
    else:
        print(f"  Classes:      all (80 COCO classes)")
    print(f"  Output:       {OUTPUT_DIR}")
    print("=" * 70)

    # ── Run detection ─────────────────────────────────────────────────
    print(f"\n{'─' * 70}")
    print(f"  RUNNING YOLO INFERENCE")
    print(f"{'─' * 70}")

    t0 = time.time()
    exit_code, output = run_yolo(
        args.confidence, args.iou, args.classes, args.verbose)
    elapsed = time.time() - t0

    if args.verbose:
        for line in output.strip().split("\n"):
            print(f"    | {line}")

    if exit_code != 0:
        print(f"\n  FAILED (exit code {exit_code})")
        if not args.verbose:
            # Show tail of output on failure even without verbose
            for line in output.strip().split("\n")[-15:]:
                print(f"    | {line}")
        sys.exit(1)

    records = parse_ndjson(OUTPUT_DIR)
    results = extract_results(records)

    # ── Generate "no detection" images if requested ───────────────
    if args.add_no_detect_text:
        import cv2
        detected_images = {r["image"] for r in results}
        uploads_dir = OUTPUT_DIR / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        no_detect_count = 0
        for img in images:
            if img.name not in detected_images:
                frame = cv2.imread(str(img))
                if frame is None:
                    continue
                h, w = frame.shape[:2]
                text = "detected no objects"
                font = cv2.FONT_HERSHEY_SIMPLEX
                # Scale font relative to image width (~12pt feel at 1000px)
                scale = max(0.5, w / 1000.0)
                thickness = max(1, int(scale * 2))
                (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
                x = 10
                y = h - 10 - baseline
                # Dark background for readability
                cv2.rectangle(frame, (x - 4, y - th - 4),
                              (x + tw + 4, y + baseline + 4), (0, 0, 0), -1)
                cv2.putText(frame, text, (x, y), font, scale,
                            (0, 255, 0), thickness)
                stem = img.stem
                out_path = uploads_dir / f"{stem}-no-detections.jpg"
                cv2.imwrite(str(out_path), frame)
                no_detect_count += 1
        if no_detect_count:
            print(f"\n  Added 'detected no objects' text to {no_detect_count} images")

    overall_pass = True
    if not results:
        print(f"\n  WARNING: No detections published at all!")
        overall_pass = False
    else:
        for idx, r in enumerate(results, 1):
            print_image_report(r, idx)

    # ── Summary ──────────────────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print(f"  SUMMARY")
    print(f"{'=' * 70}")
    total_dets = sum(r["total_detections"] for r in results)
    total_classes = set()
    for r in results:
        total_classes.update(r["class_counts"].keys())

    print(f"  Images processed:    {len(results)}/{len(images)}")
    print(f"  Total detections:    {total_dets}")
    print(f"  Unique classes:      {len(total_classes)}")
    if total_classes:
        print(f"  Classes found:       {', '.join(sorted(total_classes))}")
    print(f"  Inference time:      {elapsed:.2f}s")
    if len(images) > 0:
        print(f"  Avg per image:       {elapsed / len(images):.2f}s")

    # Check uploads
    uploads_dir = OUTPUT_DIR / "uploads"
    n_uploads = len(list(uploads_dir.iterdir())) if uploads_dir.exists() else 0
    print(f"  Annotated uploads:   {n_uploads}")
    print(f"  NDJSON records:      {len(records)}")

    # ── Save report ──────────────────────────────────────────────────
    report = {
        "test_images_dir": str(TEST_IMAGES),
        "image_count": len(images),
        "images": [img.name for img in images],
        "confidence_threshold": args.confidence,
        "iou_threshold": args.iou,
        "classes_filter": args.classes if args.classes else "all",
        "results": results,
        "total_detections": total_dets,
        "unique_classes": sorted(total_classes),
        "inference_time_s": round(elapsed, 2),
        "ndjson_records": len(records),
        "uploads": n_uploads,
        "passed": overall_pass,
    }
    report_path = OUTPUT_DIR / "report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    if overall_pass:
        print(f"\n  PASSED — all images detected successfully")
    else:
        print(f"\n  FAILED — check output above for details")

    print(f"  Report: {report_path}")
    print(f"{'=' * 70}")

    sys.exit(0 if overall_pass else 1)


if __name__ == "__main__":
    main()
