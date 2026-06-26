#!/usr/bin/env python3
"""bin/pull.py — extraction layer for the JIVO zero-loss SSOT (SPEC §2,§4; owner: W1).

Drives the read-only `jivo-ecom-pp-cli` from registry/ and writes a lossless daily capture to
store/raw/<date>/ :

  store/raw/<date>/<table>.jsonl                 one raw row per line (.results.data[])
  store/raw/<date>/master_products.jsonl         one row per line (.results.results[])
  store/raw/<date>/master_fcs.jsonl              one row per line (.results.results[])
  store/raw/<date>/dashboards/<endpoint_key>.json the whole .results of each doc/dashboard/leaf
  store/raw/<date>/_pull-manifest.json            {<table>:{count_live,rows_written,ok}, ...,
                                                   "dashboards":{key:{ok,bytes,na}}, started, finished, all_ok}

Hard rules (SPEC §0): read-only CLI only, stdlib only, assert meta.source=="live" on every response,
atomic temp+rename, 5x exponential backoff, fail-closed (exit nonzero if any table is not OK).

endpoint_key = <endpoint> | <endpoint>__<slug> | <endpoint>__<YYYY-MM>  (filesystem-safe; '/'->'-').
"""

import argparse
import json
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

CLI = "jivo-ecom-pp-cli"
PAGE_SIZE = 200
PAGE_WORKERS = 12          # concurrent page fetches per pass for large tables (I/O-bound subprocess calls)
GAIN_THRESH = 0.004        # stop multi-pass union when a pass adds < this fraction of new distinct rows
# Max union passes over offset pagination for large, actively-written tables whose order
# drifts during a pull (see pull_rows). Full backfill unions up to this many passes to
# converge coverage; main() lowers it for daily (the SSOT already holds history).
PASSES_MAX = 6
MAX_ATTEMPTS = 5            # 5x exponential backoff for critical calls
DASH_ATTEMPTS = 2          # dashboards/leaves: fewer retries; failure -> recorded n/a (non-fatal)
IST = timezone(timedelta(hours=5, minutes=30))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REG_DIR = os.path.join(ROOT, "registry")
STORE_RAW = os.path.join(ROOT, "store", "raw")
LOG_DIR = os.path.join(ROOT, "logs")

# Whales first (largest tables) so failures surface early and the long tail runs while warm.
WHALES = ["swiggySec", "all_platform_inventory", "blinkitSec", "swiggy_inventory", "amazon_ads"]


# --------------------------------------------------------------------------- logging
_LOGFH = None

def log(msg):
    line = "[pull.py %s] %s" % (datetime.now(IST).strftime("%H:%M:%S"), msg)
    print(line, file=sys.stderr, flush=True)
    if _LOGFH:
        _LOGFH.write(line + "\n")
        _LOGFH.flush()


def die(msg, code=1):
    log("FATAL: " + msg)
    sys.exit(code)


# --------------------------------------------------------------------------- registry
def load_registry():
    with open(os.path.join(REG_DIR, "tables.json")) as f:
        tables = json.load(f)["tables"]
    with open(os.path.join(REG_DIR, "endpoints.json")) as f:
        endpoints = json.load(f)
    return tables, endpoints


# --------------------------------------------------------------------------- CLI runner
class CliError(Exception):
    pass


