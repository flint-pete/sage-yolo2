"""
Unit tests for save_match.py — run locally, no GPU/node/pywaggle needed.

    python3 -m pytest tests/test_save_match.py -v
    # or, dependency-free:
    python3 tests/test_save_match.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from save_match import parse_save_match, should_save, SaveMatchError  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────
# bioclip/birdnet-style detections carry common + scientific names.
BIRD_KEYS = ["common_name", "scientific_name"]
# yolo-style detections carry a single COCO class name.
YOLO_KEYS = ["name"]


def det(common=None, scientific=None, name=None, confidence=0.0):
    d = {"confidence": confidence}
    if common is not None:
        d["common_name"] = common
    if scientific is not None:
        d["scientific_name"] = scientific
    if name is not None:
        d["name"] = name
    return d


# ── parse: valid specs ───────────────────────────────────────────────
def test_parse_empty_and_none_save_nothing():
    assert parse_save_match(None) == []
    assert parse_save_match("") == []
    assert parse_save_match("   ") == []


def test_parse_wildcard():
    rules = parse_save_match("*:0.7")
    assert len(rules) == 1
    assert rules[0].is_wildcard
    assert rules[0].min_confidence == 0.7


def test_parse_single_rule():
    rules = parse_save_match("Barn Owl:0.5")
    assert len(rules) == 1
    assert not rules[0].is_wildcard
    assert rules[0].name == "barn owl"  # lowercased
    assert rules[0].min_confidence == 0.5


def test_parse_multi_rule():
    rules = parse_save_match("Barn Owl:0.5,Northern Cardinal:0.7")
    assert len(rules) == 2
    assert rules[0].name == "barn owl"
    assert rules[1].name == "northern cardinal"
    assert rules[1].min_confidence == 0.7


def test_parse_trims_whitespace():
    rules = parse_save_match("  Barn Owl : 0.5 , Northern Cardinal:0.7 ")
    assert rules[0].name == "barn owl"
    assert rules[0].min_confidence == 0.5
    assert rules[1].name == "northern cardinal"


def test_parse_scientific_name_with_space():
    rules = parse_save_match("Cardinalis cardinalis:0.6")
    assert rules[0].name == "cardinalis cardinalis"
    assert rules[0].min_confidence == 0.6


def test_parse_conf_bounds_inclusive():
    assert parse_save_match("x:0")[0].min_confidence == 0.0
    assert parse_save_match("x:1")[0].min_confidence == 1.0


# ── parse: malformed -> fail fast ────────────────────────────────────
def _expect_error(spec):
    try:
        parse_save_match(spec)
    except SaveMatchError:
        return True
    return False


def test_parse_missing_confidence_errors():
    assert _expect_error("Barn Owl")


def test_parse_non_numeric_confidence_errors():
    assert _expect_error("Barn Owl:high")


def test_parse_out_of_range_confidence_errors():
    assert _expect_error("Barn Owl:1.5")
    assert _expect_error("Barn Owl:-0.1")


def test_parse_empty_name_errors():
    assert _expect_error(":0.5")


def test_parse_stray_comma_errors():
    assert _expect_error("Barn Owl:0.5,,Northern Cardinal:0.7")
    assert _expect_error("Barn Owl:0.5,")


# ── should_save: empty rules ─────────────────────────────────────────
def test_should_save_no_rules_saves_nothing():
    dets = [det(common="Blue Jay", scientific="Cyanocitta cristata", confidence=0.99)]
    assert should_save([], dets, BIRD_KEYS) is False


# ── should_save: wildcard ────────────────────────────────────────────
def test_wildcard_saves_above_threshold():
    rules = parse_save_match("*:0.7")
    assert should_save(rules, [det(common="Blue Jay", confidence=0.8)], BIRD_KEYS)


def test_wildcard_skips_below_threshold():
    rules = parse_save_match("*:0.7")
    assert not should_save(rules, [det(common="Blue Jay", confidence=0.6)], BIRD_KEYS)


# ── should_save: exact match, case-insensitive, common OR scientific ──
def test_match_on_common_name_case_insensitive():
    rules = parse_save_match("barn owl:0.5")
    assert should_save(rules, [det(common="Barn Owl", confidence=0.55)], BIRD_KEYS)


def test_match_on_scientific_name():
    rules = parse_save_match("Tyto alba:0.5")
    assert should_save(
        rules, [det(common="Barn Owl", scientific="Tyto alba", confidence=0.6)],
        BIRD_KEYS,
    )


def test_no_substring_match():
    # "Cardinal" must NOT match "Northern Cardinal"
    rules = parse_save_match("Cardinal:0.1")
    dets = [det(common="Northern Cardinal", scientific="Cardinalis cardinalis",
                confidence=0.99)]
    assert not should_save(rules, dets, BIRD_KEYS)


def test_exact_match_below_rule_threshold_skips():
    rules = parse_save_match("Barn Owl:0.7")
    assert not should_save(rules, [det(common="Barn Owl", confidence=0.6)], BIRD_KEYS)


# ── should_save: OR over rules and over detections ───────────────────
def test_or_over_rules():
    rules = parse_save_match("Barn Owl:0.5,Northern Cardinal:0.7")
    # only the Cardinal is present, above its threshold
    dets = [det(common="Northern Cardinal", confidence=0.8)]
    assert should_save(rules, dets, BIRD_KEYS)


def test_or_over_detections_any_match_saves():
    rules = parse_save_match("Barn Owl:0.5")
    # clip has several detections; only one matches -> save the whole clip
    dets = [
        det(common="Blue Jay", confidence=0.9),
        det(common="American Robin", confidence=0.8),
        det(common="Barn Owl", confidence=0.55),  # the match
    ]
    assert should_save(rules, dets, BIRD_KEYS)


def test_no_detection_matches():
    rules = parse_save_match("Barn Owl:0.5")
    dets = [
        det(common="Blue Jay", confidence=0.9),
        det(common="American Robin", confidence=0.8),
    ]
    assert not should_save(rules, dets, BIRD_KEYS)


# ── should_save: YOLO (COCO class name, single key) ──────────────────
def test_yolo_coco_class_match():
    rules = parse_save_match("bird:0.5,person:0.6")
    assert should_save(rules, [det(name="bird", confidence=0.55)], YOLO_KEYS)
    assert should_save(rules, [det(name="person", confidence=0.7)], YOLO_KEYS)


def test_yolo_class_below_threshold_skips():
    rules = parse_save_match("person:0.6")
    assert not should_save(rules, [det(name="person", confidence=0.5)], YOLO_KEYS)


def test_yolo_wildcard():
    rules = parse_save_match("*:0.4")
    assert should_save(rules, [det(name="fire hydrant", confidence=0.45)], YOLO_KEYS)


# ── should_save: robustness ──────────────────────────────────────────
def test_detection_missing_confidence_skipped():
    rules = parse_save_match("*:0.1")
    assert not should_save(rules, [{"common_name": "Blue Jay"}], BIRD_KEYS)


def test_detection_missing_name_keys_skipped_for_exact():
    rules = parse_save_match("Barn Owl:0.1")
    # detection has confidence but no name fields -> can't match an exact rule
    assert not should_save(rules, [det(confidence=0.9)], BIRD_KEYS)


def test_detection_missing_name_still_matches_wildcard():
    rules = parse_save_match("*:0.1")
    # wildcard cares only about confidence
    assert should_save(rules, [det(confidence=0.9)], BIRD_KEYS)


def test_empty_detection_list():
    rules = parse_save_match("*:0.1")
    assert not should_save(rules, [], BIRD_KEYS)


# ── dependency-free runner ───────────────────────────────────────────
def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed ({len(fns)} total)")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if _run_all() else 1)
