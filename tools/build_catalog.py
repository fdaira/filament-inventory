"""Build catalog.json: brand -> filament type -> colors (name, hex, SKU).

    python3 tools/build_catalog.py            # downloads the latest data
    python3 tools/build_catalog.py --from DIR # reuses files saved by an earlier run (DIR/*.json)

Sources (facts only: names, hex codes, SKUs):
  * Bambu Lab: Bambu Studio's filaments_color_codes.json (official names, hex, 5-digit filament codes)
    https://github.com/bambulab/BambuStudio
  * Snapmaker: SpoolmanDB colors + SKUs/hex from the us.snapmaker.com product feed
  * Sunlu:     SpoolmanDB colors + SKUs from the store.sunlu.com product feed (Ship-to-USA, 1-roll variant,
               plus refills)
  * Polymaker: SKUs for every spool/refill/weight/diameter from the us.polymaker.com product feed,
               hex from SpoolmanDB
  * SpoolmanDB https://github.com/Donkie/SpoolmanDB (MIT License, Copyright (c) Daniel Hultgren)
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import date
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "catalog.json"
UA = {"User-Agent": "Mozilla/5.0 (filament-inventory catalog builder)"}
SPOOLMAN = "https://raw.githubusercontent.com/Donkie/SpoolmanDB/main/filaments/{}.json"
BAMBU_CODES = ("https://raw.githubusercontent.com/bambulab/BambuStudio/master/"
               "resources/profiles/BBL/filament/filaments_color_codes.json")
STORES = {"snapmaker": "https://us.snapmaker.com", "sunlu": "https://store.sunlu.com",
          "polymaker": "https://us.polymaker.com"}


# ---------------------------------------------------------------- helpers
def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9+]", "", (s or "").lower())


TYPOS = {"oliver": "olive", "midnigh": "midnight"}


def name_keys(name: str) -> set[str]:
    """'Blue(Klein Blue)' -> {'blueklein blue', 'blue', 'kleinblue'} (normalized)."""
    name = re.sub(r"\b(\w+)\b", lambda m: TYPOS.get(m.group(1).lower(), m.group(1)), name)
    keys = {norm(name)} | {norm(part) for part in name.split("/")}
    m = re.match(r"^(.*?)\s*\((.*)\)\s*$", name)
    if m:
        keys |= {norm(m.group(1)), norm(m.group(2))}
    return {k for k in keys if k}


def hex6(h: str | None) -> str | None:
    h = (h or "").strip().lstrip("#").upper()
    return "#" + h[:6] if re.fullmatch(r"[0-9A-F]{6}([0-9A-F]{2})?", h) else None


def fetch_json(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.load(r)


def fetch_store(base: str) -> list[dict]:
    products = []
    for page in range(1, 20):
        batch = fetch_json(f"{base}/products.json?limit=250&page={page}")["products"]
        if not batch:
            break
        products += batch
    return products


def load_sources(cache: Path | None) -> dict:
    def get(name, loader):
        f = cache / f"{name}.json" if cache else None
        if f and f.exists():
            return json.loads(f.read_text())
        data = loader()
        if cache:
            cache.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(data))
        return data
    src = {f"spoolman_{b}": get(f"spoolman_{b}", lambda b=b: fetch_json(SPOOLMAN.format(b)))
           for b in ("bambulab", "snapmaker", "sunlu", "polymaker")}
    src["bambu_codes"] = get("bambu_codes", lambda: fetch_json(BAMBU_CODES))
    for b, base in STORES.items():
        src[f"store_{b}"] = get(f"store_{b}", lambda base=base: fetch_store(base))
    return src


# ---------------------------------------------------------------- SpoolmanDB base
UNNAMED = {  # lines SpoolmanDB leaves unnamed: (file, material, first color) -> line
    ("bambulab", "PLA", "Jade White"): "Basic",
    ("bambulab", "PLA", "White Marble"): "Marble",
    ("bambulab", "PLA", "Arctic Whisper"): "Basic Gradient",
    ("bambulab", "PETG", "White"): "Basic",
    ("bambulab", "PETG", "Translucent Gray"): "Translucent",
    ("bambulab", "ASA", "White Aero"): "Aero",
    ("snapmaker", "PETG", "Translucent Teal"): "Translucent",
    ("sunlu", "PLA", "White (Glow Green)"): "Glow",
}
RENAMED = {("bambulab", "PLA", "Velvet Eclipse (Black-Red)"): "Silk Multi-Color"}
TYPE_ALIASES = {"sunlu": {"WOOD": "PLA Wood"}}


def spoolman_types(key: str, data: dict) -> dict[str, dict]:
    types: dict[str, dict] = {}
    for f in data["filaments"]:
        material = re.sub(r"^TPU-(\d+A)$", r"TPU \1", f["material"])
        line = re.sub(r"\(RFID\)", "", f["name"].replace("{color_name}", "")).strip(" -")
        line = re.sub(r"\s+", " ", line)
        first = f["colors"][0]["name"] if f["colors"] else ""
        k = (key, f["material"], first)
        line = RENAMED.get(k) or line or UNNAMED.get(k, "")
        colors = f["colors"]
        if line == "Support for":
            line, colors = f"Support for {first}", [{**c, "name": "Natural"} for c in colors]
        label = line if line.startswith("Support for") else f"{material} {line}".strip()
        label = TYPE_ALIASES.get(key, {}).get(label, label)
        weights = [w.get("weight") for w in f.get("weights", []) if w.get("weight")]
        t = types.setdefault(label, {"weight": int(weights[0]) if weights else 1000, "colors": []})
        for c in colors:
            add_color(t, c["name"].strip(), hex6(c.get("hex")),
                      [h for h in (hex6(x) for x in c.get("hexes") or []) if h])
    return types


def add_color(t: dict, name: str, hex_: str | None, hexes: list[str] | None = None,
              sku: str | None = None, variant: dict | None = None) -> dict:
    """Add a color to a type (or enrich the existing one with the same name).

    variant: {"variant": "Refill", "weight": 1000, "diameter": 1.75} -- recorded with the SKU in
    c["skus"] when one color is sold under several SKUs (spool vs refill, weights, diameters)."""
    keys = name_keys(name)
    c = next((c for c in t["colors"] if name_keys(c["name"]) & keys), None)
    if c is None:
        c = {"name": name, "hex": hex_ or (hexes[0] if hexes else "")}
        if hexes and len(hexes) > 1:
            c["hexes"] = hexes
        t["colors"].append(c)
    if hex_ and not c.get("hex"):
        c["hex"] = hex_
    if sku:
        if variant is not None:
            entries = c.setdefault("skus", [])
            if sku_key(sku) not in {sku_key(e["sku"]) for e in entries}:  # one per region-neutral SKU
                entries.append({"sku": sku, **{k: v for k, v in variant.items() if v}})
        if not c.get("sku"):
            c["sku"] = sku
    return c


def sku_key(s: str) -> str:
    """Region/pack-neutral SKU: 'DLZ-EU-SL-PLA+-BK-1KG*2' -> 'SL-PLA+-BK-1KG' (same rule as the server)."""
    s = re.sub(r"\s+", "", (s or "").upper())
    return re.sub(r"\*\d+$", "", re.sub(r"^DLZ-[A-Z]{2}-", "", s))


def pick_primary_skus(types: dict) -> None:
    """With several SKUs, the headline SKU is a regular spool at the line's usual weight in 1.75 mm."""
    for t in types.values():
        for c in t["colors"]:
            e = c.get("skus")
            if not e:
                continue
            score = lambda x: ("refill" in x.get("variant", "").lower(),
                               x.get("weight", t["weight"]) != t["weight"], x.get("diameter", 1.75) != 1.75)
            c["sku"] = min(e, key=score)["sku"]


