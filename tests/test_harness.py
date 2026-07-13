"""
Test harness for Sage edge plugins.

Provides mock classes and utilities for testing plugins without
requiring actual ML models, GPU hardware, or Sage credentials.

All plugin output is captured to local files via PYWAGGLE_LOG_DIR:
  - data.ndjson     — published measurements (one JSON per line)
  - uploads/        — uploaded files (images, etc.)

Usage:
    import test_harness as th
    output_dir = th.setup_test_output("yolo-test")
    # ... run plugin ...
    results = th.parse_output(output_dir)
    th.print_report(results, "YOLO Object Counter")
"""
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Resolve project paths
TESTS_DIR = Path(__file__).parent
PROJECT_DIR = TESTS_DIR.parent
TEST_IMAGES_DIR = TESTS_DIR / "test-images"
OUTPUT_DIR = TESTS_DIR / "output"

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}


def get_test_images() -> list[str]:
    """
    Return sorted list of test image paths.

    Test images live in tests/test-images/.
    Supports: .jpg .jpeg .png .bmp .tiff .tif .webp

    Raises:
        FileNotFoundError: If directory missing or contains no images
    """
    if not TEST_IMAGES_DIR.is_dir():
        raise FileNotFoundError(
            f"Test image directory not found: {TEST_IMAGES_DIR}\n"
            f"Create it and add test images:\n"
            f"  mkdir -p {TEST_IMAGES_DIR}\n"
            f"  cp your-photo.jpg {TEST_IMAGES_DIR}/"
        )
    images = sorted(
        p for p in TEST_IMAGES_DIR.iterdir()
        if p.suffix.lower() in IMAGE_EXTENSIONS and p.is_file()
        and not p.name.startswith(".")
    )
    if not images:
        raise FileNotFoundError(
            f"No image files in {TEST_IMAGES_DIR}\n"
            f"Supported formats: {', '.join(sorted(IMAGE_EXTENSIONS))}"
        )
    return [str(p) for p in images]


def setup_test_output(test_name: str) -> str:
    """
    Create a clean output directory for a test run.
    Returns the path (also sets PYWAGGLE_LOG_DIR env var).
    """
    out = OUTPUT_DIR / test_name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    os.environ["PYWAGGLE_LOG_DIR"] = str(out)
    return str(out)


def parse_output(output_dir: str) -> dict:
    """
    Parse pywaggle local output directory.

    Returns:
        {
            "measurements": [{"timestamp":..., "name":..., "value":..., "meta":...}, ...],
            "uploads": ["path1.jpg", ...],
            "raw_lines": ["line1", "line2", ...],
        }
    """
    result = {"measurements": [], "uploads": [], "raw_lines": []}

    data_file = os.path.join(output_dir, "data.ndjson")
    if os.path.exists(data_file):
        with open(data_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    result["raw_lines"].append(line)
                    try:
                        result["measurements"].append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    uploads_dir = os.path.join(output_dir, "uploads")
    if os.path.exists(uploads_dir):
        result["uploads"] = sorted(
            str(p) for p in Path(uploads_dir).iterdir()
            if p.is_file()
        )

    return result


def print_report(results: dict, plugin_name: str):
    """Print a human-readable test report."""
    print(f"\n{'='*60}")
    print(f"  TEST REPORT: {plugin_name}")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    measurements = results["measurements"]
    uploads = results["uploads"]

    print(f"\n  Measurements published: {len(measurements)}")
    if measurements:
        # Group by name
        by_name = {}
        for m in measurements:
            name = m.get("name", "unknown")
            by_name.setdefault(name, []).append(m)

        print(f"  Unique topics: {len(by_name)}")
        print()
        for name, items in sorted(by_name.items()):
            for item in items:
                val = item.get("value", "?")
                meta = item.get("meta", {})
                ts = item.get("timestamp", "?")
                # Truncate long values
                val_str = str(val)
                if len(val_str) > 80:
                    val_str = val_str[:77] + "..."
                print(f"    {name}: {val_str}")
                if meta:
                    meta_str = json.dumps(meta)
                    if len(meta_str) > 70:
                        meta_str = meta_str[:67] + "..."
                    print(f"      meta: {meta_str}")

    print(f"\n  Files uploaded: {len(uploads)}")
    for u in uploads:
        size = os.path.getsize(u) if os.path.exists(u) else 0
        print(f"    {os.path.basename(u)} ({size:,} bytes)")

    print(f"\n{'='*60}\n")

    return len(measurements) > 0


def print_summary(test_results: dict[str, bool]):
    """Print final pass/fail summary across all tests."""
    print(f"\n{'#'*60}")
    print(f"  FINAL SUMMARY")
    print(f"{'#'*60}")
    total = len(test_results)
    passed = sum(1 for v in test_results.values() if v)
    failed = total - passed
    for name, ok in test_results.items():
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
    print()
    print(f"  {passed}/{total} passed, {failed} failed")
    print(f"{'#'*60}\n")
    return failed == 0
