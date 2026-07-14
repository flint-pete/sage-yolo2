#!/usr/bin/env python3
"""Stage 1 unit tests for crop_writer.py (crop-producer v2.1.0).

Proves the VENDORED v2 write side:
  1. v2-name build round-trips through the ts-prefix parser.
  2. embed_all() produces a self-describing JPEG whose fields + unique_id read
     back, AND that sage-yolo2's OWN reader (consumer.read_frame_metadata) can
     consume it -- i.e. a crop is byte-compatible with what BioCLIP consumes.
  3. source_* provenance survives the round-trip.
  4. ring eviction by count, by MB, and the E3 oversized-drop guard.
  5. write_frame() atomic publish + eviction end-to-end on a tmp dir.

Requires Pillow + piexif (installed by the Makefile test venv).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import crop_writer  # noqa: E402
import consumer     # noqa: E402

PIL = pytest.importorskip("PIL")
pytest.importorskip("piexif")
from PIL import Image  # noqa: E402
import io as _io      # noqa: E402


def _jpeg_bytes(w=32, h=32, color=(10, 20, 30)):
    b = _io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "JPEG")
    return b.getvalue()


# --- 1. v2 name round-trip --------------------------------------------------

def test_v2_name_roundtrip():
    name = crop_writer.build_v2_name(1700000000000000000, "H00F", "top-crop-0")
    assert name == "1700000000000000000-v2-H00F-top-crop-0.jpg"
    assert crop_writer._parse_ts_prefix(name) == 1700000000000000000


def test_v2_name_rejects_bad_input():
    with pytest.raises(ValueError):
        crop_writer.build_v2_name(0, "H00F", "top")
    with pytest.raises(ValueError):
        crop_writer.build_v2_name(1, "H00F", "bad/camera")


def test_parse_ts_prefix_ignores_non_v2():
    assert crop_writer._parse_ts_prefix("not-a-frame.jpg") is None
    assert crop_writer._parse_ts_prefix("123-v2-H00F-top.tmp") is None


# --- 2. embed round-trips through our reader AND consumer.py -----------------

def test_embed_reads_back():
    final, uid = crop_writer.embed_all(
        _jpeg_bytes(), vsn="H00F", node_id="00004cbb", job="hummingcam",
        task="sage-yolo2", plugin="reg/sage-yolo2:2.1.0", camera="top-crop-0",
        capture_ts_ns=1700000000000000000, upload_ts_ns=None,
        lat=None, lon=None, acquisition_path="opencv-reencoded")
    payload, img_uid = crop_writer.read_back_fields(final)
    assert img_uid == uid
    assert payload["unique_id"] == uid
    assert payload["vsn"] == "H00F"
    assert payload["capture_timestamp_ns"] == 1700000000000000000
    assert payload["schema_version"] == "sage-img-1"


def test_crop_readable_by_consumer(tmp_path):
    """The decisive compat test: a crop written by crop_writer must be readable
    by sage-yolo2's own consumer.read_frame_metadata (== what BioCLIP uses)."""
    ts = 1700000000000000000
    final, uid = crop_writer.embed_all(
        _jpeg_bytes(), vsn="H00F", node_id="00004cbb", job="hummingcam",
        task="sage-yolo2", plugin="reg/sage-yolo2:2.1.0", camera="top-crop-0",
        capture_ts_ns=ts, upload_ts_ns=None, lat=41.88, lon=-87.63,
        acquisition_path="opencv-reencoded")
    name = crop_writer.build_v2_name(ts, "H00F", "top-crop-0")
    path = str(tmp_path / name)
    with open(path, "wb") as f:
        f.write(final)
    frame = consumer.newest_frame(str(tmp_path))
    m = consumer.read_frame_metadata(frame)
    assert m.capture_ts_ns == ts
    assert m.unique_id == uid
    assert m.vsn == "H00F"
    assert m.has_location
    assert m.lat == pytest.approx(41.88)
    assert m.lon == pytest.approx(-87.63)


# --- 3. source_* provenance survives ----------------------------------------

