#!/usr/bin/env bash
# bin/auth.sh — token step-0 for the JIVO zero-loss pipeline (SPEC §0,§4 step 0; W1).
#
# Responsibilities:
#   1. Source .env (if present) so JIVO_ECOM_EMAIL / JIVO_ECOM_PASSWORD are available.
#   2. Read the current token expiry (the CLI keeps it, non-secret, in config.toml).
#   3. If the token is missing or has < 2h to expiry, refresh it via `auth login`
#      using --password-stdin (the password is NEVER echoed, logged, or written to disk).
#   4. Persist the EXPIRY (not the JWT) to state/token.json (chmod 600).
#   5. Connectivity gate: `jivo-ecom-pp-cli doctor` must be green (api reachable + creds valid).
#
# Cardinal rule (CLAUDE.md + SPEC §0): the only acceptable at-rest credential is the token the
# CLI already stores in its own config.toml. We therefore DO NOT copy the JWT into state/token.json;
# we record only the (non-secret) expiry there for scheduling. The password lives only in env at
# runtime and is fed to the CLI over stdin.
#
# Exit codes: 0 = ok to proceed (token valid, doctor green) OR the documented "no-creds, near-expiry
# but still-valid stored token" warn path. Nonzero = fail-closed (doctor red / refresh failed).

set -euo pipefail

CLI="${JIVO_ECOM_PP_CLI:-jivo-ecom-pp-cli}"
if ! command -v "$CLI" >/dev/null 2>&1; then
  for cand in /root/go/bin/jivo-ecom-pp-cli /root/printing-press/library/jivo-ecom/cmd/jivo-ecom-pp-cli; do
    if [[ -x "$cand" ]]; then
      CLI="$cand"
      break
    fi
  done
fi
THRESHOLD_SECONDS=$((2 * 60 * 60))   # refresh if < 2h to expiry

# --- resolve repo root (this script lives in <root>/bin/) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
STATE_DIR="$ROOT/state"
TOKEN_STATE="$STATE_DIR/token.json"
CONFIG_PATH="${XDG_CONFIG_HOME:-$HOME/.config}/jivo-ecom-pp-cli/config.toml"
mkdir -p "$STATE_DIR"

log()  { printf '[auth.sh] %s\n' "$*" >&2; }
warn() { printf '[auth.sh] WARN: %s\n' "$*" >&2; }
die()  { printf '[auth.sh] ERROR: %s\n' "$*" >&2; exit 1; }

# --- 1. source .env (0600, gitignored) without leaking values ---
if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
  log "loaded .env"
fi

# --- helper: read the non-secret token_expiry from config.toml ---
read_config_expiry() {
  [[ -f "$CONFIG_PATH" ]] || return 1
  local v
  v="$(grep -E '^[[:space:]]*token_expiry[[:space:]]*=' "$CONFIG_PATH" | head -1 \
        | sed -E "s/^[[:space:]]*token_expiry[[:space:]]*=[[:space:]]*//; s/^['\"]//; s/['\"][[:space:]]*$//")" || return 1
  [[ -n "$v" ]] || return 1
  printf '%s' "$v"
}

# epoch seconds for an ISO timestamp (GNU date); empty on failure
to_epoch() { date -d "$1" +%s 2>/dev/null || true; }

NOW_EPOCH="$(date +%s)"

EXPIRY_RAW="$(read_config_expiry || true)"
EXP_EPOCH=""
if [[ -n "$EXPIRY_RAW" ]]; then
  EXP_EPOCH="$(to_epoch "$EXPIRY_RAW")"
fi

NEED_REFRESH=0
if [[ -z "$EXP_EPOCH" ]]; then
  log "no readable token_expiry — treating as needs-refresh"
  NEED_REFRESH=1
else
  REMAIN=$(( EXP_EPOCH - NOW_EPOCH ))
  log "token expiry=$EXPIRY_RAW (${REMAIN}s remaining)"
  if (( REMAIN < THRESHOLD_SECONDS )); then
    NEED_REFRESH=1
  fi
