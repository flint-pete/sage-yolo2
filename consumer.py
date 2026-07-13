#!/usr/bin/env python3
"""sage-yolo2 -- cache consumer (Stage 1: read + fail-fast).

sage-yolo2 does NOT open a camera in its production path. It CONSUMES frames that
image-sampler2 (the producer) wrote into the shared WES ``/local-cache``. This
module is the read side of that contract; it is deliberately pure (no cv2, YOLO, or
pywaggle imports) so it is unit-testable offline.

The frame contract is image-sampler2's v2 cache layout (verified against its
``cache.py`` / ``metadata.py``):

  * cache root      : ``/local-cache`` (default), provided by wes-local-cache-manager
  * per-stream dir  : ``<root>/<cache-name>/<camera>/``
  * frame filename  : ``<capture_ts_ns>-v2-<vsn>-<camera>.jpg``
  * ordering key    : ``capture_ts_ns`` (the filename prefix -- authoritative, NOT mtime)
  * in-flight writes: ``*.tmp`` (producer writes tmp then atomically renames) -- skipped

Stage 1 implements: resolve the cache root, FAIL FAST if it is absent (no silent
fallback -- a missing cache means the node lacks wes-local-cache-manager or the mount),
scan a per-stream dir, and select the single newest committed frame.
"""
import os
import re

# Default shared cache mount, provided by wes-local-cache-manager. Overridable via
# the same env var image-sampler2 honours, so producer and consumer stay in sync.
LOCAL_CACHE_DIR = "/local-cache"
CACHE_ROOT_ENV = "IS2_CACHE_ROOT"

# A committed v2 frame: <capture_ts_ns>-v2-<vsn>-<camera>.jpg. The timestamp is
# all-digits (no '-'), so the FIRST "-v2-" delimits it from <vsn>-<camera> even when
# vsn/camera contain hyphens. Only the capture_ts is needed for ordering.
_V2_MARKER = "-v2-"


class CacheError(Exception):
    """Config-time (fail-fast) cache error -- reported and exits non-zero."""


class Frame:
    """One committed v2 frame in the cache. capture_ts_ns is the ordering key."""

    __slots__ = ("path", "name", "capture_ts_ns", "vsn", "camera")

    def __init__(self, path, name, capture_ts_ns, vsn, camera):
        self.path = path
        self.name = name
        self.capture_ts_ns = capture_ts_ns
        self.vsn = vsn
        self.camera = camera

    def __repr__(self):  # pragma: no cover - debug aid
        return "Frame(%r, ts=%r, vsn=%r, camera=%r)" % (
            self.name, self.capture_ts_ns, self.vsn, self.camera)


def resolve_cache_root(explicit=None):
    """Resolve the cache ROOT. Precedence: explicit > $IS2_CACHE_ROOT > /local-cache.

    Pure: does not probe or create. Presence/writability is enforced separately by
    assert_cache_available() so the error is a clean fail-fast, not a silent miss.
    """
    return explicit or os.environ.get(CACHE_ROOT_ENV) or LOCAL_CACHE_DIR


_MISSING_CACHE_MSG = (
    "cache directory %(dir)r is not present (or not a readable directory) on this "
    "node.\n"
    "  sage-yolo2 CONSUMES frames that image-sampler2 wrote to the shared "
    "%(default)s cache;\n"
    "  it does not open a camera in this mode. That directory is provided by the "
    "'wes-local-cache-manager'\n"
    "  WES component (a /media/plugin-data/local-cache host mount) and must be "
    "mounted into this pod.\n"
    "  It is missing here, which means the node lacks the component, the producer "
    "was never run,\n"
    "  or the plugin was started without the volume mount. Reading a nonexistent "
    "path would yield\n"
    "  no frames, so sage-yolo2 refuses to run rather than silently do nothing.\n"
    "  Fix: deploy wes-local-cache-manager and mount its host dir at %(default)s; "
    "or point --input at\n"
    "  an existing per-stream cache dir (<root>/<cache-name>/<camera>) for local "
    "development."
)


def assert_cache_available(cache_dir):
    """Fail-FAST guard: the per-stream cache dir MUST exist as a readable directory.

    There is no fallback -- a missing cache is a clean error, never a silent no-op.
    Raises CacheError. (An EMPTY but present dir is valid: nothing to process yet.)
    """
    if os.path.isdir(cache_dir) and os.access(cache_dir, os.R_OK | os.X_OK):
        return
    raise CacheError(_MISSING_CACHE_MSG
                     % {"dir": cache_dir, "default": LOCAL_CACHE_DIR})


def parse_v2_name(filename):
    """Parse ``<ts>-v2-<vsn>-<camera>.jpg`` -> (capture_ts_ns, vsn, camera) or None.

    Returns None for any name not matching the exact v2 shape (callers treat those
    as unknown -- never consumed). Only the basename is considered.
    """
    base = os.path.basename(filename)
    if not base.endswith(".jpg"):
        return None
    stem = base[:-len(".jpg")]
    idx = stem.find(_V2_MARKER)
    if idx <= 0:                      # no marker, or nothing before it
        return None
    ts_str = stem[:idx]
    rest = stem[idx + len(_V2_MARKER):]
    if not ts_str.isdigit() or not rest:
        return None
    ts = int(ts_str)
    if ts <= 0:
        return None
    # best-effort vsn/camera split (first '-'); correct when vsn has no '-'.
    vsn, camera = rest.split("-", 1) if "-" in rest else (rest, "")
    return (ts, vsn, camera)


def scan_frames(cache_dir):
    """Scan a per-stream dir -> list[Frame], OLDEST FIRST by capture_ts.

    Only fully-committed v2-named files are frames; ``*.tmp`` (in-flight producer
    writes) and any non-v2 file are ignored. Never raises on odd/vanished files;
    a missing dir yields []. (Presence is a caller concern via assert_cache_available.)
    """
    frames = []
    try:
        entries = os.listdir(cache_dir)
    except OSError:
        return frames
    for name in entries:
        if name.endswith(".tmp"):        # in-flight write; not yet consumable
            continue
        path = os.path.join(cache_dir, name)
        if not os.path.isfile(path):
            continue
        parsed = parse_v2_name(name)
        if parsed is None:               # non-v2 file; not ours
            continue
        ts, vsn, camera = parsed
        frames.append(Frame(path, name, ts, vsn, camera))
    # capture_ts is unique-enough; tie-break on name for a stable order.
    frames.sort(key=lambda f: (f.capture_ts_ns, f.name))
    return frames


def newest_frame(cache_dir):
    """The single newest committed frame, or None if the dir has no v2 frames."""
    frames = scan_frames(cache_dir)
    return frames[-1] if frames else None
