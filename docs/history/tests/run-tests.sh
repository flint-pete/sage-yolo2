#!/usr/bin/env bash
# run-tests.sh — Run YOLO Object Counter local test (GPU required)
#
# Usage:
#   cd plugins/yolo-object-counter
#   ./tests/run-tests.sh
#
# This script activates the shared venv at tests/.venv (project root)
# and runs the test for this plugin.

set -euo pipefail
TESTS_DIR="$(cd "$(dirname "$0")" && pwd)"
PLUGIN_DIR="$(dirname "$TESTS_DIR")"
PROJECT_DIR="$(dirname "$(dirname "$PLUGIN_DIR")")"

# Activate shared venv
VENV="$PROJECT_DIR/tests/.venv"
if [ ! -d "$VENV" ]; then
    echo "ERROR: Virtual environment not found at $VENV"
    echo "Create it from the project root:"
    echo "  python3 -m venv tests/.venv"
    echo "  tests/.venv/bin/pip install pywaggle numpy opencv-python-headless Pillow"
    exit 1
fi
source "$VENV/bin/activate"

# Verify test images exist
if [ ! -d "$TESTS_DIR/test-images" ] || [ -z "$(ls -A "$TESTS_DIR/test-images/" 2>/dev/null)" ]; then
    echo "ERROR: No test images found in $TESTS_DIR/test-images/"
    echo "Add test images (JPG/PNG) to that directory."
    exit 1
fi

echo "=============================================="
echo "  YOLO Object Counter — Local Test"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================================="
python3 "$TESTS_DIR/test_yolo_local.py"
