#!/usr/bin/env python3
"""Unit tests for consumer.py (Stage 1: cache read + fail-fast).

Pure-stdlib + pytest, no GPU / cv2 / YOLO. Run with ``make test`` or
``python3 -m pytest -q tests/test_consumer.py``.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import consumer  # noqa: E402


# --- helpers -----------------------------------------------------------------

def _touch(path, content=b"x"):
    with open(path, "wb") as f:
        f.write(content)
    return path


def _v2(ts, vsn="W123", camera="top"):
    return "%d-v2-%s-%s.jpg" % (ts, vsn, camera)


# --- resolve_cache_root ------------------------------------------------------

def test_resolve_default():
    assert consumer.resolve_cache_root() == consumer.LOCAL_CACHE_DIR


def test_resolve_explicit_wins(monkeypatch):
    monkeypatch.setenv(consumer.CACHE_ROOT_ENV, "/from/env")
    assert consumer.resolve_cache_root("/explicit") == "/explicit"


def test_resolve_env_over_default(monkeypatch):
    monkeypatch.setenv(consumer.CACHE_ROOT_ENV, "/from/env")
    assert consumer.resolve_cache_root() == "/from/env"


# --- assert_cache_available (fail-fast) --------------------------------------

def test_assert_present_dir_ok(tmp_path):
    consumer.assert_cache_available(str(tmp_path))   # must not raise


def test_assert_absent_dir_fails(tmp_path):
    missing = str(tmp_path / "nope")
    with pytest.raises(consumer.CacheError):
        consumer.assert_cache_available(missing)


def test_assert_file_not_dir_fails(tmp_path):
    f = _touch(str(tmp_path / "a-file"))
    with pytest.raises(consumer.CacheError):
        consumer.assert_cache_available(f)


def test_assert_empty_dir_is_valid(tmp_path):
    # present-but-empty is NOT an error (nothing to process yet, distinct from absent)
    consumer.assert_cache_available(str(tmp_path))
    assert consumer.newest_frame(str(tmp_path)) is None


# --- parse_v2_name -----------------------------------------------------------

def test_parse_valid():
    assert consumer.parse_v2_name("1700000000000000000-v2-W123-top.jpg") == \
        (1700000000000000000, "W123", "top")


def test_parse_hyphenated_camera():
    # first '-v2-' delimits ts; first '-' in rest splits vsn/camera
    assert consumer.parse_v2_name("42-v2-W123-bottom-left.jpg") == \
        (42, "W123", "bottom-left")


@pytest.mark.parametrize("bad", [
    "notaframe.jpg",
    "1700-v2-.jpg",              # empty rest
    "-v2-W123-top.jpg",         # empty ts
    "abc-v2-W123-top.jpg",      # non-digit ts
    "0-v2-W123-top.jpg",        # ts <= 0
    "1700-v2-W123-top.png",     # wrong ext
    "1700-v2-W123-top.jpg.tmp", # tmp, wrong ext
])
def test_parse_rejects(bad):
    assert consumer.parse_v2_name(bad) is None


# --- scan_frames / newest_frame ---------------------------------------------

def test_newest_picks_largest_ts(tmp_path):
    for ts in (100, 300, 200):
        _touch(str(tmp_path / _v2(ts)))
    n = consumer.newest_frame(str(tmp_path))
    assert n.capture_ts_ns == 300


def test_ordering_is_by_ts_not_mtime(tmp_path):
    # write newest-ts file FIRST (oldest mtime) -> must still be selected as newest
    newest = _touch(str(tmp_path / _v2(999)))
    os.utime(newest, (1, 1))                    # ancient mtime
    _touch(str(tmp_path / _v2(500)))            # newer mtime, older ts
    assert consumer.newest_frame(str(tmp_path)).capture_ts_ns == 999


def test_scan_ignores_tmp_and_non_v2(tmp_path):
    _touch(str(tmp_path / _v2(100)))
    _touch(str(tmp_path / "200-v2-W123-top.jpg.tmp"))  # in-flight
    _touch(str(tmp_path / "README.md"))               # non-v2
    _touch(str(tmp_path / "random.jpg"))              # non-v2 jpg
    frames = consumer.scan_frames(str(tmp_path))
    assert [f.capture_ts_ns for f in frames] == [100]


def test_scan_empty_dir(tmp_path):
    assert consumer.scan_frames(str(tmp_path)) == []


def test_scan_missing_dir_returns_empty(tmp_path):
    # scan is fail-soft on a missing dir (presence is assert_cache_available's job)
    assert consumer.scan_frames(str(tmp_path / "nope")) == []


def test_scan_skips_subdirs(tmp_path):
    os.mkdir(str(tmp_path / _v2(100)))   # a DIR named like a frame -> not a file
    _touch(str(tmp_path / _v2(200)))
    frames = consumer.scan_frames(str(tmp_path))
    assert [f.capture_ts_ns for f in frames] == [200]
