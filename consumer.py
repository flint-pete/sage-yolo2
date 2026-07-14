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

Stage 2 adds read_frame_metadata(): the frame is self-describing (image-sampler2
embeds a full JSON blob in EXIF UserComment plus standard tags). We read the
AUTHORITATIVE fields from the UserComment JSON -- capture_ts, unique_id, vsn, gps
(signed floats), camera, acquisition_path -- so a published detection is
frame-anchored (its observation time is when the photo was TAKEN, its identity/
location are the frame's, not the pod's wall clock). GPS is read from the JSON, not
reconstructed from the abs+ref GPS EXIF (see V2-Design §7.1-7.2). Reading metadata is
fail-soft: a frame with missing/corrupt UserComment still yields the filename-derived
fields so inference can proceed.
"""
import json
import logging
import os
import re

logger = logging.getLogger("sage-yolo2.consumer")

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


# ── frame metadata (Stage 2) ─────────────────────────────────────────
# image-sampler2 embeds a full JSON blob in the EXIF UserComment tag, prefixed with
# the 8-byte Exif character-code marker. The JSON is the AUTHORITATIVE source for
# every field (V2-Design §7.1-7.2): signed lat/lon floats, unique_id, vsn, camera,
# acquisition_path. Standard EXIF/GPS tags are the tool-friendly view and are NOT
# read here (GPS EXIF stores abs+ref, needing reconstruction the JSON avoids).
_UC_PREFIX = b"ASCII\x00\x00\x00"


class FrameMeta:
    """Authoritative, frame-anchored metadata for one cached frame.

    capture_ts_ns is always set (from the filename, the ordering key). The rest come
    from the UserComment JSON when present; each may be None if the frame lacks it.
    lat/lon are signed decimal floats (never fabricated -- None when absent).
    """

    __slots__ = ("capture_ts_ns", "unique_id", "vsn", "node_id", "camera",
                 "lat", "lon", "acquisition_path", "raw")

    def __init__(self, capture_ts_ns, *, unique_id=None, vsn=None, node_id=None,
                 camera=None, lat=None, lon=None, acquisition_path=None, raw=None):
        self.capture_ts_ns = capture_ts_ns
        self.unique_id = unique_id
        self.vsn = vsn
        self.node_id = node_id
        self.camera = camera
        self.lat = lat
        self.lon = lon
        self.acquisition_path = acquisition_path
        self.raw = raw or {}          # the full JSON dict, for any extra fields

    @property
    def has_location(self):
        return self.lat is not None and self.lon is not None

    def __repr__(self):  # pragma: no cover - debug aid
        return "FrameMeta(ts=%r, vsn=%r, uid=%r, loc=%r)" % (
            self.capture_ts_ns, self.vsn, self.unique_id,
            (self.lat, self.lon) if self.has_location else None)


def _extract_usercomment_json(jpeg_path):
    """Return the parsed UserComment JSON dict, or None if absent/unreadable.

    Fail-soft: any error (no EXIF, no UserComment, bad prefix, bad JSON) -> None with
    a warning. We use Pillow for the tag read (already a dependency) and json for the
    payload, mirroring the producer's embed exactly (ASCII prefix + compact JSON).
    """
    try:
        from PIL import Image
    except ImportError:                       # pragma: no cover - Pillow is required
        logger.warning("Pillow not available; cannot read frame metadata")
        return None
    try:
        with Image.open(jpeg_path) as im:
            exif = im.getexif()
            # UserComment lives in the Exif IFD (tag 0x9286).
            ifd = exif.get_ifd(0x8769)        # ExifIFD
            uc = ifd.get(0x9286)
        if not uc:
            return None
        if isinstance(uc, str):
            uc = uc.encode("ascii", "replace")
        if uc[:8] == _UC_PREFIX:
            uc = uc[8:]
        if not uc:
            return None
        return json.loads(uc.decode("ascii", "replace"))
    except (OSError, ValueError, KeyError) as e:
        logger.warning("cannot read UserComment metadata from %s: %s", jpeg_path, e)
        return None


def _coord(v):
    """A signed decimal-degree float, or None. Never fabricates."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_frame_metadata(frame):
    """Read authoritative FrameMeta for a Frame (from Stage-1 scan).

    capture_ts is taken from the filename (the ordering key). If the UserComment JSON
    carries a differing capture_timestamp_ns, we WARN and PREFER THE FILENAME ts
    (V2-Design §7.1.4) -- a mismatch signals a corrupted/edited file, not ambiguity.
    All other authoritative fields come from the JSON; each is None when absent.
    GPS (lat/lon) is omitted entirely when the frame has no fix -- never fabricated.
    """
    payload = _extract_usercomment_json(frame.path) or {}

    ts = frame.capture_ts_ns
    json_ts = payload.get("capture_timestamp_ns")
    if json_ts is not None and json_ts != ts:
        logger.warning(
            "capture_ts mismatch for %s: filename=%d json=%s -- preferring filename",
            frame.name, ts, json_ts)

    return FrameMeta(
        ts,
        unique_id=payload.get("unique_id"),
        vsn=payload.get("vsn") or (frame.vsn or None),   # JSON authoritative; filename fallback
        node_id=payload.get("node_id"),
        camera=payload.get("camera") or (frame.camera or None),
        lat=_coord(payload.get("lat")),
        lon=_coord(payload.get("lon")),
        acquisition_path=payload.get("acquisition_path"),
        raw=payload,
    )
