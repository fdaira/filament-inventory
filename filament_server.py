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
  FILAMENT_SCALE_URL     optional Wi-Fi scale running ESPHome, e.g. http://192.168.1.50
  FILAMENT_SCALE_SENSOR  its weight sensor name   (default "Spool Weight")
  FILAMENT_SCALE_TARE    its tare button name     (default "Tare")
  FILAMENT_SCALE_TAG     its NFC tag text sensor  (default "Spool Tag")
  FILAMENT_SCALE_AUTO    "0" turns off automatic weighing of tagged spools (default on)
"""

from __future__ import annotations

import hmac
import json
import math
import os
import re
import sqlite3
import sys
import threading
import time
from collections import deque
import urllib.error
import urllib.parse
import urllib.request
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
CREATE TABLE IF NOT EXISTS spool_tags (
    uid       TEXT    PRIMARY KEY,                  -- NFC/RFID tag ID, e.g. 04-A1-B2-C3
    spool_id  INTEGER NOT NULL REFERENCES spools(id) ON DELETE CASCADE,
    linked_at TEXT    NOT NULL
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
             "remaining_g", "location", "notes", "empty_spool_g"}

# Columns added after the first release; created on startup if missing.
MIGRATIONS = {
    "color_hex": "ALTER TABLE spools ADD COLUMN color_hex TEXT NOT NULL DEFAULT ''",
    # '' = in stock; otherwise the name of the printer the spool was unloaded onto.
    "printer": "ALTER TABLE spools ADD COLUMN printer TEXT NOT NULL DEFAULT ''",
    "sku": "ALTER TABLE spools ADD COLUMN sku TEXT NOT NULL DEFAULT ''",
    # Weight of the empty spool (g), to turn a scale reading into grams of filament. NULL = unknown.
    "empty_spool_g": "ALTER TABLE spools ADD COLUMN empty_spool_g REAL",
}

SCALE_URL = os.environ.get("FILAMENT_SCALE_URL", "").strip().rstrip("/")
SCALE_SENSOR = os.environ.get("FILAMENT_SCALE_SENSOR", "Spool Weight")
SCALE_TARE = os.environ.get("FILAMENT_SCALE_TARE", "Tare")
SCALE_TAG = os.environ.get("FILAMENT_SCALE_TAG", "Spool Tag")
SCALE_AUTO = os.environ.get("FILAMENT_SCALE_AUTO", "1") not in ("0", "false", "no", "off")


def norm_tag(uid: str) -> str:
    """'04 a1:b2-c3' -> '04-A1-B2-C3' (the format ESPHome's PN532 reports)."""
    parts = [p for p in re.split(r"[^0-9A-Fa-f]+", uid or "") if p]
    if len(parts) == 1 and len(parts[0]) > 2 and len(parts[0]) % 2 == 0:  # '04A1B2C3'
        parts = [parts[0][i:i + 2] for i in range(0, len(parts[0]), 2)]
    return "-".join(p.upper().zfill(2) for p in parts)


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
    if d.get("empty_spool_g") is None:
        d["empty_spool_suggested"] = default_empty_spool(d["brand"], d["material"], d.get("sku", ""))
    return d


def _with_tags(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tags: dict[int, list[str]] = {}
    for t in conn.execute("SELECT uid, spool_id FROM spool_tags ORDER BY linked_at"):
        tags.setdefault(t["spool_id"], []).append(t["uid"])
    for d in rows:
        d["tags"] = tags.get(d["id"], [])
    return rows


def _set_remaining(conn: sqlite3.Connection, r: sqlite3.Row, remaining: float, job: str) -> None:
    """Store a new grams-left figure; a decrease is logged as usage (so weighing tracks consumption)."""
    if remaining < r["remaining_g"] - 0.5:
        conn.execute("INSERT INTO usage_log (spool_id, grams, job, at) VALUES (?,?,?,?)",
                     (r["id"], round(r["remaining_g"] - remaining, 1), job, _now()))
    conn.execute("UPDATE spools SET remaining_g = ?, updated_at = ? WHERE id = ?", (remaining, _now(), r["id"]))


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
        "official colors (with hex codes and SKUs) for Bambu Lab, Snapmaker, Sunlu, Polymaker and Geeetech; use it "
        "to fill color_hex, sku and the exact color name when adding those brands. When the "
        "user gives a SKU or color code, look it up with catalog_colors(sku=...). If a Wi-Fi "
        "scale is configured, weigh_spool reads it and sets the grams left (scale reading minus "
        "the empty spool); use it when the user puts a spool on the scale. Spools can have NFC "
        "tags (link_tag / unlink_tag); a linked spool placed on the scale is weighed "
        "automatically, and spool_on_scale shows what is on it."
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
    empty_spool_g: float | None = None,
) -> list[dict[str, Any]]:
    """Add one or more spools of filament to the inventory.

    material: filament type, e.g. PLA, PLA+, PETG, ABS, ASA, TPU, Nylon.
    color: color name, e.g. "Galaxy Black".
    color_hex: optional swatch color as #RRGGBB.
    sku: optional manufacturer SKU / color code (e.g. Bambu "10100"); see catalog_colors(sku=...).
    net_weight_g: filament weight of a full spool (default 1000 g).
    remaining_g: grams left; defaults to net_weight_g (a new, full spool).
    count: how many identical spools to add (1-100).
    empty_spool_g: weight of the empty spool, for weighing later; defaults to the catalog's figure
        for this brand/type (refills have none: pass the weight of the reusable spool).
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
    if empty_spool_g is None:
        empty_spool_g = default_empty_spool(brand, material, sku)
    elif empty_spool_g < 0:
        raise ToolError("empty_spool_g cannot be negative")
    ts = _now()
    with db() as conn:
        ids = []
        for _ in range(count):
            cur = conn.execute(
                """INSERT INTO spools (material, color, color_hex, sku, brand, diameter_mm, net_weight_g,
                                       remaining_g, location, notes, empty_spool_g, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (material.upper(), color, color_hex, sku.strip().upper(), brand.strip(), diameter_mm,
                 net_weight_g, remaining, location.strip(), notes.strip(), empty_spool_g, ts, ts),
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

    search: free text; every word must match brand, material, color, SKU, location, printer or tag ID.
    status: "all" (default), "stock" (on the shelf) or "loaded" (unloaded onto a printer).
    include_empty: also show spools with 0 g left (hidden by default).
    max_remaining_g: only spools with at most this many grams left (e.g. 200 to find low stock).
    """
    sql = "SELECT * FROM spools WHERE 1=1"
    args: list[Any] = []
    for word in (search or "").split():
        sql += (" AND (brand LIKE ? OR material LIKE ? OR color LIKE ? OR location LIKE ?"
                " OR printer LIKE ? OR sku LIKE ?"
                " OR id IN (SELECT spool_id FROM spool_tags WHERE uid LIKE ?))")
        args += [f"%{word}%"] * 7
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
        return _with_tags(conn, [_row(r) for r in conn.execute(sql, args)])


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


def _return(conn: sqlite3.Connection, r: sqlite3.Row, remaining_g: float | None,
            location: str | None) -> None:
    if remaining_g is not None:
        _set_remaining(conn, r, remaining_g, f"used on {r['printer']}")
    ts = _now()
    conn.execute("UPDATE spools SET printer = '', location = ?, updated_at = ? WHERE id = ?",
                 (r["location"] if location is None else location.strip(), ts, r["id"]))
    conn.execute("INSERT INTO spool_events (spool_id, action, printer, at) VALUES (?,?,?,?)",
                 (r["id"], "return", r["printer"], ts))


@mcp.tool()
def return_spool(spool_id: int, remaining_g: float | None = None, location: str | None = None,
                 empty_spool_g: float | None = None) -> dict[str, Any]:
    """Take a spool off its printer and back into stock, optionally with its weighed grams left,
    a new storage location and the empty-spool weight. With a Wi-Fi scale, use weigh_spool instead."""
    with db() as conn:
        r = _get(conn, spool_id)
        if not r["printer"]:
            raise ToolError(f"Spool {spool_id} is already in stock")
        if remaining_g is not None and remaining_g < 0:
            raise ToolError("remaining_g cannot be negative")
        if empty_spool_g is not None:
            if empty_spool_g < 0:
                raise ToolError("empty_spool_g cannot be negative")
            conn.execute("UPDATE spools SET empty_spool_g = ? WHERE id = ?", (empty_spool_g, spool_id))
        _return(conn, r, remaining_g, location)
        return _row(_get(conn, spool_id))


def set_weight(spool_id: int, remaining_g: float, empty_spool_g: float | None = None) -> dict[str, Any]:
    """Record a weighed grams-left figure for a spool (in stock or on a printer)."""
    if remaining_g < 0:
        raise ToolError("remaining_g cannot be negative")
    with db() as conn:
        r = _get(conn, spool_id)
        if empty_spool_g is not None:
            if empty_spool_g < 0:
                raise ToolError("empty_spool_g cannot be negative")
            conn.execute("UPDATE spools SET empty_spool_g = ? WHERE id = ?", (empty_spool_g, spool_id))
        _set_remaining(conn, r, remaining_g, "weighed on scale")
        return _row(_get(conn, spool_id))


# ---------------------------------------------------------------- Wi-Fi scale (ESPHome)
def _scale_call(path: str, method: str = "GET") -> dict[str, Any]:
    if not SCALE_URL:
        raise ToolError("No scale is configured (set FILAMENT_SCALE_URL on the server)")
    url = SCALE_URL + "/" + urllib.parse.quote(path.lstrip("/"))
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=4) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ToolError(f"The scale has no '{path.split('/')[-1]}' "
                            "(its firmware may be older than this feature)") from e
        raise ToolError(f"The scale answered HTTP {e.code} for {path}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ToolError(f"Scale not reachable at {SCALE_URL}: {getattr(e, 'reason', e)}") from e
    try:
        return json.loads(body) if body.strip() else {}
    except ValueError:
        return {}


@mcp.tool()
def read_scale() -> dict[str, Any]:
    """Read the Wi-Fi scale: gross weight in grams (spool + filament) and whether it is steady."""
    values = []
    for i in range(3):
        v = _scale_call(f"/sensor/{SCALE_SENSOR}").get("value")
        if not isinstance(v, (int, float)) or math.isnan(v):
            raise ToolError("The scale has no reading yet; wait a second and try again")
        values.append(float(v))
        if i < 2:
            time.sleep(0.3)
    spread = max(values) - min(values)
    return {"gross_g": round(sorted(values)[1], 1), "stable": spread <= 3.0, "spread_g": round(spread, 1)}


@mcp.tool()
def tare_scale() -> str:
    """Zero the Wi-Fi scale. The scale (and its spool holder) must be empty."""
    _scale_call(f"/button/{SCALE_TARE}/press", method="POST")
    return "Scale zeroed"


@mcp.tool()
def weigh_spool(spool_id: int, empty_spool_g: float | None = None, return_to_stock: bool = False,
                location: str | None = None) -> dict[str, Any]:
    """Weigh a spool on the Wi-Fi scale and save its grams left (scale reading minus empty spool).

    empty_spool_g: weight of the empty spool; defaults to the spool's saved value or the catalog's
        figure for its brand/type. Needed for refills on a reusable spool.
    return_to_stock: also take it off its printer and back into stock (optionally at `location`).
    """
    reading = read_scale()
    if not reading["stable"]:
        raise ToolError(f"The scale is still settling (±{reading['spread_g']} g); try again")
    gross = reading["gross_g"]
    with db() as conn:
        r = _get(conn, spool_id)
        empty = empty_spool_g if empty_spool_g is not None else r["empty_spool_g"]
        if empty is None:
            empty = default_empty_spool(r["brand"], r["material"], r["sku"])
        if empty is None:
            raise ToolError(f"Empty-spool weight unknown for spool {spool_id}; pass empty_spool_g "
                            "(e.g. the weight of the reusable spool a refill sits on)")
        if gross < 20:
            raise ToolError(f"The scale reads {gross} g; put the spool on it first")
        if gross < empty - 15:
            raise ToolError(f"The scale reads {gross} g, less than the empty spool ({empty} g); "
                            "check the spool is on the scale and the empty-spool weight is right")
        remaining = max(0.0, round(gross - empty, 1))
        conn.execute("UPDATE spools SET empty_spool_g = ? WHERE id = ?", (empty, spool_id))
        if return_to_stock and r["printer"]:
            _return(conn, r, remaining, location)
        else:
            _set_remaining(conn, r, remaining, "weighed on scale")
            if location is not None:
                conn.execute("UPDATE spools SET location = ? WHERE id = ?", (location.strip(), spool_id))
        return {**_row(_get(conn, spool_id)), "scale_gross_g": gross}


# ---------------------------------------------------------------- NFC tags on the scale
def current_tag() -> str:
    """ID of the NFC tag lying on the scale's reader, or '' when there is none."""
    d = _scale_call(f"/text_sensor/{SCALE_TAG}")
    return norm_tag(str(d.get("value") or d.get("state") or ""))


def spool_for_tag(uid: str) -> dict[str, Any] | None:
    with db() as conn:
        t = conn.execute("SELECT spool_id FROM spool_tags WHERE uid = ?", (norm_tag(uid),)).fetchone()
        return _with_tags(conn, [_row(_get(conn, t["spool_id"]))])[0] if t else None


@mcp.tool()
def link_tag(spool_id: int, tag_uid: str | None = None) -> dict[str, Any]:
    """Link an NFC/RFID tag to a spool, so putting it on the scale weighs it automatically.

    tag_uid: the tag ID (e.g. "04-A1-B2-C3"); omit it to use the tag lying on the scale right now.
    A spool can have several tags (Bambu Lab spools have one on each side). A tag linked to
    another spool is moved to this one.
    """
    uid = norm_tag(tag_uid) if tag_uid else current_tag()
    if not uid:
        raise ToolError("No tag on the reader: put the spool (tag side down) on the scale and try again")
    with db() as conn:
        _get(conn, spool_id)
        prev = conn.execute("SELECT spool_id FROM spool_tags WHERE uid = ?", (uid,)).fetchone()
        conn.execute("INSERT OR REPLACE INTO spool_tags (uid, spool_id, linked_at) VALUES (?,?,?)",
                     (uid, spool_id, _now()))
        d = _with_tags(conn, [_row(_get(conn, spool_id))])[0]
        d["linked_tag"] = uid
        if prev and prev["spool_id"] != spool_id:
            d["moved_from_spool"] = prev["spool_id"]
        return d


@mcp.tool()
def unlink_tag(spool_id: int, tag_uid: str | None = None) -> dict[str, Any]:
    """Remove an NFC tag from a spool (or all of its tags when tag_uid is omitted)."""
    with db() as conn:
        _get(conn, spool_id)
        if tag_uid:
            conn.execute("DELETE FROM spool_tags WHERE spool_id = ? AND uid = ?", (spool_id, norm_tag(tag_uid)))
        else:
            conn.execute("DELETE FROM spool_tags WHERE spool_id = ?", (spool_id,))
        return _with_tags(conn, [_row(_get(conn, spool_id))])[0]


@mcp.tool()
def spool_on_scale() -> dict[str, Any]:
    """What is on the Wi-Fi scale now: the NFC tag ID, the spool it is linked to (if any), and
    the latest automatic weighings."""
    uid = current_tag()
    return {"tag": uid or None, "spool": spool_for_tag(uid) if uid else None,
            "recent_activity": watcher.recent(5)}


class ScaleWatcher:
    """Background loop: when a tagged spool is put on the scale, weigh it automatically.

    A linked spool gets its grams left saved (and goes back to stock if it was on a printer);
    an unknown tag is reported so the web app can offer to link it. Each tag is handled once
    per placement: lift the spool off the reader to weigh it again."""

    def __init__(self) -> None:
        self.events: deque[dict[str, Any]] = deque(maxlen=50)
        self.seq = 0
        self.lock = threading.Lock()
        self.handled = ""

    def add(self, kind: str, **fields: Any) -> dict[str, Any]:
        with self.lock:
            self.seq += 1
            ev = {"seq": self.seq, "at": _now(), "kind": kind, **fields}
            self.events.append(ev)
            return ev

    def since(self, seq: int) -> list[dict[str, Any]]:
        with self.lock:
            return [e for e in self.events if e["seq"] > seq]

    def recent(self, n: int) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.events)[-n:]

    def run(self) -> None:
        while True:
            try:
                uid = current_tag()
            except ToolError:
                time.sleep(10)  # scale offline, or firmware without NFC
                continue
            if not uid:
                self.handled = ""
            elif uid != self.handled:
                self.handled = uid
                try:
                    self.handle(uid)
                except Exception as e:  # keep the loop alive whatever happens
                    self.add("error", uid=uid, message=str(e))
            time.sleep(1)

    def handle(self, uid: str) -> None:
        spool = spool_for_tag(uid)
        if spool is None:
            self.add("unknown_tag", uid=uid, message=f"Unknown tag {uid}: link it to a spool")
            return
        title = " · ".join(x for x in (spool["brand"], spool["material"], spool["color"]) if x)
        base = {"uid": uid, "spool_id": spool["id"], "title": title}
        deadline = time.time() + 15
        while True:  # wait for the spool to settle
            if current_tag() != uid:
                return  # taken off again before a steady reading
            r = read_scale()
            if r["stable"] and r["gross_g"] >= 20:
                break
            if time.time() > deadline:
                self.add("error", **base, message=f"{title}: no steady reading; lift it off and put it back")
                return
            time.sleep(0.4)
        was_on = spool["printer"]
        try:
            res = weigh_spool(spool["id"], return_to_stock=True)
        except ToolError as e:
            msg = str(e)
            if msg.startswith("Empty-spool weight unknown"):
                msg = "set its empty-spool weight once (tap ⚖ Weigh on it), then put it back"
            self.add("error", **base, message=f"{title}: {msg}")
            return
        msg = f"{title}: {round(res['remaining_g'])} g left" + (f", back in stock from {was_on}" if was_on else "")
        self.add("returned" if was_on else "weighed", **base, remaining_g=res["remaining_g"], message=msg)


watcher = ScaleWatcher()


@mcp.tool()
def get_spool(spool_id: int) -> dict[str, Any]:
    """Get one spool by id, including its recent usage and printer history and its NFC tags."""
    with db() as conn:
        d = _with_tags(conn, [_row(_get(conn, spool_id))])[0]
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
    empty_spool_g: float | None = None,
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
    """Look up the manufacturer color catalog (Bambu Lab, Snapmaker, Sunlu, Polymaker, Geeetech).

    No arguments: list brands and their filament types.
    brand: list that brand's types with color counts (loose match: "bambulab" = "Bambu Lab").
    brand + filament_type: list that line's colors, e.g. ("Bambu Lab", "PLA Matte").
    color: substring search across colors (optionally limited by brand / filament_type).
    sku: find the color with this SKU / color code (Bambu "10100", Snapmaker "34071",
         Sunlu "DLZ-US-SL-PLA+V2-BK-1KG", Polymaker "CA04076", Geeetech "70426"; spool and refill
         SKUs both work,
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
                        refill = "refill" in e.get("variant", "").lower()
                        hit["empty_spool_g"] = None if refill else (e.get("spool_weight") or v.get("spool_weight"))
                        _sku_index.setdefault(norm_sku(e["sku"]), hit)
    hit = _sku_index.get(norm_sku(sku))
    if hit is None:  # Geeetech box codes wrap the 5-digit color code: DE39-70426-F, 700-001-1364
        m = re.search(r"(?<!\d)(7\d{4})(?!\d)", sku or "") or re.search(r"700-0\d\d-(\d{4})(?!\d)", sku or "")
        if m:
            code = m.group(1) if len(m.group(1)) == 5 else "7" + m.group(1)
            hit = _sku_index.get(code)
    return hit


def _catalog_brand(brand: str) -> str | None:
    n = _norm(brand or "")
    if not n:
        return None
    names = list(_catalog()["brands"])
    return (next((b for b in names if _norm(b) == n), None)
            or (next((b for b in names if _norm(b).startswith(n)), None) if len(n) >= 4 else None))


def default_empty_spool(brand: str, material: str, sku: str = "") -> float | None:
    """Typical empty-spool weight (g) from the catalog: by SKU (None for refills), else by
    brand + type, else the brand's usual spool. None when unknown."""
    if sku:
        hit = find_sku(sku)
        if hit:
            return hit["empty_spool_g"]
    b = _catalog_brand(brand)
    if not b:
        return None
    types = _catalog()["brands"][b]
    t = next((v for k, v in types.items() if _norm(k) == _norm(material or "")), None)
    if t and t.get("spool_weight"):
        return t["spool_weight"]
    weights = [v.get("spool_weight") for v in types.values() if v.get("spool_weight")]
    return max(set(weights), key=weights.count) if weights else None


BRAND_ALIASES = {"geetech": "geeetech", "bambu": "bambulab", "bbl": "bambulab"}  # common spellings


def _norm(s: str) -> str:
    """'Bambu Lab', 'bambulab', 'BAMBU-LAB' -> 'bambulab'; 'Geetech' -> 'geeetech'."""
    n = re.sub(r"[^a-z0-9+]", "", s.lower())
    return BRAND_ALIASES.get(n, n)


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
    from starlette.concurrency import run_in_threadpool
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
                empty_spool_g=None if body.get("empty_spool_g") in (None, "") else float(body["empty_spool_g"]),
            )
        except (ToolError, ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(added, status_code=201)

    async def options(_: Request):
        return JSONResponse(_options())

    async def scale_events(request: Request):
        """Automatic weighings since `since` (omit it to just get the current position)."""
        since = request.query_params.get("since")
        evs = watcher.since(int(since)) if since not in (None, "") else []
        return JSONResponse({"seq": watcher.seq, "events": evs})

    async def scale_tag(_: Request):
        if not SCALE_URL:
            return JSONResponse({"configured": False, "uid": None})
        try:
            uid = await run_in_threadpool(current_tag)
        except ToolError as e:
            return JSONResponse({"configured": True, "error": str(e)}, status_code=502)
        return JSONResponse({"configured": True, "uid": uid or None,
                             "spool": spool_for_tag(uid) if uid else None})

    async def catalog(_: Request):
        return JSONResponse(_catalog(), headers={"Cache-Control": "no-cache"})

    async def spool_action(request: Request):
        action = request.path_params["action"]
        if action not in ("unload", "return", "weigh", "link", "unlink"):
            return JSONResponse({"error": "unknown action"}, status_code=404)
        try:
            sid = int(request.path_params["spool_id"])
            body = await request.json() if int(request.headers.get("content-length") or 0) else {}
            opt = lambda k: None if body.get(k) in (None, "") else float(body[k])
            if action == "unload":
                result = unload_spool(sid, str(body.get("printer", "")))
            elif action == "return":
                loc = body.get("location")
                result = return_spool(sid, opt("remaining_g"), None if loc is None else str(loc),
                                      opt("empty_spool_g"))
            elif action == "link":  # tag_uid omitted: the tag lying on the scale now
                result = await run_in_threadpool(link_tag, sid, body.get("tag_uid") or None)
            elif action == "unlink":
                result = unlink_tag(sid, body.get("tag_uid") or None)
            else:  # weigh: save a grams-left figure the page computed from a scale reading
                if opt("remaining_g") is None:
                    raise ToolError("remaining_g is required")
                result = set_weight(sid, opt("remaining_g"), opt("empty_spool_g"))
        except (ToolError, ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse(result)

    async def scale(request: Request):
        if not SCALE_URL:
            return JSONResponse({"configured": False})
        if request.query_params.get("probe"):
            return JSONResponse({"configured": True, "auto": SCALE_AUTO})
        try:
            if request.method == "POST":
                return JSONResponse({"configured": True, "message": await run_in_threadpool(tare_scale)})
            return JSONResponse({"configured": True, **await run_in_threadpool(read_scale)})
        except ToolError as e:
            return JSONResponse({"configured": True, "error": str(e)}, status_code=502)

    return Starlette(routes=[
        Route("/", index),
        Route("/manifest.webmanifest", manifest),
        Route("/apple-touch-icon.png", icon),
        Route("/icon-{size:int}.png", icon),
        Route("/api/spools", spools, methods=["GET", "POST"]),
        Route("/api/options", options),
        Route("/api/catalog", catalog),
        Route("/api/spools/{spool_id:int}/{action:str}", spool_action, methods=["POST"]),
        Route("/api/scale", scale, methods=["GET"]),
        Route("/api/scale/tare", scale, methods=["POST"]),
        Route("/api/scale/events", scale_events),
        Route("/api/scale/tag", scale_tag),
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
    if SCALE_URL and SCALE_AUTO:
        threading.Thread(target=watcher.run, name="scale-watcher", daemon=True).start()
    uvicorn.run(build_http_app(token), host=host, port=port, log_level="info")


if __name__ == "__main__":
    if "--http" in sys.argv:
        _run_http()
    else:
        mcp.run()
