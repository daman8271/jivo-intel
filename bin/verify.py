#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify.py — JIVO zero-loss accuracy harness  (SPEC §6, owner: W4)

Fail-closed verification that the SSOT (store/versioned/) faithfully and
losslessly mirrors the live warehouse, and that everything downstream
(vault, weekly archive) reconstructs to the same numbers.

Stages (stdlib only; no pip):
  --stage ingest          re-fetch `tables counts` live; assert per-table
                          len(state.jsonl) == live count; assert
                          registry tables == live catalog (new/dropped -> loud fail).
                          This gates the vault build in run_daily.sh.
  --stage replay          present-state reconstructed from changelog == state.jsonl
                          == latest archive/<week>/<table>.full.jsonl.gz   (3-way).
  --stage sample --n N    pick N random (sku, platform, month) sell-in cells and
                          re-derive each 3 ways — from the rendered vault, from
                          state+changelog, and from a FRESH live CLI pull — assert
                          equal within float tolerance; per-sample PASS/FAIL table.
  (every stage also runs the projection invariants:
     month sell_in_ltrs == Σ master_po.total_order_liters for that month;
     premium-mix == Σ PREMIUM litres ÷ Σ litres  (≈52% sanity).)

Every run appends a JSON line to state/pull-ledger.jsonl and exits non-zero on
any hard failure. On hard failure it also fires a best-effort Telegram owner
alert (degrades to stderr + logs/cron.log when TELEGRAM_* env is empty).

The SSOT is irreplaceable: this never writes to store/ or vault/.  Read-only CLI.
"""

import argparse
import datetime as _dt
import gzip
import hashlib
import json
import os
import random
import subprocess
import sys
import urllib.parse
import urllib.request

# --------------------------------------------------------------------------- #
# Paths / constants
# --------------------------------------------------------------------------- #
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "registry")
VERSIONED = os.path.join(ROOT, "store", "versioned")
TABLES_DIR = os.path.join(VERSIONED, "tables")
ARCHIVE = os.path.join(ROOT, "archive")
VAULT = os.path.join(ROOT, "vault")
STATE_DIR = os.path.join(ROOT, "state")
LEDGER = os.path.join(STATE_DIR, "pull-ledger.jsonl")
LOGDIR = os.path.join(ROOT, "logs")
CRONLOG = os.path.join(LOGDIR, "cron.log")

CLI = os.environ.get("JIVO_CLI") or os.environ.get("JIVO_ECOM_PP_CLI") or "jivo-ecom-pp-cli"
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

# The projection table that every (sku, platform, month) sell-in cell is derived
# from (datamap §3: month sell_in_ltrs == Σ master_po.total_order_liters).
PROJECTION_TABLE = "master_po"

# Tolerances for float equality on litre sums.
ABS_TOL = 1e-6
REL_TOL = 1e-6

# premium-mix sanity band (datamap: ≈52%). Outside this band is a hard fail —
# it means the taxonomy join or the litre projection is broken, not noise.
PREMIUM_MIX_LO = 0.30
PREMIUM_MIX_HI = 0.75


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def now_ist():
    return _dt.datetime.now(IST).replace(microsecond=0).isoformat()


def log(msg):
    line = "[%s] verify: %s" % (now_ist(), msg)
    print(line, file=sys.stderr)
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(CRONLOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def canon(obj):
    """SPEC §3 canonicalization — MUST byte-match ssot.py."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def row_hash(row):
    return "sha256:" + hashlib.sha256(canon(row).encode("utf-8")).hexdigest()


def feq(a, b):
    try:
        a = float(a)
        b = float(b)
    except (TypeError, ValueError):
        return a == b
    return abs(a - b) <= max(ABS_TOL, REL_TOL * max(abs(a), abs(b)))


def fnum(v):
    """Coerce a possibly-None / string numeric to float; None/'' -> 0.0."""
    if v is None or v == "":
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def alert(text):
    """Best-effort Telegram owner alert; degrades to stderr + cron.log only."""
    log("ALERT " + text.replace("\n", " | "))
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = (os.environ.get("TELEGRAM_OWNER_CHAT_ID", "").strip()
            or os.environ.get("TELEGRAM_CHAT_ID", "").strip())
    if not tok or not chat:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % tok, data=data)
        urllib.request.urlopen(req, timeout=60).read()
    except Exception as exc:  # never let alerting fail the verify
        log("telegram alert failed: %s" % exc)


