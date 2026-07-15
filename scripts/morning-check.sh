#!/usr/bin/env bash
# morning-check.sh — one-shot status of the sage-yolo2 2.1.0 count+crop deploy.
# Run: ssh beckman@node-H00F.sage 'bash -s' < morning-check.sh    (or copy + run on node)
set -uo pipefail
echo "================ sage-yolo2 2.1.0 count+crop — morning check ================"
date -u

echo; echo "== 1. pods (uptime, restarts; producer + consumer both Running, 0 restarts) =="
sudo pluginctl ps 2>/dev/null
sudo kubectl get pods -n default 2>/dev/null | grep -iE "NAME|yolo|sampler"

echo; echo "== 2. errors/tracebacks in the consumer log (expect none) =="
sudo kubectl logs sage-yolo2-consumer -n default 2>/dev/null \
  | grep -iE "error|traceback|exception|OOM|CUDA|failed" | grep -vi "half.*deprecated" | tail -15 \
  || echo "  (none)"

echo; echo "== 3. crop production — did any birds get cropped? =="
echo "-- log evidence --"
sudo kubectl logs sage-yolo2-consumer -n default 2>/dev/null \
  | grep -iE "BIRD|Produced .* crop|Published env.count.bird|skip crop" | tail -20 \
  || echo "  no bird/crop log lines yet"
echo "-- crop cache on disk --"
if sudo test -d /media/plugin-data/local-cache/hummingcam-crops; then
  sudo find /media/plugin-data/local-cache/hummingcam-crops -name '*.jpg' 2>/dev/null \
    | awk -F/ '{s[$(NF-1)]++} END{for(k in s) printf "  %-24s %d crops\n", k, s[k]; if(!length(s)) print "  (dir exists, no crops yet)"}'
else
  echo "  crop cache dir not created yet (no bird >=0.5 conf seen since deploy)"
fi

echo; echo "== 4. sample crop metadata (proves valid v2 frame + provenance) =="
sample="$(sudo find /media/plugin-data/local-cache/hummingcam-crops -name '*.jpg' 2>/dev/null | head -1)"
if [ -n "$sample" ]; then
  echo "  sample: $sample"
  sudo python3 - "$sample" <<'PY'
import sys, json, piexif
p = sys.argv[1]
d = piexif.load(p)
uc = d["Exif"].get(piexif.ExifIFD.UserComment, b"")
if uc[:8] == b"ASCII\x00\x00\x00": uc = uc[8:]
j = json.loads(uc.decode("ascii"))
print("   v2 name    :", p.split("/")[-1])
print("   capture_ts :", j.get("capture_timestamp_ns"), "(inherited from parent frame)")
print("   vsn/plugin :", j.get("vsn"), "/", j.get("plugin"))
src = j.get("source", {})
print("   source     : class=%s conf=%.3f bbox=%s idx=%s parent_uid=%s" % (
    src.get("source_class"), src.get("source_confidence", 0),
    src.get("source_bbox"), src.get("detection_index"),
    (src.get("source_unique_id","")[:12] + "...") if src.get("source_unique_id") else ""))
PY
else
  echo "  (no crop yet to sample)"
fi

echo; echo "== 5. data API — counting (2.1.0) still frame-anchored, last 12h summary =="
curl -s -X POST https://data.sagecontinuum.org/api/v1/query -H 'Content-Type: application/json' \
  -d '{"start":"-12h","filter":{"vsn":"H00F","name":"env.count.bird"}}' 2>/dev/null \
  | python3 -c "import sys;L=[l for l in sys.stdin if l.strip()];print('  env.count.bird records (birds counted):',len(L))" 2>/dev/null || echo "  query failed"
curl -s -X POST https://data.sagecontinuum.org/api/v1/query -H 'Content-Type: application/json' \
  -d '{"start":"-12h","filter":{"vsn":"H00F","name":"env.crop.count"}}' 2>/dev/null \
  | python3 -c "import sys,json;L=[json.loads(l) for l in sys.stdin if l.strip()];print('  env.crop.count records:',len(L),'| total crops:',sum(int(r['value']) for r in L))" 2>/dev/null || echo "  no env.crop.count yet"
curl -s -X POST https://data.sagecontinuum.org/api/v1/query -H 'Content-Type: application/json' \
  -d '{"start":"-1h","filter":{"vsn":"H00F","name":"env.count.total"}}' 2>/dev/null \
  | tail -1 | python3 -c "import sys,json;
l=sys.stdin.read().strip()
print('  latest env.count.total plugin:', json.loads(l)['meta']['plugin']) if l else print('  (no recent count record)')" 2>/dev/null

echo; echo "================ end morning check ================"
