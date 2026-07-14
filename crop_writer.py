#!/usr/bin/env python3
# ANL:waggle-license
#  This file is part of the Waggle Platform.  See LICENSE.waggle.txt.
# ANL:waggle-license
#
# sage-yolo2 -- crop_writer (crop-producer, v2.1.0).
#
# The WRITE side of the v2 cache contract: build a self-describing v2 JPEG
# (standard EXIF + full JSON in UserComment + SHA256 in ImageUniqueID) and
# publish it into a per-stream bounded ring cache. This is what lets sage-yolo2
# act as a PRODUCER of cropped detections that a downstream classifier (BioCLIP)
# consumes exactly like an image-sampler2 frame.
#
# VENDORED from image-sampler2 (metadata.py embed + cache.py ring/eviction),
# per CROP-PRODUCER-Design.md OPEN DECISION -> (B) vendored copy (the same
# precedent as save_match.py). Kept deliberately compatible with sage-yolo2's
# READ side (consumer.py parse_v2_name / read_frame_metadata): a frame written
# here MUST be readable there. Any divergence in the v2 format must be mirrored
# in both. See references sync note in DOCKER-BUILD.md.
#
# Design invariants preserved from image-sampler2 cache.py 2.6:
#   - per-stream ring at <root>/<cache-name>/<camera>/; caps per stream.
#   - two independent caps (count, MB decimal 10^6), evict-on-EITHER.
#   - EVICT BEFORE the new file joins; atomic tmp -> os.replace.
#   - oldest = capture-ts PREFIX in the v2 name (no stat); non-v2 files untouched.
#   - E3 GUARD: a single new image larger than the size cap is DROPPED.
#   - stateless (re-scan each write); fail-SOFT at runtime, fail-FAST at config.
#
# Crop-producer additions on top of the vendored base:
#   - detection_index in object_name so N crops from ONE frame are distinct
#     (they share the parent capture_ts).
#   - source_* provenance (class/confidence/bbox/unique_id) in the JSON blob so a
#     species result traces back to the exact parent frame + box.

import datetime
import hashlib
import io
import json
import logging
import os
import re

import piexif

logger = logging.getLogger("sage-yolo2.crop_writer")

SCHEMA_VERSION = "sage-img-1"          # MUST match image-sampler2 (BioCLIP reads it)
V2_MARKER = "v2"
BYTES_PER_MB = 1_000_000               # MB is decimal (10^6), per the v2 contract
LOCAL_CACHE_DIR = "/local-cache"

_UC_PREFIX = b"ASCII\x00\x00\x00"      # UserComment 8-byte character-code prefix
_CACHE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class CacheError(Exception):
    """Config-time (fail-fast) cache error. Runtime errors are fail-soft (logged)."""


# --------------------------------------------------------------------------
# v2 filename  (compatible with consumer.parse_v2_name -- the read side)
# --------------------------------------------------------------------------

def build_v2_name(capture_ts_ns, vsn, camera, ext=".jpg"):
    """Build the v2 filename: <capture_ts_ns>-v2-<vsn>-<camera>.jpg.

    For crops the caller passes a camera like '<cam>-crop-<idx>' so that N crops
    sharing one parent capture_ts land under distinct names. vsn/camera must not
    contain path separators or whitespace.
    """
    if not isinstance(capture_ts_ns, int) or capture_ts_ns <= 0:
        raise ValueError("capture_ts_ns must be a positive int (nanoseconds)")
    if not vsn or not camera:
        raise ValueError("vsn and camera are required for the v2 name")
    for part, val in (("vsn", vsn), ("camera", camera)):
        if any(c in str(val) for c in "/\\ \t\n"):
            raise ValueError(f"{part} '{val}' must not contain path separators/whitespace")
    return f"{capture_ts_ns}-{V2_MARKER}-{vsn}-{camera}{ext}"


def object_name_for(capture_ts_ns, vsn, camera):
    """Object-store / on-disk object name (same as the v2 filename)."""
    return build_v2_name(capture_ts_ns, vsn, camera)


# --------------------------------------------------------------------------
# EXIF / self-describing embed  (vendored from image-sampler2 metadata.py)
# --------------------------------------------------------------------------

