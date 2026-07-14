#!/usr/bin/env python3
"""Unit tests for selection.py (Stage 5: batching & selection semantics)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import selection  # noqa: E402

NS = 1_000_000_000   # 1 second in ns


class _F:
    """Minimal stand-in for consumer.Frame: capture_ts_ns + name (+ optional uid)."""
    def __init__(self, ts_s, name=None, uid=None):
        self.capture_ts_ns = ts_s * NS
        self.name = name or ("f%d" % ts_s)
        if uid is not None:
            self.unique_id = uid


def _frames(*ts_s):
    return [_F(t) for t in ts_s]   # caller passes oldest-first


class _Seen:
    def __init__(self, seen_keys=()):
        self._s = set(seen_keys)
    def is_seen(self, k):
        return k in self._s


# --- parse_duration ----------------------------------------------------------

@pytest.mark.parametrize("text,secs", [
    ("0", 0), ("30", 30), ("30s", 30), ("15m", 900), ("1h", 3600), ("2h", 7200),
])
def test_parse_duration_ok(text, secs):
    assert selection.parse_duration(text) == secs


@pytest.mark.parametrize("bad", ["", "abc", "5x", "m", "-", None])
def test_parse_duration_bad(bad):
    with pytest.raises(ValueError):
        selection.parse_duration(bad)


# --- newest (select-every 0) -------------------------------------------------

def test_newest_single():
    sel = selection.select_frames(_frames(1, 2, 3))
    assert [f.name for f in sel] == ["f3"]


def test_newest_empty_window():
    assert selection.select_frames([]) == []


def test_newest_k_via_max_frames():
    # newest-K (§10) = select-every 0 + max_frames K -> the K NEWEST frames, oldest-first
    sel = selection.select_frames(_frames(1, 2, 3, 4), max_frames=2)
    assert [f.name for f in sel] == ["f3", "f4"]


def test_newest_k_fewer_than_k_available():
    sel = selection.select_frames(_frames(1, 2), max_frames=5)
    assert [f.name for f in sel] == ["f1", "f2"]


# --- stride (select-every D) -------------------------------------------------

def test_stride_picks_one_per_interval():
    # frames at 100,110,120,130,140 s; stride 20s -> pick 100,120,140
    frames = _frames(100, 110, 120, 130, 140)
    sel = selection.select_frames(frames, select_every_ns=20 * NS)
    assert [f.capture_ts_ns // NS for f in sel] == [100, 120, 140]


def test_stride_anchored_to_capture_ts_not_count():
    # uneven spacing: 100,105,125,126,150 ; stride 20s -> 100,125,150
    frames = _frames(100, 105, 125, 126, 150)
    sel = selection.select_frames(frames, select_every_ns=20 * NS)
    assert [f.capture_ts_ns // NS for f in sel] == [100, 125, 150]


def test_stride_always_takes_first():
    frames = _frames(100, 101, 102)
    sel = selection.select_frames(frames, select_every_ns=60 * NS)
    assert [f.capture_ts_ns // NS for f in sel] == [100]


# --- all-unseen backlog ------------------------------------------------------

def test_all_unseen_takes_whole_cache_ignoring_last_wake():
    frames = _frames(1, 2, 3)
    sel = selection.select_frames(frames, all_unseen=True, last_wake_ts_ns=999 * NS)
    assert [f.name for f in sel] == ["f1", "f2", "f3"]     # last_wake ignored


def test_all_unseen_overrides_select_every():
    frames = _frames(0, 10, 20)
    sel = selection.select_frames(frames, all_unseen=True, select_every_ns=15 * NS)
    assert len(sel) == 3                                    # all, not strided


def test_all_unseen_capped_drains_over_wakes():
    frames = _frames(1, 2, 3, 4, 5)
    sel = selection.select_frames(frames, all_unseen=True, max_frames=2)
    assert [f.name for f in sel] == ["f1", "f2"]           # oldest first -> drain


# --- time window (last_wake) -------------------------------------------------

def test_time_window_excludes_old_frames():
    frames = _frames(1, 2, 3, 4)
    sel = selection.select_frames(frames, last_wake_ts_ns=2 * NS)   # only >2
    # newest policy over window {3,4} -> f4
    assert [f.name for f in sel] == ["f4"]


def test_time_window_stride():
    frames = _frames(0, 10, 20, 30, 40)
    sel = selection.select_frames(frames, last_wake_ts_ns=10 * NS,
                                  select_every_ns=10 * NS)
    # window {20,30,40}, stride 10s -> all three
    assert [f.capture_ts_ns // NS for f in sel] == [20, 30, 40]


# --- dedup safety net --------------------------------------------------------

def test_dedup_skips_seen():
    frames = [_F(1, uid="a"), _F(2, uid="b"), _F(3, uid="c")]
    seen = _Seen({"c"})
    sel = selection.select_frames(frames, all_unseen=True, seen=seen,
                                  uid_of=lambda f: f.unique_id)
    assert [f.unique_id for f in sel] == ["a", "b"]        # c skipped


def test_dedup_bypassed_by_reprocess():
    frames = [_F(1, uid="a")]
    seen = _Seen({"a"})
    sel = selection.select_frames(frames, all_unseen=True, seen=seen, reprocess=True,
                                  uid_of=lambda f: f.unique_id)
    assert [f.unique_id for f in sel] == ["a"]             # reprocessed anyway


def test_newest_deduped_yields_nothing_when_seen():
    # consumer faster than producer: newest frame already seen -> zero this wake (§8.6)
    frames = [_F(5, uid="x")]
    seen = _Seen({"x"})
    sel = selection.select_frames(frames, seen=seen, uid_of=lambda f: f.unique_id)
    assert sel == []
