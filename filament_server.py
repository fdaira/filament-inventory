"""Filament Inventory MCP server.

Tracks 3D-printer filament spools (material type, color, brand, remaining grams)
in a local SQLite database.

Run modes:
  python filament_server.py                 # stdio (local, or over `ssh host ...`)
  python filament_server.py --http          # HTTP: web UI at /, JSON API at /api, MCP at /mcp

Environment:
  FILAMENT_DB     path to the SQLite file   (default ~/.local/share/filament-inventory/filament.db)
  FILAMENT_HOST   HTTP bind address         (default 0.0.0.0)
  FILAMENT_PORT   HTTP port                 (default 8765)
  FILAMENT_TOKEN  if set, HTTP clients must send "Authorization: Bearer <token>"
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

DB_PATH = Path(
    os.environ.get("FILAMENT_DB", "~/.local/share/filament-inventory/filament.db")
).expanduser()

SCHEMA = """
CREATE TABLE IF NOT EXISTS spools (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    material         TEXT    NOT NULL,              -- PLA, PETG, ABS, TPU, ASA, ...
    color            TEXT    NOT NULL,
    brand            TEXT    NOT NULL DEFAULT '',
    diameter_mm      REAL    NOT NULL DEFAULT 1.75,
    net_weight_g     REAL    NOT NULL DEFAULT 1000, -- filament weight when full
    remaining_g      REAL    NOT NULL,
    location         TEXT    NOT NULL DEFAULT '',
    notes            TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS spool_events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    spool_id  INTEGER NOT NULL REFERENCES spools(id) ON DELETE CASCADE,
    action    TEXT    NOT NULL,                     -- unload | return
    printer   TEXT    NOT NULL DEFAULT '',
    at        TEXT    NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    spool_id  INTEGER NOT NULL REFERENCES spools(id) ON DELETE CASCADE,
    grams     REAL    NOT NULL,
    job       TEXT    NOT NULL DEFAULT '',
    at        TEXT    NOT NULL
);
"""

UPDATABLE = {"material", "color", "color_hex", "sku", "brand", "diameter_mm", "net_weight_g",
             "remaining_g", "location", "notes"}

# Columns added after the first release; created on startup if missing.
MIGRATIONS = {
    "color_hex": "ALTER TABLE spools ADD COLUMN color_hex TEXT NOT NULL DEFAULT ''",
    # '' = in stock; otherwise the name of the printer the spool was unloaded onto.
    "printer": "ALTER TABLE spools ADD COLUMN printer TEXT NOT NULL DEFAULT ''",
    "sku": "ALTER TABLE spools ADD COLUMN sku TEXT NOT NULL DEFAULT ''",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(spools)")}
    for col, ddl in MIGRATIONS.items():
        if col not in cols:
            conn.execute(ddl)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    d["percent_left"] = round(100 * d["remaining_g"] / d["net_weight_g"], 1) if d["net_weight_g"] else None
    return d


def _get(conn: sqlite3.Connection, spool_id: int) -> sqlite3.Row:
    r = conn.execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
    if r is None:
        raise ToolError(f"No spool with id {spool_id}")
    return r


mcp = MCPServer(
    "filament-inventory",
    instructions=(
        "Inventory of 3D-printer filament spools. Each spool has a material type "
        "(PLA, PETG, ABS, TPU...), color, brand and remaining weight in grams. "
        "Spools are either in stock or on a named printer. Use list_spools / "
        "inventory_summary to answer stock questions, add_spool when the user buys "
        "filament, unload_spool / return_spool when a spool goes onto or comes off a "
        "printer, and use_filament after a print to deduct grams. catalog_colors lists the "
        "official colors (with hex codes and SKUs) for Bambu Lab, Snapmaker, Sunlu and Polymaker; use it "
        "to fill color_hex, sku and the exact color name when adding those brands. When the "
        "user gives a SKU or color code, look it up with catalog_colors(sku=...)."
    ),
)


@mcp.tool()
def add_spool(
    material: str,
    color: str,
    brand: str = "",
    net_weight_g: float = 1000,
    remaining_g: float | None = None,
    diameter_mm: float = 1.75,
    location: str = "",
    notes: str = "",
    count: int = 1,
    color_hex: str = "",
    sku: str = "",
) -> list[dict[str, Any]]:
    """Add one or more spools of filament to the inventory.

    material: filament type, e.g. PLA, PLA+, PETG, ABS, ASA, TPU, Nylon.
    color: color name, e.g. "Galaxy Black".
    color_hex: optional swatch color as #RRGGBB.
    sku: optional manufacturer SKU / color code (e.g. Bambu "10100"); see catalog_colors(sku=...).
    net_weight_g: filament weight of a full spool (default 1000 g).
    remaining_g: grams left; defaults to net_weight_g (a new, full spool).
    count: how many identical spools to add (1-100).
    """
    material, color = material.strip(), color.strip()
    if not material or not color:
        raise ToolError("material and color are required")
    if not 1 <= count <= 100:
        raise ToolError("count must be between 1 and 100")
    if net_weight_g <= 0:
        raise ToolError("net_weight_g must be positive")
    color_hex = color_hex.strip().upper()
    if color_hex and not re.fullmatch(r"#[0-9A-F]{6}", color_hex):
        raise ToolError("color_hex must look like #RRGGBB")
    remaining = net_weight_g if remaining_g is None else remaining_g
    ts = _now()
    with db() as conn:
        ids = []
        for _ in range(count):
            cur = conn.execute(
                """INSERT INTO spools (material, color, color_hex, sku, brand, diameter_mm, net_weight_g,
                                       remaining_g, location, notes, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (material.upper(), color, color_hex, sku.strip().upper(), brand.strip(), diameter_mm,
                 net_weight_g, remaining, location.strip(), notes.strip(), ts, ts),
            )
            ids.append(cur.lastrowid)
        return [_row(_get(conn, i)) for i in ids]


