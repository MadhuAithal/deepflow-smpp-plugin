#!/bin/bash
# sanity.sh: is the SMPP lab healthy? Run after a reboot or an OS upgrade.
# Env: DEEPFLOW_IP (default 10.233.233.1)
IP=${DEEPFLOW_IP:-10.233.233.1}
REPO=$(cd "$(dirname "$0")/.." && pwd)
ok()  { printf '  \033[32m✅\033[0m %s\n' "$*"; }
bad() { printf '  \033[31m❌\033[0m %s\n' "$*"; FAIL=1; }
chk() { local msg=$1; shift; if "$@" >/dev/null 2>&1; then ok "$msg"; else bad "$msg"; fi; }
FAIL=0
D=docker; docker info >/dev/null 2>&1 || D="sudo docker"

echo "System"
chk "kernel BTF present (/sys/kernel/btf/vmlinux)" test -r /sys/kernel/btf/vmlinux
chk "docker running" $D info
echo "Network"
chk "fixed address $IP is up" sh -c "ip -4 -br addr | grep -q '$IP'"
echo "DeepFlow server"
for c in deepflow-server deepflow-app deepflow-mysql deepflow-clickhouse deepflow-grafana; do
  chk "container $c running" sh -c "$D ps --format '{{.Names}}' | grep -q '^$c\$'"
done
chk "server API answers" deepflow-ctl -i "$IP" agent list
chk "querier answers (port 20416)" curl -fsS -XPOST "http://$IP:20416/v1/query/" \
  --data-urlencode db=flow_log --data-urlencode "sql=SELECT Count(row) FROM l7_flow_log LIMIT 1"
echo "DeepFlow agent"
chk "agent service active" systemctl is-active --quiet deepflow-agent
chk "agent registered (NORMAL)" sh -c "deepflow-ctl -i '$IP' agent list | grep -q NORMAL"
chk "smpp plugin uploaded" sh -c "deepflow-ctl -i '$IP' plugin list | grep -q smpp"
chk "agent loaded the plugin" sudo grep -q "smpp plugin loaded" /var/log/deepflow-agent/deepflow-agent.log
echo "Toolchain and lab"
chk "pinned Go 1.21 in ~/sdk"     test -x "$HOME/sdk/go1.21.13/bin/go"
chk "pinned TinyGo 0.30 in ~/sdk" test -x "$HOME/sdk/tinygo0.30.0/bin/tinygo"
chk "smpp.wasm built and < 1 MiB" sh -c "[ -f '$REPO/smpp.wasm' ] && [ \$(stat -c %s '$REPO/smpp.wasm') -lt 1040000 ]"
chk "python smpplib available" sh -c "python3 -c 'import smpplib' || ~/smpp-venv/bin/python -c 'import smpplib'"

echo
if [ $FAIL = 0 ]; then echo "All good."; else echo "Fix the ❌ items above."; fi
cat <<'NOTE'

Note: after a reboot, restart the agent once before testing
  (sudo systemctl restart deepflow-agent). The eBPF tracer can come back
  without the port 2775 whitelist, and then no SMPP rows appear. FINDINGS.md #5.
NOTE
exit $FAIL
