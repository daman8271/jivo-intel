#!/usr/bin/env bash
# run_daily.sh — JIVO zero-loss daily sweep  (SPEC §7, owner: W4)
#
# Mirrors /opt/ecom-intel/run_all.sh: a single blocking chain-lock, an integrity
# gate that MUST pass before anything is committed, atomic last-good preservation,
# and a best-effort Telegram owner alert on failure. The SSOT is irreplaceable —
# on ANY failure in the gated chain we DO NOT COMMIT, we preserve the last-good
# tree, alert, and run the deeper sample verifier for diagnostics.
#
# Chain (each step gates the next):
#   auth.sh -> pull.py --mode daily -> ssot.py -> verify.py --stage ingest -> vault_build.py
#   GREEN  -> git add store vault archive registry; commit; pull --rebase --autostash; push (1 retry)
#   RED    -> no commit; preserve last-good; Telegram 🛑 <failing stage>; verify.py --stage sample --n 40
# Sundays additionally run archive_weekly.py (then its own raw-prune).
# Everything BELOW the integrity gate (sample, archive, doctor) is best-effort `|| true`.
set -uo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"   # jivo-intel root
cd "$DIR"
mkdir -p logs state

# ---- date (default today IST) + --date override ----------------------------
D=""
while [ $# -gt 0 ]; do
  case "$1" in
    --date) D="$2"; shift 2 ;;
    --date=*) D="${1#--date=}"; shift ;;
    *) echo "run_daily: unknown arg: $1" >&2; shift ;;
  esac
done
[ -n "$D" ] || D="$(TZ='Asia/Kolkata' date +%F)"
export TZ='Asia/Kolkata'

LOG="logs/cron.log"
stamp() { date '+%F %T'; }
say()   { echo "[$(stamp)] run_daily: $*" | tee -a "$LOG" ; }

# ---- load env (.env is 0600 + gitignored; carries creds + optional TELEGRAM_*)
if [ -f "$DIR/.env" ]; then
  set -a; . "$DIR/.env"; set +a
fi
export PATH="/root/go/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"
if [ -z "${JIVO_ECOM_PP_CLI:-}" ] && [ -x /root/go/bin/jivo-ecom-pp-cli ]; then
  export JIVO_ECOM_PP_CLI="/root/go/bin/jivo-ecom-pp-cli"
fi
if [ -z "${JIVO_CLI:-}" ] && [ -n "${JIVO_ECOM_PP_CLI:-}" ]; then
  export JIVO_CLI="$JIVO_ECOM_PP_CLI"
fi

# ---- Telegram owner alert (best-effort; degrades to stderr + cron.log) ------
tg() {  # tg <text>
  local text="$1"
  echo "[$(stamp)] run_daily ALERT: ${text//$'\n'/ | }" | tee -a "$LOG" >&2
  local TG="${TELEGRAM_BOT_TOKEN:-}"
  local CH="${TELEGRAM_OWNER_CHAT_ID:-${TELEGRAM_CHAT_ID:-}}"
  [ -n "$TG" ] && [ -n "$CH" ] && command -v curl >/dev/null 2>&1 && \
    curl -s --max-time 60 -X POST "https://api.telegram.org/bot${TG}/sendMessage" \
      --data-urlencode "chat_id=${CH}" \
      --data-urlencode "text=${text}" >/dev/null 2>&1 || true
}

say "START date=$D"

# ---- DAILY CHAIN LOCK ------------------------------------------------------
# One sweep at a time. Blocking acquire with a ~2h cap; if a prior sweep still
# holds it after 7200s, alert + SKIP (never run two SSOT writers concurrently).
LOCK="logs/.daily.lock"
HAVE_LOCK=0
if command -v flock >/dev/null 2>&1; then
  if exec 8>"$LOCK" 2>/dev/null; then
    if ! flock -n 8; then
      say "waiting for prior daily sweep (logs/.daily.lock held)"
      if ! flock -w 7200 8; then
        tg "⚠️ JIVO run_daily ($D): daily lock busy >2h — sweep SKIPPED (prior sweep still running)."
        say "lock NOT acquired in 7200s -> SKIP"
        exit 0
      fi
    fi
    HAVE_LOCK=1
  else
    say "cannot open $LOCK -> degrading to unlocked"
  fi
fi