def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def _ns_to_exif_datetime(ts_ns):
    dt = datetime.datetime.fromtimestamp(ts_ns / 1e9, tz=datetime.timezone.utc)
    date_str = dt.strftime("%Y:%m:%d %H:%M:%S")
    subsec = f"{int((ts_ns % 1_000_000_000) / 1000):06d}"   # microseconds
    return date_str, subsec


def _deg_to_dms_rationals(deg):
    """Signed degrees -> abs DMS rationals (piexif can't serialize negatives)."""
    deg = abs(float(deg))
    d = int(deg)
    m = int((deg - d) * 60)
    s = round(((deg - d) * 60 - m) * 60 * 10000)
    return ((d, 1), (m, 1), (s, 10000))


def build_field_dict(*, vsn, node_id, job, task, plugin, camera,
                     capture_ts_ns, upload_ts_ns, lat, lon, acquisition_path,
                     unique_id, source=None):
    """Assemble the v2 field set + crop-producer provenance.

    `source` (optional dict) carries the crop-producer additions: source_class,
    source_confidence, source_bbox, source_unique_id, detection_index. Kept as a
    nested object so a plain image-sampler2 frame and a crop frame share the same
    base schema; a classifier reads `source` only when present.
    """
    fields = {
        "schema_version": SCHEMA_VERSION,
        "vsn": vsn,
        "node_id": node_id,
        "job": job,
        "task": task,
        "plugin": plugin,
        "camera": camera,
        "capture_timestamp_ns": capture_ts_ns,
        "upload_timestamp_ns": upload_ts_ns,
        "unique_id": unique_id,
        "object_name": object_name_for(capture_ts_ns, vsn, camera),
        "lat": lat,
        "lon": lon,
        "acquisition_path": acquisition_path,
    }
    if source is not None:
        fields["source"] = source
    return fields


def build_exif_bytes(fields):
    """EXIF block for a field dict. Standard tags = human view; UserComment = full
    JSON blob; ImageUniqueID = the source-frame SHA256 (passed in fields)."""
    cap_date, cap_subsec = _ns_to_exif_datetime(fields["capture_timestamp_ns"])
    human = (f"Sage sage-yolo2 crop {V2_MARKER}; vsn={fields['vsn']}; "
             f"camera={fields['camera']}; job={fields['job']}")
    zeroth = {
        piexif.ImageIFD.Make: "Sage/Waggle",
        piexif.ImageIFD.Model: str(fields["vsn"]),
        piexif.ImageIFD.Software: str(fields["plugin"]),
        piexif.ImageIFD.DateTime: cap_date,
        piexif.ImageIFD.ImageDescription: human,
    }
    user_comment = json.dumps(fields, separators=(",", ":"), sort_keys=True)
    exif_ifd = {
        piexif.ExifIFD.DateTimeOriginal: cap_date,
        piexif.ExifIFD.SubSecTimeOriginal: cap_subsec,
        piexif.ExifIFD.OffsetTimeOriginal: "+00:00",
        piexif.ExifIFD.ImageUniqueID: str(fields["unique_id"]),
        piexif.ExifIFD.UserComment: _UC_PREFIX + user_comment.encode("ascii"),
    }
    gps_ifd = {}
    if fields.get("lat") is not None and fields.get("lon") is not None:
        lat, lon = float(fields["lat"]), float(fields["lon"])
        gps_ifd = {
            piexif.GPSIFD.GPSLatitudeRef: "N" if lat >= 0 else "S",
            piexif.GPSIFD.GPSLatitude: _deg_to_dms_rationals(lat),
            piexif.GPSIFD.GPSLongitudeRef: "E" if lon >= 0 else "W",
            piexif.GPSIFD.GPSLongitude: _deg_to_dms_rationals(lon),
        }
    exif_dict = {"0th": zeroth, "Exif": exif_ifd, "GPS": gps_ifd,
                 "1st": {}, "thumbnail": None}
    return piexif.dump(exif_dict)


def inject_exif(jpeg_bytes, exif_bytes):
    """Insert exif_bytes into jpeg_bytes WITHOUT re-encoding pixels (piexif)."""
    sink = io.BytesIO()
    piexif.insert(exif_bytes, jpeg_bytes, sink)
    return sink.getvalue()


