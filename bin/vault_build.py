#!/usr/bin/env python3
"""
vault_build.py — JIVO-INTEL LOSSLESS Obsidian "mega-brain" vault builder
=========================================================================

Owner-ratified rewrite (see shared/VAULT-DESIGN.md). Supersedes the previous
gz-attachment / computed-summary builder. Deterministic, stdlib-only, no network,
no LLM. Reads the zero-loss versioned SSOT (`store/versioned/*`, produced by
ssot.py — SPEC §3) and regenerates the whole Obsidian graph under `vault/`.

THE FOUR HARD LINES (from the owner — enforced by the integrity gates):
  1. NO omission     — every SSOT row of every table/doc appears in the vault.
  2. NO summarizing  — exact values verbatim. We never REPLACE raw rows with our
                       own computed summary. (We DO reproduce the app's own
                       dashboard aggregates verbatim, clearly labelled.)
  3. NO redundancy   — each row is embedded ONCE under its single canonical
                       entity; every other dimension is a [[wikilink]], never a copy.
  4. 100% Markdown   — bulk rows live in fenced ```csv blocks inside notes;
                       connections are body [[wikilinks]].

ARCHITECTURE
  * Entity HUB notes (type-prefixed, globally-unique basenames): sku-<sap>,
    pf-<slug>, cat-/subcat-/brand-/tier-, vendor-, po-<po_number>, <YYYY-MM>,
    city-/store-/fc-, + MOC indexes + index.md. Hubs carry identity frontmatter
    and the connective [[wikilinks]] — they are the "everything connects" layer.
  * Per-entity DATA notes carry the COMPLETE raw rows as ```csv (full original
    column set, every row). One data note per (entity, table); chunked into
    `<entity>.<table>.<n>` sub-notes when a note would exceed CHUNK_ROWS rows or
    CHUNK_BYTES bytes. Nothing is ever dropped — only paginated.
  * Canonical placement: for each raw table, every present row is assigned to
    exactly ONE canonical entity (its most-specific grain). Sell-out / inventory /
    ads / pricing rows → the SKU they're about (else the platform, when the listing
    can't be resolved to an internal SKU). PO rows → their po-<po_number> note.
  * App dashboards / master / notifications docs → captured verbatim in dash-*/
    app-* notes labelled `source: app-dashboard`, never mixed with raw rows.

INTEGRITY GATES (hard — nonzero exit; run_daily.sh refuses to commit a broken graph):
  check_unique()        no duplicate note basenames.
  check_links()         every body [[wikilink]] resolves to a real note.
  check_lossless()      per table, 3-way: changelog-replay present-count ==
                        state.jsonl present-count == Σ rows embedded in csv. Zero tol.
  check_no_orphan_rows()  every SSOT present key maps to exactly ONE canonical data
                        note (none unplaced, none duplicated).

Usage:  python3 bin/vault_build.py [--store store/versioned] [--vault vault]
"""

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from collections import defaultdict

GEN = "bin/vault_build.py"
CHUNK_ROWS = 5000            # split a per-entity table CSV beyond this many rows
CHUNK_BYTES = 1_000_000      # ...or beyond ~1 MB of CSV text, whichever comes first
FORBIDDEN = set('[]#^|/\\:')

# Dirs fully owned by this generator — rebuilt every run; a note not (re)written is
# an orphan (its entity left the store) and is pruned so the graph never drifts.
GENERATED_DIRS = ("skus", "platforms", "taxonomy", "vendors", "pos", "months",
                  "locations", "dashboards", "data")
EXEMPT_TOP = {"analysis"}    # hand-authored; link-checked but never pruned

# master `format` (and master_po/inventory `format`) -> platform slug
FORMAT_TO_SLUG = {
    "AMAZON": "amazon", "BLINKIT": "blinkit", "SWIGGY": "swiggy", "ZEPTO": "zepto",
    "BIG BASKET": "bigbasket", "BIGBASKET": "bigbasket", "FLIPKART": "flipkart",
    "FLIPKART GROCERY": "flipkart_grocery", "JIO MART": "jiomart", "JIOMART": "jiomart",
    "CITY MALL": "citymall", "CITYMALL": "citymall", "ZOMATO": "zomato",
    "DEAL SHARE": "dealshare", "DEALSHARE": "dealshare",
}

# ---- Canonical placement config (per registry table) ------------------------- #
# kind:   'sku'  -> resolve listing -> sku-<sap>; fall back to pf-<slug> if unresolved
#         'pf'   -> always pf-<slug> (no per-row listing identity, e.g. ads/coupon)
#         'po'   -> po-<po_number>; fall back to pf-<slug> if po_number missing
# slug:   a fixed platform slug, or "FORMAT" to derive per-row from row['format']
# sku_cols: ordered candidate columns carrying the platform listing code
# date_cols: ordered candidate columns for month derivation (sku<->month links)
P = lambda kind, slug, sku_cols=(), date_cols=(): {
    "kind": kind, "slug": slug, "sku_cols": list(sku_cols), "date_cols": list(date_cols)}

PLACEMENT = {
    # --- Amazon (archetype A) — listing code is the ASIN -----------------------
    "amazon_inventory":             P("sku", "amazon", ["asin"], ["inventory_date", "date"]),
    "amazon_price_data":            P("sku", "amazon", ["asin"], ["date", "price_date", "created_at"]),
    "amazon_sec_daily":             P("sku", "amazon", ["asin"], ["report_date", "from_date", "date"]),
    "amazon_sec_daily_master_view": P("sku", "amazon", ["asin"], ["from_date", "to_date"]),
    "amazon_sec_range":             P("sku", "amazon", ["asin"], ["from_date", "to_date"]),
    "amazon_sec_range_margins":     P("sku", "amazon", ["asin"], ["from_date", "to_date"]),
    "amazon_sec_range_master_view": P("sku", "amazon", ["asin"], ["from_date", "to_date"]),
    "amazon_ads":                   P("sku", "amazon", ["asin", "advertised_asin"], ["date", "report_date"]),
    "amazon_coupon":                P("sku", "amazon", ["asin"], ["start_date", "date"]),
    # --- Quick-commerce (archetype B) -----------------------------------------
    "swiggySec":        P("sku", "swiggy",  ["ITEM_CODE"], ["ORDERED_DATE"]),
    "swiggy_inventory": P("sku", "swiggy",  ["sku_code"], ["inventory_date", "date"]),
    "swiggy_brandfund": P("sku", "swiggy",  ["item_code"], ["date", "start_date"]),
    "swiggy_ads":       P("pf",  "swiggy",  [], ["date", "start_date"]),
    "blinkitSec":       P("sku", "blinkit", ["item_id"], ["date"]),
    "blinkit_inventory": P("sku", "blinkit", ["item_id"], ["inventory_date", "date"]),
    "blinkit_ads":      P("pf",  "blinkit", [], ["date"]),
    "blinkit_brandfund": P("pf", "blinkit", ["item_id"], ["date"]),
    "zeptoSec":         P("sku", "zepto",   ["SKU Number"], ["Date"]),
    "zepto_inventory":  P("sku", "zepto",   ["sku_code"], ["inventory_date", "date"]),
    "zepto_ads":        P("pf",  "zepto",   [], ["date"]),
    "zepto_brandfund":  P("pf",  "zepto",   [], ["date"]),
    # --- Marketplace / grocery (archetype C) ----------------------------------
    "bigbasketSec":      P("sku", "bigbasket", ["source_sku_id"], ["date_range", "date"]),
    "bigbasket_inventory": P("sku", "bigbasket", ["sku_id"], ["inventory_date", "date"]),
    "bigbasket_ads":     P("pf",  "bigbasket", [], ["date"]),
    "flipkartSec":       P("sku", "flipkart", ["Product Id"], ["Order Date", "real_date"]),
    "flipkart_secondary_all": P("sku", "flipkart", ["Product Id"], ["Order Date", "real_date"]),
    "flipkart_ads":      P("pf",  "flipkart", [], ["date"]),
    "flipkart_grocery_master": P("sku", "flipkart_grocery", ["sku_id"], ["date"]),
    "fk_grocery":        P("pf",  "flipkart_grocery", [], ["date"]),
    "jiomartSec":        P("sku", "jiomart", ["FSN_PRODUCT_ID"], ["ORDER_DATE"]),
    "jiomart_inventory": P("sku", "jiomart", ["sku_id"], ["inventory_date", "date"]),
    "citymallSec":       P("pf",  "citymall", [], ["date"]),
    "citymall_inventory": P("pf", "citymall", [], ["inventory_date", "date"]),
    "zomatoSec":         P("pf",  "zomato", [], ["date"]),
    "zomato_inventory":  P("pf",  "zomato", [], ["inventory_date", "date"]),
    # --- Unified SOH — slug is per-row 'format' --------------------------------
    "all_platform_inventory": P("sku", "FORMAT", ["sku_code"], ["inventory_date", "date"]),
    # --- PO / sell-in — canonical grain is the PO ------------------------------
    "master_po":     P("po", "FORMAT", ["sku_code"], ["po_date", "po_month"]),
    "total_po":      P("po", "FORMAT", ["sku_code"], ["po_date"]),
    "total_po_zbs":  P("po", "FORMAT", ["sku_code"], ["po_date"]),
    "prim_master_po": P("po", "FORMAT", ["sku_code"], ["po_date"]),
    "test_master_po": P("po", "FORMAT", ["sku_code"], ["po_date"]),
}

