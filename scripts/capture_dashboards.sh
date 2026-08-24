#!/usr/bin/env bash
# Render dashboards to PNG so the evidence lives in the repo rather than only in a
# running Grafana. Regenerating is a single command, which matters because a
# screenshot nobody can reproduce is an assertion, not evidence.
#
#   bash scripts/capture_dashboards.sh [FROM_MS] [TO_MS]
#
# Defaults to the window of the most recent recorded run.
set -euo pipefail

GRAFANA="${GRAFANA_URL:-http://localhost:3000}"
OUT="${OUT_DIR:-docs/evidence}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
mkdir -p "$OUT"

if [ $# -ge 2 ]; then
  FROM="$1"; TO="$2"
else
  read -r FROM TO <<<"$(python3 - <<'PY'
import glob, json, os
# Prefer the soak manifest; fall back to the newest manifest on disk.
cands = sorted(glob.glob("results/real/*-manifest.json"), key=os.path.getmtime)
soak = [c for c in cands if "soak" in c] or cands
m = json.load(open(soak[-1]))
print(int((m["started_at"] - 30) * 1000), int((m["finished_at"] + 30) * 1000))
PY
)"
fi

echo "window: $FROM -> $TO"

render() {
  local uid="$1" slug="$2" h="${3:-1400}"
  local url="$GRAFANA/render/d/$uid/$slug?orgId=1&from=$FROM&to=$TO&width=1600&height=$h&theme=dark&kiosk=1"
  printf '  %-28s ' "$slug.png"
  # Rendering a tall dashboard takes a while; the renderer streams nothing until done.
  if curl -sf --max-time 180 "$url" -o "$OUT/$slug.png" && [ -s "$OUT/$slug.png" ]; then
    echo "$(du -h "$OUT/$slug.png" | cut -f1)"
  else
    echo "FAILED"
    rm -f "$OUT/$slug.png"
  fi
}

render takehome-soak-evidence soak-evidence 1700
render takehome-overview overview 2600
render takehome-cost cost 1000

echo
echo "wrote to $OUT/"
ls -la "$OUT"/*.png 2>/dev/null | awk '{printf "  %s  %s\n", $5, $9}' || echo "  (nothing rendered)"