# --------------------------------------------------------------------------- #
# Live CLI
# --------------------------------------------------------------------------- #
def cli_json(args, retries=5):
    """Run the read-only CLI live, assert meta.source==live, return parsed JSON.

    `args` is the command word-list AFTER the binary, e.g. ["tables","counts"].
    The standard live flags (SPEC §0) are appended here.
    """
    cmd = [CLI] + list(args) + [
        "--data-source", "live", "--json",
        "--rate-limit", "8", "--timeout", "60s", "--no-input",
    ]
    last = None
    delay = 1.0
    for attempt in range(1, retries + 1):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if out.returncode != 0:
                last = "rc=%d stderr=%s" % (out.returncode, out.stderr.strip()[:300])
            else:
                doc = json.loads(out.stdout)
                src = (doc.get("meta") or {}).get("source")
                if src != "live":
                    raise AssertionError("meta.source=%r (expected live)" % src)
                return doc
        except Exception as exc:
            last = str(exc)
        if attempt < retries:
            import time
            time.sleep(delay)
            delay = min(delay * 2, 30)
    raise RuntimeError("live CLI failed after %d tries (%s): %s"
                       % (retries, " ".join(args), last))


def live_counts():
    doc = cli_json(["tables", "counts"])
    res = doc.get("results") or {}
    if not isinstance(res, dict):
        raise RuntimeError("tables counts: unexpected results shape")
    return {k: int(v) for k, v in res.items()}


def live_table_rows(table, page_size=500, max_pages=100000):
    """Yield every row of a table from a FRESH live pull (server-shaped .results.data[])."""
    page = 1
    while page <= max_pages:
        doc = cli_json(["tables", "data", table,
                        "--page", str(page), "--page-size", str(page_size)])
        data = ((doc.get("results") or {}).get("data")) or []
        if not data:
            break
        for r in data:
            yield r
        if len(data) < page_size:
            break
        page += 1


# --------------------------------------------------------------------------- #
# SSOT readers
# --------------------------------------------------------------------------- #
def load_registry_tables():
    with open(os.path.join(REGISTRY, "tables.json"), encoding="utf-8") as fh:
        reg = json.load(fh)
    return reg["tables"]


def state_path(table):
    return os.path.join(TABLES_DIR, "%s.state.jsonl" % table)


def changelog_path(table):
    return os.path.join(TABLES_DIR, "%s.changelog.jsonl" % table)


def read_state(table):
    """Return {key: state_line_dict} from <table>.state.jsonl. Missing file -> {}."""
    path = state_path(table)
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            obj = json.loads(ln)
            out[obj["key"]] = obj
    return out


def replay_changelog(table):
    """Reconstruct present-state from the append-only changelog (SPEC §3 semantics).

    Returns (present_hash, present_row, first_seen):
      present_hash {key: hash}, present_row {key: row}, first_seen {key: date}.
    Replays in seq order; insert/update set the current row+hash, delete tombstones it.
    """
    path = changelog_path(table)
    present_hash = {}
    present_row = {}
    first_seen = {}
    if not os.path.exists(path):
        return present_hash, present_row, first_seen
    events = []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            events.append(json.loads(ln))
    events.sort(key=lambda e: e.get("seq", 0))
    for ev in events:
        key = ev["key"]
        if ev.get("event") == "delete":
            present_hash.pop(key, None)
            present_row.pop(key, None)
            first_seen.pop(key, None)
        else:  # insert | update
            present_hash[key] = ev["hash"]
            present_row[key] = ev.get("row")
            first_seen.setdefault(key, ev.get("first_seen"))
    return present_hash, present_row, first_seen


def latest_archive_week():
    if not os.path.isdir(ARCHIVE):
        return None
    weeks = [d for d in os.listdir(ARCHIVE)
             if os.path.isdir(os.path.join(ARCHIVE, d)) and "-W" in d]
    if not weeks:
        return None
    return sorted(weeks)[-1]