@mcp.tool()
def list_spools(
    search: str | None = None,
    material: str | None = None,
    color: str | None = None,
    brand: str | None = None,
    status: str = "all",
    include_empty: bool = False,
    max_remaining_g: float | None = None,
) -> list[dict[str, Any]]:
    """Find spools. Text filters are case-insensitive substring matches.

    search: free text; every word must match brand, material, color, SKU, location or printer.
    status: "all" (default), "stock" (on the shelf) or "loaded" (unloaded onto a printer).
    include_empty: also show spools with 0 g left (hidden by default).
    max_remaining_g: only spools with at most this many grams left (e.g. 200 to find low stock).
    """
    sql = "SELECT * FROM spools WHERE 1=1"
    args: list[Any] = []
    for word in (search or "").split():
        sql += (" AND (brand LIKE ? OR material LIKE ? OR color LIKE ? OR location LIKE ?"
                " OR printer LIKE ? OR sku LIKE ?)")
        args += [f"%{word}%"] * 6
    for col, val in (("material", material), ("color", color), ("brand", brand)):
        if val:
            sql += f" AND {col} LIKE ?"
            args.append(f"%{val.strip()}%")
    if status == "stock":
        sql += " AND printer = ''"
    elif status == "loaded":
        sql += " AND printer != ''"
    elif status != "all":
        raise ToolError('status must be "all", "stock" or "loaded"')
    if not include_empty:
        sql += " AND remaining_g > 0"
    if max_remaining_g is not None:
        sql += " AND remaining_g <= ?"
        args.append(max_remaining_g)
    sql += " ORDER BY material, color, brand, remaining_g"
    with db() as conn:
        return [_row(r) for r in conn.execute(sql, args)]


