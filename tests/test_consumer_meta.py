#!/usr/bin/env python3
"""Unit tests for consumer.py frame metadata (Stage 2).

Builds REAL v2 JPEGs with a producer-format UserComment (ASCII prefix + compact JSON)
via piexif -- the same mechanism image-sampler2 uses -- so these tests prove the
reader round-trips the producer's embed. Requires Pillow + piexif (installed by the
Makefile test venv).
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import consumer  # noqa: E402

PIL = pytest.importorskip("PIL")
piexif = pytest.importorskip("piexif")
from PIL import Image  # noqa: E402


# --- helpers: write a producer-format v2 JPEG -------------------------------

def _write_v2_jpeg(path, *, capture_ts_ns, vsn="W123", camera="top",
                   unique_id="deadbeef", node_id="000048b02d",
                   lat=None, lon=None, acquisition_path="native-raw",
                   embed_json=True, json_overrides=None):
    """Create a JPEG with the producer's UserComment JSON (and optional GPS EXIF)."""
    Image.new("RGB", (8, 8), (10, 20, 30)).save(path, "JPEG")
    if not embed_json:
        return path
    fields = {
        "schema_version": "sage-img-1", "vsn": vsn, "node_id": node_id,
        "job": "sage", "task": "image-sampler2", "plugin": "reg/is2:0.5.1",
        "camera": camera, "capture_timestamp_ns": capture_ts_ns,
        "upload_timestamp_ns": None, "unique_id": unique_id,
        "object_name": "%d-v2-%s-%s.jpg" % (capture_ts_ns, vsn, camera),
        "lat": lat, "lon": lon, "acquisition_path": acquisition_path,
    }
    if json_overrides:
        fields.update(json_overrides)
    uc = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    exif_ifd = {
        piexif.ExifIFD.ImageUniqueID: str(unique_id),
        piexif.ExifIFD.UserComment: b"ASCII\x00\x00\x00" + uc.encode("ascii"),
    }
    gps_ifd = {}
    if lat is not None and lon is not None:
        gps_ifd = {piexif.GPSIFD.GPSLatitudeRef: "N" if lat >= 0 else "S",
                   piexif.GPSIFD.GPSLongitudeRef: "E" if lon >= 0 else "W"}
    exif_bytes = piexif.dump({"0th": {}, "Exif": exif_ifd, "GPS": gps_ifd,
                              "1st": {}, "thumbnail": None})
    piexif.insert(exif_bytes, path)
    return path


def _frame(tmp_path, ts, **kw):
    name = "%d-v2-%s-%s.jpg" % (ts, kw.get("vsn", "W123"), kw.get("camera", "top"))
    p = str(tmp_path / name)
    _write_v2_jpeg(p, capture_ts_ns=ts, **kw)
    return consumer.newest_frame(str(tmp_path))


# --- happy path: full metadata inherited ------------------------------------

def test_reads_authoritative_fields(tmp_path):
    f = _frame(tmp_path, 1700000000000000000, vsn="W123", camera="top",
               unique_id="abc123", node_id="000048b02d",
               lat=41.88, lon=-87.63, acquisition_path="native-raw")
    m = consumer.read_frame_metadata(f)
    assert m.capture_ts_ns == 1700000000000000000
    assert m.unique_id == "abc123"
    assert m.vsn == "W123"
    assert m.node_id == "000048b02d"
    assert m.camera == "top"
    assert m.acquisition_path == "native-raw"


def test_observation_ts_is_capture_ts_not_now(tmp_path):
    f = _frame(tmp_path, 42_000_000_000)
    assert consumer.read_frame_metadata(f).capture_ts_ns == 42_000_000_000


# --- GPS: signed round-trip + never-fabricate --------------------------------

def test_signed_lat_lon_roundtrip(tmp_path):
    # southern + western hemisphere -> negative floats, from JSON (not EXIF abs+ref)
    f = _frame(tmp_path, 100, lat=-33.8688, lon=-151.2093)
    m = consumer.read_frame_metadata(f)
    assert m.has_location
    assert m.lat == pytest.approx(-33.8688)
    assert m.lon == pytest.approx(-151.2093)


def test_no_gps_is_omitted_never_fabricated(tmp_path):
    f = _frame(tmp_path, 100, lat=None, lon=None)
    m = consumer.read_frame_metadata(f)
    assert m.lat is None and m.lon is None
    assert not m.has_location


def test_partial_gps_is_no_location(tmp_path):
    f = _frame(tmp_path, 100, json_overrides={"lat": 41.0, "lon": None})
    m = consumer.read_frame_metadata(f)
    assert not m.has_location


# --- capture_ts mismatch: warn + prefer filename -----------------------------

def test_ts_mismatch_prefers_filename(tmp_path, caplog):
    # filename ts=100, JSON says 999 -> reader keeps 100 and warns
    f = _frame(tmp_path, 100, json_overrides={"capture_timestamp_ns": 999})
    with caplog.at_level("WARNING"):
        m = consumer.read_frame_metadata(f)
    assert m.capture_ts_ns == 100
    assert any("mismatch" in r.message for r in caplog.records)


def test_ts_match_no_warning(tmp_path, caplog):
    f = _frame(tmp_path, 100)   # JSON capture_timestamp_ns == 100
    with caplog.at_level("WARNING"):
        m = consumer.read_frame_metadata(f)
    assert m.capture_ts_ns == 100
    assert not any("mismatch" in r.message for r in caplog.records)


# --- fail-soft: missing / corrupt UserComment --------------------------------

def test_no_usercomment_falls_back_to_filename(tmp_path):
    # plain JPEG, no embedded JSON -> ts from filename, other fields best-effort
    f = _frame(tmp_path, 555, vsn="W999", camera="bottom", embed_json=False)
    m = consumer.read_frame_metadata(f)
    assert m.capture_ts_ns == 555
    assert m.vsn == "W999"        # filename fallback
    assert m.camera == "bottom"
    assert m.unique_id is None    # only the JSON has this
    assert not m.has_location


def test_corrupt_usercomment_is_fail_soft(tmp_path, caplog):
    p = str(tmp_path / "100-v2-W123-top.jpg")
    Image.new("RGB", (8, 8)).save(p, "JPEG")
    exif_ifd = {piexif.ExifIFD.UserComment: b"ASCII\x00\x00\x00" + b"{not valid json"}
    piexif.insert(piexif.dump({"0th": {}, "Exif": exif_ifd, "GPS": {},
                               "1st": {}, "thumbnail": None}), p)
    f = consumer.newest_frame(str(tmp_path))
    with caplog.at_level("WARNING"):
        m = consumer.read_frame_metadata(f)
    assert m.capture_ts_ns == 100   # still usable
    assert m.unique_id is None
