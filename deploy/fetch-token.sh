#!/usr/bin/env bash
# Copies the server's access token to ~/.config/filament-mcp/token on this Mac (mode 600).
set -euo pipefail
HOST="${1:?usage: deploy/fetch-token.sh <ssh-host>}"
OUT="$HOME/.config/filament-mcp/token"
mkdir -p "$(dirname "$OUT")"
umask 077
ssh -t "$HOST" 'umask 077; sudo sed -n "s/^FILAMENT_TOKEN=//p" /etc/filament-mcp.env > ~/.filament-mcp-token'
scp -q "$HOST:.filament-mcp-token" "$OUT"
ssh "$HOST" 'rm -f ~/.filament-mcp-token'
echo "Saved token to $OUT ($(wc -c < "$OUT" | tr -d ' ') bytes)"