@mcp.tool()
def unload_spool(spool_id: int, printer: str) -> dict[str, Any]:
    """Take a spool out of stock and put it on a printer (records the printer name)."""
    printer = printer.strip()
    if not printer:
        raise ToolError("printer name is required")
    with db() as conn:
        r = _get(conn, spool_id)
        if r["printer"]:
            raise ToolError(f"Spool {spool_id} is already on printer {r['printer']}")
        ts = _now()
        conn.execute("UPDATE spools SET printer = ?, updated_at = ? WHERE id = ?", (printer, ts, spool_id))
        conn.execute("INSERT INTO spool_events (spool_id, action, printer, at) VALUES (?,?,?,?)",
                     (spool_id, "unload", printer, ts))
        return _row(_get(conn, spool_id))


@mcp.tool()
def return_spool(spool_id: int, remaining_g: float | None = None,
                 location: str | None = None) -> dict[str, Any]:
    """Take a spool off its printer and back into stock, optionally with its weighed grams left
    and a new storage location."""
    with db() as conn:
        r = _get(conn, spool_id)
        if not r["printer"]:
            raise ToolError(f"Spool {spool_id} is already in stock")
        if remaining_g is not None and remaining_g < 0:
            raise ToolError("remaining_g cannot be negative")
        ts = _now()
        conn.execute(
            "UPDATE spools SET printer = '', remaining_g = ?, location = ?, updated_at = ? WHERE id = ?",
            (r["remaining_g"] if remaining_g is None else remaining_g,
             r["location"] if location is None else location.strip(), ts, spool_id))
        conn.execute("INSERT INTO spool_events (spool_id, action, printer, at) VALUES (?,?,?,?)",
                     (spool_id, "return", r["printer"], ts))
        return _row(_get(conn, spool_id))


@mcp.tool()
def get_spool(spool_id: int) -> dict[str, Any]:
    """Get one spool by id, including its recent usage and printer history."""
    with db() as conn:
        d = _row(_get(conn, spool_id))
        d["usage"] = [dict(r) for r in conn.execute(
            "SELECT grams, job, at FROM usage_log WHERE spool_id = ? ORDER BY at DESC LIMIT 20",
            (spool_id,))]
        d["events"] = [dict(r) for r in conn.execute(
            "SELECT action, printer, at FROM spool_events WHERE spool_id = ? ORDER BY id DESC LIMIT 20",
            (spool_id,))]
        return d


@mcp.tool()
def use_filament(spool_id: int, grams: float, job: str = "") -> dict[str, Any]:
    """Deduct grams consumed by a print from a spool. Remaining weight never goes below 0."""
    if grams <= 0:
        raise ToolError("grams must be positive")
    with db() as conn:
        r = _get(conn, spool_id)
        new = max(0.0, r["remaining_g"] - grams)
        conn.execute("UPDATE spools SET remaining_g = ?, updated_at = ? WHERE id = ?",
                     (new, _now(), spool_id))
        conn.execute("INSERT INTO usage_log (spool_id, grams, job, at) VALUES (?,?,?,?)",
                     (spool_id, grams, job, _now()))
        return _row(_get(conn, spool_id))


@mcp.tool()
def update_spool(
    spool_id: int,
    material: str | None = None,
    color: str | None = None,
    brand: str | None = None,
    diameter_mm: float | None = None,
    net_weight_g: float | None = None,
    remaining_g: float | None = None,
    location: str | None = None,
    notes: str | None = None,
    sku: str | None = None,
    color_hex: str | None = None,
) -> dict[str, Any]:
    """Change fields of a spool (e.g. set remaining_g after weighing it). Omitted fields are unchanged."""
    changes = {k: v for k, v in locals().items() if k in UPDATABLE and v is not None}
    if "material" in changes:
        changes["material"] = changes["material"].strip().upper()
    if "sku" in changes:
        changes["sku"] = changes["sku"].strip().upper()
    with db() as conn:
        _get(conn, spool_id)
        if changes:
            sets = ", ".join(f"{k} = ?" for k in changes)
            conn.execute(f"UPDATE spools SET {sets}, updated_at = ? WHERE id = ?",
                         (*changes.values(), _now(), spool_id))
        return _row(_get(conn, spool_id))