def embed_all(jpeg_bytes, *, vsn, node_id, job, task, plugin, camera,
              capture_ts_ns, upload_ts_ns, lat, lon, acquisition_path,
              source=None):
    """Compute unique_id (SHA256 of the crop JPEG) -> build EXIF -> inject.

    Returns (final_bytes, unique_id_hex). unique_id is the hash of the crop bytes
    BEFORE injection (stable, recomputable, no self-reference paradox), and is the
    key BioCLIP's seen-store dedups on (distinct per crop).
    """
    unique_id = sha256_hex(jpeg_bytes)
    fields = build_field_dict(
        vsn=vsn, node_id=node_id, job=job, task=task, plugin=plugin, camera=camera,
        capture_ts_ns=capture_ts_ns, upload_ts_ns=upload_ts_ns, lat=lat, lon=lon,
        acquisition_path=acquisition_path, unique_id=unique_id, source=source)
    final_bytes = inject_exif(jpeg_bytes, build_exif_bytes(fields))
    return final_bytes, unique_id


def read_back_fields(jpeg_bytes):
    """Read our fields back for verification: (json_dict, image_unique_id)."""
    exif_dict = piexif.load(jpeg_bytes)
    uc = exif_dict["Exif"].get(piexif.ExifIFD.UserComment, b"")
    if uc[:8] == _UC_PREFIX:
        uc = uc[8:]
    payload = json.loads(uc.decode("ascii")) if uc else {}
    uid = exif_dict["Exif"].get(piexif.ExifIFD.ImageUniqueID, b"")
    if isinstance(uid, bytes):
        uid = uid.decode("ascii", "replace")
    return payload, uid


# --------------------------------------------------------------------------
# per-stream ring cache  (vendored from image-sampler2 cache.py)
# --------------------------------------------------------------------------

def validate_cache_name(name):
    if not name or not _CACHE_NAME_RE.match(name) or name in (".", ".."):
        raise CacheError(
            "cache-name %r must be non-empty and contain only letters, digits, "
            "dot, dash, underscore (no path separators/whitespace)" % (name,))
    return name


def stream_dir(cache_root, cache_name, camera, *, create=True):
    """Compute (and by default create) <cache-root>/<cache-name>/<camera>/."""
    validate_cache_name(cache_name)
    if not camera or any(c in camera for c in "/\\"):
        raise CacheError("camera %r must be a single path segment (no separators)"
                         % (camera,))
    sdir = os.path.join(cache_root, cache_name, camera)
    if create:
        try:
            os.makedirs(sdir, exist_ok=True)
        except OSError as e:
            raise CacheError("cannot create cache dir %r: %s" % (sdir, e))
        if not os.access(sdir, os.W_OK | os.X_OK):
            raise CacheError("cache dir %r is not writable" % (sdir,))
    return os.path.abspath(sdir)


def _parse_ts_prefix(name):
    """Recover the capture-ts prefix from a v2 name for ordering, else None."""
    if not name.endswith(".jpg"):
        return None
    stem = name[:-len(".jpg")]
    marker = "-" + V2_MARKER + "-"
    idx = stem.find(marker)
    if idx <= 0:
        return None
    ts_str = stem[:idx]
    if not ts_str.isdigit():
        return None
    ts = int(ts_str)
    return ts if ts > 0 else None


class RingMember:
    __slots__ = ("path", "name", "capture_ts_ns", "size")

    def __init__(self, path, name, capture_ts_ns, size):
        self.path = path
        self.name = name
        self.capture_ts_ns = capture_ts_ns
        self.size = size


class RingState:
    __slots__ = ("count", "total_bytes", "members", "unknown_files")

    def __init__(self, members, unknown_files):
        self.members = members            # oldest-first
        self.unknown_files = unknown_files
        self.count = len(members)
        self.total_bytes = sum(m.size for m in members)


