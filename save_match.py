"""
save_match.py — shared --save-match logic for Sage inference plugins.

Decouples "publish" (topics, always) from "save" (media, selective). A plugin
publishes a topic for every detection above --min-confidence, but only UPLOADS
the image/audio artifact when a detection matches a user-supplied save rule.

Spec: see docs/DESIGN-save-match-and-sampling.md.

NOTE: This module is intentionally dependency-free and is COPIED identically into
each plugin repo (bioclip, birdnet, yolo) because they do not share a Python
package. Keep the copies in sync; a future refactor will extract a shared package
(tracked separately). Any change here must be mirrored to the other copies.

Grammar:
    --save-match "Name:conf,Name:conf,..."
      - rules separated by ','
      - within a rule, the LAST ':' separates name from confidence, so names
        containing ':' are not supported (no target taxa do).
      - name is matched case-insensitively and EXACTLY (no substring) against
        any of the candidate names a caller supplies for a detection (e.g. the
        common name OR the scientific name; or the COCO class name for YOLO).
      - '*' is the only wildcard: matches any name (still gated by its conf).
      - conf is a float in [0, 1].

    Examples:
      "*:0.7"                              save any detection >= 0.7
      "Barn Owl:0.5,Northern Cardinal:0.7" save Barn Owls >=0.5 OR N. Cardinals >=0.7
      "bird:0.5,person:0.6"                (YOLO) save bird>=0.5 OR person>=0.6

Behavior:
      - Omitting --save-match (empty/None spec) -> no rules -> save NOTHING.
      - A malformed spec raises SaveMatchError; callers should fail fast at
        startup (do NOT silently ignore a bad rule — a typo'd rule that quietly
        saves nothing would waste a whole deployment).
      - should_save() is evaluated over the PUBLISHED detections (those already
        at/above --min-confidence). Single floor; see design note Model (A).
      - ANY (rule x detection) match -> save the whole clip/frame once.
"""

from __future__ import annotations

from typing import Iterable, Sequence


class SaveMatchError(ValueError):
    """Raised on a malformed --save-match specification."""


class Rule:
    """A single save rule: a name (or '*' wildcard) and a min confidence."""

    __slots__ = ("name", "is_wildcard", "min_confidence")

    def __init__(self, name: str, min_confidence: float):
        self.is_wildcard = (name == "*")
        # store lowercased for case-insensitive exact compare
        self.name = name.lower()
        self.min_confidence = min_confidence

    def matches(self, candidate_names_lower: Sequence[str], confidence: float) -> bool:
        if confidence < self.min_confidence:
            return False
        if self.is_wildcard:
            return True
        return self.name in candidate_names_lower

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Rule(name={self.name!r}, min_confidence={self.min_confidence})"


def parse_save_match(spec: str | None) -> list[Rule]:
    """Parse a --save-match spec string into a list of Rule.

    Returns [] for an empty/None spec (meaning: save nothing).
    Raises SaveMatchError (fail fast) on any malformed rule.
    """
    if spec is None:
        return []
    spec = spec.strip()
    if not spec:
        return []

    rules: list[Rule] = []
    for raw in spec.split(","):
        rule_str = raw.strip()
        if not rule_str:
            raise SaveMatchError(
                f"empty rule in --save-match (check for stray commas): {spec!r}"
            )
        if ":" not in rule_str:
            raise SaveMatchError(
                f"rule {rule_str!r} missing ':confidence' "
                f"(expected 'Name:0.5'); full spec: {spec!r}"
            )
        # split on the LAST ':' so names may (in principle) contain ':' — though
        # we document that they should not.
        name_part, _, conf_part = rule_str.rpartition(":")
        name = name_part.strip()
        conf_part = conf_part.strip()
        if not name:
            raise SaveMatchError(
                f"rule {rule_str!r} has an empty name; full spec: {spec!r}"
            )
        try:
            conf = float(conf_part)
        except ValueError:
            raise SaveMatchError(
                f"rule {rule_str!r} has a non-numeric confidence "
                f"{conf_part!r}; full spec: {spec!r}"
            )
        if not (0.0 <= conf <= 1.0):
            raise SaveMatchError(
                f"rule {rule_str!r} confidence {conf} is out of range [0, 1]; "
                f"full spec: {spec!r}"
            )
        rules.append(Rule(name, conf))

    return rules


def should_save(
    rules: Sequence[Rule],
    detections: Iterable[dict],
    name_keys: Sequence[str],
    confidence_key: str = "confidence",
) -> bool:
    """Return True if ANY detection matches ANY rule.

    Args:
        rules: parsed Rule list (from parse_save_match). Empty -> never save.
        detections: iterable of detection dicts. These should already be the
            PUBLISHED set (>= --min-confidence); the caller is responsible for
            that filtering (single-floor model).
        name_keys: dict keys on each detection whose values are candidate names
            to match against (e.g. ["common_name", "scientific_name"] for
            bioclip/birdnet; ["name"] or ["class_name"] for yolo). Missing keys
            and empty/None values are skipped.
        confidence_key: dict key holding the detection's confidence (float).

    Any (rule x detection) match -> True. Designed so the caller saves the whole
    clip/frame exactly once.
    """
    if not rules:
        return False

    for det in detections:
        try:
            conf = float(det[confidence_key])
        except (KeyError, TypeError, ValueError):
            # a detection without a usable confidence cannot match a threshold
            continue

        candidate_names_lower = []
        for key in name_keys:
            val = det.get(key)
            if val:
                candidate_names_lower.append(str(val).lower())

        for rule in rules:
            if rule.matches(candidate_names_lower, conf):
                return True

    return False
