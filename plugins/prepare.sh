#!/bin/sh
# Lays out the plugins the agents follow under one folder (the image's /opt/vrixel/plugins):
# Anthropic's Product Management, Engineering and Design plugins from a knowledge-work-plugins
# checkout, and ours. Only their skills and commands are kept. Each plugin also names connectors
# (.mcp.json: Slack, Linear, Atlassian, Notion, GitHub...) and may carry hooks; the agents run
# unattended on product code, so they reach no tool through a plugin. The folder is also a plugin
# marketplace (.claude-plugin/marketplace.json), which is how Codex installs them.
#
#   plugins/prepare.sh <knowledge-work-plugins checkout> <vrixel plugin dir> <output dir>
set -eu
upstream=$1 ours=$2 out=$3
mkdir -p "$out/.claude-plugin"
for p in product-management engineering design; do cp -R "$upstream/$p" "$out/$p"; done
cp -R "$ours" "$out/vrixel"
cp "$upstream/LICENSE" "$out/LICENSE.knowledge-work-plugins"
cp "$(dirname "$0")/marketplace.json" "$out/.claude-plugin/marketplace.json"
for p in product-management engineering design vrixel; do
  rm -rf "$out/$p/.mcp.json" "$out/$p/.app.json" "$out/$p/hooks" "$out/$p/hooks.json"
done
if find "$out" \( -name .mcp.json -o -name .app.json -o -name hooks -o -name hooks.json \) | grep -q .; then
  echo "a plugin still carries connectors or hooks" >&2
  exit 1
fi
