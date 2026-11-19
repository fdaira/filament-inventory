#!/usr/bin/env bash
# Run from your Mac: copies the app to the server and installs/upgrades the service.
#   ./deploy/deploy.sh <ssh-host>     e.g. ./deploy/deploy.sh myserver
set -euo pipefail
HOST="${1:?usage: deploy/deploy.sh <ssh-host>}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="/tmp/filament-mcp-deploy"

ssh "$HOST" "rm -rf $STAGE && mkdir -p $STAGE"
scp -q "$HERE/filament_server.py" "$HERE/ui.html" "$HERE/catalog.json" "$HERE/requirements.txt" \
       "$HERE/deploy/filament-mcp.service" "$HERE/deploy/install.sh" "$HOST:$STAGE/"
# -t so sudo can ask for your password
ssh -t "$HOST" "sudo bash $STAGE/install.sh $STAGE && rm -rf $STAGE"
