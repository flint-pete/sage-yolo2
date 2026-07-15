# Overnight Deploy Plan — sage-yolo2 2.1.0 (count + crop-produce) on H00F

Date: 2026-07-15 (overnight run for Pete's morning review).
Goal: replace the live 2.0.0 bird-counting consumer with 2.1.0, which does BOTH
the existing bird counting AND crops each detected bird into the local-cache.

## Ground truth (probed live, 2026-07-15 01:2x UTC)

- Node H00F (Thor/arm64), `ssh beckman@node-H00F.sage`, passwordless sudo.
- Running (pluginctl-managed), both up 23h:
  - `hummingcam-producer` — image-sampler2, writes frames to
    `/local-cache/hummingcam/top` (`--cache-name hummingcam --name top`, cap 500).
    ** MUST NOT TOUCH — it feeds the whole pipeline.**
  - `sage-yolo2-consumer` — 2.0.0, args:
    `--source cache --input /local-cache/hummingcam/top --every 10m --all-unseen
     --max-frames 0 --model yolo11x.pt --conf-thres 0.25 --classes bird
     --save-match bird:0.4`, resources `limit.memory=16Gi,request.memory=4Gi`,
    `-v /media/plugin-data/local-cache:/local-cache`. **This is what we replace.**
- k3s has only `...sage-yolo2:2.0.0` imported; 2.1.0 not built yet.
- Node repo `~/AI-projects/sage-yolo2` is at 2.0.0 (`ed86a6f`); needs `git pull`
  to `a356824` (v2.1.0).
- Headroom: ~70 GB RAM free, GPU idle, cache full (500 frames). Fine.
- Deploy path (from HANDOFF/DOCKER-BUILD): native Thor build → `k3s ctr import`
  → `pluginctl run` (bypasses ECR catalog; no SES needed). `-v` requires
  `--selector zone=core`; GPU pod requires `--resource limit.memory=16Gi`.

## Plan (each step verified before the next)

1. **Pull 2.1.0 on the node.** `cd ~/AI-projects/sage-yolo2 && git pull` →
   confirm HEAD `a356824`, VERSION `2.1.0`. (Read-only until confirmed.)

2. **Build + import 2.1.0** via the repo's own script:
   `scripts/deploy-sideload.sh --skip-register` (build natively arm64 → import to
   k3s). Skip-register because pluginctl run needs no catalog. Verify the tag
   `registry.sagecontinuum.org/beckman/sage-yolo2:2.1.0` shows in `k3s ctr images ls`.
   (This is the long step — CUDA base, ~10 GiB image, several minutes.)

3. **Stop the old consumer ONLY.** `sudo pluginctl rm sage-yolo2-consumer`.
   Leave `hummingcam-producer` running. Confirm producer still Running and the
   consumer is gone (`pluginctl ps`).

4. **Launch 2.1.0** with the SAME counting args + the crop flags added:
   ```
   sudo pluginctl run --name sage-yolo2-consumer \
     --selector zone=core \
     --resource limit.memory=16Gi,request.memory=4Gi \
     -v /media/plugin-data/local-cache:/local-cache \
     -e WAGGLE_JOB_NAME=hummingcam -e WAGGLE_TASK_NAME=sage-yolo2 \
     registry.sagecontinuum.org/beckman/sage-yolo2:2.1.0 -- \
     --source cache --input /local-cache/hummingcam/top \
     --every 10m --all-unseen --max-frames 0 \
     --model yolo11x.pt --conf-thres 0.25 --classes bird \
     --save-match "bird:0.4" \
     --crop-match "bird:0.5" --crop-padding 0.15 \
     --crop-cache-name hummingcam-crops \
     --crop-max-count 500 --crop-max-mb 500
   ```
   Rationale for the crop params:
   - `--crop-match bird:0.5` — crop birds at ≥0.5 conf (a touch stricter than the
     0.4 save threshold: only crop birds we're fairly sure are birds, since crops
     feed a classifier).
   - `--crop-padding 0.15`, `--crop-min-px 32` (default) — design defaults.
   - `--crop-cache-name hummingcam-crops` — crops land in
     `/local-cache/hummingcam-crops/top-crop-<idx>/`, a NEW dir separate from the
     raw `hummingcam` stream, so a future BioCLIP consumer reads only crops.
   - Rings 500/500 — same as the raw cache; safe default until a consumer's drain
     cadence is known.

5. **Verify it's alive and correct** (first ~15 min):
   - `pluginctl ps` → consumer Running.
   - Logs show `crop-producer ON: rules=bird:0.5 ...` at startup (proves the new
     flag path is active) and the model loaded.
   - On the next bird: log line `Produced N crop(s) into hummingcam-crops/top-crop-*`.
   - `env.count.total` still publishing (counting unbroken) via the data API.

6. **Let it run overnight.** Every 10 min it wakes, counts, and crops any bird.

## Morning verification (what I'll collect for Pete)

- `pluginctl ps` uptime + restart count (0 = clean).
- Crop cache: `ls /local-cache/hummingcam-crops/*/` counts + a sample crop's
  metadata read-back (v2 name, capture_ts, `source{}` provenance) to prove the
  crops are valid and frame-anchored.
- Data API: `env.count.*` (counting still works) and `env.crop.count` (crops
  observable in the data plane), both frame-anchored, `meta.plugin=...:2.1.0`.
- Any errors/tracebacks in the journal (expect none; per-crop failures are
  fail-soft anyway).

## Safety / rollback

- Only `sage-yolo2-consumer` is touched; producer untouched; no SES jobs resumed;
  plebbyd's 5606 untouched.
- Crops write to a NEW cache dir — cannot corrupt the raw `hummingcam` stream.
- Off-nominal rollback: `sudo pluginctl rm sage-yolo2-consumer` then re-run the
  2.0.0 line (image still imported) — one command, back to prior state.
- The 2.0.0 image stays in k3s (not deleted), so rollback needs no rebuild.