@mcp.tool()
def remove_spool(spool_id: int) -> str:
    """Delete a spool from the inventory entirely (e.g. the empty spool was thrown away)."""
    with db() as conn:
        r = _get(conn, spool_id)
        conn.execute("DELETE FROM spools WHERE id = ?", (spool_id,))
        return f"Removed spool {spool_id} ({r['material']} {r['color']} {r['brand']})".strip()


@mcp.tool()
def inventory_summary(group_by: str = "material_color") -> list[dict[str, Any]]:
    """Totals of non-empty spools: spools in stock, spools on printers, and grams left.

    group_by: "material", "color", "material_color" (default) or "brand".
    """
    cols = {"material": "material", "color": "color",
            "material_color": "material, color", "brand": "brand"}.get(group_by)
    if cols is None:
        raise ToolError('group_by must be "material", "color", "material_color" or "brand"')
    with db() as conn:
        rows = conn.execute(
            f"""SELECT {cols}, SUM(printer = '') AS in_stock, SUM(printer != '') AS on_printers,
                       ROUND(SUM(remaining_g), 1) AS remaining_g
                FROM spools WHERE remaining_g > 0
                GROUP BY {cols} ORDER BY {cols}""")
        return [dict(r) for r in rows]


@mcp.tool()
def catalog_colors(brand: str | None = None, filament_type: str | None = None,
                   color: str | None = None, sku: str | None = None) -> dict[str, Any]:
    """Look up the manufacturer color catalog (Bambu Lab, Snapmaker, Sunlu, Polymaker) with hex codes.

    No arguments: list brands and their filament types.
    brand: list that brand's types with color counts (loose match: "bambulab" = "Bambu Lab").
    brand + filament_type: list that line's colors, e.g. ("Bambu Lab", "PLA Matte").
    color: substring search across colors (optionally limited by brand / filament_type).
    sku: find the color with this SKU / color code (Bambu "10100", Snapmaker "34071",
         Sunlu "DLZ-US-SL-PLA+V2-BK-1KG", Polymaker "CA04076"; spool and refill SKUs both work,
         and Sunlu region prefixes / "*2" pack suffixes are ignored). The result says the variant
         (e.g. Refill), spool weight and diameter.
    """
    if sku:
        hit = find_sku(sku)
        if not hit:
            raise ToolError(f"SKU {sku!r} is not in the catalog")
        return hit
    brands = _catalog()["brands"]
    if brand:
        brands = {b: t for b, t in brands.items() if _norm(brand) in _norm(b)}
        if not brands:
            raise ToolError(f"No catalog for brand {brand!r}; known: {', '.join(_catalog()['brands'])}")
    if filament_type:
        brands = {b: {t: v for t, v in types.items() if _norm(filament_type) == _norm(t)}
                  for b, types in brands.items()}
        if not any(brands.values()):
            raise ToolError(f"No filament type {filament_type!r} in catalog for {brand or 'any brand'}")
    if color:
        hits = [{"brand": b, "type": t, **c} for b, types in brands.items() for t, v in types.items()
                for c in v["colors"] if color.lower() in c["name"].lower()]
        return {"matches": hits[:100], "total": len(hits)}
    if filament_type:
        return {"lines": [{"brand": b, "type": t, "spool_weight_g": v["weight"], "colors": v["colors"]}
                          for b, types in brands.items() for t, v in types.items()]}
    if brand:
        return {b: {t: len(v["colors"]) for t, v in types.items()} for b, types in brands.items()}
    return {b: list(types) for b, types in brands.items()}


UI_FILE = Path(__file__).with_name("ui.html")
CATALOG_FILE = Path(__file__).with_name("catalog.json")
_catalog_cache: dict[str, Any] | None = None


