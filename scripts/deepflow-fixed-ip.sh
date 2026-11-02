#!/bin/bash
# deepflow-fixed-ip.sh
#
# Moves the DeepFlow lab onto a fixed private address (10.233.233.1) that exists only
# on this laptop, so home Wi-Fi, the phone hotspot or no network at all make no difference.
#
# What it does:
#   1. creates a permanent dummy interface df0 = 10.233.233.1 (fixed MAC, survives reboots)
#   2. stops DeepFlow and moves the old server data aside (/opt/deepflow/*.bak-<time>)
#   3. starts a clean server on 10.233.233.1
#   4. recreates the legacy-host domain, agent group, group config and the smpp plugin
#   5. points the agent at 10.233.233.1 via df0 and waits for it to register
#   6. restarts the agent once more so it pulls the plugin, and shows the result
#
# Run from the repo root:  bash scripts/deepflow-fixed-ip.sh
# Env:  DF_DIR (deepflow-docker-compose checkout, default ~/deepflow/deepflow-docker-compose)
#       FIXED_IP (default 10.233.233.1)
set -euo pipefail

FIXED_IP=${FIXED_IP:-10.233.233.1}
FIXED_MAC=02:00:0a:e9:e9:01
DF_DIR=${DF_DIR:-$HOME/deepflow/deepflow-docker-compose}
PLUGIN_DIR=$(cd "$(dirname "$0")/.." && pwd)
CONFIG=$PLUGIN_DIR/deploy/agent-group-config.yaml
STATE_DIR=$HOME/.config/deepflow-smpp
STAMP=$(date +%Y%m%d-%H%M%S)
CTL="deepflow-ctl -i $FIXED_IP"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

[ -f "$DF_DIR/docker-compose.yaml" ] || { echo "Can't find $DF_DIR/docker-compose.yaml"; exit 1; }
[ -f "$PLUGIN_DIR/smpp.wasm" ] || { echo "Can't find $PLUGIN_DIR/smpp.wasm - run 'make plugin' first"; exit 1; }
[ -f "$CONFIG" ] || { echo "Can't find $CONFIG"; exit 1; }
sudo -v

say "1/6 Fixed address $FIXED_IP on dummy interface df0"
sudo tee /etc/systemd/system/deepflow-dummy.service > /dev/null << EOF
[Unit]
Description=Fixed local address for the DeepFlow lab ($FIXED_IP on df0)
Before=docker.service deepflow-agent.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'ip link show df0 >/dev/null 2>&1 || ip link add df0 type dummy; ip link set df0 address $FIXED_MAC; ip addr replace $FIXED_IP/32 dev df0; ip link set df0 up'
ExecStop=/bin/sh -c 'ip link del df0 || true'

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now deepflow-dummy.service
ip -4 -br addr show df0

say "2/6 Stop DeepFlow and move old server data aside"
sudo systemctl stop deepflow-agent || true
sudo docker compose -f "$DF_DIR/docker-compose.yaml" down
for d in mysql clickhouse clickhouse_storage; do
  if [ -d "/opt/deepflow/$d" ]; then
    sudo mv "/opt/deepflow/$d" "/opt/deepflow/$d.bak-$STAMP"
    echo "moved /opt/deepflow/$d -> /opt/deepflow/$d.bak-$STAMP"
  fi
done

say "3/6 Start a clean server on $FIXED_IP"
sed -i "s/^NODE_IP_FOR_DEEPFLOW=.*/NODE_IP_FOR_DEEPFLOW=$FIXED_IP/" "$DF_DIR/.env"
grep -E "VERSION|NODE_IP" "$DF_DIR/.env"
sudo docker compose -f "$DF_DIR/docker-compose.yaml" up -d
echo -n "waiting for the server API"
for i in $(seq 1 60); do
  if $CTL agent list > /dev/null 2>&1 && $CTL domain list > /dev/null 2>&1; then echo " ready"; break; fi
  echo -n "."; sleep 5
  [ "$i" = 60 ] && { echo " server did not come up; check: sudo docker logs deepflow-server --tail 50"; exit 1; }
done
sleep 20   # let the server finish registering its own controller and ingester records

say "4/6 Domain, agent group, config and plugin"
printf 'name: legacy-host\ntype: agent_sync\n' | $CTL domain create -f - > /dev/null
$CTL agent-group create legacy-host > /dev/null
GROUP_ID=$($CTL agent-group list legacy-host | awk '$1=="legacy-host"{print $2}')
[ -n "$GROUP_ID" ] || { echo "could not read the agent group ID"; exit 1; }
echo "agent group: $GROUP_ID"
mkdir -p "$STATE_DIR"; echo "$GROUP_ID" > "$STATE_DIR/group-id"
$CTL agent-group-config create "$GROUP_ID" -f "$CONFIG"
$CTL plugin create --type wasm --image "$PLUGIN_DIR/smpp.wasm" --name smpp
$CTL plugin list

say "5/6 Point the agent at $FIXED_IP through df0"
sudo tee /etc/deepflow-agent.yaml > /dev/null << EOF
controller-ips:
  - $FIXED_IP
vtap-group-id-request: '$GROUP_ID'
EOF
sudo mkdir -p /etc/systemd/system/deepflow-agent.service.d
printf '[Unit]\nAfter=deepflow-dummy.service\nRequires=deepflow-dummy.service\n[Service]\nEnvironment=CTRL_NETWORK_INTERFACE=df0\n' \
  | sudo tee /etc/systemd/system/deepflow-agent.service.d/iface.conf > /dev/null
sudo systemctl daemon-reload
sudo systemctl restart deepflow-agent
echo -n "waiting for the agent to register (up to 8 minutes)"
REG=""
for i in $(seq 1 48); do
  if $CTL agent list | awk 'NR>1' | grep -q NORMAL; then REG=yes; echo " registered"; break; fi
  echo -n "."; sleep 10
  # the agent sends full host details only at start-up; nudge it once halfway through
  [ "$i" = 24 ] && sudo systemctl restart deepflow-agent
done
$CTL agent list
[ -n "$REG" ] || { echo "Agent not registered yet. Paste the output of:"; \
  echo "  sudo docker exec deepflow-server sh -c 'tail -n 500 /var/log/deepflow/server.log' | grep -iE 'register|host_device|not found' | tail -12"; exit 1; }

say "6/6 Restart the agent so it pulls the plugin"
sudo systemctl restart deepflow-agent
sleep 60
sudo grep -iE "smpp plugin loaded|plugins pulled|fetch wasm" /var/log/deepflow-agent/deepflow-agent.log | tail -4

say "Done"
echo "Server address from now on: $FIXED_IP   (Grafana: http://$FIXED_IP:3000)"
echo "Agent group: $GROUP_ID   (saved in $STATE_DIR/group-id; use it as make config GROUP_ID=$GROUP_ID)"
echo "Old data kept in /opt/deepflow/*.bak-$STAMP - delete it once you're happy."
