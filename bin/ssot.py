#!/usr/bin/env python3
"""
ssot.py — JIVO-INTEL zero-loss versioned store engine  (W2 / SPEC §3)

Reads one day's raw capture (store/raw/<date>/<table>.jsonl, produced by pull.py)
and folds it into the append-only versioned SSOT:

    store/versioned/tables/<table>.changelog.jsonl   (APPEND-ONLY events)
    store/versioned/tables/<table>.state.jsonl       (derived current index, atomic rewrite)

Doc-level Type-2 history (whole-document, append-on-change) for dashboards / master / notifications:

    store/versioned/dashboards/<endpoint_key>.changelog.jsonl
    store/versioned/master/<products|fcs>.changelog.jsonl
    store/versioned/notifications.changelog.jsonl

Contract (FIXED — see SPEC.md §3):
  changelog line: {seq,pull_id,ts,event,key,hash,prev_hash,first_seen,row}
  state line:     {key,hash,first_seen,last_seen,version}

Guarantees:
  * append-only changelog — a (key,hash) content pair is written AT MOST ONCE EVER
  * fail-closed reconcile — inserts+updates+unchanged == count_live == len(state) per table
  * idempotent — re-running a date over identical raw appends ZERO new changelog lines
  * "no information lost at any cost" — tombstone deletes carry the last-known row;
    doc-level history keeps every distinct version of every document

Python 3 stdlib only. No network, no LLM, fully rebuildable from the store.
"""

import argparse
import glob
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# Canonicalisation + hashing  (EXACTLY as SPEC §3 — do not change)
# --------------------------------------------------------------------------- #
def canon(obj):
    """Canonical JSON: sorted keys, compact separators, unicode preserved.
    Null preserved; floats serialise via Python's repr (json default)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha_hex(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def content_hash(row):
    """The `hash` field value: 'sha256:<hex>' over the canonical row."""
    return "sha256:" + sha_hex(canon(row))


def derive_key(table, strategy, cols, row):
    """Identity key per registry strategy.
      id           -> '<table>:<row.id>'
      natural      -> '<table>:' + sha256hex(canon(subset over key.cols))
      content-hash -> '<table>:' + content_hash(row)   (== '<table>:sha256:<hex>')
    """
    if strategy == "id":
        if "id" not in row or row["id"] is None:
            raise KeyError(f"{table}: id-keyed row missing 'id': {canon(row)[:200]}")
        return f"{table}:{row['id']}"
    if strategy == "natural":
        subset = {c: row.get(c) for c in cols}
        return f"{table}:" + sha_hex(canon(subset))
    if strategy == "content-hash":
        return f"{table}:" + content_hash(row)
    raise ValueError(f"{table}: unknown key strategy {strategy!r}")


# --------------------------------------------------------------------------- #
# Small IO helpers
# --------------------------------------------------------------------------- #
def now_ts():
    return datetime.now(IST).isoformat()


def read_jsonl(path):
    """Yield parsed objects from a .jsonl file (skips blank lines)."""
    with open(path, "r", encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{ln}: bad JSON line: {e}") from e


def atomic_write(path, text):
    """Write text to path atomically via temp file + os.replace (+ fsync)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def append_lines(path, lines):
    """Append already-serialised JSON lines to an append-only file (+ fsync)."""
    if not lines:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for ln in lines:
            f.write(ln + "\n")
        f.flush()
        os.fsync(f.fileno())


def log_ledger(ledger_path, record):
    os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(canon(record) + "\n")
        f.flush()
        os.fsync(f.fileno())


# --------------------------------------------------------------------------- #
# Changelog / state loading
# --------------------------------------------------------------------------- #
def load_state(state_path):
    """key -> {hash, first_seen, last_seen, version}."""
    state = {}
    if not os.path.exists(state_path):
        return state
    for obj in read_jsonl(state_path):
        state[obj["key"]] = obj
    return state


