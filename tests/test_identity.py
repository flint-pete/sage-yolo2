#!/usr/bin/env python3
"""Unit tests for node identity + cross-check (Stage 3).

Covers the vendored pywaggle2 reader (node_info.py) sentinel normalization and
consumer.resolve_identity() -- the frame-authoritative attribution with pod
cross-check, vsn-mismatch warning, GPS frame->node fallback, and never-fabricate.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import consumer  # noqa: E402
from node_info import read_node_info, NodeInfo  # noqa: E402


def _fm(vsn=None, node_id=None, lat=None, lon=None):
    return consumer.FrameMeta(100, vsn=vsn, node_id=node_id, lat=lat, lon=lon)


# --- vendored reader: sentinel normalization (contract parity) ---------------

def test_reader_empty_env_all_none():
    ni = read_node_info(env={})
    assert ni == NodeInfo(None, None, None, None, "unknown", True)


def test_reader_vsn_zero_is_placeholder():
    ni = read_node_info(env={"WAGGLE_NODE_VSN": "0"})
    assert ni.vsn is None and ni.vsn_is_placeholder is True


def test_reader_real_values():
    ni = read_node_info(env={
        "WAGGLE_NODE_VSN": "W123", "WAGGLE_NODE_ID": "000048b02d",
        "WAGGLE_NODE_GPS_LAT": "41.88", "WAGGLE_NODE_GPS_LON": "-87.63",
        "WAGGLE_NODE_MOBILITY": "static"})
    assert ni.vsn == "W123" and ni.node_id == "000048b02d"
    assert ni.lat == 41.88 and ni.lon == -87.63
    assert ni.mobility == "static" and ni.vsn_is_placeholder is False


def test_reader_gps_999_sentinel_by_range():
    ni = read_node_info(env={"WAGGLE_NODE_GPS_LAT": "999", "WAGGLE_NODE_GPS_LON": "999"})
    assert ni.lat is None and ni.lon is None


def test_reader_signed_southern_western():
    ni = read_node_info(env={"WAGGLE_NODE_GPS_LAT": "-33.87", "WAGGLE_NODE_GPS_LON": "-151.2"})
    assert ni.lat == -33.87 and ni.lon == -151.2


# --- resolve_identity: vsn/node_id authority ---------------------------------

def test_frame_vsn_is_authoritative():
    ni = NodeInfo("Wnode", "nid-node", None, None, "static", False)
    ident = consumer.resolve_identity(_fm(vsn="Wframe", node_id="nid-frame"), node_info=ni)
    assert ident.vsn == "Wframe"          # frame wins
    assert ident.node_id == "nid-frame"


def test_pod_vsn_fallback_when_frame_lacks():
    ni = NodeInfo("Wnode", "nid-node", None, None, "static", False)
    ident = consumer.resolve_identity(_fm(vsn=None, node_id=None), node_info=ni)
    assert ident.vsn == "Wnode"           # fallback to pod
    assert ident.node_id == "nid-node"


def test_vsn_mismatch_warns_but_uses_frame(caplog):
    ni = NodeInfo("Wnode", None, None, None, "static", False)
    with caplog.at_level("WARNING"):
        ident = consumer.resolve_identity(_fm(vsn="Wframe"), node_info=ni)
    assert ident.vsn == "Wframe"
    assert any("vsn mismatch" in r.message for r in caplog.records)


def test_vsn_agree_no_warning(caplog):
    ni = NodeInfo("W123", None, None, None, "static", False)
    with caplog.at_level("WARNING"):
        ident = consumer.resolve_identity(_fm(vsn="W123"), node_info=ni)
    assert ident.vsn == "W123"
    assert not any("vsn mismatch" in r.message for r in caplog.records)


# --- resolve_identity: GPS frame -> node -> none, never fabricate -------------

def test_location_prefers_frame():
    ni = NodeInfo(None, None, 1.0, 2.0, "static", False)
    ident = consumer.resolve_identity(_fm(lat=41.88, lon=-87.63), node_info=ni)
    assert (ident.lat, ident.lon) == (41.88, -87.63)
    assert ident.location_source == "frame"


def test_location_falls_back_to_node():
    ni = NodeInfo(None, None, 41.88, -87.63, "static", False)
    ident = consumer.resolve_identity(_fm(lat=None, lon=None), node_info=ni)
    assert (ident.lat, ident.lon) == (41.88, -87.63)
    assert ident.location_source == "node"


def test_location_none_when_neither_has_it():
    ni = NodeInfo(None, None, None, None, "static", False)
    ident = consumer.resolve_identity(_fm(lat=None, lon=None), node_info=ni)
    assert not ident.has_location
    assert ident.location_source is None      # never fabricated


def test_no_node_info_uses_frame_only(monkeypatch):
    # no WES env at all -> get_node_info() reads empty -> attribute purely from frame
    for k in ("WAGGLE_NODE_VSN", "WAGGLE_NODE_ID",
              "WAGGLE_NODE_GPS_LAT", "WAGGLE_NODE_GPS_LON", "WAGGLE_NODE_MOBILITY"):
        monkeypatch.delenv(k, raising=False)
    ident = consumer.resolve_identity(_fm(vsn="Wframe", lat=41.0, lon=-87.0), node_info=None)
    assert ident.vsn == "Wframe"
    assert ident.location_source == "frame"
