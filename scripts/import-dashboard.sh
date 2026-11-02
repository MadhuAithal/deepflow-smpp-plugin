#!/bin/bash
# import-dashboard.sh: load the "SMPP delivery" dashboard into DeepFlow's Grafana.
#
#   bash scripts/import-dashboard.sh
#
# Env: DEEPFLOW_IP (default 10.233.233.1)  GRAFANA_USER / GRAFANA_PASSWORD
#      (default admin / deepflow, DeepFlow's Docker Compose defaults)
# Re-running replaces the dashboard with the version in deploy/.
set -euo pipefail
cd "$(dirname "$0")/.."
IP=${DEEPFLOW_IP:-10.233.233.1}
URL="http://$IP:3000"
python3 -c 'import json,sys; print(json.dumps({"dashboard": json.load(open(sys.argv[1])), "overwrite": True}))' \
  deploy/grafana-smpp-dashboard.json \
| curl -sS -u "${GRAFANA_USER:-admin}:${GRAFANA_PASSWORD:-deepflow}" -H 'Content-Type: application/json' \
    -X POST "$URL/api/dashboards/db" --data-binary @- \
| python3 -c 'import json,sys; r=json.load(sys.stdin); print(r.get("status"), r.get("message",""))'
echo "Open: $URL/d/smpp-delivery"
