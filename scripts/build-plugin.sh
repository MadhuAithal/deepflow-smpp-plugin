#!/bin/bash
# build-plugin.sh: build smpp.wasm with the toolchain DeepFlow's plugin SDK targets
# (Go 1.21 + TinyGo 0.30) and optionally upload it.
#
# Why a pinned toolchain: with newer Go/TinyGo (e.g. Go 1.27 + TinyGo 0.43) the SDK
# needs the default goroutine scheduler, which inflates the plugin to ~1.4 MB. The
# agent fetches plugins with a 1 MiB message limit, so delivery fails with
# "message length too large". See FINDINGS.md #4.
#
# The pinned versions go into ~/sdk and are used only inside this script.
#
# Run from anywhere:   bash scripts/build-plugin.sh            (build + test)
#                      bash scripts/build-plugin.sh --upload   (also upload, apply config, restart agent)
# Env:  DEEPFLOW_IP (default 10.233.233.1)   GROUP_ID (default: ~/.config/deepflow-smpp/group-id)
set -euo pipefail

GO_VER=1.21.13
TINYGO_VER=0.30.0
SDK=$HOME/sdk
REPO=$(cd "$(dirname "$0")/.." && pwd)
IP=${DEEPFLOW_IP:-10.233.233.1}
CTL="deepflow-ctl -i $IP"

say() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
cd "$REPO"

say "Toolchain: Go $GO_VER and TinyGo $TINYGO_VER in $SDK"
mkdir -p "$SDK"
if [ ! -x "$SDK/go$GO_VER/bin/go" ]; then
  curl -fL -o /tmp/go$GO_VER.tgz "https://go.dev/dl/go$GO_VER.linux-amd64.tar.gz"
  mkdir -p "$SDK/go$GO_VER" && tar -C "$SDK/go$GO_VER" --strip-components=1 -xzf /tmp/go$GO_VER.tgz
fi
if [ ! -x "$SDK/tinygo$TINYGO_VER/bin/tinygo" ]; then
  curl -fL -o /tmp/tinygo$TINYGO_VER.tgz \
    "https://github.com/tinygo-org/tinygo/releases/download/v$TINYGO_VER/tinygo$TINYGO_VER.linux-amd64.tar.gz"
  mkdir -p "$SDK/tinygo$TINYGO_VER" && tar -C "$SDK/tinygo$TINYGO_VER" --strip-components=1 -xzf /tmp/tinygo$TINYGO_VER.tgz
fi
export PATH="$SDK/go$GO_VER/bin:$SDK/tinygo$TINYGO_VER/bin:$PATH"
export GOTOOLCHAIN=local
go version
tinygo version

say "Test the parser"
go mod tidy   # creates go.sum on first run
go test ./smpp

say "Build"
make plugin

[ "${1:-}" = "--upload" ] || { echo; echo "Built. Re-run with --upload to send it to DeepFlow."; exit 0; }

GROUP_ID=${GROUP_ID:-$(cat "$HOME/.config/deepflow-smpp/group-id" 2>/dev/null || true)}
[ -n "$GROUP_ID" ] || { echo "Set GROUP_ID (deepflow-ctl -i $IP agent-group list)"; exit 1; }

say "Upload plugin and apply agent config to $GROUP_ID"
$CTL agent-group-config update "$GROUP_ID" -f deploy/agent-group-config.yaml
$CTL plugin delete smpp 2>/dev/null || true
$CTL plugin create --type wasm --image smpp.wasm --name smpp
$CTL plugin list

say "Restart the agent and check the plugin loads"
sudo systemctl restart deepflow-agent
sleep 75
sudo grep -iE "smpp plugin loaded|plugins pulled|add wasm prog fail|fetch wasm|message length" \
  /var/log/deepflow-agent/deepflow-agent.log | tail -5 | cut -c1-260 || true
echo
echo "Success looks like: '1 wasm and 0 so plugins pulled from server' and 'smpp plugin loaded (protocol 200)'."
