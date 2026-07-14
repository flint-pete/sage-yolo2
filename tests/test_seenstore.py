#!/usr/bin/env python3
"""Unit tests for seenstore.py (Stage 4: durable dedup memory)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seenstore  # noqa: E402


# --- composite path (V2-Design §8.4) ----------------------------------------

def test_seen_store_path_composition():
    p = seenstore.seen_store_path("/local-cache", "human", "hummingcam", "top")
    assert p == "/local-cache/.state/sage-yolo2/human/hummingcam/top/seen"


def test_seen_store_path_lands_in_reserved_state():
    p = seenstore.seen_store_path("/local-cache", "id", "cn", "cam")
    # must sit under the never-evicted .state carve-out
    assert "/.state/" in p
    assert p.startswith("/local-cache/.state/")


def test_two_consumer_ids_get_distinct_stores():
    a = seenstore.seen_store_path("/lc", "human", "cam0", "top")
    b = seenstore.seen_store_path("/lc", "fast-hummers", "cam0", "top")
    assert a != b


# --- basic dedup -------------------------------------------------------------

def test_unseen_then_seen(tmp_path):
    s = seenstore.SeenStore(str(tmp_path / "seen"))
    assert not s.is_seen("abc")
    s.mark("abc")
    assert s.is_seen("abc")


def test_mark_is_idempotent(tmp_path):
    s = seenstore.SeenStore(str(tmp_path / "seen"))
    s.mark("abc"); s.mark("abc")
    assert len(s) == 1


def test_empty_id_never_seen_never_stored(tmp_path):
    s = seenstore.SeenStore(str(tmp_path / "seen"))
    assert not s.is_seen("")
    s.mark("")
    assert len(s) == 0


# --- persistence across reload (the whole point) -----------------------------

def test_survives_reload(tmp_path):
    path = str(tmp_path / "seen")
    s1 = seenstore.SeenStore(path)
    s1.mark("id1"); s1.mark("id2")
    s2 = seenstore.SeenStore(path)          # fresh instance = new pod
    assert s2.is_seen("id1") and s2.is_seen("id2")
    assert len(s2) == 2


def test_creates_nested_dirs_on_first_mark(tmp_path):
    path = str(tmp_path / ".state" / "sage-yolo2" / "human" / "cn" / "top" / "seen")
    s = seenstore.SeenStore(path)
    s.mark("id1")
    assert os.path.exists(path)


# --- prune horizon -----------------------------------------------------------

def test_prune_keeps_newest(tmp_path):
    s = seenstore.SeenStore(str(tmp_path / "seen"), max_ids=3)
    for i in range(5):
        s.mark("id%d" % i)                    # id0..id4
    assert len(s) == 3
    assert not s.is_seen("id0") and not s.is_seen("id1")
    assert s.is_seen("id2") and s.is_seen("id4")


def test_prune_persists(tmp_path):
    path = str(tmp_path / "seen")
    s = seenstore.SeenStore(path, max_ids=2)
    for i in range(4):
        s.mark("id%d" % i)
    s2 = seenstore.SeenStore(path, max_ids=2)
    assert len(s2) == 2
    assert s2.is_seen("id3") and not s2.is_seen("id0")


# --- --reprocess bypass ------------------------------------------------------

def test_reprocess_reports_unseen_but_records(tmp_path):
    path = str(tmp_path / "seen")
    s = seenstore.SeenStore(path, reprocess=True)
    s.mark("id1")
    assert not s.is_seen("id1")               # bypass: always process
    # ...but it WAS recorded, so a later non-reprocess run dedups correctly
    s2 = seenstore.SeenStore(path, reprocess=False)
    assert s2.is_seen("id1")


# --- fail-soft: corrupt / missing / unwritable -------------------------------

def test_missing_store_is_empty(tmp_path):
    s = seenstore.SeenStore(str(tmp_path / "does-not-exist" / "seen"))
    assert len(s) == 0
    assert not s.is_seen("anything")


def test_blank_lines_ignored(tmp_path):
    path = str(tmp_path / "seen")
    with open(path, "w") as f:
        f.write("id1\n\n  \nid2\n")
    s = seenstore.SeenStore(path)
    assert len(s) == 2
    assert s.is_seen("id1") and s.is_seen("id2")


def test_duplicate_lines_deduped_on_load(tmp_path):
    path = str(tmp_path / "seen")
    with open(path, "w") as f:
        f.write("id1\nid1\nid2\n")
    s = seenstore.SeenStore(path)
    assert len(s) == 2