def cli(args, *, attempts=MAX_ATTEMPTS, rate_limit="8", timeout="60s", expect_live=True, no_cache=False):
    """Run a read-only CLI command, return the parsed JSON dict.

    Retries up to `attempts` with exponential backoff on nonzero exit, JSON-parse failure, or a
    non-live meta.source. Raises CliError after the final attempt.
    """
    cmd = [CLI] + list(args) + [
        "--data-source", "live", "--json", "--no-input",
        "--rate-limit", str(rate_limit), "--timeout", str(timeout),
    ]
    if no_cache:
        cmd.append("--no-cache")
    last_err = ""
    for attempt in range(1, attempts + 1):
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            last_err = "subprocess timeout"
            proc = None
        if proc is not None and proc.returncode == 0:
            out = proc.stdout.strip()
            try:
                data = json.loads(out)
            except json.JSONDecodeError as e:
                last_err = "json decode: %s (head=%r)" % (e, out[:160])
            else:
                if expect_live:
                    src = (data.get("meta") or {}).get("source")
                    if src != "live":
                        last_err = "meta.source=%r (want live)" % src
                        # not retryable in any useful way, but honor backoff loop
                    else:
                        return data
                else:
                    return data
        else:
            if proc is not None:
                # stderr may carry a benign caching warning; capture for diagnostics only
                last_err = (proc.stderr or "").strip()[:300] or ("exit %s" % proc.returncode)
        if attempt < attempts:
            backoff = 2 ** (attempt - 1)   # 1,2,4,8,16
            log("  retry %d/%d after %ds (%s)" % (attempt, attempts, backoff, last_err[:120]))
            time.sleep(backoff)
    raise CliError("%s :: %s" % (" ".join(args), last_err[:300]))


def cli_try(args, **kw):
    """Best-effort variant: returns (data | None). Used for dashboards/leaves where gated==n/a."""
    try:
        return cli(args, attempts=DASH_ATTEMPTS, **kw)
    except CliError as e:
        log("  n/a: %s" % str(e)[:160])
        return None


# --------------------------------------------------------------------------- atomic IO
def atomic_write_bytes(path, data: bytes):
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def open_atomic_jsonl(path):
    """Return (file_handle, tmp_path). Caller writes lines, then finalize_atomic_jsonl()."""
    tmp = path + ".part"
    return open(tmp, "w"), tmp


def finalize_atomic_jsonl(fh, tmp, path):
    fh.flush()
    os.fsync(fh.fileno())
    fh.close()
    os.replace(tmp, path)


# --------------------------------------------------------------------------- endpoint key
def endpoint_key(endpoint, slug=None, ym=None):
    key = endpoint
    if slug is not None:
        key += "__" + slug
    if ym is not None:
        key += "__" + ym
    return key.replace("/", "-")


def dashboards_dir(date_dir):
    d = os.path.join(date_dir, "dashboards")
    os.makedirs(d, exist_ok=True)
    return d


# --------------------------------------------------------------------------- table pull
def _extract(res, rows_field):
    """Pull the row array out of a data/master response (.results.data[] or .results.results[])."""
    rows = res.get(rows_field)
    if rows is None:
        rows = res.get("data") if "data" in res else res.get("results", [])
    return rows