def scan_changelog(changelog_path, want_rows_for):
    """Single pass over an existing changelog. Returns:
        max_seq          highest seq seen (0 if empty)
        seen_pairs       set of (key, hash) for insert|update events (content de-dup)
        last_row_by_key  key -> most-recent event row, ONLY for keys in want_rows_for
                         (used to reconstruct tombstone rows for deletes without
                          bloating memory for the whole table)
    """
    max_seq = 0
    seen_pairs = set()
    last_row_by_key = {}
    if not os.path.exists(changelog_path):
        return max_seq, seen_pairs, last_row_by_key
    for ev in read_jsonl(changelog_path):
        seq = ev.get("seq", 0)
        if seq > max_seq:
            max_seq = seq
        etype = ev.get("event")
        key = ev.get("key")
        if etype in ("insert", "update"):
            seen_pairs.add((key, ev.get("hash")))
        if key in want_rows_for and "row" in ev:
            last_row_by_key[key] = ev["row"]
    return max_seq, seen_pairs, last_row_by_key


# --------------------------------------------------------------------------- #
# Per-table upsert (compute-then-commit — table-atomic)
# --------------------------------------------------------------------------- #
class ReconcileError(Exception):
    pass


def process_table(table, tdef, raw_path, versioned_dir, date, count_live):
    """Fold one table's daily raw file into its changelog+state.
    Returns a dict describing the (uncommitted) plan + counts. Reconcile is
    asserted here BEFORE any bytes are written by the caller.
    """
    strategy = tdef["key"]["strategy"]
    cols = tdef["key"].get("cols", [])

    changelog_path = os.path.join(versioned_dir, "tables", f"{table}.changelog.jsonl")
    state_path = os.path.join(versioned_dir, "tables", f"{table}.state.jsonl")

    # 1. Today's rows -> today_map (distinct identity keys; last row wins on dup).
    today_map = {}          # key -> (hash, row)
    rows_read = 0
    dup_keys = 0
    for row in read_jsonl(raw_path):
        rows_read += 1
        key = derive_key(table, strategy, cols, row)
        h = content_hash(row)
        if key in today_map:
            dup_keys += 1
        today_map[key] = (h, row)

    # 2. Prior state + which keys are disappearing (need tombstone rows).
    old_state = load_state(state_path)
    deleted_keys = set(old_state) - set(today_map)

    # 3. One pass over the existing changelog.
    max_seq, seen_pairs, last_row_by_key = scan_changelog(changelog_path, deleted_keys)

    # 4. Classify.
    inserts = updates = unchanged = deletes = 0
    new_events = []          # (sort_key, event_dict)  — content lines + tombstones
    new_state = {}           # rebuilt present-key index

    for key in sorted(today_map):
        h, row = today_map[key]
        prev = old_state.get(key)
        if prev is None:
            # INSERT
            inserts += 1
            new_state[key] = {
                "key": key, "hash": h, "first_seen": date,
                "last_seen": date, "version": 1,
            }
            if (key, h) not in seen_pairs:
                new_events.append((key, {
                    "event": "insert", "key": key, "hash": h, "prev_hash": None,
                    "first_seen": date, "row": row,
                }))
                seen_pairs.add((key, h))
        elif prev["hash"] == h:
            # UNCHANGED — bump last_seen only (state); no changelog line.
            unchanged += 1
            new_state[key] = {
                "key": key, "hash": h, "first_seen": prev["first_seen"],
                "last_seen": date, "version": prev["version"],
            }
        else:
            # UPDATE — content changed; carry prev_hash.
            updates += 1
            new_state[key] = {
                "key": key, "hash": h, "first_seen": prev["first_seen"],
                "last_seen": date, "version": prev["version"] + 1,
            }
            if (key, h) not in seen_pairs:   # skip if content seen before (revert)
                new_events.append((key, {
                    "event": "update", "key": key, "hash": h,
                    "prev_hash": prev["hash"], "first_seen": prev["first_seen"],
                    "row": row,
                }))
                seen_pairs.add((key, h))

    # 5. Deletes — tombstone carries last-known row; key leaves state.
    for key in sorted(deleted_keys):
        prev = old_state[key]
        deletes += 1
        last_row = last_row_by_key.get(key)   # None if changelog lacked it
        new_events.append((key, {
            "event": "delete", "key": key, "hash": prev["hash"],
            "prev_hash": prev["hash"], "first_seen": prev["first_seen"],
            "row": last_row,
        }))

    # 6. Reconcile — FAIL-CLOSED.
    #    present (distinct keys) == len(state); and every raw row is accounted for:
    #    present + dup_keys == count_live (dup_keys = rows that collapsed onto an
    #    existing key this pull). For content-hash tables a collapse is a BYTE-IDENTICAL
    #    row (key IS the content hash) → provably lossless. For id/natural tables a
    #    collapse means the key is too coarse and would HIDE a distinct row → data-loss
    #    risk → hard fail (this is what catches an under-specified natural key).
    present = inserts + updates + unchanged
    if present != len(new_state):
        raise ReconcileError(
            f"{table}: present({present}) != len(state)({len(new_state)})")
    if strategy in ("id", "natural") and dup_keys != 0:
        raise ReconcileError(
            f"{table}: {dup_keys} duplicate {strategy}-keys — key is not unique and "
            f"would lose distinct rows; use content-hash for this table "
            f"[rows_read={rows_read}, present={present}]")
    if count_live is not None and (present + dup_keys) != count_live:
        raise ReconcileError(
            f"{table}: present({present})+dup_keys({dup_keys}) != count_live({count_live}) "
            f"[rows_read={rows_read}] — rows unaccounted for")

    # 7. Serialise the planned writes (NOT yet committed by caller).
    serialized_events = []
    seq = max_seq
    for _, ev in sorted(new_events, key=lambda x: (x[0], x[1]["event"])):
        seq += 1
        line = {
            "seq": seq, "pull_id": date, "ts": now_ts(), "event": ev["event"],
            "key": ev["key"], "hash": ev["hash"], "prev_hash": ev["prev_hash"],
            "first_seen": ev["first_seen"], "row": ev["row"],
        }
        serialized_events.append(canon(line))

    state_text = "".join(
        canon(new_state[k]) + "\n" for k in sorted(new_state)
    )

    return {
        "table": table, "changelog_path": changelog_path, "state_path": state_path,
        "events": serialized_events, "state_text": state_text,
        "inserts": inserts, "updates": updates, "unchanged": unchanged,
        "deletes": deletes, "present": present, "count_live": count_live,
        "rows_read": rows_read, "dup_keys": dup_keys, "state_len": len(new_state),
    }