def scan_ring(sdir):
    """Scan a per-stream dir -> RingState. Never raises on missing/odd files."""
    members = []
    unknown = []
    try:
        entries = os.listdir(sdir)
    except FileNotFoundError:
        return RingState([], [])
    except OSError as e:  # pragma: no cover
        logger.warning("crop cache scan: cannot list %r: %s", sdir, e)
        return RingState([], [])
    for name in entries:
        path = os.path.join(sdir, name)
        if not os.path.isfile(path):
            continue
        if name.endswith(".tmp"):
            unknown.append(path)
            continue
        ts = _parse_ts_prefix(name)
        try:
            size = os.path.getsize(path)
        except OSError:                   # vanished mid-scan
            continue
        if ts is None:
            unknown.append(path)
            continue
        members.append(RingMember(path, name, ts, size))
    members.sort(key=lambda m: (m.capture_ts_ns, m.name))   # oldest first, stable
    return RingState(members, unknown)


class EvictPlan:
    __slots__ = ("drop_new", "evict", "reason")

    def __init__(self, drop_new, evict, reason=""):
        self.drop_new = drop_new
        self.evict = evict
        self.reason = reason


def plan_evictions(ring, new_bytes, max_count, max_mb):
    """Pure eviction planner (no I/O). Either cap may be None (unset)."""
    max_bytes = max_mb * BYTES_PER_MB if max_mb is not None else None
    if max_bytes is not None and new_bytes > max_bytes:
        return EvictPlan(
            True, [],
            reason="new crop %d B exceeds crop-max-mb budget %d B (E3 drop)"
                   % (new_bytes, max_bytes))
    remaining = list(ring.members)
    cur_count = ring.count
    cur_bytes = ring.total_bytes
    evict = []

    def over():
        if max_count is not None and cur_count + 1 > max_count:
            return True
        if max_bytes is not None and cur_bytes + new_bytes > max_bytes:
            return True
        return False

    while over() and remaining:
        victim = remaining.pop(0)
        evict.append(victim)
        cur_count -= 1
        cur_bytes -= victim.size
    return EvictPlan(False, evict)


class CommitResult:
    __slots__ = ("written", "final_path", "evicted", "warnings")

    def __init__(self, written, final_path, evicted, warnings):
        self.written = written
        self.final_path = final_path
        self.evicted = evicted
        self.warnings = warnings


def commit_capture(sdir, tmp_path, final_name, plan):
    """Apply an EvictPlan then atomically publish tmp_path -> <sdir>/<final_name>.

    EVICT FIRST, then os.replace, so the ring never transiently exceeds caps.
    Fail-soft on eviction-delete errors. drop_new -> tmp removed, nothing written.
    """
    warnings = []
    evicted = []
    if plan.drop_new:
        _safe_remove(tmp_path, warnings, "drop-new tmp")
        if plan.reason:
            warnings.append(plan.reason)
        return CommitResult(False, None, evicted, warnings)
    for victim in plan.evict:
        try:
            os.remove(victim.path)
            evicted.append(victim.path)
        except FileNotFoundError:
            evicted.append(victim.path)
        except OSError as e:
            warnings.append("eviction failed for %r: %s" % (victim.path, e))
    final_path = os.path.join(sdir, final_name)
    try:
        os.replace(tmp_path, final_path)
    except OSError as e:
        warnings.append("atomic publish failed (%r -> %r): %s"
                        % (tmp_path, final_path, e))
        _safe_remove(tmp_path, warnings, "failed-publish tmp")
        return CommitResult(False, None, evicted, warnings)
    return CommitResult(True, final_path, evicted, warnings)


def _safe_remove(path, warnings, what):
    if path is None:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        warnings.append("could not remove %s %r: %s" % (what, path, e))


def write_frame(final_bytes, sdir, final_name, *, max_count, max_mb):
    """High-level one-call write: scan -> plan -> atomic tmp -> commit.

    Writes final_bytes (already EXIF-embedded) into the ring at sdir under
    final_name, evicting per the caps first. tmp is fsync'd before the rename so
    a crash never leaves a torn file under the final name. Returns CommitResult.
    """
    ring = scan_ring(sdir)
    plan = plan_evictions(ring, len(final_bytes), max_count, max_mb)
    if plan.drop_new:
        return commit_capture(sdir, None, final_name, plan)  # nothing to publish
    tmp_path = os.path.join(sdir, final_name + ".tmp")
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, final_bytes)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as e:
        return CommitResult(False, None, [], ["tmp write failed %r: %s" % (tmp_path, e)])
    return commit_capture(sdir, tmp_path, final_name, plan)