TAX = {}            # loaded from registry/taxonomy.json
VAULT = None        # set in main()
_WRITTEN = set()    # abspaths of .md written this run (drives prune_orphans)


# --------------------------------------------------------------------------- #
# Formatting helpers
# --------------------------------------------------------------------------- #
def yv(v):
    """Quote a scalar for safe flat YAML (no wikilinks ever live in frontmatter)."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "":
        return '""'
    if (any(c in s for c in ':#|[]{}",\'') or s[0] in '-?&*!%@`>' or
            s.lower() in ("null", "true", "false", "yes", "no")):
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


def frontmatter(pairs):
    out = ["---"]
    for k, v in pairs:
        if isinstance(v, list):
            out.append(f"{k}:")
            for it in v:
                out.append(f"  - {yv(it)}")
        else:
            out.append(f"{k}: {yv(v)}")
    out.append("---")
    return "\n".join(out)


def safe_name(s):
    """Globally-resolvable, non-empty Obsidian basename. Keeps case; replaces only
    Obsidian-forbidden chars + control chars; collapses runs of '-'."""
    s = (s or "").strip()
    cleaned = ["-" if (ch in FORBIDDEN or ord(ch) < 32) else ch for ch in s]
    s = "".join(cleaned).strip(" .")
    while "--" in s:
        s = s.replace("--", "-")
    return s or "untitled"


def tok(s):
    """Taxonomy token: uppercase, spaces/underscores -> '-', forbidden chars stripped."""
    s = safe_name(str(s or "")).upper().replace(" ", "-").replace("_", "-")
    while "--" in s:
        s = s.replace("--", "-")
    return s.strip("-") or "NA"


def link(basename, display=None):
    b = str(basename)
    return f"[[{b}|{display}]]" if (display is not None and str(display) != b) else f"[[{b}]]"


def link_row(basenames, sep=" · "):
    bs = [b for b in basenames if b]
    return sep.join(link(b) for b in bs) if bs else "_none_"


def _csv_cell(v):
    """Render a JSON value as a CSV cell, preserving fidelity: None->'', dict/list->
    compact JSON, float->repr (round-trippable), everything else->str."""
    if v is None:
        return ""
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(v)


def csv_text(rows, cols):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        w.writerow([_csv_cell(r.get(c)) for c in cols])
    return buf.getvalue().rstrip("\n")


def table_columns(rows):
    """Deterministic full column set: union of keys over rows (which arrive in sorted
    __key order), first-seen order, with injected __meta columns dropped from the CSV
    (kept in a trailing block so the raw API columns lead)."""
    cols, seen = [], set()
    meta = []
    for r in rows:
        for k in r.keys():
            if k in seen:
                continue
            seen.add(k)
            (meta if k.startswith("__") else cols).append(k)
    return cols + sorted(meta)


def footer(extra=""):
    tail = f" {extra}" if extra else ""
    return f"---\n*Auto-generated by `{GEN}` from `store/versioned/*` — deterministic rebuild.{tail}*"


# --------------------------------------------------------------------------- #
# Writers + orphan bookkeeping
# --------------------------------------------------------------------------- #
def write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not text.endswith("\n"):
        text += "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    _WRITTEN.add(os.path.abspath(path))


def write_note(subdir, basename, fm_pairs, body_lines):
    text = frontmatter(fm_pairs) + "\n\n" + "\n".join(body_lines)
    path = (os.path.join(VAULT, subdir, f"{basename}.md") if subdir
            else os.path.join(VAULT, f"{basename}.md"))
    write_file(path, text)
    return basename


# --------------------------------------------------------------------------- #
# Store loading (reads the ssot.py serialisation — SPEC §3)
# --------------------------------------------------------------------------- #
def read_jsonl(path):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def state_keys(store, table):
    """Present keys straight from <table>.state.jsonl (the authoritative index)."""
    path = os.path.join(store, "tables", f"{table}.state.jsonl")
    return {st["key"]: st for st in read_jsonl(path)}


def replay_present(store, table):
    """Independent present-key reconstruction from the append-only changelog
    (SPEC §3 semantics) — used by check_lossless as a third, independent leg."""
    path = os.path.join(store, "tables", f"{table}.changelog.jsonl")
    present = {}
    events = sorted(read_jsonl(path), key=lambda e: e.get("seq", 0))
    for ev in events:
        k, e = ev.get("key"), ev.get("event")
        if e == "delete":
            present.pop(k, None)
        else:
            present[k] = ev.get("hash")
    return present


def load_table_rows(store, table):
    """Reconstruct a table's PRESENT-state rows. state.jsonl gives the authoritative
    present (key,hash) set; the changelog insert/update events supply the row for each
    (key,hash). Each row gets __first_seen/__last_seen/__key injected. Returns rows in
    sorted-key order (deterministic)."""
    clog = os.path.join(store, "tables", f"{table}.changelog.jsonl")
    if not os.path.exists(clog):
        return []
    rowmap = {}
    for ev in read_jsonl(clog):
        k, h, e = ev.get("key"), ev.get("hash"), ev.get("event")
        if e in ("insert", "update") and ev.get("row") is not None:
            rowmap[(k, h)] = (ev["row"], ev.get("first_seen"))
    present = state_keys(store, table)
    out = []
    for key in sorted(present):
        st = present[key]
        h = st.get("hash")
        rv = rowmap.get((key, h))
        if not rv or rv[0] is None:
            continue                 # present key with no row -> caught by check_lossless
        r = dict(rv[0])
        r["__first_seen"] = st.get("first_seen") or rv[1]
        r["__last_seen"] = st.get("last_seen")
        r["__key"] = key
        out.append(r)
    return out


def _doc_rows(doc, depth=0):
    """Dig the list-of-rows out of a doc payload (master/notifications/fcs)."""
    if isinstance(doc, list):
        return [x for x in doc if isinstance(x, dict)]
    if isinstance(doc, dict) and depth < 6:
        for k in ("results", "data", "rows", "notifications", "products", "fcs", "items"):
            if k in doc:
                v = doc[k]
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
                if isinstance(v, dict):
                    sub = _doc_rows(v, depth + 1)
                    if sub:
                        return sub
        for v in doc.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
    return []


def load_latest_doc(path):
    """Latest recorded version of a doc-level changelog: (doc_payload, rows)."""
    last = None
    for ev in read_jsonl(path):
        last = ev
    if not last:
        return None, []
    doc = last.get("doc", last.get("row"))
    return doc, _doc_rows(doc)


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_MONTH_RE = re.compile(r"(\d{4})-(\d{2})")


def month_of(s):
    if not s:
        return None
    m = _DATE_RE.search(str(s)) or _MONTH_RE.search(str(s))
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}"


def month_shift(ym, delta):
    y, mo = int(ym[:4]), int(ym[5:7])
    idx = (y * 12 + (mo - 1)) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def first_val(row, cols):
    for c in cols:
        v = row.get(c)
        if v is not None and v != "":
            return v
    return None


def row_month(row, cfg):
    return month_of(first_val(row, cfg["date_cols"])) if cfg.get("date_cols") else None


# --------------------------------------------------------------------------- #
# Type-prefixed basename builders (globally unique by construction)
# --------------------------------------------------------------------------- #
def b_sku(sap):  return "sku-" + safe_name(str(sap))
def b_pf(slug):  return "pf-" + safe_name(str(slug))
def b_cat(c):    return "cat-" + tok(c)
def b_subcat(s): return "subcat-" + tok(s)
def b_brand(b):  return "brand-" + tok(b)
def b_tier(h):   return "tier-" + tok(h)
def b_vendor(v): return "vendor-" + tok(v)
def b_fc(c):     return "fc-" + tok(c)
def b_po(p):     return "po-" + safe_name(str(p))
def b_month(m):  return safe_name(str(m))


# --------------------------------------------------------------------------- #
# Model: listing->sku bridge + sku taxonomy + entity registries
# --------------------------------------------------------------------------- #
class Model:
    def __init__(self):
        self.skus = {}             # sap -> dict(identity + connective sets)
        self.listing2sap = {}      # (FORMAT_UPPER, code) -> sap
        self.slug_listing2sap = {} # (slug, code) -> sap
        self.pfs = set()           # slugs
        self.pos = {}              # po_number -> dict(vendors,skus,months,formats)
        self.vendors = defaultdict(lambda: {"skus": set(), "pos": set(), "formats": set()})
        self.cats = defaultdict(lambda: {"subcats": set(), "skus": set(), "brands": set()})
        self.subcats = defaultdict(lambda: {"cat": None, "skus": set()})
        self.brands = defaultdict(lambda: {"skus": set(), "cats": set()})
        self.tiers = defaultdict(lambda: {"skus": set()})
        self.months = defaultdict(lambda: {"skus": set(), "slugs": set(), "pos": set()})
        self.fcs = {}              # fc_code -> row
        self.cities = defaultdict(lambda: {"slugs": set(), "skus": set(), "stores": set()})
        self.stores = defaultdict(lambda: {"city": None, "slug": None, "skus": set()})

    def sku(self, sap):
        if sap not in self.skus:
            self.skus[sap] = {
                "sap": sap, "item": None, "item_head": None, "per_unit_value": None,
                "sap_sku_name": None, "brand": None, "category": None, "sub_category": None,
                "listings": defaultdict(set), "platforms": set(), "vendors": set(),
                "months": set(), "pos": set(), "first_seen": None, "last_seen": None,
            }
        return self.skus[sap]


def build_model(store):
    m = Model()
    products_doc, mp = load_latest_doc(os.path.join(store, "master", "products.changelog.jsonl"))
    fcs_doc, fcs = load_latest_doc(os.path.join(store, "master", "fcs.changelog.jsonl"))
    notif_doc, notifs = load_latest_doc(os.path.join(store, "notifications.changelog.jsonl"))

    # 1. master products: SKU identity + listing->sap bridge
    for r in mp:
        sap = r.get("sku_sap_code")
        fmt = (r.get("format") or "").upper()
        code = r.get("format_sku_code")
        if not sap:
            continue
        s = m.sku(sap)
        s["item"] = s["item"] or r.get("item")
        s["item_head"] = s["item_head"] or r.get("item_head")
        if s["per_unit_value"] is None and r.get("per_unit_value") is not None:
            s["per_unit_value"] = r.get("per_unit_value")
        s["sap_sku_name"] = s["sap_sku_name"] or r.get("sku_sap_name")
        if code:
            slug = FORMAT_TO_SLUG.get(fmt, safe_name((fmt or "na").lower()))
            s["listings"][slug].add(str(code))
            s["platforms"].add(slug)
            m.pfs.add(slug)
            m.listing2sap[(fmt, str(code))] = sap
            m.slug_listing2sap[(slug, str(code))] = sap

    # 2. master_po: taxonomy (brand/cat/subcat/item_head), vendors, months, PO entities
    for r in load_table_rows(store, "master_po"):
        fmt = (r.get("format") or "").upper()
        code = str(r.get("sku_code") or "")
        sap = m.listing2sap.get((fmt, code)) or m.slug_listing2sap.get(
            (FORMAT_TO_SLUG.get(fmt, ""), code))
        ym = month_of(r.get("po_date")) or month_of(r.get("po_month"))
        vend = r.get("vendor_new") or r.get("vendor_name")
        if sap:
            s = m.sku(sap)
            s["category"] = s["category"] or r.get("category")
            s["sub_category"] = s["sub_category"] or r.get("sub_category")
            s["brand"] = s["brand"] or r.get("brand")
            s["item_head"] = s["item_head"] or r.get("item_head")
            if vend:
                s["vendors"].add(vend)
            if ym:
                s["months"].add(ym)
        if vend:
            m.vendors[vend]["formats"].add(FORMAT_TO_SLUG.get(fmt, fmt.lower()) if fmt else "")
            if sap:
                m.vendors[vend]["skus"].add(sap)

    # 3. notifications: taxonomy fallback per listing
    for r in notifs:
        slug = r.get("platform_slug") or FORMAT_TO_SLUG.get((r.get("format") or "").upper())
        code = str(r.get("sku_code") or "")
        sap = m.slug_listing2sap.get((slug, code)) if slug else None
        if sap:
            s = m.sku(sap)
            s["brand"] = s["brand"] or r.get("brand")
            s["category"] = s["category"] or r.get("category")
            s["sub_category"] = s["sub_category"] or r.get("sub_category")
            s["item_head"] = s["item_head"] or r.get("item_head")

    # 4. fulfilment centres (skip the HLK1 '#N/A' junk row)
    for r in fcs:
        code = r.get("fc_code")
        if code and not str(code).upper().startswith("#"):
            m.fcs[code] = r

    m._docs = {"products": (products_doc, mp), "fcs": (fcs_doc, fcs),
               "notifications": (notif_doc, notifs)}
    return m


# --------------------------------------------------------------------------- #
# Canonical placement — assign every present row to exactly one entity note
# --------------------------------------------------------------------------- #
def resolve_slug(cfg, row):
    if cfg["slug"] == "FORMAT":
        fmt = (row.get("format") or "").upper()
        return FORMAT_TO_SLUG.get(fmt, safe_name((fmt or "na").lower()))
    return cfg["slug"]


def resolve_sap(m, cfg, row, slug):
    code = first_val(row, cfg["sku_cols"])
    if code is None:
        return None
    code = str(code)
    sap = m.slug_listing2sap.get((slug, code))
    if sap is None:
        fmt = next((f for f, s in FORMAT_TO_SLUG.items() if s == slug), slug.upper())
        sap = m.listing2sap.get((fmt, code))
    return sap


def place_rows(store, m):
    """Stream per-table: load ONE table's present rows, assign each to its single
    canonical entity, emit that table's data notes immediately (memory bounded to one
    table at a time — critical for the full ~1.8GB backfill), then free the rows.
    Returns (children, placed, embed_count):
       children    {entity_basename: [data_note_basename, ...]}  (for hub links)
       placed      {table: {key: entity_basename}}               (orphan gate)
       embed_count {table: int}                                  (lossless gate)
    """
    children = defaultdict(list)
    placed = defaultdict(dict)
    embed_count = defaultdict(int)

    for table in sorted(PLACEMENT):
        cfg = PLACEMENT[table]
        group = defaultdict(list)          # entity_basename -> rows (THIS table only)
        for r in load_table_rows(store, table):
            key = r.get("__key")
            slug = resolve_slug(cfg, r)
            m.pfs.add(slug)
            ym = row_month(r, cfg)
            if cfg["kind"] == "po":
                po = r.get("po_number")
                if po is None or str(po) == "":
                    base = b_pf(slug)            # fallback: PO row has no number
                else:
                    po = str(po)
                    base = b_po(po)
                    pe = m.pos.setdefault(po, {"vendors": set(), "skus": set(),
                                               "months": set(), "formats": set()})
                    vend = r.get("vendor_new") or r.get("vendor_name")
                    sap = resolve_sap(m, cfg, r, slug)
                    if vend:
                        pe["vendors"].add(vend)
                        m.vendors[vend]["pos"].add(po)
                    if sap:
                        pe["skus"].add(sap)
                        m.sku(sap)["pos"].add(po)
                    if ym:
                        pe["months"].add(ym)
                        m.months[ym]["pos"].add(po)
                    pe["formats"].add(slug)
            else:
                sap = resolve_sap(m, cfg, r, slug) if cfg["kind"] == "sku" else None
                if sap:
                    base = b_sku(sap)
                    s = m.sku(sap)
                    s["platforms"].add(slug)
                    if ym:
                        s["months"].add(ym)
                else:
                    base = b_pf(slug)
            if ym:
                m.months[ym]["slugs"].add(slug)
            group[base].append(r)
            placed[table][key] = base
            embed_count[table] += 1
        # emit THIS table's data notes now, then drop its rows
        for base in sorted(group):
            for cbase in emit_entity_table(base, table, group[base], "raw-table"):
                children[base].append(cbase)
        group.clear()

    # finalise taxonomy registries from sku facts (for connective hub links)
    for sap, s in m.skus.items():
        if s["category"]:
            m.cats[s["category"]]["skus"].add(sap)
            if s["brand"]:
                m.cats[s["category"]]["brands"].add(s["brand"])
            if s["sub_category"]:
                m.cats[s["category"]]["subcats"].add(s["sub_category"])
        if s["sub_category"]:
            m.subcats[s["sub_category"]]["skus"].add(sap)
            m.subcats[s["sub_category"]]["cat"] = m.subcats[s["sub_category"]]["cat"] or s["category"]
        if s["brand"]:
            m.brands[s["brand"]]["skus"].add(sap)
            if s["category"]:
                m.brands[s["brand"]]["cats"].add(s["category"])
        if s["item_head"]:
            m.tiers[s["item_head"]]["skus"].add(sap)
        for ym in s["months"]:
            m.months[ym]["skus"].add(sap)
    return children, placed, embed_count


# --------------------------------------------------------------------------- #
# Data-note emission (the COMPLETE rows, chunked, never dropped)
# --------------------------------------------------------------------------- #
def emit_entity_table(base, table, rows, source):
    """Write one (or more, chunked) data note holding the COMPLETE rows for one
    (entity, table) pair as verbatim ```csv. Chunks at CHUNK_ROWS / CHUNK_BYTES.
    Returns the list of data-note basenames written."""
    cols = table_columns(rows)
    chunks, cur, cur_bytes = [], [], 0
    for r in rows:
        line_bytes = len(csv_text([r], cols)) + 1
        if cur and (len(cur) >= CHUNK_ROWS or cur_bytes + line_bytes > CHUNK_BYTES):
            chunks.append(cur)
            cur, cur_bytes = [], 0
        cur.append(r)
        cur_bytes += line_bytes
    if cur or not rows:
        chunks.append(cur)
    n = len(chunks)
    written = []
    for i, chunk in enumerate(chunks, 1):
        cbase = f"{base}.{table}" + (f".{i}" if n > 1 else "")
        chunk_lbl = f"{i}/{n}" if n > 1 else "1/1"
        fm = [("type", "data"), ("entity", base), ("table", table),
              ("rows", len(chunk)), ("chunk", chunk_lbl), ("source", source),
              ("tags", ["type/data", f"table/{table}", f"source/{source}"])]
        body = [f"# Raw `{table}` rows for {link(base)}"
                + (f" — chunk {chunk_lbl}" if n > 1 else ""), "",
                "Up: " + link(base),
                f"Canonical raw rows ({len(chunk)}) for `{table}`, embedded once here "
                "(verbatim, full column set). Other dimensions are links on the parent.", "",
                "```csv", csv_text(chunk, cols), "```", "", footer()]
        write_note("data", cbase, fm, body)
        written.append(cbase)
    return written


# --------------------------------------------------------------------------- #
# Hub note builders (connective layer — identity + [[wikilinks]] + data links)
# --------------------------------------------------------------------------- #
def data_section(children, base):
    kids = sorted(children.get(base, []))
    if not kids:
        return ["", "## Complete data", "_no raw rows under this entity_"]
    by_table = defaultdict(list)
    for k in kids:
        # child basename = <base>.<table>[.<n>]
        rest = k[len(base) + 1:]
        table = rest.split(".")[0] if "." in rest else rest
        by_table[table].append(k)
    out = ["", "## Complete data",
           "Full raw rows (embedded once here — this is the canonical home):"]
    for table in sorted(by_table):
        out.append(f"- `{table}`: " + link_row(sorted(by_table[table])))
    return out


def build_sku_hub(m, sap, children):
    s = m.skus[sap]
    item = s["item"] or sap
    head = s["item_head"]
    slugs = sorted(s["platforms"] | set(s["listings"]))
    latest_month = max(s["months"]) if s["months"] else None
    fm = [("type", "sku-hub"), ("sku_sap_code", sap), ("item", item),
          ("item_head", head or ""),
          ("per_unit_value", s["per_unit_value"] if s["per_unit_value"] is not None else ""),
          ("brand", s["brand"] or ""), ("category", s["category"] or ""),
          ("sub_category", s["sub_category"] or ""), ("platforms", slugs),
          ("vendors", sorted(s["vendors"])), ("pos", sorted(s["pos"])),
          ("months", sorted(s["months"])),
          ("tags", ["type/sku-hub"] + ([f"tier/{tok(head)}"] if head else [])
           + [f"platform/{p}" for p in slugs])]
    edges = []
    if head:
        edges.append(link(b_tier(head), head))
    if s["brand"]:
        edges.append(link(b_brand(s["brand"]), s["brand"]))
    if s["category"]:
        edges.append(link(b_cat(s["category"]), s["category"]))
    if s["sub_category"]:
        edges.append(link(b_subcat(s["sub_category"]), s["sub_category"]))
    body = [f"# {item} — `{sap}`", "", "Up: " + link("skus-index"), "",
            f"**{s['sap_sku_name'] or item}** · tier **{head or '—'}** · "
            f"per-unit {s['per_unit_value'] if s['per_unit_value'] is not None else '—'} L", "",
            "## Connects to", "Everything this SKU touches (the convergence node):",
            "- **Taxonomy:** " + (" · ".join(edges) if edges else "_unmapped_"),
            "- **Sells on:** " + link_row([b_pf(p) for p in slugs])]
    if s["vendors"]:
        body.append("- **Shipped by:** " + link_row([b_vendor(v) for v in sorted(s["vendors"])]))
    if s["pos"]:
        body.append("- **POs:** " + link_row([b_po(p) for p in sorted(s["pos"])]))
    if latest_month:
        body.append("- **Latest month:** " + link(b_month(latest_month)))
    if s["months"]:
        body.append("- **Active months:** " + link_row([b_month(x) for x in sorted(s["months"])]))
    body += ["", "## Platform listings",
             "| Platform | Listing codes |", "|---|---|"]
    for slug in slugs:
        codes = ", ".join(sorted(s["listings"].get(slug, []))) or "_(resolved via data)_"
        body.append(f"| {link(b_pf(slug), slug)} | {codes} |")
    body += data_section(children, b_sku(sap))
    body += ["", footer()]
    return write_note("skus", b_sku(sap), fm, body)


def build_platform_hub(m, slug, children):
    arche = next((a for a, mem in (TAX.get("archetypes") or {}).items() if slug in mem), None)
    arche_label = {"A_vendor_central": "A — Vendor-Central e-com",
                   "B_qcomm": "B — Quick-commerce (availability battleground)",
                   "C_marketplace_grocery": "C — Marketplace / grocery long-tail"}.get(arche, "—")
    skus = sorted(s for s, v in m.skus.items() if slug in (v["platforms"] | set(v["listings"])))
    empty = slug in (TAX.get("empty_platforms") or [])
    fm = [("type", "platform-hub"), ("platform", slug), ("archetype", arche or ""),
          ("skus_listed", len(skus)), ("sell_out_empty", empty),
          ("tags", ["type/platform-hub", "moc", f"platform/{slug}"]
           + ([f"archetype/{arche}"] if arche else []))]
    body = [f"# {slug} — platform hub", "", "Up: " + link("platforms-index"), "",
            f"**Archetype {arche_label}**" + ("  ·  ⚠️ sell-out empty/stale" if empty else ""), "",
            f"## SKUs listed here ({len(skus)})",
            link_row([b_sku(x) for x in skus]) if skus else "_none_"]
    body += data_section(children, b_pf(slug))
    body += ["", footer()]
    return write_note("platforms", b_pf(slug), fm, body)


def build_po_hub(m, po, children):
    info = m.pos[po]
    fm = [("type", "po"), ("po_number", po), ("vendors", sorted(info["vendors"])),
          ("skus", sorted(info["skus"])), ("months", sorted(info["months"])),
          ("platforms", sorted(f for f in info["formats"] if f)),
          ("tags", ["type/po"] + [f"platform/{f}" for f in sorted(info["formats"]) if f])]
    body = [f"# PO {po}", "", "Up: " + link("pos-index"), "",
            "## Connects to",
            "- **Vendors:** " + link_row([b_vendor(v) for v in sorted(info["vendors"])]),
            "- **SKUs:** " + link_row([b_sku(x) for x in sorted(info["skus"])]),
            "- **Platforms:** " + link_row([b_pf(f) for f in sorted(info["formats"]) if f]),
            "- **Months:** " + link_row([b_month(x) for x in sorted(info["months"])])]
    body += data_section(children, b_po(po))
    body += ["", footer()]
    return write_note("pos", b_po(po), fm, body)


def build_vendor(m, vend):
    info = m.vendors[vend]
    skus = sorted(info["skus"])
    fm = [("type", "vendor"), ("vendor", vend), ("po_count", len(info["pos"])),
          ("skus", len(skus)), ("tags", ["type/vendor", f"vendor/{tok(vend)}"])]
    body = [f"# Distributor {vend}", "", "Up: " + link("vendors-index"), "",
            "- **Platforms:** " + link_row([b_pf(s) for s in sorted(info["formats"]) if s]),
            "- **POs:** " + link_row([b_po(p) for p in sorted(info["pos"])]),
            "", f"## SKUs shipped ({len(skus)})", link_row([b_sku(x) for x in skus]),
            "", footer()]
    return write_note("vendors", b_vendor(vend), fm, body)


def build_tier(m, head):
    skus = sorted(m.tiers[head]["skus"])
    anchor = (head.upper() == "PREMIUM")
    fm = [("type", "tier"), ("item_head", head), ("premium_anchor", anchor),
          ("skus", len(skus)), ("tags", ["type/tier", f"tier/{tok(head)}"])]
    body = [f"# Tier {head}", "", "Up: " + link("taxonomy-index"), "",
            ("**Premium-mix anchor** — the North-Star KPI is premium-mix ≈ 52% by litres."
             if anchor else f"Product tier **{head}**."),
            "", f"## SKUs in this tier ({len(skus)})", link_row([b_sku(x) for x in skus]),
            "", footer()]
    return write_note("taxonomy", b_tier(head), fm, body)


def build_brand(m, brand):
    info = m.brands[brand]
    skus = sorted(info["skus"])
    fm = [("type", "brand"), ("brand", brand), ("skus", len(skus)),
          ("tags", ["type/brand", f"brand/{tok(brand)}"])]
    body = [f"# Brand {brand}", "", "Up: " + link("taxonomy-index"), "",
            "## Categories: " + link_row([b_cat(c) for c in sorted(info["cats"])]),
            "", f"## SKUs ({len(skus)})", link_row([b_sku(x) for x in skus]), "", footer()]
    return write_note("taxonomy", b_brand(brand), fm, body)


def build_category(m, cat):
    info = m.cats[cat]
    skus = sorted(info["skus"])
    fm = [("type", "category"), ("category", cat), ("skus", len(skus)),
          ("tags", ["type/category", f"category/{tok(cat)}"])]
    body = [f"# Category {cat}", "", "Up: " + link("taxonomy-index"), "",
            "## Sub-categories: " + link_row([b_subcat(x) for x in sorted(info["subcats"])]),
            "## Brands: " + link_row([b_brand(x) for x in sorted(info["brands"])]),
            "", f"## SKUs ({len(skus)})", link_row([b_sku(x) for x in skus]), "", footer()]
    return write_note("taxonomy", b_cat(cat), fm, body)


def build_subcat(m, sub):
    info = m.subcats[sub]
    skus = sorted(info["skus"])
    fm = [("type", "sub_category"), ("sub_category", sub), ("category", info["cat"] or ""),
          ("skus", len(skus)), ("tags", ["type/sub_category", f"sub_category/{tok(sub)}"])]
    body = [f"# Sub-category {sub}", "", "Up: "
            + (link(b_cat(info["cat"])) if info["cat"] else link("taxonomy-index")), "",
            f"## SKUs ({len(skus)})", link_row([b_sku(x) for x in skus]), "", footer()]
    return write_note("taxonomy", b_subcat(sub), fm, body)


def build_fc(m, code):
    r = m.fcs[code]
    city = r.get("city")
    fm = [("type", "fc"), ("fc_code", code), ("fc_id", r.get("fc_id")),
          ("city", city or ""), ("state", r.get("state") or ""),
          ("tags", ["type/fc", f"fc/{tok(code)}"])]
    body = [f"# FC {code}", "", "Up: " + link("locations-index"), "",
            f"Amazon fulfilment centre **{code}**"
            + (f" — {city}, {r.get('state') or ''}".rstrip(", ") if city else "") + ".",
            "", "_E-com (Amazon) geography — disjoint from q-comm dark-stores._", "", footer()]
    return write_note("locations", b_fc(code), fm, body)


def build_month(m, ym):
    info = m.months[ym]
    prev_m, next_m = month_shift(ym, -1), month_shift(ym, +1)
    nav = []
    if prev_m in m.months:
        nav.append("Prev: " + link(b_month(prev_m)))
    if next_m in m.months:
        nav.append("Next: " + link(b_month(next_m)))
    fm = [("type", "month"), ("month", ym), ("skus_active", len(info["skus"])),
          ("pos", len(info["pos"])), ("tags", ["type/month", f"month/{ym}"])]
    body = [f"# {ym} — month rollup", "", "Up: " + link("months-index")
            + ((" · " + " · ".join(nav)) if nav else ""), "",
            "Connective month note. Raw rows live under their SKU/PO; app aggregates "
            "for this month (if any) are embedded in the linked `dash-*` notes.", "",
            "## Platforms active: " + link_row([b_pf(s) for s in sorted(info["slugs"])]),
            f"## SKUs active ({len(info['skus'])}): "
            + link_row([b_sku(x) for x in sorted(info["skus"])]),
            "## POs: " + link_row([b_po(p) for p in sorted(info["pos"])]),
            "", footer()]
    return write_note("months", b_month(ym), fm, body)


# --------------------------------------------------------------------------- #
# Doc-level: master products / fcs / notifications + dashboards (verbatim)
# --------------------------------------------------------------------------- #
def emit_docs(store, m):
    """Embed app docs VERBATIM, labelled. master products & notifications rows are
    placed (once) as raw csv under their SKU/platform; dashboards captured verbatim
    as ```json. Returns (doc_embed_counts, dash_notes)."""
    counts = {}
    dash_notes = []

    # master products rows -> canonical SKU (their grain is the listing) ----------
    prod_children = defaultdict(list)
    _, mp = m._docs["products"]
    prod_buckets = defaultdict(list)
    placed = 0
    for r in mp:
        sap = r.get("sku_sap_code")
        if sap:
            prod_buckets[b_sku(sap)].append(r)
        else:                                  # unmapped listing -> platform fallback
            fmt = (r.get("format") or "").upper()
            prod_buckets[b_pf(FORMAT_TO_SLUG.get(fmt, safe_name((fmt or "na").lower())))].append(r)
        placed += 1
    for base in sorted(prod_buckets):
        for cbase in emit_entity_table(base, "master_products", prod_buckets[base], "app-master"):
            prod_children[base].append(cbase)
    counts["master_products"] = (placed, len(mp))

    # notifications rows -> canonical SKU/platform --------------------------------
    _, notifs = m._docs["notifications"]
    notif_buckets = defaultdict(list)
    for r in notifs:
        slug = r.get("platform_slug") or FORMAT_TO_SLUG.get((r.get("format") or "").upper(), "amazon")
        code = str(r.get("sku_code") or "")
        sap = m.slug_listing2sap.get((slug, code)) if slug else None
        notif_buckets[b_sku(sap) if sap else b_pf(slug)].append(r)
    for base in sorted(notif_buckets):
        for cbase in emit_entity_table(base, "notifications", notif_buckets[base], "app-notifications"):
            prod_children[base].append(cbase)
    counts["notifications"] = (sum(len(v) for v in notif_buckets.values()), len(notifs))

    # dashboards -> verbatim json notes, labelled, linked to entity if derivable ---
    dash_dir = os.path.join(store, "dashboards")
    if os.path.isdir(dash_dir):
        for fn in sorted(os.listdir(dash_dir)):
            if not fn.endswith(".changelog.jsonl"):
                continue
            key = fn[:-len(".changelog.jsonl")]
            doc, _ = load_latest_doc(os.path.join(dash_dir, fn))
            if doc is None:
                continue
            ym = month_of(key)
            slug = next((s for s in sorted(m.pfs, key=len, reverse=True)
                         if s and s in key.lower()), None)
            up_links = [link("dashboards-index")]
            if ym and ym in m.months:
                up_links.append(link(b_month(ym)))
            if slug:
                up_links.append(link(b_pf(slug)))
            base = "dash-" + safe_name(key)
            pretty = json.dumps(doc, ensure_ascii=False, indent=1, sort_keys=True)
            write_note("dashboards", base,
                       [("type", "app-dashboard"), ("endpoint_key", key),
                        ("source", "app-dashboard"), ("month", ym or ""), ("platform", slug or ""),
                        ("tags", ["type/app-dashboard", "source/app-dashboard"]
                         + ([f"platform/{slug}"] if slug else [])
                         + ([f"month/{ym}"] if ym else []))],
                       [f"# App dashboard — `{key}`", "", "Up: " + " · ".join(up_links), "",
                        f"> **source: app-dashboard `{key}`** — the app's OWN computed aggregate, "
                        "captured verbatim (NOT a summary we invented; NOT raw rows).", "",
                        "```json", pretty, "```", "", footer()])
            dash_notes.append(base)
    return counts, dash_notes, prod_children


# --------------------------------------------------------------------------- #
# Indexes / MOCs
# --------------------------------------------------------------------------- #
def build_indexes(m, dash_notes):
    skus = sorted(m.skus)
    write_note("", "skus-index",
               [("type", "moc"), ("title", "SKUs"), ("count", len(skus)),
                ("tags", ["moc", "type/sku-moc"])],
               [f"# SKUs — Map of Content ({len(skus)} hubs)", "", "Up: " + link("index"), "",
                "Every internal SKU hub — the convergence nodes where all platform listings meet."]
               + [f"- {link(b_sku(x))} — {m.skus[x]['item'] or ''} "
                  f"({m.skus[x]['item_head'] or '—'})" for x in skus] + ["", footer()])
    slugs = sorted(m.pfs)
    write_note("", "platforms-index",
               [("type", "moc"), ("title", "Platforms"), ("count", len(slugs)),
                ("tags", ["moc", "type/platform-moc"])],
               [f"# Platforms — Map of Content ({len(slugs)})", "", "Up: " + link("index"), ""]
               + [f"- {link(b_pf(s))}" for s in slugs] + ["", footer()])
    write_note("", "taxonomy-index",
               [("type", "moc"), ("title", "Taxonomy"), ("tags", ["moc", "type/taxonomy-moc"])],
               [f"# Taxonomy — Map of Content", "", "Up: " + link("index"), "",
                "## Tiers", link_row([b_tier(x) for x in sorted(m.tiers)]),
                "## Brands", link_row([b_brand(x) for x in sorted(m.brands)]),
                "## Categories", link_row([b_cat(x) for x in sorted(m.cats)]),
                "## Sub-categories", link_row([b_subcat(x) for x in sorted(m.subcats)]),
                "", footer()])
    vends = sorted(m.vendors)
    write_note("", "vendors-index",
               [("type", "moc"), ("title", "Distributors"), ("count", len(vends)),
                ("tags", ["moc", "type/vendor-moc"])],
               [f"# Distributors — Map of Content ({len(vends)})", "", "Up: " + link("index"), ""]
               + [f"- {link(b_vendor(v))} ({len(m.vendors[v]['pos'])} POs)" for v in vends]
               + ["", footer()])
    pos = sorted(m.pos)
    write_note("", "pos-index",
               [("type", "moc"), ("title", "Purchase Orders"), ("count", len(pos)),
                ("tags", ["moc", "type/po-moc"])],
               [f"# Purchase Orders — Map of Content ({len(pos)})", "", "Up: " + link("index"), "",
                "Each PO note is the canonical home of its master_po / total_po(_zbs) line rows."]
               + [f"- {link(b_po(p))}" for p in pos] + ["", footer()])
    cities = sorted(m.cities)
    fcs = sorted(m.fcs)
    write_note("", "locations-index",
               [("type", "moc"), ("title", "Locations"), ("cities", len(cities)),
                ("fcs", len(fcs)), ("tags", ["moc", "type/location-moc"])],
               [f"# Locations — Map of Content", "", "Up: " + link("index"), "",
                f"## Amazon fulfilment centres ({len(fcs)})", link_row([b_fc(x) for x in fcs]),
                f"## Q-commerce cities ({len(cities)})", link_row([b_city(x) for x in cities]),
                "", footer()])
    months = sorted(m.months)
    write_note("", "months-index",
               [("type", "moc"), ("title", "Months"), ("count", len(months)),
                ("tags", ["moc", "type/month-moc"])],
               [f"# Months — Map of Content ({len(months)})", "", "Up: " + link("index"), ""]
               + [f"- {link(b_month(x))}" for x in months] + ["", footer()])
    write_note("", "dashboards-index",
               [("type", "moc"), ("title", "App dashboards"), ("count", len(dash_notes)),
                ("tags", ["moc", "type/dashboard-moc"])],
               [f"# App dashboards — Map of Content ({len(dash_notes)})", "", "Up: " + link("index"), "",
                "The app's OWN computed aggregates, captured verbatim (labelled `source: app-dashboard`)."]
               + [f"- {link(d)}" for d in sorted(dash_notes)] + ["", footer()])
    # Home
    body = ["# JIVO E-Com Intelligence — Memory Vault", "",
            "**Home MOC.** Lossless, deterministic projection of the zero-loss SSOT. Every raw row is "
            "embedded ONCE (as ```csv) under its single canonical entity; every other dimension is a "
            "body wikilink. The SKU hub is THE convergence node.", "",
            "## Maps of Content",
            f"- {link('skus-index')} — {len(m.skus)} SKU hubs",
            f"- {link('platforms-index')} — {len(m.pfs)} platforms",
            f"- {link('taxonomy-index')} — tiers · brands · categories · sub-categories",
            f"- {link('vendors-index')} — {len(m.vendors)} distributors",
            f"- {link('pos-index')} — {len(m.pos)} purchase orders",
            f"- {link('locations-index')} — {len(m.fcs)} FCs · {len(m.cities)} cities",
            f"- {link('months-index')} — {len(m.months)} months",
            f"- {link('dashboards-index')} — {len(dash_notes)} app dashboards", "", footer()]
    write_note("", "index", [("type", "home-moc"), ("title", "JIVO E-Com Intelligence"),
                             ("tags", ["moc", "home"])], body)


def b_city(c):  return "city-" + tok(c)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def build(store):
    m = build_model(store)
    children, placed, embed_count = place_rows(store, m)
    doc_counts, dash_notes, prod_children = emit_docs(store, m)
    # merge doc children into the per-entity data-link map
    for base, kids in prod_children.items():
        children[base].extend(kids)

    for sap in sorted(m.skus):
        build_sku_hub(m, sap, children)
    for slug in sorted(m.pfs):
        build_platform_hub(m, slug, children)
    for po in sorted(m.pos):
        build_po_hub(m, po, children)
    for vend in sorted(m.vendors):
        build_vendor(m, vend)
    for head in sorted(m.tiers):
        build_tier(m, head)
    for brand in sorted(m.brands):
        build_brand(m, brand)
    for cat in sorted(m.cats):
        build_category(m, cat)
    for sub in sorted(m.subcats):
        build_subcat(m, sub)
    for code in sorted(m.fcs):
        build_fc(m, code)
    for ym in sorted(m.months):
        build_month(m, ym)
    build_indexes(m, dash_notes)

    pruned = prune_orphans()
    print(f"vault_build: {len(m.skus)} SKUs · {len(m.pfs)} platforms · {len(m.pos)} POs · "
          f"{len(m.vendors)} vendors · {len(m.cats)} cat/{len(m.subcats)} subcat · "
          f"{len(m.months)} months · {len(dash_notes)} dashboards"
          + (f" · pruned {len(pruned)}" if pruned else ""))
    return m, placed, embed_count, doc_counts


def prune_orphans():
    removed = []
    for sub in GENERATED_DIRS:
        base = os.path.join(VAULT, sub)
        if not os.path.isdir(base):
            continue
        for dp, _, fns in os.walk(base):
            for fn in fns:
                if fn.endswith(".md") and os.path.abspath(os.path.join(dp, fn)) not in _WRITTEN:
                    os.remove(os.path.join(dp, fn))
                    removed.append(os.path.relpath(os.path.join(dp, fn), VAULT))
    return sorted(removed)


# --------------------------------------------------------------------------- #
# Integrity gates (hard — nonzero exit)
# --------------------------------------------------------------------------- #
def check_unique():
    seen = defaultdict(list)
    for dp, _, fns in os.walk(VAULT):
        if ".obsidian" in dp.split(os.sep):
            continue
        for fn in fns:
            if fn.endswith(".md"):
                seen[fn.lower()].append(os.path.relpath(os.path.join(dp, fn), VAULT))
    dups = {k: v for k, v in seen.items() if len(v) > 1}
    if dups:
        print("vault_build: BASENAME COLLISIONS:", file=sys.stderr)
        for k, v in sorted(dups.items()):
            print(f"  {k}: {v}", file=sys.stderr)
        return False
    print(f"vault_build: check_unique OK — {len(seen)} unique note basenames")
    return True


def check_links():
    link_re = re.compile(r"\[\[([^\]\n]*?)\]\]")
    basenames, notes = set(), []
    for dp, _, fns in os.walk(VAULT):
        if ".obsidian" in dp.split(os.sep):
            continue
        for fn in fns:
            if fn.endswith(".md"):
                basenames.add(fn[:-3].lower())
                notes.append(os.path.join(dp, fn))
    broken = []
    for fp in notes:
        rel = os.path.relpath(fp, VAULT)
        if rel.split(os.sep)[0] in EXEMPT_TOP:
            continue
        in_code = False
        with open(fp, encoding="utf-8") as fh:
            for line in fh:
                if line.lstrip().startswith("```"):
                    in_code = not in_code
                    continue
                if in_code:
                    continue
                for mt in link_re.finditer(line):
                    tgt = mt.group(1).split("|")[0].split("#")[0].strip()
                    if not tgt or tgt.lower() not in basenames:
                        broken.append((rel, tgt or "<empty>"))
    if broken:
        print(f"vault_build: {len(broken)} BROKEN wikilink(s) (first 20):", file=sys.stderr)
        for f, t in broken[:20]:
            print(f"  {f}: [[{t}]]", file=sys.stderr)
        return False
    print(f"vault_build: check_links OK — 0 broken wikilinks across {len(notes)} notes")
    return True


def count_csv_rows_per_table():
    """Count embedded csv DATA rows per source table by reading the generated data
    notes back off disk (independent of the in-memory build) — the real proof. Uses
    csv.reader so quoted embedded newlines in cells are counted as one record."""
    counts = defaultdict(int)
    ddir = os.path.join(VAULT, "data")
    if not os.path.isdir(ddir):
        return counts
    for fn in sorted(os.listdir(ddir)):
        if not fn.endswith(".md"):
            continue
        with open(os.path.join(ddir, fn), encoding="utf-8") as fh:
            text = fh.read()
        # frontmatter table: value
        table = None
        for line in text.split("\n", 40)[:40]:
            if line.startswith("table:"):
                table = line.split(":", 1)[1].strip().strip('"')
                break
        # extract the ```csv ... ``` block
        start = text.find("```csv\n")
        if start == -1:
            continue
        start += len("```csv\n")
        end = text.find("\n```", start)
        block = text[start:end] if end != -1 else text[start:]
        records = list(csv.reader(io.StringIO(block)))
        if records:
            counts[table] += max(0, len(records) - 1)   # minus header row
    return counts


def _content_hash_tables():
    """Tables whose key strategy is content-hash: byte-identical duplicate rows collapse
    in the changelog, so changelog-replay present-count can be < state present-count
    LOSSLESSLY by design (mirrors the verify.py 2026-06-27 dup-awareness fix).
    Fail-safe: an unreadable registry returns an empty set, leaving every table strict."""
    try:
        _here = os.path.dirname(os.path.abspath(__file__))
        _reg = json.load(open(os.path.join(_here, "..", "registry", "tables.json"),
                              encoding="utf-8"))["tables"]
        return {_t for _t, _e in _reg.items()
                if ((_e.get("key") or {}).get("strategy")) == "content-hash"}
    except Exception:
        return set()


def check_lossless(store, embed_count):
    """3-way per table: changelog-replay present == state.jsonl present == Σ embedded csv rows."""
    disk_counts = count_csv_rows_per_table()
    _ch = _content_hash_tables()
    ok = True
    print("vault_build: check_lossless — per-table (replay / state / embedded):")
    tables = sorted(set(PLACEMENT))
    for t in tables:
        replay_n = len(replay_present(store, t))
        state_n = len(state_keys(store, t))
        embed_n = embed_count.get(t, 0)
        disk_n = disk_counts.get(t, 0)
        value_ok = (state_n == embed_n == disk_n)
        if t in _ch:
            # content-hash: exact-dup rows collapse losslessly in the changelog,
            # so replay_n <= state_n is correct by design (port of verify.py 2026-06-27).
            row_ok = value_ok and (replay_n <= state_n)
        else:
            row_ok = value_ok and (replay_n == state_n)
        ok = ok and row_ok
        flag = "ok" if row_ok else "**FAIL**"
        if not row_ok or replay_n:
            print(f"    {t:<30} replay={replay_n} state={state_n} embed={embed_n} disk={disk_n}  {flag}")
        if not row_ok:
            print(f"vault_build: LOSSLESS FAIL {t}: replay={replay_n} state={state_n} "
                  f"embed={embed_n} disk={disk_n}", file=sys.stderr)
    print(f"vault_build: check_lossless {'OK' if ok else 'FAILED'} "
          f"({len(tables)} tables, zero tolerance)")
    return ok


def check_no_orphan_rows(store, placed):
    """Every SSOT present key is placed in exactly one canonical note (bijection)."""
    ok = True
    for t in sorted(set(PLACEMENT)):
        present = set(state_keys(store, t))
        placed_keys = placed.get(t, {})
        placed_set = set(placed_keys)
        unplaced = present - placed_set
        extra = placed_set - present
        if unplaced or extra:
            ok = False
            print(f"vault_build: ORPHAN-ROWS FAIL {t}: unplaced={len(unplaced)} "
                  f"not-in-state={len(extra)}", file=sys.stderr)
            for k in list(unplaced)[:5]:
                print(f"    unplaced: {k}", file=sys.stderr)
    print(f"vault_build: check_no_orphan_rows {'OK' if ok else 'FAILED'} — "
          "every present key mapped to exactly one canonical note")
    return ok


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None):
    global VAULT, TAX
    ap = argparse.ArgumentParser(description="JIVO lossless vault builder (VAULT-DESIGN.md)")
    ap.add_argument("--store", default="store/versioned", help="versioned SSOT dir")
    ap.add_argument("--vault", default="vault", help="output vault dir")
    ap.add_argument("--taxonomy", default="registry/taxonomy.json")
    args = ap.parse_args(argv)

    VAULT = os.path.abspath(args.vault)
    try:
        with open(args.taxonomy, encoding="utf-8") as f:
            TAX = json.load(f)
    except OSError:
        TAX = {}

    if not os.path.isdir(args.store):
        print(f"vault_build: store not found: {args.store}", file=sys.stderr)
        return 2
    os.makedirs(VAULT, exist_ok=True)

    m, placed, embed_count, doc_counts = build(args.store)

    g_unique = check_unique()
    g_links = check_links()
    g_lossless = check_lossless(args.store, embed_count)
    g_orphan = check_no_orphan_rows(args.store, placed)
    for name, (placed_n, src_n) in sorted(doc_counts.items()):
        s = "ok" if placed_n == src_n else "**FAIL**"
        print(f"vault_build: doc-lossless {name}: placed={placed_n} source={src_n}  {s}")
        if placed_n != src_n:
            g_lossless = False

    all_ok = g_unique and g_links and g_lossless and g_orphan
    print(f"vault_build: GATES {'ALL GREEN' if all_ok else 'FAILED'}")
    return 0 if all_ok else 2


if __name__ == "__main__":
    sys.exit(main())