def find_type(types: dict, label: str) -> str:
    n = norm(label)
    return next((k for k in types if norm(k) == n), label)


# ---------------------------------------------------------------- Bambu Lab (official)
def bambu_types(codes: dict, spoolman: dict[str, dict]) -> dict[str, dict]:
    types: dict[str, dict] = {}
    for e in codes["data"]:
        label = e["fila_type"].strip()
        sm = spoolman.get(find_type(spoolman, label))
        weight = sm["weight"] if sm else (500 if label.startswith(("Support", "PVA")) else 1000)
        t = types.setdefault(label, {"weight": weight, "colors": []})
        hexes = [h for h in (hex6(x) for x in e.get("fila_color") or []) if h]
        add_color(t, e["fila_color_name"]["en"].strip(), hexes[0] if hexes else None, hexes,
                  e["fila_color_code"])
    return types


# ---------------------------------------------------------------- Snapmaker store
SNAP_SKIP = ("bundle", "holder", "module", "tube", "sensor", "switch")
SNAP_SPECIAL = {  # legacy single-color products: title -> (type, color)
    "Black High Flow TPU95": ("TPU 95A HF", "Black"),
    "Black Nylon": ("PA Nylon", "Black"),
    "Glow-in-the-dark Green PLA": ("PLA Glow-in-the-dark", "Green"),
    "PVA": ("PVA", "Natural"),
    "Wood PLA": ("PLA Wood", "Wood"),
}


