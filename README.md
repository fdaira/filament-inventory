# Filament Inventory

A self-hosted inventory for 3D-printer filament: a small **web app** for your phone or desktop,
plus an **MCP server** so Claude (or any MCP client) can read and update the same inventory.

- **Load filament** — brand, type, color, quantity, weight, location. Type or scan the **SKU**
  printed on the spool and everything fills itself in.
- **Find & unload** — search by brand, type, color, SKU, location or printer; move a spool onto a
  named printer and return it to stock with the grams left.
- **Color catalog** — official color names, hex swatches and SKUs for **Bambu Lab, Snapmaker,
  Sunlu, Polymaker and Geeetech** (about 1,800 colors and over 2,200 SKUs, including refills and
  2.85 mm variants).
- **Wi-Fi scale (optional)** — put a spool on a DIY ESP32 scale, tap **Weigh**, and the grams left
  (reading minus the empty spool) are saved; each decrease is logged as usage.
- **Phone-friendly** — works in iPhone Safari and Android Chrome; use *Add to Home Screen* to open
  it full-screen like an app.
- **Claude integration** — ask "how much black PLA do I have?", "put the blue Polymaker on the
  X1C", or "add a spool of CA04076".

One Python process serves everything: the web UI at `/`, a JSON API at `/api/*` and the MCP
endpoint at `/mcp`. Data lives in a single SQLite file.

## Quick start (local)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
FILAMENT_TOKEN=choose-a-secret .venv/bin/python filament_server.py --http
```

Open <http://localhost:8765/> and enter the token. Without `--http` the server speaks MCP over
stdio instead (for local MCP clients).

| Variable | Default | Meaning |
|---|---|---|
| `FILAMENT_DB` | `~/.local/share/filament-inventory/filament.db` | SQLite database file |
| `FILAMENT_HOST` | `0.0.0.0` | HTTP bind address |
| `FILAMENT_PORT` | `8765` | HTTP port |
| `FILAMENT_TOKEN` | *(unset)* | Bearer token required for `/api` and `/mcp` (strongly recommended) |
| `FILAMENT_SCALE_URL` | *(unset)* | Wi-Fi scale address, e.g. `http://192.168.1.50` (enables Weigh) |
| `FILAMENT_SCALE_SENSOR` / `FILAMENT_SCALE_TARE` | `Spool Weight` / `Tare` | Names of the scale's sensor and tare button |

## Deploy to a Linux server (systemd)

From your computer, with SSH access and `sudo` on the server:

```bash
./deploy/deploy.sh myserver
```

This copies the app to `/opt/filament-mcp`, creates a `filament-mcp` system user and a hardened
systemd service, stores the database in `/var/lib/filament-mcp/` (backed up on every deploy),
and generates an access token in `/etc/filament-mcp.env` (root-only). Re-running it upgrades the
code and keeps your data and token.

```bash
ssh myserver sudo grep FILAMENT_TOKEN /etc/filament-mcp.env   # show the token
./deploy/fetch-token.sh myserver                              # or copy it to ~/.config/filament-mcp/token
```

Logs: `journalctl -u filament-mcp -f`.

## Connect Claude Code

```bash
claude mcp add --transport http --scope user filament http://myserver:8765/mcp --header "Authorization: Bearer <TOKEN>"
```

### MCP tools

| Tool | What it does |
|---|---|
| `add_spool` | Add spool(s): material, color, brand, weight, location, SKU, `count` |
| `list_spools` | Search, filter by brand/type/color, in stock vs on a printer, low stock |
| `get_spool` | One spool with usage and printer history |
| `use_filament` | Deduct grams after a print |
| `unload_spool` / `return_spool` | Put a spool on a printer / back into stock |
| `update_spool` / `remove_spool` | Edit or delete a spool |
| `inventory_summary` | Spools and grams per material, color, or brand |
| `catalog_colors` | Look up catalog colors by brand, type, name or **SKU** |
| `weigh_spool` | Weigh a spool on the Wi-Fi scale and save grams left (optionally return it to stock) |
| `read_scale` / `tare_scale` | Read or zero the Wi-Fi scale |

## Wi-Fi scale (optional)

A ~$30 DIY scale lets you weigh a spool when it comes off the printer. The server reads it over
your network, so it works from any phone (iPhone Safari can't talk to Bluetooth scales).

**Parts:** an ESP32-WROOM-32 dev board, a **5 kg bar load cell with an HX711** amplifier board,
4 female-to-female jumper wires, a USB power supply, and a printed base and spool cradle (search
Printables or MakerWorld for "filament scale HX711").

**Wiring**

| Load cell wire | HX711 | | HX711 | ESP32 |
|---|---|---|---|---|
| red | E+ | | VCC | 3V3 |
| black | E− | | GND | GND |
| white | A− | | DT (DOUT) | GPIO16 |
| green | A+ | | SCK (CLK) | GPIO17 |

Mount the load cell with one end fixed to the base and the other end holding the spool cradle
(the arrow on the cell points down), so the bar can flex.

**Flash the ESP32** (once, over USB) with [ESPHome](https://esphome.io):

```bash
pip install esphome
cp esphome/secrets.example.yaml esphome/secrets.yaml   # then put your Wi-Fi name and password in it
esphome run esphome/filament-scale.yaml                 # first build downloads tools; takes a few minutes
```

The log shows the scale's IP address. Give it a fixed address in your router (DHCP reservation)
so it doesn't change. Later updates can be sent over Wi-Fi with the same command.

**Calibrate** in a browser at `http://<scale-ip>/`:
1. With nothing on the scale, press **Tare**.
2. Put something of known weight on it (a calibration weight, or an item weighed on a kitchen
   scale), type its grams into **Calibration Weight**, and press **Calibrate**.
3. Check that **Spool Weight** now shows the right number. The calibration survives restarts.

**Connect it to the server** and restart the service:

```bash
./deploy/set-scale.sh myserver http://192.168.1.50
```

The app then shows **⚖ Weigh** on spools in stock, and weighs automatically when you tap
**Return to stock**. Grams left = scale reading − empty spool. Empty-spool weights come from the
catalog (Bambu Lab 250 g, Sunlu 189 g, Geeetech 180 g, Snapmaker 154 g, Polymaker 140 g cardboard)
and can be edited per spool; refills have none, so weigh your reusable spool empty once and
enter it. To try everything without hardware, run `python3 tools/fake_scale.py` and set
`FILAMENT_SCALE_URL=http://127.0.0.1:8099`.

## Color catalog

`catalog.json` is generated by `tools/build_catalog.py`; re-run it to pick up new colors:

```bash
python3 tools/build_catalog.py
```

Sources (names, hex codes and SKUs only):

- **Bambu Lab** — `filaments_color_codes.json` from [Bambu Studio](https://github.com/bambulab/BambuStudio)
- **Snapmaker, Sunlu, Polymaker, Geeetech** — the public product listings of their online stores
  (for Geeetech, the 5-digit color code inside each store SKU, e.g. `70426`)
- **Hex codes** for Snapmaker, Sunlu, Polymaker and Geeetech — [SpoolmanDB](https://github.com/Donkie/SpoolmanDB)
  (MIT License, Copyright (c) Daniel Hultgren)

Brand names and trademarks belong to their owners; this project is not affiliated with them.
Store data can be incomplete or change at any time — some colors have no SKU or no hex code.

## Security

The HTTP endpoint is plain HTTP protected by a bearer token, which suits a home network. Don't
expose port 8765 to the internet; put it behind a TLS reverse proxy or a VPN such as Tailscale if
you need remote access.

## License

[MIT](LICENSE)
