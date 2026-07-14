#!/usr/bin/env python3
"""Stage 4 offline end-to-end: detect -> crop-produce -> downstream-consume.

Proves the WHOLE crop cascade offline (no GPU, no live camera): a 2-bird frame
is run through the REAL crop path (_maybe_produce_crops), and then this test acts
as the DOWNSTREAM CONSUMER -- the role BioCLIP plays -- scanning each crop stream
and reading each crop back with the SAME consumer API (scan_frames +
read_frame_metadata) that any v2-contract consumer uses. Asserts:

  * exactly 2 crops produced, one per bird, in distinct <cam>-crop-<idx> streams;
  * each crop is a valid, readable v2 frame (capture_ts inherited from parent);
  * each crop's PIXELS match the source region (crop geometry is correct);
  * source_* provenance (class/confidence/bbox/unique_id, detection_index) is
    intact and traces each crop back to its parent detection;
  * env.crop.count is published once, value 2, frame-anchored.

This is the offline gate before the on-node H00F e2e (Stage 5).
"""
import io
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("piexif")
import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402


def _install_stubs():
    cv2 = sys.modules.get("cv2") or types.ModuleType("cv2")

    def _imencode(ext, img):
        pil = Image.fromarray(img[:, :, ::-1])      # BGR->RGB, real JPEG bytes
        b = io.BytesIO()
        pil.save(b, "JPEG")
        return True, np.frombuffer(b.getvalue(), dtype=np.uint8)

    cv2.imencode = _imencode
    for attr, val in (("imread", lambda p: ("IMG" if os.path.exists(p) else None)),
                      ("imwrite", lambda p, i: True),
                      ("rectangle", lambda *a, **k: None),
                      ("putText", lambda *a, **k: None),
                      ("getTextSize", lambda *a, **k: ((0, 0), 0)),
                      ("FONT_HERSHEY_SIMPLEX", 0)):
        setattr(cv2, attr, val)
    sys.modules["cv2"] = cv2
    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    sys.modules["torch"] = torch
    ultra = types.ModuleType("ultralytics"); ultra.YOLO = object
    sys.modules["ultralytics"] = ultra
    waggle = types.ModuleType("waggle")
    wplugin = types.ModuleType("waggle.plugin"); wplugin.Plugin = object
    wdata = types.ModuleType("waggle.data")
    wvision = types.ModuleType("waggle.data.vision"); wvision.Camera = object
    sys.modules.update({"waggle": waggle, "waggle.plugin": wplugin,
                        "waggle.data": wdata, "waggle.data.vision": wvision})


_install_stubs()
import app          # noqa: E402
import consumer     # noqa: E402
import crop_writer  # noqa: E402


class _Timeit:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakePlugin:
    def __init__(self):
        self.published = []
    def timeit(self, name): return _Timeit()
    def publish(self, topic, value, timestamp=None, meta=None):
        self.published.append((topic, value, timestamp, dict(meta or {})))


class FakeDetector:
    """Two birds at KNOWN, distinctly-colored regions of the frame."""
    def detect(self, img, target_classes):
        return [
            {"class": "bird", "confidence": 0.91, "bbox": [20, 20, 120, 120]},
            {"class": "bird", "confidence": 0.77, "bbox": [260, 160, 360, 300]},
        ]


class Args:
    model = "yolo11x.pt"
    crop_padding = 0.0          # exact geometry so pixel check is deterministic
    crop_min_px = 8
    crop_cache_name = ""
    crop_max_count = 500
    crop_max_mb = 500.0


def _two_bird_frame():
    """400x400 BGR frame; bird-1 region red, bird-2 region blue, bg gray."""
    img = np.full((400, 400, 3), 100, dtype=np.uint8)
    img[20:120, 20:120] = (0, 0, 200)      # BGR red block  (bird 0)
    img[160:300, 260:360] = (200, 0, 0)    # BGR blue block (bird 1)
    return img