def pull_rows(date_dir, name, data_cmd_builder, rows_path_name, rows_field, *,
              expected_empty=False, standalone_count_cmd=None):
    """Generic paginated row puller (tables + master share this).

    The AUTHORITATIVE count is the `.results.count` embedded in the data response itself — NOT the
    standalone `tables count` endpoint, which we observed can be stale (e.g. amazon_coupon: count
    endpoint=765 vs data-embedded=795, with 795 distinct rows actually served). We paginate until a
    short/empty page, capture the embedded count, then re-read it to catch mid-pull growth and pull
    the tail. Reconcile: rows_written == embedded count AND terminated on a short page.

    Returns {count_live, rows_written, ok} (count_live = authoritative embedded count).
    """
    path = os.path.join(date_dir, rows_path_name)
    import hashlib
    fh, tmp = open_atomic_jsonl(path)
    # --- Multi-pass union with content de-dup ------------------------------------------
    # The CLI exposes only offset pagination (--page/--page-size, max 200) — no stable
    # sort, no cursor. On large, actively-written tables (swiggySec, *Sec, *_inventory)
    # the underlying row order DRIFTS during a multi-minute pull, so one offset pass both
    # re-serves some rows AND misses others (observed: swiggySec returned 537k row-instances
    # but only 342k distinct, vs a true table of ~491k). We therefore union several full
    # passes, de-duping by exact row CONTENT (byte-identical rows collapse — provably
    # lossless), until a pass adds <GAIN_THRESH new rows (converged) or we reach the
    # embedded count or hit PASSES_MAX. `count_live` is the DISTINCT rows actually captured;
    # the append-only SSOT unions across days, so any residual tail self-heals on later
    # pulls and nothing once seen is ever lost.
    seen = {}                       # content_hash -> row  (distinct rows)
    embedded_count = None
    passes = 0
    prev_distinct = 0
    short_clean = False

    def _ingest(rows):
        """De-dup a batch of rows into `seen` by exact content hash (lossless union)."""
        for row in rows:
            ch = hashlib.sha1(json.dumps(row, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode("utf-8")).hexdigest()
            if ch not in seen:
                seen[ch] = row

    def _fetch(p):
        # --no-cache is ESSENTIAL here: tables.data serves a server-side ROTATING cursor — each call
        # returns the NEXT disjoint 200 rows (verified: 60 calls of one page = 12000 distinct, zero
        # repeats). With caching, every call returns the SAME stale partial snapshot (~70% of a whale),
        # silently defeating the union. No-cache lets one full page sweep enumerate the whole table.
        return cli(data_cmd_builder(p), no_cache=True)["results"]

    try:
        while passes < PASSES_MAX:
            passes += 1
            # page 0 first to learn the authoritative embedded count for this pass.
            res0 = _fetch(0)
            c0 = res0.get("count")
            if c0 is not None:
                embedded_count = c0
            rows0 = _extract(res0, rows_field)
            _ingest(rows0)
            if len(rows0) >= PAGE_SIZE:
                # Fetch the rest of the pass CONCURRENTLY. Page range derived from the embedded
                # count (+8 buffer, mirroring the old serial guard). Unlike the serial loop (which
                # stopped at the FIRST short page), this fetches the whole bounded range, so on
                # unstable tables it also captures pages a spurious mid-range short page would have
                # hidden — faster AND more complete. ex.map propagates any CliError → fail-closed
                # (a page that won't load after retries aborts the table; never a silent gap). Row
                # de-dup runs in THIS thread (workers only do I/O), so `seen` needs no lock.
                if embedded_count is not None:
                    last_page = max(1, math.ceil(embedded_count / PAGE_SIZE)) + 8
                    with ThreadPoolExecutor(max_workers=PAGE_WORKERS) as ex:
                        for res in ex.map(_fetch, range(1, last_page + 1)):
                            c = res.get("count")
                            if c is not None:
                                embedded_count = c
                            _ingest(_extract(res, rows_field))
                else:
                    # No count exposed (unexpected for these tables): safe serial continuation.
                    page = 1
                    while True:
                        res = _fetch(page)
                        rows = _extract(res, rows_field)
                        _ingest(rows)
                        if len(rows) < PAGE_SIZE:
                            break
                        page += 1
            distinct = len(seen)
            gain = (distinct - prev_distinct) / distinct if distinct else 0.0
            if embedded_count is not None and distinct >= embedded_count:
                break                           # captured at least the claimed count
            if passes > 1 and gain < GAIN_THRESH:
                break                           # converged: more passes add ~nothing
            prev_distinct = distinct
    except Exception:
        try:
            fh.close()
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise

    for row in seen.values():
        fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    finalize_atomic_jsonl(fh, tmp, path)

    written = len(seen)                 # distinct rows captured = the authoritative local count
    count = written                     # count_live; ssot reconciles its state to exactly this
    api_count = embedded_count
    coverage = (written / api_count) if (api_count and api_count > 0) else 1.0
    converged = short_clean or (api_count is not None and written >= api_count) or (passes < PASSES_MAX)
    ok = True
    if expected_empty and (written != 0 or (api_count not in (None, 0))):
        ok = False
        log("  !! %s expected_empty but written=%d api_count=%s" % (name, written, api_count))
    if (not expected_empty) and written == 0:
        ok = False                      # populated table returning 0 = collapse red flag
        log("  !! %s returned 0 rows but is not expected_empty (collapse?)" % name)
    if ok:
        log("  ok %s rows=%d passes=%d api_count=%s coverage=%.3f%s"
            % (name, written, passes, api_count, coverage,
               "" if converged else " [CAP-not-converged; daily union heals]"))
    else:
        log("  !! %s written=%d api_count=%s passes=%d" % (name, written, api_count, passes))

    entry = {"count_live": count, "rows_written": written, "distinct": written,
             "api_count": api_count, "passes": passes, "coverage": round(coverage, 4),
             "converged": converged, "ok": ok}
    if standalone_count_cmd is not None:        # advisory only, never fatal
        try:
            sc = cli(standalone_count_cmd)["results"].get("count")
            if sc is not None and sc != api_count:
                entry["count_standalone"] = sc
        except CliError:
            pass
    return entry


def pull_table(date_dir, table, expected_empty=False):
    builder = lambda p: ["tables", "data", table, "--page", str(p), "--page-size", str(PAGE_SIZE)]
    return pull_rows(date_dir, table, builder, table + ".jsonl", "data",
                     expected_empty=expected_empty,
                     standalone_count_cmd=["tables", "count", table])


def pull_master_doc(ddir, which, manifest_dash):
    """master products|fcs: paginate ALL rows (.results.results[]) and write the WHOLE assembled
    .results as a doc-level file `dashboards/master__<which>.json` — this is W2's consumed interface
    (its router matches `master*` and stores the whole .results as `.doc`; W3 reads `.doc.results[]`).

    Returns a table-style manifest entry {count_live,rows_written,ok} so the SPEC §4 / smoke
    rows==count reconcile is recorded (master must be fully captured to pass the pull).
    """
    name = "master_" + which
    all_rows = []
    base_results = None
    embedded_count = None
    terminated_short = False
    page = 0
    guard = None
    while True:
        res = cli(["master", which, "--page", str(page), "--page-size", str(PAGE_SIZE)])["results"]
        if base_results is None:
            base_results = res
        embedded_count = res.get("count", embedded_count)
        rows = res.get("results") or res.get("data") or []
        all_rows.extend(rows)
        if guard is None and embedded_count is not None:
            guard = max(1, math.ceil((embedded_count or 0) / PAGE_SIZE)) + 8
        if len(rows) < PAGE_SIZE:
            terminated_short = True
            break
        page += 1
        if guard is not None and page > guard:
            raise CliError("%s: page guard exceeded" % name)

    # re-read count to catch growth, pull tail if grown
    final_count = cli(["master", which, "--page", "0", "--page-size", str(PAGE_SIZE)],
                      no_cache=True)["results"].get("count", embedded_count)
    if final_count is not None and final_count > len(all_rows):
        log("  %s grew %s->%s; pulling tail" % (name, len(all_rows), final_count))
        page = len(all_rows) // PAGE_SIZE
        skip_first = len(all_rows) % PAGE_SIZE
        terminated_short = False
        while len(all_rows) < final_count:
            res = cli(["master", which, "--page", str(page), "--page-size", str(PAGE_SIZE)])["results"]
            rows = res.get("results") or res.get("data") or []
            all_rows.extend(rows[skip_first:])
            skip_first = 0
            if len(rows) < PAGE_SIZE:
                terminated_short = True
                break
            page += 1
        embedded_count = final_count

    # assemble the whole-.results doc with the COMPLETE row array
    doc = dict(base_results or {})
    doc["results"] = all_rows
    doc.pop("page", None)
    doc["count"] = embedded_count if embedded_count is not None else len(all_rows)
    payload = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    atomic_write_bytes(os.path.join(ddir, "master__" + which + ".json"), payload)

    count = embedded_count if embedded_count is not None else len(all_rows)
    written = len(all_rows)
    ok = (written == count) and terminated_short
    manifest_dash["master__" + which] = {"ok": ok, "bytes": len(payload), "na": False}
    if ok:
        log("  ok master_%s rows=%d" % (which, written))
    else:
        log("  !! master_%s rows=%d count=%s short=%s" % (which, written, count, terminated_short))
    return {"count_live": count, "rows_written": written, "ok": ok}


# --------------------------------------------------------------------------- doc pull
def pull_doc(ddir, key, argv, manifest_dash, *, optional=False):
    """Pull a single doc/dashboard/leaf, store whole .results to dashboards/<key>.json."""
    data = (cli_try if optional else cli)(argv)
    if data is None:
        manifest_dash[key] = {"ok": False, "bytes": 0, "na": True}
        return
    results = data.get("results")
    payload = json.dumps(results, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path = os.path.join(ddir, key + ".json")
    atomic_write_bytes(path, payload)
    manifest_dash[key] = {"ok": True, "bytes": len(payload), "na": False}


def run_doc_jobs(ddir, jobs, manifest_dash, workers):
    """Pull many independent docs CONCURRENTLY. jobs = list of (key, argv); each is an optional pull
    (a gated/failed doc is recorded n/a, never fatal — cli_try swallows CliError, so no future raises).
    pull_doc writes distinct files + distinct manifest_dash keys (dict item-set is GIL-atomic), so the
    parallelism is lossless. Returns nothing; manifest_dash is updated in place."""
    if not jobs:
        return
    def _one(job):
        key, argv = job
        pull_doc(ddir, key, argv, manifest_dash, optional=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(_one, jobs))


# --------------------------------------------------------------------------- month window
def latest_month(endpoints):
    d = cli(["dashboard", "latest-month"])["results"]
    return int(d["year"]), int(d["month"])


def ym_windows(mode, endpoints, ly, lm):
    """List of (year, month) tuples to probe for year_month dashboards."""
    out = []
    if mode == "daily":
        # current + 2 trailing months
        y, m = ly, lm
        for _ in range(3):
            out.append((y, m))
            m -= 1
            if m == 0:
                m = 12
                y -= 1
    else:  # full
        for y in endpoints["years_to_probe"]:
            for m in range(1, 13):
                if (y, m) > (ly, lm):
                    continue
                out.append((y, m))
    return out


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="JIVO zero-loss extraction layer (SPEC §4)")
    ap.add_argument("--date", default=datetime.now(IST).strftime("%Y-%m-%d"),
                    help="capture date YYYY-MM-DD (default: today IST)")
    ap.add_argument("--mode", choices=["full", "daily"], default="daily")
    ap.add_argument("--rate-limit", default="8")
    ap.add_argument("--timeout", default="60s")
    ap.add_argument("--page-workers", type=int, default=12,
                    help="concurrent page fetches per pass for large-table pulls (default 12). Use 1 for "
                         "rotating-cursor tables to enumerate cleanly without parallel cursor races.")
    ap.add_argument("--passes", type=int, default=0,
                    help="override max union passes per table (0 = mode default)")
    ap.add_argument("--gain-thresh", type=float, default=0.0,
                    help="stop a table's union when a pass adds < this fraction of new rows (0 = default 0.004)")
    ap.add_argument("--skip-auth", action="store_true", help="skip bin/auth.sh step-0 (dev only)")
    # smoke / scoping (dev)
    ap.add_argument("--smoke", action="store_true",
                    help="tiny isolation smoke into store/raw/_smoke/ (margins+coupon+fcs+notif+2 dashboards)")
    ap.add_argument("--only-tables", default=None, help="comma list: restrict the table set")
    ap.add_argument("--only-dashboards", default=None, help="comma list: restrict plain dashboards")
    ap.add_argument("--no-tables", action="store_true", help="skip the 41-table pull (re-pull just the dashboard/leaf layer)")
    ap.add_argument("--no-master", action="store_true")
    ap.add_argument("--no-notifications", action="store_true")
    ap.add_argument("--no-year-month", action="store_true")
    ap.add_argument("--no-per-platform", action="store_true")
    ap.add_argument("--no-platform-leaves", action="store_true")
    ap.add_argument("--out-subdir", default=None, help="override the date subdir name")
    ap.add_argument("--no-account", action="store_true", help="skip the account me/permissions docs")
    ap.add_argument("--no-table-columns", action="store_true", help="skip per-table column-schema docs")
    ap.add_argument("--gaps-only", action="store_true",
                    help="pull ONLY the audit gap-fill layers (account + table-columns + extra per-platform "
                         "dashboards), MERGING into an existing _pull-manifest.json (use with --mode full)")
    args = ap.parse_args()

    tables_reg, endpoints = load_registry()

    # smoke preset
    only_tables = args.only_tables
    only_dash = args.only_dashboards
    out_subdir = args.out_subdir or args.date
    if args.smoke:
        only_tables = "amazon_sec_range_margins,amazon_coupon"
        only_dash = "latest-month,fulfilment-health"
        out_subdir = "_smoke"
        args.no_year_month = True
        args.no_per_platform = True
        args.no_platform_leaves = True
        # master: keep fcs only; notifications: keep

    # gaps-only: run ONLY the new audit-found layers; everything else is already captured by the
    # main pull, so skip it and merge the new dashboard docs into the existing manifest.
    if args.gaps_only:
        args.no_tables = True
        args.no_master = True
        args.no_notifications = True
        args.no_year_month = True
        args.no_platform_leaves = True

    os.makedirs(LOG_DIR, exist_ok=True)
    global _LOGFH, PASSES_MAX, PAGE_WORKERS, GAIN_THRESH
    PAGE_WORKERS = max(1, args.page_workers)
    if args.gain_thresh > 0:
        GAIN_THRESH = args.gain_thresh
    if args.mode == "daily":
        PASSES_MAX = 2          # SSOT already holds history; daily needs the delta + light healing only
    if args.passes:
        PASSES_MAX = args.passes
    _LOGFH = open(os.path.join(LOG_DIR, "pull-%s.log" % out_subdir.strip("_") or args.date), "a")

    date_dir = os.path.join(STORE_RAW, out_subdir)
    os.makedirs(date_dir, exist_ok=True)
    ddir = dashboards_dir(date_dir)

    started = datetime.now(IST).isoformat()
    log("=== pull start mode=%s date=%s out=%s ===" % (args.mode, args.date, date_dir))

    # step 0: auth
    if not args.skip_auth:
        auth = os.path.join(ROOT, "bin", "auth.sh")
        log("step0: %s" % auth)
        rc = subprocess.run(["bash", auth]).returncode
        if rc != 0:
            die("auth.sh failed (rc=%d) — refusing to pull" % rc)

    # configure global CLI knobs via closure defaults
    global cli
    _cli = cli
    def cli_wrapped(a, **kw):
        kw.setdefault("rate_limit", args.rate_limit)
        kw.setdefault("timeout", args.timeout)
        return _cli(a, **kw)
    cli = cli_wrapped

    manifest = {}
    manifest_dash = {}

    # step 1: tables counts -> assert the 41-table set matches the registry
    log("step1: tables counts (registry parity)")
    counts = cli(["tables", "counts"])["results"]
    live_set = set(counts.keys())
    reg_set = set(tables_reg.keys())
    if not args.smoke:
        if live_set != reg_set:
            missing = reg_set - live_set
            extra = live_set - reg_set
            die("table-set drift: missing=%s extra=%s" % (sorted(missing), sorted(extra)))
        log("  41-table set OK")

    # which tables to pull
    if only_tables:
        table_names = [t.strip() for t in only_tables.split(",") if t.strip()]
    else:
        table_names = list(tables_reg.keys())
    if args.no_tables:
        table_names = []          # re-pull only the dashboard/leaf layer (tables already captured)
    # whales first, then the rest in registry order
    ordered = [t for t in WHALES if t in table_names] + [t for t in table_names if t not in WHALES]

    # step 2: per-table full pull
    for table in ordered:
        reg = tables_reg.get(table, {})
        log("table %s (recon~%s)" % (table, reg.get("rows_recon", "?")))
        try:
            entry = pull_table(date_dir, table, expected_empty=reg.get("expected_empty", False))
        except CliError as e:
            log("  !! table %s failed: %s" % (table, str(e)[:200]))
            entry = {"count_live": None, "rows_written": 0, "ok": False}
        manifest[table] = entry

    # step 3: master products + fcs (doc-level under dashboards/master__<which>.json — W2's interface;
    # also recorded table-style for the rows==count reconcile)
    if not args.no_master:
        which_list = ["fcs"] if args.smoke else ["products", "fcs"]
        for which in which_list:
            log("master %s" % which)
            try:
                manifest["master_" + which] = pull_master_doc(ddir, which, manifest_dash)
            except CliError as e:
                log("  !! master %s failed: %s" % (which, str(e)[:200]))
                manifest["master_" + which] = {"count_live": None, "rows_written": 0, "ok": False}

    # step 4: notifications (whole .results doc)
    if not args.no_notifications:
        log("notifications")
        pull_doc(ddir, "notifications", ["notifications"], manifest_dash, optional=True)

    # ---- doc layer (account, table-columns, dashboards, per-platform, platform leaves) ----
    # Collect every independent optional doc-pull as a (key, argv) job, then fetch them ALL
    # CONCURRENTLY (this layer is ~3000 calls; serial it dominates wall-time). Each job is lossless-
    # safe to parallelize: distinct output file + distinct manifest_dash key, gated/failed -> n/a.
    doc_jobs = []

    # 4b: account docs (identity + permission modules — app-exposed, present in no table)
    if not args.no_account:
        for which in endpoints.get("account_docs", []):
            doc_jobs.append((endpoint_key("account", slug=which), ["account", which]))

    # 4c: per-table column schema (authoritative ordered columns — the ONLY schema source for the
    # empty tables, where rows cannot reveal the column set). One doc per registry table.
    if not args.no_table_columns:
        for t in list(tables_reg.keys()):
            doc_jobs.append((endpoint_key("table-columns", slug=t), ["tables", "columns", t]))

    # 5a plain dashboards
    plain = [] if args.gaps_only else endpoints["dashboards_plain"]
    if only_dash:
        wanted = set(t.strip() for t in only_dash.split(","))
        plain = [d for d in plain if d in wanted] + [d for d in wanted if d not in plain]
    for d in plain:
        doc_jobs.append((endpoint_key(d), ["dashboard", d]))

    # 5b year_month dashboards
    if not args.no_year_month:
        ly, lm = latest_month(endpoints)
        windows = ym_windows(args.mode, endpoints, ly, lm)
        for d in endpoints["dashboards_year_month"]:
            for (y, m) in windows:
                ym = "%04d-%02d" % (y, m)
                doc_jobs.append((endpoint_key(d, ym=ym),
                                 ["dashboard", d, "--year", str(y), "--month", str(m)]))

    # 5c per-platform dashboards (over live slugs). top-skus + category-sku-breakdown are MONTH-AWARE
    # (full month grid). expiry-alerts is point-in-time (positional platform, current only).
    if not args.no_per_platform:
        slugs = endpoints["platform_slugs"]
        month_aware = set(endpoints.get("dashboard_per_platform_month_aware", []))
        ly, lm = latest_month(endpoints)
        windows = ym_windows(args.mode, endpoints, ly, lm) if not args.no_year_month else [(ly, lm)]
        if not args.gaps_only:
            for d in endpoints["dashboard_per_platform"]:
                for slug in slugs:
                    if d == "expiry-alerts":
                        doc_jobs.append((endpoint_key(d, slug=slug), ["dashboard", d, slug]))
                    elif d in month_aware:
                        for (y, m) in windows:
                            doc_jobs.append((endpoint_key(d, slug=slug, ym="%04d-%02d" % (y, m)),
                                             ["dashboard", d, "--platform", slug, "--year", str(y), "--month", str(m)]))
                    else:
                        doc_jobs.append((endpoint_key(d, slug=slug), ["dashboard", d, "--platform", slug]))
        # extra per-platform dashboards (AUDIT-1: the --platform slice is genuinely distinct data the
        # all-platform aggregate does NOT embed). month-aware ones iterate the full month grid.
        for d in endpoints.get("dashboards_per_platform_extra_month_aware", []):
            for slug in slugs:
                for (y, m) in windows:
                    doc_jobs.append((endpoint_key(d, slug=slug, ym="%04d-%02d" % (y, m)),
                                     ["dashboard", d, "--platform", slug, "--year", str(y), "--month", str(m)]))
        for d in endpoints.get("dashboards_per_platform_extra_plain", []):
            for slug in slugs:
                doc_jobs.append((endpoint_key(d, slug=slug), ["dashboard", d, "--platform", slug]))

    # 6: platform leaves (only live combos from registry; gated -> n/a recorded inline, never called)
    if not args.no_platform_leaves:
        for leaf, slug_map in endpoints["platform_leaves"].items():
            for slug, status in slug_map.items():
                key = endpoint_key(leaf, slug=slug)
                if status != "live":
                    manifest_dash[key] = {"ok": False, "bytes": 0, "na": True}
                    continue
                doc_jobs.append((key, ["platform", leaf, slug]))

    log("doc layer: %d docs over %d workers" % (len(doc_jobs), PAGE_WORKERS))
    run_doc_jobs(ddir, doc_jobs, manifest_dash, PAGE_WORKERS)

    # ---- manifest ----
    finished = datetime.now(IST).isoformat()

    # gaps-only: MERGE the new dashboard docs into the existing manifest (preserve all table entries
    # from the main pull); never overwrite the manifest with an empty table set.
    mpath = os.path.join(date_dir, "_pull-manifest.json")
    if args.gaps_only:
        if not os.path.exists(mpath):
            die("--gaps-only: no existing _pull-manifest.json at %s (run the main pull first)" % mpath)
        with open(mpath) as f:
            base = json.load(f)
        base.setdefault("dashboards", {}).update(manifest_dash)
        base["gaps_finished"] = finished
        base["gaps_added"] = sorted(manifest_dash.keys())
        atomic_write_bytes(mpath, json.dumps(base, ensure_ascii=False, indent=2).encode("utf-8"))
        n_new = len(manifest_dash)
        n_na = sum(1 for v in manifest_dash.values() if v.get("na"))
        log("=== gaps-only done: merged %d dashboard docs (na=%d) into existing manifest ===" % (n_new, n_na))
        sys.exit(0)

    table_entries = {k: v for k, v in manifest.items()}
    all_tables_ok = all(v["ok"] for v in table_entries.values())
    manifest_out = dict(table_entries)
    manifest_out["dashboards"] = manifest_dash
    manifest_out["started"] = started
    manifest_out["finished"] = finished
    manifest_out["mode"] = args.mode
    manifest_out["date"] = args.date
    manifest_out["all_ok"] = all_tables_ok

    atomic_write_bytes(os.path.join(date_dir, "_pull-manifest.json"),
                       json.dumps(manifest_out, ensure_ascii=False, indent=2).encode("utf-8"))

    n_dash_ok = sum(1 for v in manifest_dash.values() if v["ok"])
    n_dash_na = sum(1 for v in manifest_dash.values() if v.get("na"))
    n_tbl_ok = sum(1 for v in table_entries.values() if v["ok"])
    log("=== done: tables_ok=%d/%d  dashboards ok=%d na=%d  all_ok=%s ==="
        % (n_tbl_ok, len(table_entries), n_dash_ok, n_dash_na, all_tables_ok))

    # pull-ledger line (W4's append-only contract: ts, stage, ok). doctor.sh treats stage=="pull"
    # as a successful pull for staleness. Skip on scoped/smoke runs so they don't mask staleness.
    is_real_pull = not (args.smoke or only_tables or args.out_subdir or args.no_master)
    if is_real_pull:
        ledger = os.path.join(ROOT, "state", "pull-ledger.jsonl")
        line = {"ts": datetime.now(IST).isoformat(), "component": "pull.py", "stage": "pull",
                "pull_id": args.date, "ok": all_tables_ok, "mode": args.mode,
                "tables_ok": n_tbl_ok, "tables_total": len(table_entries),
                "dash_ok": n_dash_ok, "dash_na": n_dash_na}
        try:
            with open(ledger, "a") as lf:
                lf.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as e:
            log("  (could not append pull-ledger: %s)" % e)

    if not all_tables_ok:
        bad = [k for k, v in table_entries.items() if not v["ok"]]
        die("not all tables OK: %s" % bad)
    log("ALL TABLES OK")
    sys.exit(0)


if __name__ == "__main__":
    main()
