#!/usr/bin/env bash
# doctor.sh — JIVO pipeline health check  (SPEC §7, owner: W4)
#
# A read-only status probe (mirrors /opt/ecom-intel/healthcheck.sh as a stable,
# never-crashing entrypoint). Prints a status block and exits NON-ZERO on RED so
# cron / a wrapper can alert. Checks:
#   * last successful pull age          (state/pull-ledger.jsonl)
#   * per-table row count vs last run   (state/.doctor-rowcounts.json; alert on collapse)
#   * token expiry                      (state/token.json JWT exp)
#   * git push status                   (ahead/behind/dirty)
#   * vault integrity-gate last result  (last verify ingest line in the ledger)
# Fires a best-effort Telegram alert on RED (degrades to stderr + cron.log).
set -uo pipefail
export TZ='Asia/Kolkata'

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"
mkdir -p logs state
LOG="logs/cron.log"

if [ -f "$DIR/.env" ]; then set -a; . "$DIR/.env"; set +a; fi

# Thresholds (env-overridable).
PULL_MAX_AGE_H="${DOCTOR_PULL_MAX_AGE_H:-30}"     # RED if last good pull older than this
COLLAPSE_PCT="${DOCTOR_COLLAPSE_PCT:-50}"         # RED if a non-empty table drops > this %

# The heavy lifting is a single stdlib python probe that prints the block and
# returns an exit code: 0 GREEN, 1 RED. Bash wraps it for git + Telegram.
STATUS_OUT="$(python3 - "$PULL_MAX_AGE_H" "$COLLAPSE_PCT" <<'PYDOCTOR'
import datetime as dt, json, os, sys, subprocess

ROOT = os.getcwd()
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
now = dt.datetime.now(IST)
pull_max_age_h = float(sys.argv[1])
collapse_pct = float(sys.argv[2])

LEDGER = os.path.join(ROOT, "state", "pull-ledger.jsonl")
TABLES_DIR = os.path.join(ROOT, "store", "versioned", "tables")
SNAP = os.path.join(ROOT, "state", ".doctor-rowcounts.json")
TOKEN = os.path.join(ROOT, "state", "token.json")
REG = os.path.join(ROOT, "registry", "tables.json")

red = []
lines = []
def add(label, val): lines.append("  %-26s %s" % (label + ":", val))

def parse_ts(s):
    try:
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None

