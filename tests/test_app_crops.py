#!/usr/bin/env python3
"""Stage 2 unit tests for app.py crop production (_maybe_produce_crops + geometry).

app.py imports the heavy stack (cv2, torch, ultralytics, waggle) at module top;
we inject light stubs before import (same pattern as test_app_cache.py). cv2 here
is stubbed so imencode returns REAL JPEG bytes (via PIL) and imdecode/slicing use
a REAL numpy frame -- so crop_writer.embed_all gets valid input and the crops it
writes are byte-real, readable back by consumer.read_frame_metadata.

Covers: pad/clamp geometry, --crop-min-px floor, multi-detection -> N crops,
--crop-match filtering, off-by-default no-op, env.crop.count publish, and the
end-to-end crop-readable-by-consumer compat proof.
"""
import io
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytest.importorskip("piexif")
import numpy as np  # noqa: E402  (real; installed in the test venv)
from PIL import Image  # noqa: E402


def _install_stubs():
    # cv2 may already be stubbed by an earlier test module in the same session
    # (sys.modules is shared). Reuse the existing module object if present and just
    # ensure OUR crop path's needs (imencode returning real JPEG bytes) are set,
    # so import order between test files can't leave imencode missing.
    cv2 = sys.modules.get("cv2") or types.ModuleType("cv2")

    def _imencode(ext, img):
        # img is a real numpy HxWx3 array -> encode to real JPEG bytes via PIL.
        pil = Image.fromarray(img[:, :, ::-1])  # BGR->RGB for a valid-looking JPEG
        b = io.BytesIO()
        pil.save(b, "JPEG")
        return True, np.frombuffer(b.getvalue(), dtype=np.uint8)

    cv2.imencode = _imencode
    # NOTE: keep imread compatible with test_app_cache.py (which expects a truthy
    # "IMG" for existing paths) so that whichever test module imports `app` first
    # -- fixing the cv2 object app binds -- both files still work. sys.modules is
    # shared and app binds cv2 once, so these stubs must agree across test files.
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
    ultra = types.ModuleType("ultralytics")
    ultra.YOLO = object
    sys.modules["ultralytics"] = ultra
    waggle = types.ModuleType("waggle")
    wplugin = types.ModuleType("waggle.plugin"); wplugin.Plugin = object
    wdata = types.ModuleType("waggle.data")
    wvision = types.ModuleType("waggle.data.vision"); wvision.Camera = object
    sys.modules.update({"waggle": waggle, "waggle.plugin": wplugin,
                        "waggle.data": wdata, "waggle.data.vision": wvision})


_install_stubs()
import app          # noqa: E402
import crop_writer  # noqa: E402
import consumer     # noqa: E402


class _Timeit:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakePlugin:
    def __init__(self):
        self.published = []
    def timeit(self, name): return _Timeit()
    def publish(self, topic, value, timestamp=None, meta=None):
        self.published.append((topic, value, timestamp, dict(meta or {})))


class Args:
    model = "yolo11x.pt"
    crop_padding = 0.10
    crop_min_px = 16
    crop_cache_name = ""
    crop_max_count = 500
    crop_max_mb = 500.0

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _frame(w=200, h=200):
    return np.full((h, w, 3), 128, dtype=np.uint8)


# --- geometry: pad + clamp ---------------------------------------------------

def test_pad_clamp_basic():
    # box 50..100 in a 200px image, 10% padding -> pad 5px each side
    assert app._pad_clamp_bbox([50, 50, 100, 100], 200, 200, 0.10) == (45, 45, 105, 105)


def test_pad_clamp_edges_clamped():
    # box touching top-left; padding must clamp to 0, not go negative
    out = app._pad_clamp_bbox([0, 0, 40, 40], 200, 200, 0.5)
    assert out[0] == 0 and out[1] == 0
    assert out[2] <= 200 and out[3] <= 200