def _catalog() -> dict[str, Any]:
    global _catalog_cache
    if _catalog_cache is None:
        try:
            _catalog_cache = json.loads(CATALOG_FILE.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _catalog_cache = {"brands": {}}
    return _catalog_cache


def norm_sku(s: str) -> str:
    """Region/pack-neutral SKU key: 'DLZ-EU-SL-PLA+-BK-1KG*2' -> 'SL-PLA+-BK-1KG'."""
    s = re.sub(r"\s+", "", (s or "").upper())
    s = re.sub(r"^DLZ-[A-Z]{2}-", "", s)
    return re.sub(r"\*\d+$", "", s)


_sku_index: dict[str, dict[str, Any]] | None = None


def find_sku(sku: str) -> dict[str, Any] | None:
    global _sku_index
    if _sku_index is None:
        _sku_index = {}
        for b, types in _catalog()["brands"].items():
            for t, v in types.items():
                for c in v["colors"]:
                    base = {"brand": b, "type": t, "name": c["name"], "hex": c["hex"]}
                    for e in c.get("skus") or ([{"sku": c["sku"]}] if c.get("sku") else []):
                        hit = {**base, "sku": e["sku"], "spool_weight_g": e.get("weight") or v["weight"],
                               "diameter_mm": e.get("diameter", 1.75)}
                        if e.get("variant"):
                            hit["variant"] = e["variant"]
                        _sku_index.setdefault(norm_sku(e["sku"]), hit)
    return _sku_index.get(norm_sku(sku))


def _norm(s: str) -> str:
    """'Bambu Lab', 'bambulab', 'BAMBU-LAB' -> 'bambulab'."""
    return re.sub(r"[^a-z0-9+]", "", s.lower())


def _options() -> dict[str, list]:
    """Distinct values already in use, for autocomplete in the web UI."""
    with db() as conn:
        def distinct(col: str) -> list[str]:
            return [r[0] for r in conn.execute(
                f"SELECT DISTINCT {col} FROM spools WHERE {col} != '' ORDER BY {col} COLLATE NOCASE")]
        colors = [dict(r) for r in conn.execute(
            """SELECT color, MAX(color_hex) AS color_hex FROM spools
               GROUP BY color COLLATE NOCASE ORDER BY color COLLATE NOCASE""")]
        return {"brands": distinct("brand"), "materials": distinct("material"),
                "locations": distinct("location"),
                "printers": [r[0] for r in conn.execute(
                    "SELECT DISTINCT printer FROM spool_events WHERE printer != '' "
                    "ORDER BY printer COLLATE NOCASE")],
                "colors": colors}


def _icon_png(size: int) -> bytes:
    """App icon (a filament spool on the accent color), drawn without image libraries."""
    import struct
    import zlib

    bg, ring, hub = (217, 98, 43), (255, 243, 234), (217, 98, 43)
    c = (size - 1) / 2
    r_outer, r_hub = size * 0.34, size * 0.12

    def cover(d: float, r: float) -> float:  # anti-aliased disc edge
        return max(0.0, min(1.0, r - d + 0.5))

    rows = bytearray()
    for y in range(size):
        rows.append(0)
        for x in range(size):
            d = ((x - c) ** 2 + (y - c) ** 2) ** 0.5
            a, h = cover(d, r_outer), cover(d, r_hub)
            px = [bg[i] * (1 - a) + ring[i] * a for i in range(3)]
            px = [px[i] * (1 - h) + hub[i] * h for i in range(3)]
            rows += bytes(int(v + 0.5) for v in px)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 9)) + chunk(b"IEND", b""))


_icons: dict[int, bytes] = {}
MANIFEST = {
    "name": "Filament Inventory", "short_name": "Filament", "start_url": "./", "scope": "./",
    "display": "standalone", "background_color": "#f6f4f1", "theme_color": "#d9622b",
    "icons": [{"src": f"icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png"} for n in (192, 512)],
}
# Served without the token: the page shell, its icons and manifest (no inventory data).
PUBLIC_PATHS = {"/", "/manifest.webmanifest", "/apple-touch-icon.png", "/icon-192.png", "/icon-512.png"}


