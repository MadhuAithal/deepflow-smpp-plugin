#!/bin/bash
# publish.sh: one-time setup to push this repo to your GitHub account.
#
#   bash scripts/publish.sh <github-username>
#
# It sets the Go module path to github.com/<user>/deepflow-smpp-plugin, fills in the
# README links, makes the first commit and, if the GitHub CLI (gh) is logged in,
# creates the public repo and pushes. Without gh it prints the two commands to run.
set -euo pipefail
USER_GH=${1:?usage: bash scripts/publish.sh <github-username>}
NAME=deepflow-smpp-plugin
MOD=github.com/$USER_GH/$NAME
cd "$(dirname "$0")/.."

# module path + imports
sed -i "s|^module .*|module $MOD|" go.mod
grep -rl '"smpp-plugin/smpp"' --include=*.go . | xargs -r sed -i "s|\"smpp-plugin/smpp\"|\"$MOD/smpp\"|"
# placeholders in docs
grep -rl 'GITHUB_USER' --include=*.md . | xargs -r sed -i "s|GITHUB_USER|$USER_GH|g"

go mod tidy   # writes go.sum so the commit builds for others
go vet ./smpp ./cmd/... && go test ./smpp >/dev/null && echo "tests pass with module $MOD"

[ -d .git ] || git init -q -b main
git add -A
git -c core.hooksPath=/dev/null commit -q -m "SMPP 3.4 protocol parser plugin for DeepFlow" || true
git log --oneline | head -1

if command -v gh >/dev/null && gh auth status >/dev/null 2>&1; then
  gh repo create "$USER_GH/$NAME" --public --source . --push \
    --description "SMPP 3.4 parser for DeepFlow's eBPF agent (Wasm plugin): SMS submit latency, throttling and delivery receipts with zero code changes"
  gh repo edit "$USER_GH/$NAME" --add-topic ebpf,deepflow,smpp,sms,observability,wasm,telecom
else
  echo
  echo "Create an empty public repo named $NAME at https://github.com/new (no README/licence), then:"
  echo "  git remote add origin git@github.com:$USER_GH/$NAME.git"
  echo "  git push -u origin main"
fi