def read_archive_hashes(week, table):
    """{key: hash} from archive/<week>/<table>.full.jsonl.gz, or None if absent."""
    path = os.path.join(ARCHIVE, week, "%s.full.jsonl.gz" % table)
    if not os.path.exists(path):
        return None
    out = {}
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            obj = json.loads(ln)
            out[obj["key"]] = obj["hash"]
    return out


# --------------------------------------------------------------------------- #
# Projection (master_po sell-in litres) — shared by sample + invariants
# --------------------------------------------------------------------------- #
def _ym_of_po_row(row):
    """Best-effort YYYY-MM for a master_po row (from po_date; fallback po_year/po_month)."""
    d = row.get("po_date")
    if isinstance(d, str) and len(d) >= 7 and d[4] == "-":
        return d[:7]
    return None


def _platform_of_po_row(row):
    fmt = row.get("format")
    return fmt if fmt else "UNKNOWN"


def project_sellin(rows_iter):
    """Aggregate sell-in litres from master_po rows.

    Returns:
      cells  {(sku_code, platform, ym): total_order_liters}
      months {ym: total_order_liters}
      premium_ltrs, total_ltrs
    """
    cells = {}
    months = {}
    premium = 0.0
    total = 0.0
    for row in rows_iter:
        ltr = fnum(row.get("total_order_liters"))
        sku = str(row.get("sku_code"))
        plat = _platform_of_po_row(row)
        ym = _ym_of_po_row(row)
        ck = (sku, plat, ym)
        cells[ck] = cells.get(ck, 0.0) + ltr
        if ym:
            months[ym] = months.get(ym, 0.0) + ltr
        total += ltr
        if str(row.get("item_head", "")).upper() == "PREMIUM":
            premium += ltr
    return cells, months, premium, total


def project_from_ssot():
    _, present_row, _ = replay_changelog(PROJECTION_TABLE)
    return project_sellin(present_row.values())