def build_web_app():
    """Small JSON API + the single-page UI, served next to the MCP endpoint."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, Response
    from starlette.routing import Route

    async def index(_: Request):
        return HTMLResponse(UI_FILE.read_text(encoding="utf-8"))

    async def icon(request: Request):
        size = 180 if request.url.path == "/apple-touch-icon.png" else int(request.path_params["size"])
        if size not in (180, 192, 512):
            return Response(status_code=404)
        if size not in _icons:
            _icons[size] = _icon_png(size)
        return Response(_icons[size], media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})

    async def manifest(_: Request):
        return JSONResponse(MANIFEST, media_type="application/manifest+json")

    async def spools(request: Request):
        if request.method == "GET":
            q = request.query_params
            try:
                return JSONResponse(list_spools(search=q.get("q") or None,
                                                status=q.get("status", "all")))
            except ToolError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ToolError("expected a JSON object")
            num = lambda k, d: float(body.get(k) if body.get(k) not in (None, "") else d)
            added = add_spool(
                material=str(body.get("material", "")),
                color=str(body.get("color", "")),
                color_hex=str(body.get("color_hex", "")),
                sku=str(body.get("sku", "")),
                brand=str(body.get("brand", "")),
                net_weight_g=num("net_weight_g", 1000),
                diameter_mm=num("diameter_mm", 1.75),
                location=str(body.get("location", "")),
                notes=str(body.get("notes", "")),
                count=int(num("count", 1)),
            )
        except (ToolError, ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(added, status_code=201)

    async def options(_: Request):
        return JSONResponse(_options())

    async def catalog(_: Request):
        return JSONResponse(_catalog(), headers={"Cache-Control": "no-cache"})

    async def spool_action(request: Request):
        if request.path_params["action"] not in ("unload", "return"):
            return JSONResponse({"error": "unknown action"}, status_code=404)
        try:
            sid = int(request.path_params["spool_id"])
            body = await request.json() if int(request.headers.get("content-length") or 0) else {}
            if request.path_params["action"] == "unload":
                result = unload_spool(sid, str(body.get("printer", "")))
            else:
                rg = body.get("remaining_g")
                loc = body.get("location")
                result = return_spool(sid, None if rg in (None, "") else float(rg),
                                      None if loc is None else str(loc))
        except (ToolError, ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(result)

    return Starlette(routes=[
        Route("/", index),
        Route("/manifest.webmanifest", manifest),
        Route("/apple-touch-icon.png", icon),
        Route("/icon-{size:int}.png", icon),
        Route("/api/spools", spools, methods=["GET", "POST"]),
        Route("/api/options", options),
        Route("/api/catalog", catalog),
        Route("/api/spools/{spool_id:int}/{action:str}", spool_action, methods=["POST"]),
    ])


def build_http_app(token: str):
    """Route / to the UI (public page), /api/* and /mcp to token-protected handlers."""
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse

    # LAN deployment: clients reach us by hostname/IP, so DNS-rebinding host checks are off;
    # the token protects the API and MCP endpoints instead.
    mcp_app = mcp.streamable_http_app(
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    web_app = build_web_app()

    async def app(scope, receive, send):
        if scope["type"] != "http":  # lifespan events go to the MCP app (starts its session manager)
            await mcp_app(scope, receive, send)
            return
        path = scope["path"]
        if path in PUBLIC_PATHS:
            await web_app(scope, receive, send)
            return
        if token:
            auth = dict(scope["headers"]).get(b"authorization", b"").decode()
            if not hmac.compare_digest(auth, f"Bearer {token}"):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await (web_app if path.startswith("/api/") else mcp_app)(scope, receive, send)

    return app


def _run_http() -> None:
    import uvicorn

    host = os.environ.get("FILAMENT_HOST", "0.0.0.0")
    port = int(os.environ.get("FILAMENT_PORT", "8765"))
    token = os.environ.get("FILAMENT_TOKEN", "")
    if not token:
        print("WARNING: FILAMENT_TOKEN not set; HTTP endpoints are unauthenticated", file=sys.stderr)
    uvicorn.run(build_http_app(token), host=host, port=port, log_level="info")


if __name__ == "__main__":
    if "--http" in sys.argv:
        _run_http()
    else:
        mcp.run()
