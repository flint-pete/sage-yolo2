#!/usr/bin/env python3
"""sage-yolo2 -- frame selection (Stage 5): which frames a wake processes.

This is the pure, testable core of the wake loop (the loop itself, in app.py, adds
YOLO + publish + sleep). Given the frames currently in the cache, the time of the
last wake, and the seen-store, select_frames() decides exactly which frames this wake
should process, in oldest->newest order.

Two windowing models are kept distinct (V2-Design §8.5):
  * time-windowed (default): only frames with capture_ts > last_wake_ts -- what the
    producer made since we last looked.
  * backlog (--all-unseen): the WHOLE cache, ignoring last_wake_ts -- "process
    everything I haven't examined."
Seen-store dedup is the universal safety net applied AFTER either window, so no frame
is processed twice regardless of model or a producer capture_ts reset.

Selection knobs map to the clean CLI (§10):
  --select-every D : one frame per D of CAPTURE-time (stride). D == 0 -> just newest.
  --max-frames N   : cap frames processed this wake (0 = unlimited).
  --all-unseen     : backlog model; overrides --select-every.
"""
import logging

logger = logging.getLogger("sage-yolo2.selection")

_UNITS = {"s": 1, "m": 60, "h": 3600}


def parse_duration(text):
    """Parse '15m' / '1h' / '30s' / '90' -> seconds (int). Bare number = seconds.

    Raises ValueError on garbage. '0' -> 0 (single-shot / newest sentinel).
    """
    if text is None:
        raise ValueError("duration is required")
    s = str(text).strip().lower()
    if not s:
        raise ValueError("empty duration")
    if s.isdigit():
        return int(s)
    unit = s[-1]
    if unit not in _UNITS:
        raise ValueError("bad duration %r (use s/m/h or a bare number of seconds)" % text)
    num = s[:-1]
    if not num or not num.replace(".", "", 1).isdigit():
        raise ValueError("bad duration %r" % text)
    return int(float(num) * _UNITS[unit])


def _stride_pick(frames, stride_ns):
    """Walk oldest->newest, pick one frame per `stride_ns` of capture-time.

    Anchored to capture_ts (not wall-clock), so it's deterministic/reproducible.
    Always picks the first frame; then each next frame at least `stride_ns` after the
    last pick. frames must be oldest-first.
    """
    picked = []
    last_ts = None
    for f in frames:
        if last_ts is None or (f.capture_ts_ns - last_ts) >= stride_ns:
            picked.append(f)
            last_ts = f.capture_ts_ns
    return picked


def select_frames(frames, *, last_wake_ts_ns=0, select_every_ns=0, all_unseen=False,
                  max_frames=0, seen=None, reprocess=False, uid_of=None):
    """Select the frames this wake should process, oldest->newest.

    Args:
      frames         : oldest-first list of Frame (from consumer.scan_frames).
      last_wake_ts_ns: capture_ts boundary for the time-windowed model (0 = all).
      select_every_ns: stride in capture-time ns; 0 = newest-only within the window.
      all_unseen     : backlog model -- ignore last_wake_ts, consider the whole cache.
      max_frames     : cap on this wake (0 = unlimited).
      seen           : a SeenStore (or None) -- dedup safety net.
      reprocess      : if True, skip dedup (seen.is_seen already honors this too).
      uid_of         : callable(frame) -> unique_id for dedup. The wake loop injects
                       one that reads the frame's resolved SHA256; defaults to a
                       name-based fallback so selection stays pure/testable.

    Returns oldest-first list of selected frames.
    """
    key = uid_of or _uid

    # 1) window
    if all_unseen:
        window = list(frames)                                 # backlog: whole cache
    else:
        window = [f for f in frames if f.capture_ts_ns > last_wake_ts_ns]

    # 2) selection policy within the window
    if all_unseen:
        candidates = window                                   # every (unseen) frame
    elif select_every_ns > 0:
        candidates = _stride_pick(window, select_every_ns)    # stride
    elif max_frames and max_frames > 1:
        candidates = window[-max_frames:]                     # K newest (§10: select-every 0 + max-frames K)
    else:
        candidates = window[-1:] if window else []            # newest single

    # 3) dedup safety net (universal, after either window)
    if seen is not None and not reprocess:
        candidates = [c for c in candidates if not seen.is_seen(key(c))]

    # 4) cap. For the backlog/stride models this keeps the OLDEST first so the rest
    #    drain on later wakes (no unbounded storm, §8.6). The newest branch above
    #    already selected the K newest, so this is a no-op there.
    if max_frames and len(candidates) > max_frames:
        candidates = candidates[:max_frames]

    return candidates


def _uid(frame):
    """unique_id for dedup. Frame (Stage 1) has no uid; the wake loop passes frames
    whose unique_id was resolved via read_frame_metadata. Accept either an attribute
    or fall back to the frame name (stable within a cache) so selection stays pure."""
    return getattr(frame, "unique_id", None) or getattr(frame, "name", None)