def snap_type(title: str) -> str:
    t = re.sub(r"\(RFID\)|\bFilament\b|-?\s*\(?\d+(\.\d+)?\s*k?g\)?", "", title, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" -")
    if t.startswith("Support for"):
        return t
    t = re.sub(r"^TPU$", "TPU 95A", t)
    t = re.sub(r"^PEBA 90A High Rebound$", "PEBA 90A", t)
    m = re.match(r"^(.+?)\s+PLA\b(.*)$", t)  # "SnapSpeed PLA" -> "PLA SnapSpeed"
    if m and m.group(1) != "Basic":
        return f"PLA {m.group(1)}{m.group(2)}".strip()
    return re.sub(r"^Basic PLA$", "PLA", t)


def apply_snapmaker(types: dict, products: list[dict]) -> int:
    n = 0
    for p in products:
        title = p["title"]
        if "filament" not in title.lower() or any(s in title.lower() for s in SNAP_SKIP):
            continue
        base = re.sub(r"\s*(\(RFID\)|Filament|-?\s*\(?\d+\s*k?g\)?)\s*", " ", title, flags=re.I).strip()
        base = re.sub(r"\s+", " ", base)
        special = SNAP_SPECIAL.get(base)
        label = find_type(types, special[0] if special else snap_type(title))
        m = re.search(r"(\d+(?:\.\d+)?)\s*(k?g)\b", title, re.I)
        weight = int(float(m.group(1)) * (1000 if m.group(2).lower() == "kg" else 1)) if m else 1000
        t = types.setdefault(label, {"weight": weight, "colors": []})
        for v in p["variants"]:
            vt = v["title"]
            if vt == "Default Title":
                name, hexes = (special[1] if special else base), []
            else:
                mm = re.match(r"^(.*?)\s*\(([0-9A-Fa-f+ ]{6,})\)\s*$", vt)
                name = (mm.group(1) if mm else vt).strip()
                raw = mm.group(2).replace(" ", "").split("+") if mm else []
                hexes = [h for h in (hex6(x) for x in raw) if h]
            if v.get("sku"):
                add_color(t, name, hexes[0] if hexes else None, hexes, str(v["sku"]))
                n += 1
    return n


# ---------------------------------------------------------------- Sunlu store
SUNLU_SKIP = ("250g", "large spool", "collection", "special offers", "bundle",
              "gift", "dryer", "filadc", "connector", "mother", "prime deals", "clearance",
              "special filament")