def commit_table(plan):
    """Write a validated table plan: append changelog, atomic-rewrite state."""
    append_lines(plan["changelog_path"], plan["events"])
    atomic_write(plan["state_path"], plan["state_text"])


# --------------------------------------------------------------------------- #
# Doc-level Type-2 (dashboards / master / notifications)
# --------------------------------------------------------------------------- #
def doc_target_path(versioned_dir, endpoint_key):
    """Route a raw dashboards/*.json doc to its versioned changelog file."""
    k = endpoint_key
    low = k.lower()
    if "notification" in low:
        return os.path.join(versioned_dir, "notifications.changelog.jsonl")
    if low.startswith("master"):
        leaf = "products" if "product" in low else "fcs" if "fcs" in low else \
            k.split("__", 1)[-1].split("-", 1)[-1] or k
        return os.path.join(versioned_dir, "master", f"{leaf}.changelog.jsonl")
    return os.path.join(versioned_dir, "dashboards", f"{k}.changelog.jsonl")


def last_doc_hash(path):
    """Hash of the most-recently recorded doc version, or None if no history."""
    last = None
    if os.path.exists(path):
        for ev in read_jsonl(path):
            last = ev
    return last.get("hash") if last else None


def last_doc_seq(path):
    seq = 0
    if os.path.exists(path):
        for ev in read_jsonl(path):
            seq = max(seq, ev.get("seq", 0))
    return seq


