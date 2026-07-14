# Vendored code

## `node_info.py` ← pywaggle2-nodeinfo

`node_info.py` is a vendored copy of the pywaggle2 node-identity reader
(`waggle/data/node_info_env.py`) from the **pywaggle2-nodeinfo** repo.

| | |
|---|---|
| Source repo | `pywaggle2-nodeinfo` |
| Source path | `waggle/data/node_info_env.py` |
| Vendored at | `v0.1.0` (commit `4f3e589`) |
| Local path | `node_info.py` (repo root) |
| Import | `from node_info import read_node_info, NodeInfo` |

### Why vendored (not a pip dependency)
pywaggle2 is not yet pip-installable upstream. Vendoring keeps sage-yolo2
self-contained and matches image-sampler2's pattern (which mirrors the same
contract in its `nodemeta.py`).

### Why at repo root, NOT under `waggle/`
A repo-root `waggle/` package would **shadow the installed pywaggle** on
`sys.path` and break `from waggle.plugin import Plugin` /
`from waggle.data.vision import Camera` when running from the repo directory. So
the reader lives at repo root as `node_info.py`. The file *content* (below the
vendoring header) is byte-identical to the source.

### Sync obligation
If `pywaggle2-nodeinfo/waggle/data/node_info_env.py` changes, re-vendor and bump
the ref in this file and in the header comment of `node_info.py`. To verify the
body is still in sync (skipping each file's header):

```sh
diff <(tail -n +11 node_info.py) \
     <(tail -n +2 ../pywaggle2-nodeinfo/waggle/data/node_info_env.py)
```

(`+11` skips our shebang + 9-line vendoring header; `+2` skips the source shebang.
Adjust if the header length changes.)

## `crop_writer.py` ← image-sampler2 (metadata.py + cache.py)

`crop_writer.py` is the WRITE side of the v2 cache contract, vendored from
image-sampler2 so sage-yolo2 can act as a crop **producer** (the detect→classify
cascade feeding a downstream classifier like BioCLIP — see
`CROP-PRODUCER-Design.md`).

| | |
|---|---|
| Source repo | `image-sampler2` |
| Source paths | `metadata.py` (EXIF/UserComment embed) + `cache.py` (ring/eviction) |
| Local path | `crop_writer.py` (repo root) |
| Import | `import crop_writer` |

### Why vendored (not a shared package)
Per the CROP-PRODUCER-Design.md OPEN DECISION → **(B) vendored copy**: same
precedent as `node_info.py` above and image-sampler2's own `save_match.py` mirror.
It keeps the crop feature a single-repo change (no image-sampler2 edit) at the
cost of manual sync. A shared package (option A) is the cleaner long-term move if
these ever diverge enough to warrant it.

### What was adapted (NOT byte-identical)
Unlike `node_info.py`, `crop_writer.py` is a **curated merge**, not a verbatim
copy — it combines only the write-side pieces of two source files and adds the
crop-producer extensions. Divergences from the source:
- Merges `metadata.embed_all/build_exif_bytes` + `cache.scan_ring/plan_evictions/
  commit_capture` into one module; drops the read-side + config-probe helpers
  (sage-yolo2 already has those in `consumer.py`).
- `build_field_dict` gains an optional nested `source` object (crop provenance:
  source_class/confidence/bbox/unique_id + detection_index).
- Adds `write_frame()` (scan→plan→atomic tmp→commit one-call helper).
- EXIF Make is fixed to `Sage/Waggle` and Software/human strings say `sage-yolo2
  crop` (the source stamps `image-sampler2`).

### Sync obligation
The v2 FORMAT must stay byte-compatible with what image-sampler2 writes and what
sage-yolo2's own `consumer.py` reads — a crop is only useful if a v2 consumer can
read it (proven by `tests/test_crop_writer.py::test_crop_readable_by_consumer`
and `tests/test_crop_e2e.py`). If image-sampler2's `metadata.py` v2 field set,
filename scheme, or EXIF mapping changes, mirror it here AND in `consumer.py`, and
re-run `make test`. There is no automated diff (this is a merge, not a mirror);
the tests are the contract guard.