SUNLU_TYPES = [  # first matching title pattern wins; None = type comes from the variant's Material option
    (r"Refill Filament", None),
    (r"High Speed PLA\+ ?2\.0", "PLA+ High Speed 2.0"), (r"High Speed PLA\+", "PLA+ High Speed"),
    (r"High Speed Matte PLA", "PLA High Speed Matte"), (r"High Speed Matte PETG", "PETG High Speed Matte"),
    (r"High Speed PLA Marble", "PLA High Speed Marble"), (r"High Speed PLA", "PLA High Speed"),
    (r"SILK PLA\+ ?2\.0", "PLA+ Silk 2.0"), (r"SILK PLA Plus|SILK PLA\+", "PLA+ Silk"),
    (r"Multi-Color SILK", "PLA Silk Multi-Color"), (r"PLA\+ ?2\.0", "PLA+ 2.0"), (r"PETG 2\.0", "PETG 2.0"),
    (r"PP 2\.0", "PP 2.0"), (r"Matte PLA Dual-Color", "PLA Matte Dual-Color"), (r"Matte PLA", "PLA Matte"),
    (r"PLA Galaxy", "PLA Galaxy"), (r"Twinkling", "PLA Twinkling"), (r"PETG Glow", "PETG Glow"),
    (r"Glow in The Dark.*PLA", "PLA Glow"), (r"Anti-string", "PLA Anti-string"), (r"TPU-SILK", "TPU Silk"),
    (r"\bE ABS\b", "ABS Easy"), (r"PLA Meta|Macaron", "PLA Meta"), (r"PLA Lightweight", "PLA Lightweight"),
    (r"PLA Metallic", "PLA Metallic"), (r"Wood PLA", "PLA Wood"), (r"ABS-GF", "ABS-GF"), (r"ABS-FR", "ABS FR"),
    (r"PA6-GF", "PA6-GF"), (r"PA6-CF", "PA6-CF"), (r"PA12-CF", "PA12-CF"), (r"PETG-CF", "PETG-CF"),
    (r"PLA-CF", "PLA-CF"), (r"PC-ABS", "PC+ABS"), (r"Easy Nylon", "PA Easy"), (r"\bPEEK\b", "PEEK"),
    (r"\bPVB\b", "PVB"), (r"\bPC \(Polycarbonate", "PC"), (r"\bPP \(Polypropylene", "PP"),
    (r"Rainbow", None), (r"\bTPU\b", None), (r"PLA\+\(PLA Plus\)", "PLA+"), (r"\bASA\b", "ASA"),
    (r"\bABS\b", "ABS"), (r"\bPETG\b", "PETG"), (r"\bPLA\b", None),
]
SUNLU_MATERIAL = {"SILK Rainbow": "PLA Silk Rainbow", "Matte PLA": "PLA Matte"}


def clean_sunlu_color(color: str, label: str, material: str, left: str) -> str:
    """'Matte Black' (PLA Matte) -> 'Black'; 'Brick Red Marble' -> 'Brick Red'; 'Anti-string PLA / Black' -> 'Black'."""
    color = re.sub(r"(\S)\(", r"\1 (", color).strip()
    color = re.sub(r"\s*\((Removable|Ordinary) Spool.*$", "", color, flags=re.I)
    color = re.sub(r"\s*\d+(\.\d+)?\s*k?g(/roll)?\b|\s+Filament\b|\s+color$", "", color, flags=re.I).strip()
    color = re.sub(r"\s+", " ", color)
    if " / " in color:
        color = color.split(" / ")[-1].strip()
    for pre in sorted({label, material, left.strip(), "PLA+", "PLA", "PETG", "SILK", "ABS", "TPU"},
                      key=len, reverse=True):
        if pre and color.lower().startswith(pre.lower() + " "):
            color = color[len(pre) + 1:]
            break
    words = {w.lower() for w in re.split(r"[\s+]", label) if w and not re.match(r"^(PLA|PETG|ABS|TPU|PA|PC)", w)}
    parts = color.split()
    while len(parts) > 1 and parts[0].lower() in words:
        parts = parts[1:]
    while len(parts) > 1 and parts[-1].lower() in words | {"marble", "wood"} and "(" not in parts[-1]:
        parts = parts[:-1]
    return " ".join(parts)


def sunlu_variant_options(p: dict, v: dict) -> dict[str, str]:
    return {o["name"].strip().lower(): v.get(f"option{i + 1}") or "" for i, o in enumerate(p["options"])}


