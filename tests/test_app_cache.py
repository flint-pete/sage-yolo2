#!/usr/bin/env python3
"""Integration test for app.py's cache wake loop (Stage 6).

app.py imports the heavy runtime stack (cv2, numpy, torch, ultralytics, waggle) at
module top. To exercise the wiring offline we inject lightweight STUBS into
sys.modules before importing app, then drive _process_cache_wake with real cache
files + a fake plugin/detector. This proves consumer -> selection -> seenstore ->
identity -> publish -> mark are wired correctly, without a GPU.
"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

piexif = pytest.importorskip("piexif")
from PIL import Image  # noqa: E402


# --- inject stubs for the heavy imports BEFORE importing app -----------------

def _install_stubs():
    # cv2: imread returns a tiny fake "image" (any truthy object); imwrite no-ops.
    cv2 = types.ModuleType("cv2")
    cv2.imread = lambda path: ("IMG" if os.path.exists(path) else None)
    cv2.imwrite = lambda path, img: True
    cv2.rectangle = lambda *a, **k: None
    cv2.putText = lambda *a, **k: None
    cv2.FONT_HERSHEY_SIMPLEX = 0
    cv2.imdecode = lambda *a, **k: "IMG"
    cv2.IMREAD_COLOR = 1
    sys.modules["cv2"] = cv2

    # numpy is REAL (installed in the test venv) -- do NOT stub it, or pytest.approx
    # (which probes numpy) breaks across the whole session. app.py only needs it
    # importable; the cache path never calls into numpy.

    torch = types.ModuleType("torch")
    torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    sys.modules["torch"] = torch

    ultra = types.ModuleType("ultralytics")
    ultra.YOLO = object
    sys.modules["ultralytics"] = ultra

    # waggle.plugin.Plugin + waggle.data.vision.Camera
    waggle = types.ModuleType("waggle")
    wplugin = types.ModuleType("waggle.plugin")
    wplugin.Plugin = object
    wdata = types.ModuleType("waggle.data")
    wvision = types.ModuleType("waggle.data.vision")
    wvision.Camera = object
    sys.modules["waggle"] = waggle
    sys.modules["waggle.plugin"] = wplugin
    sys.modules["waggle.data"] = wdata
    sys.modules["waggle.data.vision"] = wvision


_install_stubs()
import app  # noqa: E402
import consumer  # noqa: E402
import seenstore  # noqa: E402


# --- fakes -------------------------------------------------------------------

class _Timeit:
    def __enter__(self): return self
    def __exit__(self, *a): return False


class FakePlugin:
    def __init__(self):
        self.published = []
        self.uploads = []
    def timeit(self, name): return _Timeit()
    def publish(self, topic, value, timestamp=None, meta=None):
        self.published.append((topic, value, timestamp, dict(meta or {})))
    def upload_file(self, path, timestamp=None, meta=None):
        self.uploads.append((path, timestamp, dict(meta or {})))


class FakeDetector:
    """Returns one 'bird' detection for every frame."""
    def detect(self, img, target_classes):
        return [{"class": "bird", "confidence": 0.9, "bbox": [0, 0, 1, 1]}]


class Args:
    source = "cache"
    model = "yolo11x.pt"
    all_unseen = True
    max_frames = 0
    reprocess = False
    upload_image = "N"
    save_match = ""


def _write_frame(path, ts, vsn="W123", camera="top", uid="uid", lat=None, lon=None):
    Image.new("RGB", (8, 8), (1, 2, 3)).save(path, "JPEG")
    fields = {"schema_version": "sage-img-1", "vsn": vsn, "node_id": "nid",
              "camera": camera, "capture_timestamp_ns": ts, "unique_id": uid,
              "lat": lat, "lon": lon, "acquisition_path": "native-raw"}
    uc = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    exif = {piexif.ExifIFD.ImageUniqueID: uid,
            piexif.ExifIFD.UserComment: b"ASCII\x00\x00\x00" + uc.encode("ascii")}
    piexif.insert(piexif.dump({"0th": {}, "Exif": exif, "GPS": {},
                               "1st": {}, "thumbnail": None}), path)


# --- the wake-loop integration ----------------------------------------------

def test_cache_wake_publishes_frame_anchored_and_marks_seen(tmp_path):
    cam_dir = tmp_path / "hummingcam" / "top"
    cam_dir.mkdir(parents=True)
    _write_frame(str(cam_dir / "100-v2-W123-top.jpg"), 100, uid="uidA", lat=41.0, lon=-87.0)
    _write_frame(str(cam_dir / "200-v2-W123-top.jpg"), 200, uid="uidB")

    seen = seenstore.SeenStore(str(tmp_path / "seen"))
    plugin, det, args = FakePlugin(), FakeDetector(), Args()
    args.input = str(cam_dir)

    app._process_cache_wake(plugin, det, args, ["bird"], [], seen,
                            last_wake_ts_ns=0, select_every_s=0)

    # both frames processed (all_unseen)
    totals = [p for p in plugin.published if p[0] == "env.count.total"]
    assert len(totals) == 2
    # observation_ts == capture_ts (frame-anchored), NOT now()
    ts_published = sorted(p[2] for p in totals)
    assert ts_published == [100, 200]
    # frame A carried GPS -> published with signed lat/lon; frame B had none -> omitted
    a = next(p for p in plugin.published if p[0] == "env.count.total" and p[2] == 100)
    b = next(p for p in plugin.published if p[0] == "env.count.total" and p[2] == 200)
    assert a[3].get("lat") == "41.0" and a[3].get("lon") == "-87.0"
    assert "lat" not in b[3]                    # never fabricated
    assert a[3].get("vsn") == "W123"
    # both marked seen
    assert seen.is_seen("uidA") and seen.is_seen("uidB")


def test_cache_wake_dedups_on_second_pass(tmp_path):
    cam_dir = tmp_path / "cn" / "cam"
    cam_dir.mkdir(parents=True)
    _write_frame(str(cam_dir / "100-v2-W1-cam.jpg"), 100, uid="only")
    seen = seenstore.SeenStore(str(tmp_path / "seen"))
    plugin, det, args = FakePlugin(), FakeDetector(), Args()
    args.input = str(cam_dir)

    app._process_cache_wake(plugin, det, args, None, [], seen, 0, 0)
    app._process_cache_wake(plugin, det, args, None, [], seen, 0, 0)  # again

    totals = [p for p in plugin.published if p[0] == "env.count.total"]
    assert len(totals) == 1                     # second pass deduped -> 0 new


# --- consumer-id resolution --------------------------------------------------

def test_consumer_id_prefers_job_task(monkeypatch):
    monkeypatch.setenv("WAGGLE_JOB_NAME", "human")
    monkeypatch.setenv("WAGGLE_TASK_NAME", "yolo")
    assert app.resolve_consumer_id() == "human-yolo"


def test_consumer_id_override_wins(monkeypatch):
    monkeypatch.setenv("WAGGLE_JOB_NAME", "human")
    assert app.resolve_consumer_id("explicit") == "explicit"


def test_consumer_id_appid_fallback_warns(monkeypatch, caplog):
    for k in ("WAGGLE_JOB_NAME", "WAGGLE_TASK_NAME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("WAGGLE_APP_ID", "pod-uid-xyz")
    with caplog.at_level("WARNING"):
        cid = app.resolve_consumer_id()
    assert cid == "pod-uid-xyz"
    assert any("APP_ID" in r.message for r in caplog.records)


def test_parse_cache_input_splits_stream():
    cn, cam = app.parse_cache_input("/local-cache/hummingcam/top", "/local-cache")
    assert (cn, cam) == ("hummingcam", "top")
