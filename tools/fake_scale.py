"""A stand-in for the Wi-Fi scale, for trying the app without hardware.

It answers like ESPHome's web server does for the scale config in esphome/:
    GET  /sensor/Spool Weight     -> {"id": "sensor-spool_weight", "value": 1234.5, "state": "1234.5 g"}
    POST /button/Tare/press       -> zero the scale
and adds a control:
    GET  /set?g=1234              -> put 1234 g on the scale
    GET  /set?g=1234&settle=3     -> ...with a wobbly reading for 3 seconds
    GET  /                        -> small page with the current reading

    python3 tools/fake_scale.py [port]       (default 8099)
    then run the server with FILAMENT_SCALE_URL=http://127.0.0.1:8099
"""

import json
import random
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

state = {"gross": 0.0, "offset": 0.0, "settle_until": 0.0}


def reading() -> float:
    noise = random.uniform(-25, 25) if time.time() < state["settle_until"] else random.uniform(-0.4, 0.4)
    return round(state["gross"] - state["offset"] + noise, 1)


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: dict | str) -> None:
        data = (json.dumps(body) if isinstance(body, dict) else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if isinstance(body, dict) else "text/html")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        path = unquote(u.path)
        if path == "/sensor/Spool Weight":
            v = reading()
            self._send(200, {"id": "sensor-spool_weight", "value": v, "state": f"{v:.1f} g"})
        elif path == "/set":
            q = parse_qs(u.query)
            state["gross"] = float(q.get("g", ["0"])[0])
            state["settle_until"] = time.time() + float(q.get("settle", ["0"])[0])
            self._send(200, {"gross_g": state["gross"]})
        elif path == "/":
            self._send(200, f"<h1>Fake scale</h1><p>Reading: {reading()} g</p>")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if unquote(urlparse(self.path).path) == "/button/Tare/press":
            state["offset"] = state["gross"]
            self._send(200, "")
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8099
    print(f"Fake scale on http://127.0.0.1:{port}  (set weight: /set?g=1234)")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
