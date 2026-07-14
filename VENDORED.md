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