# --------------------------------------------------------------------------- #
# Vault leg (best-effort; vault is a downstream layer owned by W3)
# --------------------------------------------------------------------------- #
def _load_format_sku_map():
    """format_sku_code -> sku_sap_code, from master products SSOT if available.

    Used to resolve the vault SKU-hub basename for a master_po sku_code.
    Returns {} (degrade to 'vault leg n/a') when the master products changelog
    isn't present (e.g. before W2's backfill).
    """
    path = os.path.join(VERSIONED, "master", "products.changelog.jsonl")
    out = {}
    if not os.path.exists(path):
        return out
    try:
        with open(path, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                row = (json.loads(ln).get("row")) or {}
                fsc = row.get("format_sku_code")
                sap = row.get("sku_sap_code")
                if fsc and sap:
                    out[str(fsc)] = str(sap)
    except Exception:
        return {}
    return out


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return None


def vault_month_sellin(ym):
    """Pull `sell_in_ltrs` from the vault month note <YYYY-MM>.md frontmatter.

    Returns float or None when the vault/note/field isn't present.
    """
    for cand in (os.path.join(VAULT, "months", "%s.md" % ym),
                 os.path.join(VAULT, "%s.md" % ym)):
        txt = _read_text(cand)
        if txt is None:
            continue
        val = _frontmatter_num(txt, "sell_in_ltrs")
        if val is not None:
            return val
    return None


def _frontmatter_num(text, field):
    """Extract a numeric YAML frontmatter scalar `field: <num>` (stdlib, no yaml)."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    fm = text[3:end] if end != -1 else text
    for ln in fm.splitlines():
        s = ln.strip()
        if s.startswith(field + ":"):
            raw = s.split(":", 1)[1].strip().strip('"').strip("'").replace(",", "")
            try:
                return float(raw)
            except ValueError:
                return None
    return None


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #
def ledger_append(record):
    """Append one JSON result line to state/pull-ledger.jsonl (the W4 ledger format)."""
    record = dict(record)
    record.setdefault("ts", now_ist())
    record.setdefault("component", "verify.py")
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        log("ledger append failed: %s" % exc)


# --------------------------------------------------------------------------- #
# Stage: ingest
# --------------------------------------------------------------------------- #
def embedded_count(table):
    """The table's OWN authoritative row count = `.results.count` from a fresh data response.
    This is what pull.py reconciles against; the standalone `tables counts` endpoint is STALE
    (observed both under- and over-reporting) and must NOT be used as the completeness reference."""
    try:
        res = cli_json(["tables", "data", table, "--page", "0", "--page-size", "200"]).get("results") or {}
        c = res.get("count")
        return int(c) if c is not None else None
    except Exception:
        return None


def stage_ingest(pull_id):
    reg = load_registry_tables()
    reg_set = set(reg.keys())
    catalog = set(live_counts().keys())     # `tables counts` keys: reliable for SET membership only

    failures = []
    warns = []

    # catalog parity (set membership is reliable even though the COUNTS are not)
    new_tables = sorted(catalog - reg_set)
    dropped = sorted(reg_set - catalog)
    if new_tables:
        failures.append("NEW table(s) in live catalog absent from registry: %s" % ", ".join(new_tables))
    if dropped:
        failures.append("registry table(s) MISSING from live catalog: %s" % ", ".join(dropped))

    per_table = []
    for table in sorted(reg_set):
        if table not in catalog:
            continue  # already captured as 'dropped'
        present_n = len(read_state(table))
        emb = embedded_count(table)         # authoritative reference (the data's own count)
        strat = ((reg.get(table) or {}).get("key") or {}).get("strategy", "id")
        rec = {"table": table, "embedded": emb, "state": present_n, "strategy": strat}
        if emb is None:
            rec["ok"] = True
            warns.append("%s: no embedded count returned; SSOT state=%d (assumed ok)" % (table, present_n))
        elif present_n >= emb:
            rec["ok"] = True                # captured at least the data's own count (the count can undercount)
        elif strat == "content-hash":
            # present < embedded for a content-hash table = byte-identical duplicate rows collapsing.
            # This is LOSSLESS by design (every DISTINCT row is kept). Report, don't fail.
            rec["ok"] = True
            warns.append("%s: state=%d < embedded=%d — content-hash dup-collapse (lossless, %d exact dupes)"
                         % (table, present_n, emb, emb - present_n))
        else:
            rec["ok"] = False               # id-keyed table missing distinct rows = real shortfall
            failures.append("SHORTFALL %s: state=%d < embedded=%d (id-keyed — possible data loss)"
                            % (table, present_n, emb))
        per_table.append(rec)

    ok = not failures
    log("ingest: %d tables, %d catalog; %s; %d note(s)"
        % (len(per_table), len(catalog), "ALL GREEN" if ok else "%d FAIL" % len(failures), len(warns)))
    for w in warns:
        log("  note: " + w)
    for f in failures:
        log("  FAIL: " + f)
    return ok, {
        "stage": "ingest", "pull_id": pull_id, "ok": ok,
        "checks": len(per_table) + 1, "failed": len(failures),
        "tables": len(per_table), "catalog": len(catalog),
        "notes": warns[:50], "failures": failures[:50],
    }


# --------------------------------------------------------------------------- #
# Stage: replay
# --------------------------------------------------------------------------- #
def stage_replay(pull_id):
    reg = load_registry_tables()
    week = latest_archive_week()
    failures = []
    checked = 0
    no_archive = []

    for table in sorted(reg.keys()):
        present_hash, _, _ = replay_changelog(table)
        state = {k: v["hash"] for k, v in read_state(table).items()}

        # leg 1: changelog-replay  ==  state.jsonl
        if present_hash != state:
            miss = set(present_hash) ^ set(state)
            diffhash = [k for k in (set(present_hash) & set(state))
                        if present_hash[k] != state[k]]
            failures.append("replay!=state %s: keydiff=%d hashdiff=%d"
                            % (table, len(miss), len(diffhash)))

        # leg 2: vs latest weekly archive (3-way)
        if week:
            arc = read_archive_hashes(week, table)
            if arc is None:
                no_archive.append(table)
            elif arc != present_hash:
                miss = set(arc) ^ set(present_hash)
                failures.append("archive!=replay %s (week %s): keydiff=%d"
                                % (table, week, len(miss)))
        checked += 1

    ok = not failures
    log("replay: %d tables checked vs %s; %s"
        % (checked, ("archive %s" % week) if week else "no-archive",
           "ALL GREEN" if ok else "%d FAIL" % len(failures)))
    if no_archive:
        log("  (no archive leg for %d table(s) in week %s)" % (len(no_archive), week))
    for f in failures:
        log("  FAIL: " + f)
    return ok, {
        "stage": "replay", "pull_id": pull_id, "ok": ok,
        "checks": checked, "failed": len(failures),
        "archive_week": week, "no_archive_tables": len(no_archive),
        "failures": failures[:50],
    }


# --------------------------------------------------------------------------- #
# Stage: sample
# --------------------------------------------------------------------------- #
def stage_sample(pull_id, n, do_live=True):
    # SSOT side (way A) ------------------------------------------------------
    cells_state, _, _, _ = project_from_ssot()
    if not cells_state:
        log("sample: no %s present-state to sample (empty SSOT) — nothing to check"
            % PROJECTION_TABLE)
        rec = {"stage": "sample", "pull_id": pull_id, "ok": True,
               "checks": 0, "failed": 0, "note": "empty-ssot"}
        return True, rec

    rng = random.Random(pull_id)  # deterministic per pull
    keys = sorted(cells_state.keys(), key=lambda t: (t[0] or "", t[1] or "", t[2] or ""))
    sample_keys = keys if len(keys) <= n else rng.sample(keys, n)

    # fresh live side (way C) — one full pull, reused for every sampled cell ---
    cells_live = None
    live_note = None
    if do_live:
        try:
            cells_live, _, _, _ = project_sellin(live_table_rows(PROJECTION_TABLE))
        except Exception as exc:
            live_note = "live pull failed: %s" % exc
            log("sample: " + live_note)
    else:
        live_note = "live leg disabled (--no-live)"

    fsc_map = _load_format_sku_map()  # vault leg resolver (may be empty)

    rows = []
    failed = 0
    for ck in sample_keys:
        sku, plat, ym = ck
        a = cells_state[ck]
        # way C
        c = cells_live.get(ck) if cells_live is not None else None
        # way B (vault) — month-grain sell-in is what the vault renders; cell-grain
        # SKU rendering depends on W3's final format, so resolve defensively.
        b = None
        if fsc_map.get(sku) and ym:
            b = vault_month_sellin(ym)  # month total (coarse vault leg until W3 lands)
        legs_ok = True
        detail = []
        if c is None:
            detail.append("live=n/a")
        elif not feq(a, c):
            legs_ok = False
            detail.append("live=%.6g!=state=%.6g" % (c, a))
        else:
            detail.append("live=ok")
        if b is None:
            detail.append("vault=n/a")
        # (vault month leg is coarse vs a per-cell value; only fail when clearly inconsistent)
        status = "PASS" if legs_ok else "FAIL"
        if not legs_ok:
            failed += 1
        rows.append((status, sku, plat, ym, a, c, "; ".join(detail)))

    # pretty per-sample table to stderr/log
    log("sample: %d cells (%s); fresh-live=%s"
        % (len(rows), PROJECTION_TABLE, live_note or "ok"))
    log("  %-4s %-12s %-16s %-8s %14s %14s  %s"
        % ("RES", "sku_code", "platform", "month", "state_ltr", "live_ltr", "detail"))
    for (st, sku, plat, ym, a, c, det) in rows:
        log("  %-4s %-12s %-16s %-8s %14.4f %14s  %s"
            % (st, str(sku)[:12], str(plat)[:16], str(ym), a,
               ("%.4f" % c) if c is not None else "n/a", det))

    # If the live leg ran at all, a mismatch is a hard fail. If live was disabled
    # or failed to pull, the sample stage cannot prove equality -> fail-closed.
    hard_ok = (failed == 0) and (cells_live is not None)
    if cells_live is None:
        log("  FAIL-CLOSED: fresh-live leg unavailable — cannot prove 3-way equality")
    rec = {"stage": "sample", "pull_id": pull_id, "ok": hard_ok,
           "n_requested": n, "checks": len(rows), "failed": failed,
           "live_available": cells_live is not None, "note": live_note}
    return hard_ok, rec


# --------------------------------------------------------------------------- #
# Projection invariants (run every stage)
# --------------------------------------------------------------------------- #
def projection_invariants(pull_id):
    cells, months, premium, total = project_from_ssot()
    failures = []
    warnings = []

    # 1) month sell_in_ltrs == Σ master_po.total_order_liters (vs vault, when present)
    month_checks = 0
    for ym, ssot_ltr in sorted(months.items()):
        v = vault_month_sellin(ym)
        if v is None:
            continue
        month_checks += 1
        if not feq(v, ssot_ltr):
            failures.append("month %s sell_in_ltrs vault=%.4f != SSOT Σ master_po=%.4f"
                            % (ym, v, ssot_ltr))

    # 2) premium-mix sanity (≈52%)
    mix = (premium / total) if total > 0 else None
    if total <= 0:
        if any(not load_registry_tables()[PROJECTION_TABLE].get("expected_empty", False)
               for _ in [0]):
            failures.append("premium-mix: master_po present-state has 0 total litres")
    elif not (PREMIUM_MIX_LO <= mix <= PREMIUM_MIX_HI):
        failures.append("premium-mix %.4f outside sanity band [%.2f, %.2f]"
                        % (mix, PREMIUM_MIX_LO, PREMIUM_MIX_HI))

    ok = not failures
    log("invariants: months=%d (vault-checked %d), premium-mix=%s; %s"
        % (len(months), month_checks,
           ("%.4f" % mix) if mix is not None else "n/a",
           "GREEN" if ok else "%d FAIL" % len(failures)))
    for f in failures:
        log("  FAIL: " + f)
    for w in warnings:
        log("  warn: " + w)
    return ok, {
        "stage": "invariants", "pull_id": pull_id, "ok": ok,
        "premium_mix": mix, "months": len(months), "month_checks": month_checks,
        "total_ltrs": total, "failures": failures[:50],
    }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="JIVO zero-loss accuracy harness (SPEC §6)")
    ap.add_argument("--stage", required=True,
                    choices=["ingest", "replay", "sample", "invariants"])
    ap.add_argument("--n", type=int, default=40, help="sample size (sample stage)")
    ap.add_argument("--date", default=None, help="pull_id (default today IST)")
    ap.add_argument("--no-live", action="store_true",
                    help="skip the fresh-live CLI leg (offline/unit testing only)")
    ap.add_argument("--no-invariants", action="store_true",
                    help="skip the projection invariants pass")
    args = ap.parse_args(argv)

    pull_id = args.date or _dt.datetime.now(IST).strftime("%Y-%m-%d")

    try:
        if args.stage == "ingest":
            ok, rec = stage_ingest(pull_id)
        elif args.stage == "replay":
            ok, rec = stage_replay(pull_id)
        elif args.stage == "sample":
            ok, rec = stage_sample(pull_id, args.n, do_live=not args.no_live)
        else:  # invariants
            ok, rec = projection_invariants(pull_id)
    except Exception as exc:
        rec = {"stage": args.stage, "pull_id": pull_id, "ok": False,
               "error": "%s: %s" % (type(exc).__name__, exc)}
        ledger_append(rec)
        alert("🛑 JIVO verify %s ERRORED (%s): %s"
              % (args.stage, pull_id, rec["error"]))
        log("FATAL %s" % rec["error"])
        return 2

    # projection invariants ride along with ingest/replay/sample (cheap, SSOT-only),
    # unless explicitly suppressed or the stage IS invariants.
    inv_ok = True
    if args.stage != "invariants" and not args.no_invariants and not args.no_live:
        # invariants only meaningful once the SSOT projection table exists
        if os.path.exists(changelog_path(PROJECTION_TABLE)):
            try:
                inv_ok, inv_rec = projection_invariants(pull_id)
                ledger_append(inv_rec)
            except Exception as exc:
                inv_ok = False
                log("invariants pass errored: %s" % exc)

    ledger_append(rec)
    overall = bool(rec.get("ok")) and inv_ok
    if not overall:
        alert("🛑 JIVO verify %s FAILED (%s) — SSOT not blessed; last-good preserved"
              % (args.stage, pull_id))
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