def process_docs(raw_day_dir, versioned_dir, date):
    """Whole-document Type-2: append a new version ONLY when the doc hash differs
    from the last recorded one. Returns (written, scanned)."""
    docs_dir = os.path.join(raw_day_dir, "dashboards")
    written = scanned = 0
    if not os.path.isdir(docs_dir):
        return written, scanned
    for path in sorted(glob.glob(os.path.join(docs_dir, "*.json"))):
        scanned += 1
        endpoint_key = os.path.splitext(os.path.basename(path))[0]
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        h = content_hash(doc)
        target = doc_target_path(versioned_dir, endpoint_key)
        prev = last_doc_hash(target)
        if prev == h:
            continue                      # unchanged — record nothing
        seq = last_doc_seq(target) + 1
        # first_seen: keep the original if this doc was ever recorded before.
        first_seen = date
        if os.path.exists(target):
            for ev in read_jsonl(target):
                if ev.get("key") == endpoint_key:
                    first_seen = ev.get("first_seen", date)
                    break
        line = {
            "seq": seq, "pull_id": date, "ts": now_ts(),
            "event": "insert" if prev is None else "update",
            "key": endpoint_key, "hash": h,
            "prev_hash": prev, "first_seen": first_seen, "doc": doc,
        }
        append_lines(target, [canon(line)])
        written += 1
    return written, scanned


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description="JIVO zero-loss SSOT folder (SPEC §3)")
    ap.add_argument("--date", required=True, help="pull date YYYY-MM-DD (== pull_id)")
    ap.add_argument("--raw-dir", default=None,
                    help="raw day dir holding <table>.jsonl (default store/raw/<date>)")
    ap.add_argument("--versioned-dir", default="store/versioned",
                    help="output versioned store (default store/versioned)")
    ap.add_argument("--registry", default="registry/tables.json")
    ap.add_argument("--ledger", default="state/pull-ledger.jsonl")
    args = ap.parse_args(argv)

    date = args.date
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        print(f"ssot: --date must be YYYY-MM-DD, got {date!r}", file=sys.stderr)
        return 2

    raw_day_dir = args.raw_dir if args.raw_dir else os.path.join("store", "raw", date)
    versioned_dir = args.versioned_dir

    if not os.path.isdir(raw_day_dir):
        print(f"ssot: raw dir not found: {raw_day_dir}", file=sys.stderr)
        return 2

    with open(args.registry, "r", encoding="utf-8") as f:
        registry = json.load(f)
    tables = registry["tables"]

    # Manifest gives the authoritative count_live per table for fail-closed reconcile.
    manifest = {}
    manifest_path = os.path.join(raw_day_dir, "_pull-manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

    processed = []
    skipped = []
    summaries = []

    # Per-table: compute + reconcile fully in memory, then commit (table-atomic),
    # then release. Bounds resident memory to one table at a time — critical for
    # the lead's full historical backfill (e.g. swiggySec ~491k rows). The
    # changelog is append-only and every table write is reconcile-gated, so a
    # failure mid-sweep leaves earlier tables advanced but UNCOMMITTED-to-git
    # (the cron blocks the commit on our nonzero exit) and loses no data; the
    # next green run converges idempotently.
    for table in sorted(tables):
        raw_path = os.path.join(raw_day_dir, f"{table}.jsonl")
        if not os.path.exists(raw_path):
            skipped.append(table)
            continue
        mrow = manifest.get(table) if isinstance(manifest, dict) else None
        count_live = mrow.get("count_live") if isinstance(mrow, dict) else None
        try:
            plan = process_table(table, tables[table], raw_path,
                                  versioned_dir, date, count_live)
            commit_table(plan)
        except (ReconcileError, KeyError, ValueError) as e:
            log_ledger(args.ledger, {
                "ts": now_ts(), "stage": "ssot", "date": date, "table": table,
                "status": "FAIL", "reason": str(e),
            })
            print(f"ssot: FAIL-CLOSED on {table}: {e}", file=sys.stderr)
            print("ssot: stopped; committed-git last-good preserved (cron blocks "
                  "this pull's commit).", file=sys.stderr)
            return 1
        processed.append(table)
        summaries.append({k: plan[k] for k in
                          ("table", "inserts", "updates", "unchanged",
                           "deletes", "state_len")})

    # ---- Doc-level Type-2 history ----
    docs_written, docs_scanned = process_docs(raw_day_dir, versioned_dir, date)

    # ---- Summary line to the ledger (audit trail) ----
    tot = {k: sum(s[k] for s in summaries) for k in
           ("inserts", "updates", "unchanged", "deletes")}
    log_ledger(args.ledger, {
        "ts": now_ts(), "stage": "ssot", "date": date, "status": "OK",
        "tables_processed": len(processed), "tables_skipped": len(skipped),
        "docs_written": docs_written, "docs_scanned": docs_scanned, **tot,
    })

    print(f"ssot OK  date={date}  tables={len(processed)} "
          f"(skipped {len(skipped)})  ins={tot['inserts']} upd={tot['updates']} "
          f"unch={tot['unchanged']} del={tot['deletes']}  "
          f"docs={docs_written}/{docs_scanned}", file=sys.stderr)
    for s in summaries:
        print(f"  {s['table']:<28} ins={s['inserts']} upd={s['updates']} "
              f"unch={s['unchanged']} del={s['deletes']} state={s['state_len']}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
