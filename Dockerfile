# YOLO Object Counter — Sage/Waggle edge plugin
# Default model: YOLO11x (54.7% mAP COCO, 56.9M params)
# Target: 128GB unified memory ARM64 (DGX Spark / Sage Thor)
#
# Base image: NVIDIA PyTorch 25.08 — CUDA 13.0, PyTorch 2.8, Python 3.12
# Supports Blackwell GPUs (sm_120/sm_121) natively. Requires driver R575+.
# Previous base (24.06-py3) only supported up to sm_90 (Hopper).
FROM nvcr.io/nvidia/pytorch:25.08-py3

WORKDIR /app
COPY requirements.txt .

# CRITICAL: The NVIDIA base image ships PyTorch 2.8 compiled with CUDA 13.0
# and Blackwell GPU support (sm_110 Thor, sm_120/sm_121 DGX Spark).  pip install ultralytics will
# try to pull in its own torch/torchvision from PyPI, which overwrites the
# base image's torch with a generic build that LACKS Blackwell kernels —
# causing "unable to find an engine to execute this computation" at runtime.
#
# Fix: freeze torch + torchvision + torchaudio so pip cannot touch them,
# then install everything else normally.
RUN pip install --no-cache-dir --upgrade pip && \
    TORCH_VER=$(python3 -c "import torch; print(torch.__version__)") && \
    TV_VER=$(python3 -c "import torchvision; print(torchvision.__version__)") && \
    NP_VER=$(python3 -c "import numpy; print(numpy.__version__)") && \
    echo "Freezing base-image stack: torch==${TORCH_VER} torchvision==${TV_VER} numpy==${NP_VER}" && \
    printf "torch==${TORCH_VER}\ntorchvision==${TV_VER}\nnumpy==${NP_VER}\n" > /tmp/constraints.txt && \
    pip install --no-cache-dir -c /tmp/constraints.txt -r requirements.txt

# The NVIDIA base image may ship an opencv compiled against a different numpy.
# Fix: fully remove old opencv (pip uninstall + rm stale files), then
# install a fresh opencv-python-headless matching the current numpy.
RUN pip uninstall -y opencv-python opencv-python-headless 2>/dev/null; \
    rm -rf /usr/local/lib/python3.*/dist-packages/cv2* && \
    pip install --no-cache-dir -c /tmp/constraints.txt opencv-python-headless>=4.8.0

# Pre-download default model weights into the image
# Layer order matters: model weights change rarely, app.py changes often.
# Putting the model download BEFORE COPY app.py means code edits don't
# invalidate the expensive (~130 MB) model download layer.
RUN python3 -c "from ultralytics import YOLO; YOLO('yolo11x.pt')"

COPY save_match.py .
COPY app.py .

ENTRYPOINT ["python3", "/app/app.py"]
