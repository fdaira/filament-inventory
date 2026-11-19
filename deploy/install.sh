#!/usr/bin/env bash
# Runs ON the Linux server as root (via sudo). Expects the app files in $SRC.
# Idempotent: safe to re-run for upgrades; keeps the existing database and token.
set -euo pipefail

SRC="${1:?usage: install.sh <staging-dir>}"
APP=/opt/filament-mcp
ENVFILE=/etc/filament-mcp.env
PORT="${FILAMENT_PORT:-8765}"

command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python >= 3.10 required, found $(python3 --version)"; exit 1; }
python3 -c 'import venv, ensurepip' 2>/dev/null || {
  echo "python3-venv missing; installing"
  if command -v apt-get >/dev/null; then apt-get update -qq && apt-get install -y -qq python3-venv
  elif command -v dnf >/dev/null; then dnf install -y -q python3-virtualenv
  else echo "Install the python3 venv package manually"; exit 1; fi
}

id filament-mcp >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin filament-mcp

DB=/var/lib/filament-mcp/filament.db
if [ -f "$DB" ]; then
  BK="$DB.bak-$(date +%Y%m%d-%H%M%S)"
  cp -p "$DB" "$BK" && echo "Backed up database to $BK"
fi

install -d -m 755 "$APP"
install -m 644 "$SRC/filament_server.py" "$SRC/ui.html" "$SRC/catalog.json" "$SRC/requirements.txt" "$APP/"
[ -d "$APP/.venv" ] || python3 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade pip
"$APP/.venv/bin/pip" install -q -r "$APP/requirements.txt"

if [ ! -f "$ENVFILE" ]; then
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cat > "$ENVFILE" <<EOF
FILAMENT_DB=/var/lib/filament-mcp/filament.db
FILAMENT_HOST=0.0.0.0
FILAMENT_PORT=$PORT
FILAMENT_TOKEN=$TOKEN
EOF
  chmod 600 "$ENVFILE"
  echo "Created $ENVFILE with a new access token"
fi

install -m 644 "$SRC/filament-mcp.service" /etc/systemd/system/filament-mcp.service
systemctl daemon-reload
systemctl enable --now filament-mcp
systemctl restart filament-mcp
sleep 2
systemctl --no-pager --lines=5 status filament-mcp

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT/tcp" comment 'filament-mcp' >/dev/null && echo "ufw: opened $PORT/tcp"
fi

echo
echo "Web UI:   http://$(hostname -f 2>/dev/null || hostname):$PORT/"
echo "MCP:      http://$(hostname -f 2>/dev/null || hostname):$PORT/mcp"
echo "Token:    sudo grep FILAMENT_TOKEN $ENVFILE"