def apply_sunlu(types: dict, products: list[dict]) -> int:
    n = 0
    for p in products:
        title = p["title"]
        tl = title.lower()
        if "filament" not in tl or any(s in tl for s in SUNLU_SKIP):
            continue
        fixed = next((lbl for pat, lbl in SUNLU_TYPES if re.search(pat, title, re.I)), "skip")
        if fixed == "skip":
            continue
        best: dict[tuple[str, str, bool], tuple[int, str]] = {}  # (type, color, refill) -> (rank, sku)
        for v in p["variants"]:
            opts = sunlu_variant_options(p, v)
            ship = next((val for k, val in opts.items() if k.startswith("ship")), "")
            pkg = opts.get("package", "")
            if pkg and not pkg.lower().startswith("1 roll"):
                continue
            color_val = next((val for k, val in opts.items() if "color" in k), "")
            material = next((val for k, val in opts.items() if k.startswith("material") and "color" not in k), "")
            left, _, right = color_val.partition("|")
            color = (right or left).strip()
            color = re.sub(r"\s*\d+(\.\d+)?\s*k?g$", "", color, flags=re.I)
            refill = bool(re.search(r"\bRefill\b", material, re.I))
            material = re.sub(r"\s*\bRefill\b", "", material, flags=re.I).strip()
            label = fixed or SUNLU_MATERIAL.get(material, material) or (left.strip() if right else "")
            if not label or not color or not v.get("sku"):
                continue
            color = clean_sunlu_color(color, label, material, left)
            rank = 0 if "usa" in ship.lower() else 1 if not ship else 2
            k = (label, color, refill)
            if k not in best or rank < best[k][0]:
                best[k] = (rank, v["sku"])
        for (label, color, refill), (_, sku) in sorted(best.items(), key=lambda kv: kv[0][2]):
            t = types.setdefault(find_type(types, label), {"weight": 1000, "colors": []})
            add_color(t, color, None, None, sku, {"variant": "Refill" if refill else "Spool"})
            n += 1
    return n


# ---------------------------------------------------------------- Polymaker store
def poly_weight(s: str) -> int | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(k?g)\b", s or "", re.I)
    return int(float(m.group(1)) * (1000 if m.group(2).lower() == "kg" else 1)) if m else None


def poly_types(products: list[dict], spoolman: dict) -> dict[str, dict]:
    import collections
    known = {k: c["hex"] for t in spoolman.values() for c in t["colors"] if c["hex"] for k in name_keys(c["name"])}
    types: dict[str, dict] = {}
    for p in products:
        names = [o["name"].strip().lower() for o in p["options"]]
        if "color" not in names or "filament" not in (p.get("product_type") or "Polymaker filament").lower() + " filament":
            continue
        label = re.sub(r"™|®", "", p["title"]).strip()
        label = re.sub(r"^Polymaker\s+", "", label)
        weights = collections.Counter()
        rows = []
        for v in p["variants"]:
            opts = {names[i]: (v.get(f"option{i + 1}") or "").strip() for i in range(len(names))}
            if not v.get("sku"):
                continue
            w = poly_weight(next((val for k, val in opts.items() if "weight" in k), ""))
            d = next((val for k, val in opts.items() if "diameter" in k), "")
            dia = float(re.sub(r"[^0-9.]", "", d)) if re.search(r"\d", d) else 1.75
            kind = opts.get("type", "") or "Spool"
            weights[w] += 1
            rows.append((opts["color"], v["sku"].strip(), {"variant": kind, "weight": w, "diameter": dia}))
        if not rows:
            continue
        t = types.setdefault(label, {"weight": weights.most_common(1)[0][0] or 1000, "colors": []})
        words = {w.lower() for w in label.split()}
        for color, sku, variant in rows:
            short = " ".join(x for x in color.split() if x.lower() not in words) or color
            hex_ = next((known[k] for k in name_keys(color) | name_keys(short) if k in known), None)
            add_color(t, color, hex_, None, sku, variant)
    return types