# --- ledger scan -----------------------------------------------------------
ledger = []
if os.path.exists(LEDGER):
    with open(LEDGER, encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if ln:
                try: ledger.append(json.loads(ln))
                except Exception: pass

# last successful pull age (any ok record from pull or verify-ingest)
def last_ok(stages):
    for rec in reversed(ledger):
        if rec.get("stage") in stages and rec.get("ok"):
            return rec
    return None

last_pull = last_ok({"pull", "ingest"})
if last_pull and parse_ts(last_pull.get("ts", "")):
    age_h = (now - parse_ts(last_pull["ts"])).total_seconds() / 3600.0
    flag = "OK" if age_h <= pull_max_age_h else "RED"
    if flag == "RED": red.append("last good pull %.1fh old (> %.0fh)" % (age_h, pull_max_age_h))
    add("last good pull", "%.1fh ago  [%s]" % (age_h, flag))
else:
    red.append("no successful pull/ingest in ledger")
    add("last good pull", "NONE  [RED]")

# vault integrity gate (last verify ingest result)
last_ing = None
for rec in reversed(ledger):
    if rec.get("stage") == "ingest":
        last_ing = rec; break
if last_ing is None:
    add("vault integrity gate", "no ingest verify yet  [warn]")
else:
    g = "OK" if last_ing.get("ok") else "RED"
    if g == "RED":
        red.append("last ingest verify FAILED (%d failures)" % last_ing.get("failed", 0))
    add("vault integrity gate", "%s  (%s)  [%s]"
        % ("GREEN" if last_ing.get("ok") else "FAIL", last_ing.get("ts", "?"), g))

# --- per-table row counts vs last run -------------------------------------
cur = {}
if os.path.isdir(TABLES_DIR):
    for fn in os.listdir(TABLES_DIR):
        if fn.endswith(".state.jsonl"):
            t = fn[:-len(".state.jsonl")]
            n = 0
            with open(os.path.join(TABLES_DIR, fn), encoding="utf-8") as fh:
                for ln in fh:
                    if ln.strip(): n += 1
            cur[t] = n
prev = {}
if os.path.exists(SNAP):
    try: prev = json.load(open(SNAP, encoding="utf-8")).get("counts", {})
    except Exception: prev = {}

collapses = []
for t, n in sorted(cur.items()):
    p = prev.get(t)
    if p and p > 0 and n < p * (1 - collapse_pct / 100.0):
        collapses.append("%s %d<-%d" % (t, n, p))
if collapses:
    red.append("row collapse: " + ", ".join(collapses))
    add("row-count vs last run", "COLLAPSE: " + "; ".join(collapses) + "  [RED]")
else:
    add("row-count vs last run", "%d tables, no collapse  [OK]" % len(cur))

# persist snapshot for next run (best-effort)
try:
    tmp = SNAP + ".tmp"
    json.dump({"ts": now.replace(microsecond=0).isoformat(), "counts": cur},
              open(tmp, "w", encoding="utf-8"))
    os.replace(tmp, SNAP)
except Exception:
    pass

# --- token expiry ----------------------------------------------------------
def jwt_exp(tokstr):
    import base64
    try:
        payload = tokstr.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
        return data.get("exp")
    except Exception:
        return None

tok_exp = None
if os.path.exists(TOKEN):
    try:
        tj = json.load(open(TOKEN, encoding="utf-8"))
        for k in ("expires_at", "exp"):
            if k in tj:
                v = tj[k]
                tok_exp = v if isinstance(v, (int, float)) else parse_ts(str(v))
                break
        if tok_exp is None:
            for k in ("token", "access_token", "jwt", "bearer"):
                if isinstance(tj.get(k), str):
                    tok_exp = jwt_exp(tj[k]); break
    except Exception:
        pass
if tok_exp is None:
    add("token expiry", "unknown (auth.sh refreshes via env creds)  [warn]")
else:
    if isinstance(tok_exp, dt.datetime):
        exp_dt = tok_exp if tok_exp.tzinfo else tok_exp.replace(tzinfo=IST)
    else:
        exp_dt = dt.datetime.fromtimestamp(float(tok_exp), IST)
    left_h = (exp_dt - now).total_seconds() / 3600.0
    g = "OK" if left_h > 1 else "RED"
    if g == "RED": red.append("token expired/near-expiry (%.1fh left)" % left_h)
    add("token expiry", "%.1fh left  [%s]" % (left_h, g))

print("\n".join(lines))
print("__REDCOUNT__%d" % len(red))
for r in red:
    print("__RED__" + r)
sys.exit(1 if red else 0)
PYDOCTOR
)"
PYRC=$?

# Split probe output from the RED markers.
BLOCK="$(printf '%s\n' "$STATUS_OUT" | grep -v '^__RED' || true)"
REDS="$(printf '%s\n' "$STATUS_OUT" | sed -n 's/^__RED__//p' || true)"

# --- git push status (bash side) -------------------------------------------
GIT_LINE="not a git repo"
if git rev-parse --git-dir >/dev/null 2>&1; then
  DIRTY="$(git status --porcelain 2>/dev/null | wc -l | tr -d ' ')"
  AHEAD="$(git rev-list --count @{u}..HEAD 2>/dev/null || echo '?')"
  BEHIND="$(git rev-list --count HEAD..@{u} 2>/dev/null || echo '?')"
  GIT_LINE="dirty=${DIRTY} ahead=${AHEAD} behind=${BEHIND}"
fi

echo "=================== JIVO doctor — $(date '+%F %T %Z') ==================="
echo "$BLOCK"
echo "  git push status:          $GIT_LINE"
echo "======================================================================="

if [ "$PYRC" != "0" ]; then
  echo "STATUS: RED"
  MSG="🛑 JIVO doctor RED ($(date '+%F %T')):"$'\n'"$REDS"
  TG="${TELEGRAM_BOT_TOKEN:-}"; CH="${TELEGRAM_OWNER_CHAT_ID:-${TELEGRAM_CHAT_ID:-}}"
  if [ -n "$TG" ] && [ -n "$CH" ] && command -v curl >/dev/null 2>&1; then
    curl -s --max-time 60 -X POST "https://api.telegram.org/bot${TG}/sendMessage" \
      --data-urlencode "chat_id=${CH}" --data-urlencode "text=${MSG}" >/dev/null 2>&1 || true
  fi
  echo "[$(date '+%F %T')] doctor: RED -> ${REDS//$'\n'/ ; }" >> "$LOG"
  exit 1
fi
echo "STATUS: GREEN"
echo "[$(date '+%F %T')] doctor: GREEN" >> "$LOG"
exit 0
