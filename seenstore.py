#!/usr/bin/env python3
"""sage-yolo2 -- seen-store (Stage 4): the consumer's durable dedup memory.

A consumer must remember which frames it has already processed so a re-scan (or a
fresh one-shot pod) does not re-infer the whole cache. The memory:

  * is keyed on ``unique_id`` (SHA256 of the ORIGINAL frame bytes) -- stable across
    producer restarts, re-scans, and mtime changes; the only correct identity.
  * is a plain **newline-delimited list of hex SHA256s** -- simple, append-only
    (crash-safe: a torn final line is skipped), greppable, prune by rewrite.
  * lives in the WES cache's reserved, never-evicted ``.state`` area at a COMPOSITE
    path so two consumer instances never clobber each other (V2-Design §8.4):

        <root>/.state/<plugin>/<consumer-id>/<cache-name>/<camera>/seen

  * is node-persistent (survives pod restarts) and bounded (pruned to a horizon).

Everything here is fail-soft: a missing/corrupt/unwritable store degrades to
"nothing seen" (worst case = reprocess once) and never blocks inference.
"""
import logging
import os

logger = logging.getLogger("sage-yolo2.seenstore")

RESERVED_STATE_DIRNAME = ".state"      # matches wes-local-cache-manager's carve-out
PLUGIN_NAME = "sage-yolo2"
SEEN_FILENAME = "seen"
DEFAULT_MAX_IDS = 100_000              # prune horizon; the cache is bounded, so is this


def seen_store_path(cache_root, consumer_id, cache_name, camera,
                    *, plugin=PLUGIN_NAME, state_dirname=RESERVED_STATE_DIRNAME):
    """Build the composite seen-store path (V2-Design §8.4). Pure."""
    return os.path.join(cache_root.rstrip("/"), state_dirname, plugin,
                        consumer_id, cache_name, camera, SEEN_FILENAME)


class SeenStore:
    """Durable, bounded dedup memory keyed on unique_id.

    Loads once into an in-memory set for O(1) membership; appends new ids to the file
    as they are marked; prunes by rewrite when it exceeds ``max_ids``. When
    ``reprocess`` is True, membership always reports False (process regardless) while
    still recording ids, so turning reprocess back off resumes correct dedup.
    """

    def __init__(self, path, *, max_ids=DEFAULT_MAX_IDS, reprocess=False):
        self.path = path
        self.max_ids = max_ids
        self.reprocess = reprocess
        self._ids = []          # insertion order preserved for prune-oldest
        self._set = set()
        self._load()

    # -- load / query ---------------------------------------------------------

    def _load(self):
        try:
            with open(self.path, "r") as f:
                for line in f:
                    uid = line.strip()
                    if uid and uid not in self._set:
                        self._set.add(uid)
                        self._ids.append(uid)
        except FileNotFoundError:
            pass                # first run -- nothing seen yet
        except OSError as e:
            logger.warning("cannot read seen-store %s: %s -- treating as empty",
                           self.path, e)

    def is_seen(self, unique_id):
        """True if this id was processed before. Always False under --reprocess."""
        if self.reprocess:
            return False
        return bool(unique_id) and unique_id in self._set

    def __len__(self):
        return len(self._ids)

    # -- mutate ---------------------------------------------------------------

    def mark(self, unique_id):
        """Record an id as processed (idempotent). Appends to the file; prunes if over
        the horizon. Fail-soft: a write error is logged, never raised."""
        if not unique_id or unique_id in self._set:
            return
        self._set.add(unique_id)
        self._ids.append(unique_id)
        if len(self._ids) > self.max_ids:
            self._prune()
            return              # _prune rewrites the whole file, incl. this id
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a") as f:
                f.write(unique_id + "\n")
        except OSError as e:
            logger.warning("cannot append to seen-store %s: %s", self.path, e)

    def _prune(self):
        """Keep the newest ``max_ids`` ids; rewrite the file atomically."""
        keep = self._ids[-self.max_ids:]
        self._ids = keep
        self._set = set(keep)
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w") as f:
                f.write("\n".join(keep) + ("\n" if keep else ""))
            os.replace(tmp, self.path)          # atomic
        except OSError as e:
            logger.warning("cannot prune seen-store %s: %s", self.path, e)
            try:
                os.remove(tmp)
            except OSError:
                pass
