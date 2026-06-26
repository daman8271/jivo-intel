#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
archive_weekly.py — weekly cold archive  (SPEC §7, owner: W4)

For the target ISO week (default the week containing today, IST):
  * per table, replay the append-only changelog into present-state ROWS and
    write them to  archive/<YYYY-Www>/<table>.full.jsonl.gz
    (one {key,hash,first_seen,row} object per line, key-sorted, deterministic gzip).
  * bundle the doc-level Type-2 history (dashboards/master/notifications changelogs)
    into  archive/<YYYY-Www>/dashboards.tar.gz
  * write  archive/<YYYY-Www>/MANIFEST.json  with per-artifact {rows,sha256,bytes}.
  * prune store/raw/<date>/ directories OLDER than the archived week's Monday
    (the daily raw capture is transient; the versioned SSOT + this archive are
    the durable record).

Stdlib only. Atomic temp+rename everywhere. Read-only w.r.t. the SSOT
(store/versioned/) — it only reads changelogs and only writes under archive/
and prunes store/raw/.
"""

import argparse
import datetime as _dt
import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "registry")
VERSIONED = os.path.join(ROOT, "store", "versioned")
TABLES_DIR = os.path.join(VERSIONED, "tables")
RAW = os.path.join(ROOT, "store", "raw")
ARCHIVE = os.path.join(ROOT, "archive")
LOGDIR = os.path.join(ROOT, "logs")
CRONLOG = os.path.join(LOGDIR, "cron.log")

IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def now_ist():
    return _dt.datetime.now(IST).replace(microsecond=0).isoformat()


def log(msg):
    line = "[%s] archive_weekly: %s" % (now_ist(), msg)
    print(line, file=sys.stderr)
    try:
        os.makedirs(LOGDIR, exist_ok=True)
        with open(CRONLOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_registry_tables():
    with open(os.path.join(REGISTRY, "tables.json"), encoding="utf-8") as fh:
        return json.load(fh)["tables"]


def replay_present_rows(table):
    """Replay <table>.changelog.jsonl into present-state {key: (hash, first_seen, row)}."""
    path = os.path.join(TABLES_DIR, "%s.changelog.jsonl" % table)
    present = {}
    if not os.path.exists(path):
        return present
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
            present.pop(key, None)
        else:
            fs = present[key][1] if key in present else ev.get("first_seen")
            present[key] = (ev["hash"], fs, ev.get("row"))
    return present


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def iso_week_id(d):
    iso = d.isocalendar()
    return "%04d-W%02d" % (iso[0], iso[1])


def week_monday(d):
    """Monday of the ISO week containing date d."""
    return d - _dt.timedelta(days=d.weekday())


def write_table_archive(week_dir, table):
    """Write <table>.full.jsonl.gz; return (rows, sha256, bytes) or None if no SSOT."""
    present = replay_present_rows(table)
    path = os.path.join(week_dir, "%s.full.jsonl.gz" % table)
    tmp = path + ".tmp"
    rows = 0
    # deterministic gzip: zero mtime so sha256 is reproducible across runs
    with open(tmp, "wb") as raw_fh:
        with gzip.GzipFile(fileobj=raw_fh, mode="wb", mtime=0) as gz:
            with io.TextIOWrapper(gz, encoding="utf-8") as txt:
                for key in sorted(present.keys()):
                    h, fs, row = present[key]
                    txt.write(canon({"key": key, "hash": h,
                                     "first_seen": fs, "row": row}) + "\n")
                    rows += 1
    os.replace(tmp, path)
    return rows, sha256_file(path), os.path.getsize(path)


def write_dashboards_bundle(week_dir):
    """tar.gz of the doc-level changelogs (dashboards/master/notifications)."""
    path = os.path.join(week_dir, "dashboards.tar.gz")
    tmp = path + ".tmp"
    members = []
    for sub in ("dashboards", "master"):
        d = os.path.join(VERSIONED, sub)
        if os.path.isdir(d):
            for fn in sorted(os.listdir(d)):
                full = os.path.join(d, fn)
                if os.path.isfile(full):
                    members.append((full, os.path.join(sub, fn)))
    notif = os.path.join(VERSIONED, "notifications.changelog.jsonl")
    if os.path.isfile(notif):
        members.append((notif, "notifications.changelog.jsonl"))

    with open(tmp, "wb") as raw_fh:
        with gzip.GzipFile(fileobj=raw_fh, mode="wb", mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as tar:
                for full, arcname in members:
                    ti = tar.gettarinfo(full, arcname=arcname)
                    ti.mtime = 0  # deterministic
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    with open(full, "rb") as fh:
                        tar.addfile(ti, fh)
    os.replace(tmp, path)
    return len(members), sha256_file(path), os.path.getsize(path)


def prune_old_raw(monday):
    """Remove store/raw/<YYYY-MM-DD>/ dirs strictly before the archived week's Monday."""
    pruned = []
    if not os.path.isdir(RAW):
        return pruned
    for name in sorted(os.listdir(RAW)):
        full = os.path.join(RAW, name)
        if not os.path.isdir(full):
            continue
        try:
            d = _dt.date.fromisoformat(name)
        except ValueError:
            continue  # not a date dir — leave it alone
        if d < monday:
            shutil.rmtree(full)
            pruned.append(name)
    return pruned


def main(argv=None):
    ap = argparse.ArgumentParser(description="JIVO weekly cold archive (SPEC §7)")
    ap.add_argument("--date", default=None, help="any date in the target week (default today IST)")
    ap.add_argument("--no-prune", action="store_true", help="skip pruning store/raw/")
    args = ap.parse_args(argv)

    if args.date:
        ref = _dt.date.fromisoformat(args.date)
    else:
        ref = _dt.datetime.now(IST).date()
    week = iso_week_id(ref)
    monday = week_monday(ref)
    week_dir = os.path.join(ARCHIVE, week)
    os.makedirs(week_dir, exist_ok=True)

    tables = load_registry_tables()
    artifacts = {}
    failures = []

    for table in sorted(tables.keys()):
        try:
            rows, digest, nbytes = write_table_archive(week_dir, table)
            artifacts["%s.full.jsonl.gz" % table] = {
                "rows": rows, "sha256": digest, "bytes": nbytes}
        except Exception as exc:
            failures.append("%s: %s" % (table, exc))
            log("FAIL archiving %s: %s" % (table, exc))

    try:
        m_members, m_sha, m_bytes = write_dashboards_bundle(week_dir)
        artifacts["dashboards.tar.gz"] = {
            "rows": m_members, "sha256": m_sha, "bytes": m_bytes}
    except Exception as exc:
        failures.append("dashboards bundle: %s" % exc)
        log("FAIL dashboards bundle: %s" % exc)

    manifest = {
        "week": week,
        "week_monday": monday.isoformat(),
        "generated": now_ist(),
        "tables": len(tables),
        "artifacts": artifacts,
        "ok": not failures,
        "failures": failures,
    }
    mpath = os.path.join(week_dir, "MANIFEST.json")
    mtmp = mpath + ".tmp"
    with open(mtmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(mtmp, mpath)

    log("week %s: %d artifacts, %d failures -> %s"
        % (week, len(artifacts), len(failures), week_dir))

    if not args.no_prune and not failures:
        pruned = prune_old_raw(monday)
        if pruned:
            log("pruned %d raw dir(s) older than %s: %s"
                % (len(pruned), monday.isoformat(), ", ".join(pruned)))
    elif failures:
        log("prune SKIPPED — archive had failures (preserve raw as fallback)")

    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