def test_source_provenance_roundtrip():
    source = {"source_class": "bird", "source_confidence": 0.8867,
              "source_bbox": [10, 20, 60, 80], "source_unique_id": "parentsha",
              "detection_index": 2}
    final, _ = crop_writer.embed_all(
        _jpeg_bytes(), vsn="H00F", node_id="n", job="j", task="t",
        plugin="p", camera="top-crop-2", capture_ts_ns=1700000000000000000,
        upload_ts_ns=None, lat=None, lon=None,
        acquisition_path="opencv-reencoded", source=source)
    payload, _ = crop_writer.read_back_fields(final)
    assert payload["source"]["source_class"] == "bird"
    assert payload["source"]["detection_index"] == 2
    assert payload["source"]["source_bbox"] == [10, 20, 60, 80]


# --- 4. eviction planner (pure) ---------------------------------------------

class _Ring:
    def __init__(self, members):
        self.members = members
        self.count = len(members)
        self.total_bytes = sum(m.size for m in members)


def _m(ts, size):
    return crop_writer.RingMember("p%d" % ts, "%d-v2-H00F-c.jpg" % ts, ts, size)


def test_evict_by_count():
    ring = _Ring([_m(1, 100), _m(2, 100), _m(3, 100)])
    plan = crop_writer.plan_evictions(ring, 100, max_count=3, max_mb=None)
    assert not plan.drop_new
    assert [v.capture_ts_ns for v in plan.evict] == [1]   # oldest evicted to fit


def test_evict_by_mb():
    ring = _Ring([_m(1, 400_000), _m(2, 400_000)])   # 0.8 MB used
    plan = crop_writer.plan_evictions(ring, 400_000, max_count=None, max_mb=1.0)
    assert not plan.drop_new
    assert [v.capture_ts_ns for v in plan.evict] == [1]   # evict one to fit under 1MB


def test_e3_oversized_drop():
    ring = _Ring([])
    plan = crop_writer.plan_evictions(ring, 2_000_000, max_count=None, max_mb=1.0)
    assert plan.drop_new is True
    assert plan.evict == []


def test_no_caps_never_evicts():
    ring = _Ring([_m(1, 100), _m(2, 100)])
    plan = crop_writer.plan_evictions(ring, 100, max_count=None, max_mb=None)
    assert not plan.drop_new and plan.evict == []


# --- 5. write_frame end-to-end on a tmp ring --------------------------------

def test_write_frame_publishes_and_evicts(tmp_path):
    sdir = crop_writer.stream_dir(str(tmp_path), "hummingcam-crops", "top-crop-0")
    # write 3 frames with cap=2 -> the oldest must be evicted, 2 remain.
    written = []
    for i, ts in enumerate((1000, 2000, 3000)):
        final, _ = crop_writer.embed_all(
            _jpeg_bytes(), vsn="H00F", node_id="n", job="j", task="t",
            plugin="p", camera="top-crop-0", capture_ts_ns=ts,
            upload_ts_ns=None, lat=None, lon=None,
            acquisition_path="opencv-reencoded")
        name = crop_writer.build_v2_name(ts, "H00F", "top-crop-0")
        res = crop_writer.write_frame(final, sdir, name, max_count=2, max_mb=None)
        assert res.written
        written.append(name)
    ring = crop_writer.scan_ring(sdir)
    assert ring.count == 2
    remaining_ts = sorted(m.capture_ts_ns for m in ring.members)
    assert remaining_ts == [2000, 3000]        # oldest (1000) evicted
    assert not any(f.endswith(".tmp") for f in os.listdir(sdir))   # no torn files


def test_write_frame_e3_drop_writes_nothing(tmp_path):
    sdir = crop_writer.stream_dir(str(tmp_path), "hummingcam-crops", "top-crop-0")
    final, _ = crop_writer.embed_all(
        _jpeg_bytes(200, 200), vsn="H00F", node_id="n", job="j", task="t",
        plugin="p", camera="top-crop-0", capture_ts_ns=1000,
        upload_ts_ns=None, lat=None, lon=None, acquisition_path="opencv-reencoded")
    # cap the ring MB below the frame size -> E3 drop, nothing written.
    res = crop_writer.write_frame(final, sdir, "1000-v2-H00F-top-crop-0.jpg",
                                  max_count=None, max_mb=0.0001)
    assert not res.written
    assert crop_writer.scan_ring(sdir).count == 0