fi

REFRESHED=false

if (( NEED_REFRESH )); then
  if [[ -n "${JIVO_ECOM_EMAIL:-}" && -n "${JIVO_ECOM_PASSWORD:-}" ]]; then
    log "refreshing token via auth login (email=${JIVO_ECOM_EMAIL}, password via stdin)"
    # Feed the password on stdin ONLY. Never expand it onto a command line / log / file.
    if printf '%s' "$JIVO_ECOM_PASSWORD" \
         | "$CLI" auth login --email "$JIVO_ECOM_EMAIL" --password-stdin --no-input >/dev/null 2>&1; then
      REFRESHED=true
      unset JIVO_ECOM_PASSWORD
      # re-read freshly-written expiry
      EXPIRY_RAW="$(read_config_expiry || true)"
      [[ -n "$EXPIRY_RAW" ]] && EXP_EPOCH="$(to_epoch "$EXPIRY_RAW")"
      log "token refreshed; new expiry=$EXPIRY_RAW"
    else
      unset JIVO_ECOM_PASSWORD
      die "auth login failed (check credentials / connectivity)"
    fi
  else
    # No creds available. Documented behavior: warn + continue on the stored token (exit 0),
    # so the pipeline still runs. Downstream pull.py is fail-closed on meta.source if the token is dead.
    warn "token is missing or < 2h to expiry and no JIVO_ECOM_EMAIL/JIVO_ECOM_PASSWORD in env."
    warn "continuing on the stored token; refresh it soon with: $CLI auth login"
  fi
fi

# --- 4. persist expiry (NOT the token) to state/token.json ---
write_token_state() {
  local tmp="$TOKEN_STATE.part"
  local epoch_field="null"
  [[ -n "$EXP_EPOCH" ]] && epoch_field="$EXP_EPOCH"
  cat > "$tmp" <<EOF
{
  "_note": "Expiry-only token state. The JWT itself lives solely in the CLI config.toml per the cardinal rule; never stored here.",
  "expiry": $( [[ -n "$EXPIRY_RAW" ]] && printf '"%s"' "$EXPIRY_RAW" || printf 'null' ),
  "expiry_epoch": $epoch_field,
  "checked_at": "$(date -Iseconds)",
  "checked_epoch": $NOW_EPOCH,
  "source": "config.toml",
  "refreshed": $REFRESHED
}
EOF
  chmod 600 "$tmp"
  mv -f "$tmp" "$TOKEN_STATE"
  chmod 600 "$TOKEN_STATE"
}
write_token_state
log "wrote $TOKEN_STATE (0600, expiry only)"

# --- 5. connectivity gate: doctor must be green ---
DOCTOR_JSON="$("$CLI" doctor --json --no-input 2>/dev/null || true)"
API_STATUS="$(printf '%s' "$DOCTOR_JSON" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(d.get("api",""))
except Exception:
    print("")' 2>/dev/null)"
CRED_STATUS="$(printf '%s' "$DOCTOR_JSON" | python3 -c 'import sys,json
try:
    d=json.load(sys.stdin)
    print(d.get("credentials",""))
except Exception:
    print("")' 2>/dev/null)"

log "doctor: api=$API_STATUS credentials=$CRED_STATUS"

if [[ "$API_STATUS" == "reachable" && "$CRED_STATUS" == "valid" ]]; then
  log "connectivity gate GREEN"
  exit 0
fi

# Gate not green. If we got here via the documented no-creds/near-expiry warn path, the operator
# explicitly accepts running on the stored token — but a red doctor means we genuinely cannot reach
# the API or the token is dead. Fail-closed so the pipeline preserves last-good instead of writing junk.
die "connectivity gate RED (api=$API_STATUS credentials=$CRED_STATUS) — refusing to proceed"