def dedupe_sunlu_skus(types: dict) -> None:
    """A SKU listed under two lines stays only on the line whose code it shares
    (DLZ-US-SL-<LINE>-<COLOR>-1KG), e.g. HSPLAYG belongs to High Speed Matte, not Matte."""
    import collections
    line_of = lambda sku: (sku.split("-") + [""] * 4)[3]
    all_skus = lambda c: [e["sku"] for e in c.get("skus", [])] or ([c["sku"]] if c.get("sku") else [])
    usual = {t: collections.Counter(line_of(x) for c in v["colors"] for x in all_skus(c)).most_common(1)
             for t, v in types.items()}
    owners = collections.defaultdict(list)
    for t, v in types.items():
        for c in v["colors"]:
            for x in all_skus(c):
                owners[sku_key(x)].append((t, c, x))
    for key, holders in owners.items():
        if len({id(c) for _, c, _ in holders}) > 1:
            for t, c, x in holders:
                if usual[t] and usual[t][0][0] != line_of(x):
                    c["skus"] = [e for e in c.get("skus", []) if sku_key(e["sku"]) != key]
                    if sku_key(c.get("sku", "")) == key:
                        c.pop("sku", None)
                        if c["skus"]:
                            c["sku"] = c["skus"][0]["sku"]
                    if not c["skus"]:
                        c.pop("skus", None)


# ---------------------------------------------------------------- main
def main() -> None:
    cache = Path(sys.argv[sys.argv.index("--from") + 1]) if "--from" in sys.argv else None
    src = load_sources(cache)
    sm = {b: spoolman_types(b, src[f"spoolman_{b}"]) for b in ("bambulab", "snapmaker", "sunlu", "polymaker")}
    brands = {
        "Bambu Lab": bambu_types(src["bambu_codes"], sm["bambulab"]),
        "Snapmaker": sm["snapmaker"],
        "Sunlu": sm["sunlu"],
        "Polymaker": poly_types(src["store_polymaker"], sm["polymaker"]),
    }
    applied = {"Snapmaker": apply_snapmaker(brands["Snapmaker"], src["store_snapmaker"]),
               "Sunlu": apply_sunlu(brands["Sunlu"], src["store_sunlu"])}
    for b in ("Snapmaker", "Sunlu"):  # store-only colors: borrow the brand's hex for the same color name
        known = {k: c["hex"] for t in brands[b].values() for c in t["colors"] if c["hex"]
                 for k in name_keys(c["name"])}
        for t in brands[b].values():
            for c in t["colors"]:
                if not c["hex"]:
                    c["hex"] = next((known[k] for k in name_keys(c["name"]) if k in known), "")
                if not c["hex"] and "+" in c["name"]:  # "Red+Yellow" -> gradient of the parts
                    parts = [next((known[k] for k in name_keys(x) if k in known), None)
                             for x in c["name"].split("+")]
                    if all(parts):
                        c["hex"], c["hexes"] = parts[0], parts
    dedupe_sunlu_skus(brands["Sunlu"])
    for types in brands.values():
        pick_primary_skus(types)
    catalog = {
        "sources": [
            "Bambu Lab: Bambu Studio filaments_color_codes.json (https://github.com/bambulab/BambuStudio)",
            "Snapmaker: us.snapmaker.com product listings",
            "Sunlu: store.sunlu.com product listings (Ship to USA, 1 roll; spools and refills)",
            "Polymaker: us.polymaker.com product listings (every spool/refill/weight/diameter SKU)",
            "SpoolmanDB (https://github.com/Donkie/SpoolmanDB), MIT License, Copyright (c) Daniel Hultgren",
        ],
        "built": date.today().isoformat(),
        "brands": {b: dict(sorted(t.items(), key=lambda kv: kv[0].lower())) for b, t in brands.items()},
    }
    OUT.write_text(json.dumps(catalog, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    for b, types in catalog["brands"].items():
        cols = [c for t in types.values() for c in t["colors"]]
        with_sku = sum(1 for c in cols if c.get("sku"))
        n_skus = sum(max(1, len(c.get("skus", []))) for c in cols if c.get("sku"))
        no_hex = sum(1 for c in cols if not c["hex"])
        extra = f", {applied[b]} store variants applied" if b in applied else ""
        print(f"{b}: {len(types)} types, {len(cols)} colors, {with_sku} with SKU ({n_skus} SKUs), "
              f"{no_hex} without hex{extra}")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