def test_offline_e2e_detect_crop_consume(tmp_path, monkeypatch):
    monkeypatch.setattr(consumer, "resolve_cache_root", lambda explicit=None: str(tmp_path))
    monkeypatch.setenv("WAGGLE_JOB_NAME", "hummingcam")
    monkeypatch.setenv("WAGGLE_PLUGIN_NAME", "registry.sagecontinuum.org/beckman/sage-yolo2")
    monkeypatch.setenv("WAGGLE_PLUGIN_VERSION", "2.1.0")

    from save_match import parse_save_match
    plugin = FakePlugin()
    detector = FakeDetector()
    frame = _two_bird_frame()
    parent_ts = 1784050107392537595          # a real-looking capture ts (ns)
    parent_uid = "parentframesha256"

    # --- PRODUCER SIDE: run the real crop path -----------------------------
    detections = detector.detect(frame, ["bird"])
    n = app._maybe_produce_crops(
        plugin, Args(), detections, frame, timestamp=parent_ts, camera="top",
        crop_rules=parse_save_match("bird:0.5"), source_uid=parent_uid)
    assert n == 2, "expected 2 crops produced"

    # env.crop.count published once, value 2, frame-anchored to the parent capture
    counts = [x for x in plugin.published if x[0] == "env.crop.count"]
    assert len(counts) == 1
    assert counts[0][1] == 2
    assert counts[0][2] == parent_ts

    # --- CONSUMER SIDE (BioCLIP's role): scan + read each crop stream -------
    expected = {
        0: {"class": "bird", "conf": 0.91, "bbox": [20, 20, 120, 120],
            "color": (200, 0, 0)},          # RGB red (crop_writer wrote RGB via PIL)
        1: {"class": "bird", "conf": 0.77, "bbox": [260, 160, 360, 300],
            "color": (0, 0, 200)},          # RGB blue
    }
    for idx, exp in expected.items():
        sdir = os.path.join(str(tmp_path), "hummingcam-crops", "top-crop-%d" % idx)
        frames = consumer.scan_frames(sdir)
        assert len(frames) == 1, "one crop per stream (idx=%d)" % idx
        m = consumer.read_frame_metadata(frames[0])

        # frame-anchored to the PARENT capture instant (species stays traceable)
        assert m.capture_ts_ns == parent_ts
        assert m.vsn == "unknown"           # no node identity in this offline path

        # provenance blob (read directly; consumer surfaces base fields, we want source)
        payload, img_uid = crop_writer.read_back_fields(open(frames[0].path, "rb").read())
        src = payload["source"]
        assert src["source_class"] == exp["class"]
        assert src["source_confidence"] == pytest.approx(exp["conf"])
        assert src["source_bbox"] == exp["bbox"]
        assert src["source_unique_id"] == parent_uid
        assert src["detection_index"] == idx
        assert payload["unique_id"] == img_uid == m.unique_id

        # PIXELS: the crop must be the right region (geometry correct).
        crop = np.array(Image.open(frames[0].path).convert("RGB"))
        bx1, by1, bx2, by2 = exp["bbox"]
        assert crop.shape[0] == (by2 - by1) and crop.shape[1] == (bx2 - bx1)
        center = crop[crop.shape[0] // 2, crop.shape[1] // 2]
        # JPEG is lossy -> allow tolerance; dominant channel must match expected.
        assert np.argmax(center) == np.argmax(exp["color"]), \
            "crop %d dominant color mismatch: got %s" % (idx, center)


def test_offline_e2e_off_by_default_writes_nothing(tmp_path, monkeypatch):
    """The safety invariant: no --crop-match => the cache stays empty."""
    monkeypatch.setattr(consumer, "resolve_cache_root", lambda explicit=None: str(tmp_path))
    plugin = FakePlugin()
    dets = FakeDetector().detect(None, ["bird"])
    n = app._maybe_produce_crops(plugin, Args(), dets, _two_bird_frame(),
                                 timestamp=1784050107392537595, camera="top",
                                 crop_rules=[])
    assert n == 0
    assert not any(x[0] == "env.crop.count" for x in plugin.published)
    assert not os.path.exists(os.path.join(str(tmp_path), "hummingcam-crops"))