# ---- gated chain -----------------------------------------------------------
GATE_OK=1
FAIL_STAGE=""
run_stage() {  # run_stage <name> <cmd...>
  local name="$1"; shift
  say "stage $name: $*"
  if "$@" >>"logs/pull-$D.log" 2>&1; then
    say "stage $name: OK"
    return 0
  else
    local rc=$?
    say "stage $name: FAILED (rc=$rc)"
    GATE_OK=0; FAIL_STAGE="$name"
    return "$rc"
  fi
}

# 0) token refresh (auth.sh owns env-cred login; read-only otherwise)
if [ -x bin/auth.sh ] || [ -f bin/auth.sh ]; then
  run_stage auth bash bin/auth.sh || true   # auth failure surfaces at the live pull anyway
fi

# 1..4) the integrity chain — break on first failure
if [ "$GATE_OK" = "1" ]; then run_stage pull   python3 bin/pull.py --date "$D" --mode daily || true; fi
if [ "$GATE_OK" = "1" ]; then run_stage ssot   python3 bin/ssot.py --date "$D"             || true; fi
if [ "$GATE_OK" = "1" ]; then run_stage verify-ingest python3 bin/verify.py --stage ingest --date "$D" || true; fi
if [ "$GATE_OK" = "1" ]; then run_stage vault  python3 bin/vault_build.py                  || true; fi

# ---- INTEGRITY GATE --------------------------------------------------------
if [ "$GATE_OK" = "1" ]; then
  say "integrity gate GREEN -> committing"
  git add store vault archive registry >/dev/null 2>&1 || true
  if git diff --cached --quiet 2>/dev/null; then
    say "nothing changed — no commit"
  else
    git commit -m "daily $D" >/dev/null 2>&1 || true
    git pull --rebase --autostash >/dev/null 2>&1 || true
    if git push >/dev/null 2>&1; then
      say "pushed."
    else
      say "push failed — retrying once"
      git pull --rebase --autostash >/dev/null 2>&1 || true
      if git push >/dev/null 2>&1; then say "pushed (retry)."; else
        tg "⚠️ JIVO run_daily ($D): commit OK but git push FAILED twice — investigate."
        say "push FAILED twice."
      fi
    fi
  fi
else
  # RED: never commit. Last-good tree is already on disk (we never overwrote the
  # committed SSOT — ssot.py is atomic and fail-closed). Alert + deep diagnostics.
  say "integrity gate RED at stage '$FAIL_STAGE' -> NOT committing; last-good preserved"
  tg "🛑 JIVO run_daily ($D) FAILED at stage: ${FAIL_STAGE}. SSOT NOT updated/committed; last-good preserved. See logs/pull-$D.log"
  # best-effort: discard any uncommitted partial staging so the tree stays last-good
  git reset -q >/dev/null 2>&1 || true
fi

# ---- below the gate: best-effort only --------------------------------------
# Deep sample verification (runs on RED for diagnostics; also a nightly spot-check on GREEN).
say "sample verification (best-effort)"
python3 bin/verify.py --stage sample --n 40 --date "$D" >>"logs/pull-$D.log" 2>&1 || true

# Sundays: weekly cold archive + raw prune (best-effort). DOW 7 = Sunday (IST).
if [ "$(date +%u)" = "7" ]; then
  say "Sunday -> weekly archive (best-effort)"
  python3 bin/archive_weekly.py --date "$D" >>"logs/pull-$D.log" 2>&1 || true
  if [ "$GATE_OK" = "1" ]; then
    git add archive store >/dev/null 2>&1 || true
    git diff --cached --quiet 2>/dev/null || { git commit -m "weekly archive $D" >/dev/null 2>&1 || true; git push >/dev/null 2>&1 || true; }
  fi
fi

# Health snapshot (never blocks).
[ -f bin/doctor.sh ] && bash bin/doctor.sh >>"logs/pull-$D.log" 2>&1 || true

if [ "$HAVE_LOCK" = "1" ]; then exec 8>&- 2>/dev/null || true; fi
say "DONE date=$D gate=$([ "$GATE_OK" = 1 ] && echo GREEN || echo RED)"

# --- eager today/ hook (instant-per-source rule): publish the ecom-app slice
# into the data-bank today/ once its "daily $D" commit exists. Self-gates on that
# marker (no-ops on RED / unchanged); never blocks this sweep. ---
/opt/ecom-intel/bin/advance_today_section.sh ecom-app --date "$D" >> /opt/ecom-intel/bin/build_today.log 2>&1 || true

exit 0
