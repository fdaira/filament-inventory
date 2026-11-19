#!/usr/bin/env bash
# Point the inventory server at the Wi-Fi scale (or remove it) and restart the service.
#   ./deploy/set-scale.sh <ssh-host> http://192.168.1.50     use this scale
#   ./deploy/set-scale.sh <ssh-host> none                    no scale
set -euo pipefail
HOST="${1:?usage: deploy/set-scale.sh <ssh-host> <scale-url|none>}"
URL="${2:?usage: deploy/set-scale.sh <ssh-host> <scale-url|none>}"
if [ "$URL" != "none" ] && ! [[ "$URL" =~ ^https?://[A-Za-z0-9.:-]+/?$ ]]; then
  echo "Scale URL should look like http://192.168.1.50 or http://filament-scale.local" >&2
  exit 1
fi
LINE=""
[ "$URL" != "none" ] && LINE="FILAMENT_SCALE_URL=${URL%/}"
# -t so sudo can ask for your password
ssh -t "$HOST" "sudo sed -i '/^FILAMENT_SCALE_URL=/d' /etc/filament-mcp.env \
  && { [ -z '$LINE' ] || echo '$LINE' | sudo tee -a /etc/filament-mcp.env >/dev/null; } \
  && sudo systemctl restart filament-mcp && sleep 2 \
  && { [ -z '$LINE' ] && echo 'Scale removed.' || \
       { echo 'Scale set to ${URL%/}; test reading from the server:'; \
         curl -s -m 5 '${URL%/}/sensor/Spool%20Weight' || echo '(scale did not answer)'; echo; }; }"