def test_pad_clamp_degenerate_none():
    assert app._pad_clamp_bbox([10, 10, 10, 10], 200, 200, 0.0) is None


# --- off by default ----------------------------------------------------------

def test_no_crop_rules_is_noop():
    p = FakePlugin()
    dets = [{"class": "bird", "confidence": 0.9, "bbox": [10, 10, 90, 90]}]
    n = app._maybe_produce_crops(p, Args(), dets, _frame(), timestamp=1000,
                                 camera="top", crop_rules=[])
    assert n == 0
    assert p.published == []          # nothing published when OFF


# --- multi-detection -> N crops, readable by consumer ------------------------

def test_two_birds_two_crops(tmp_path, monkeypatch):
    monkeypatch.setattr(consumer, "resolve_cache_root", lambda explicit=None: str(tmp_path))
    monkeypatch.setenv("WAGGLE_JOB_NAME", "hummingcam")
    p = FakePlugin()
    dets = [
        {"class": "bird", "confidence": 0.9, "bbox": [10, 10, 90, 90]},
        {"class": "bird", "confidence": 0.8, "bbox": [110, 110, 190, 190]},
    ]
    from save_match import parse_save_match
    crop_rules = parse_save_match("bird:0.5")
    n = app._maybe_produce_crops(p, Args(), dets, _frame(), timestamp=1700000000000000000,
                                 camera="top", crop_rules=crop_rules, source_uid="parentsha")
    assert n == 2
    # env.crop.count published once, value 2, frame-anchored
    crop_counts = [x for x in p.published if x[0] == "env.crop.count"]
    assert len(crop_counts) == 1 and crop_counts[0][1] == 2
    assert crop_counts[0][2] == 1700000000000000000
    # two crop streams exist, each with one readable v2 crop
    for idx in (0, 1):
        sdir = os.path.join(str(tmp_path), "hummingcam-crops", "top-crop-%d" % idx)
        frame = consumer.newest_frame(sdir)
        assert frame is not None
        m = consumer.read_frame_metadata(frame)
        assert m.capture_ts_ns == 1700000000000000000
        # provenance survives
        payload, _ = crop_writer.read_back_fields(open(frame.path, "rb").read())
        assert payload["source"]["detection_index"] == idx
        assert payload["source"]["source_class"] == "bird"
        assert payload["source"]["source_unique_id"] == "parentsha"


# --- --crop-match filtering: only matching class/conf gets cropped -----------

def test_crop_match_filters(tmp_path, monkeypatch):
    monkeypatch.setattr(consumer, "resolve_cache_root", lambda explicit=None: str(tmp_path))
    from save_match import parse_save_match
    p = FakePlugin()
    dets = [
        {"class": "bird", "confidence": 0.9, "bbox": [10, 10, 90, 90]},   # crop
        {"class": "person", "confidence": 0.9, "bbox": [110, 10, 190, 90]},  # skip
        {"class": "bird", "confidence": 0.3, "bbox": [10, 110, 90, 190]},  # below conf -> skip
    ]
    n = app._maybe_produce_crops(p, Args(), dets, _frame(), timestamp=1700000000000000000,
                                 camera="top", crop_rules=parse_save_match("bird:0.5"))
    assert n == 1                    # only the 0.9 bird


# --- --crop-min-px floor: tiny box skipped ----------------------------------

def test_min_px_floor_skips_tiny(tmp_path, monkeypatch):
    monkeypatch.setattr(consumer, "resolve_cache_root", lambda explicit=None: str(tmp_path))
    from save_match import parse_save_match
    p = FakePlugin()
    # a 10px box with min-px=16 -> skipped even after padding
    dets = [{"class": "bird", "confidence": 0.9, "bbox": [100, 100, 110, 110]}]
    n = app._maybe_produce_crops(p, Args(crop_min_px=32, crop_padding=0.0), dets,
                                 _frame(), timestamp=1700000000000000000,
                                 camera="top", crop_rules=parse_save_match("bird:0.5"))
    assert n == 0
