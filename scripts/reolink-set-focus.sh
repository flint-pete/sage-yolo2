#!/usr/bin/env bash
#
# reolink-set-focus.sh — set a Reolink camera's focus to a specific numeric value.
#
# Reolink splits auth by operation: *reads* (GetZoomFocus) work with query-param
# auth, but the *actuating* SetAutoFocus/StartZoomFocus writes need a real session
# token AND an ADMIN account (a guest account gets rspCode -26 "ability error").
# This script does the full flow: Login -> token -> disable autofocus -> set focus,
# validating the focus value against the camera's advertised range first
# (fail-fast on bad input).
#
# By DEFAULT it disables autofocus first so the focus value LOCKS and stays put
# (autofocus, if left on, can re-drive the lens on scene/light changes and undo
# your setting). Pass --keep-autofocus to skip that step and just nudge focus
# once, leaving autofocus enabled.
#
# Usage:
#   ./reolink-set-focus.sh [--keep-autofocus] <camera-url> <username> <password> <focus-value>
#
# Example (H00F hummingcam, RLC-811A — requires the ADMIN account):
#   ./reolink-set-focus.sh http://10.107.0.221:10000 admin '<ADMIN_PASSWORD>' 3065
#
# Notes:
#   - <camera-url> is the scheme+host+port only (no path), e.g.
#     http://10.107.0.221:10000 — the script appends /cgi-bin/api.cgi.
#   - Quote the password in single quotes if it contains ! & ? etc.
#   - Focus range is camera-specific; the script reads it live and rejects
#     out-of-range values before touching the lens.
#   - Focus control is ADMIN-only; a guest account yields -26 "ability error".
#
# Exit codes:
#   0  focus set successfully
#   1  bad usage / arguments
#   2  camera unreachable or malformed response
#   3  login failed (bad credentials / no token)
#   4  focus value out of range
#   5  camera rejected the focus command
#   6  camera rejected the disable-autofocus command
#
set -euo pipefail

# ── dependencies ────────────────────────────────────────────────────
for dep in curl python3; do
    if ! command -v "$dep" >/dev/null 2>&1; then
        echo "ERROR: required command '$dep' not found in PATH" >&2
        exit 1
    fi
done

# ── arguments (fail fast) ───────────────────────────────────────────
DISABLE_AF=1   # default: lock focus by disabling autofocus first
if [ "${1:-}" = "--keep-autofocus" ]; then
    DISABLE_AF=0
    shift
fi

if [ "$#" -ne 4 ]; then
    echo "Usage: $0 [--keep-autofocus] <camera-url> <username> <password> <focus-value>" >&2
    echo "  e.g. $0 http://10.107.0.221:10000 admin '<ADMIN_PASSWORD>' 3065" >&2
    exit 1
fi

CAM_URL="${1%/}"          # strip any trailing slash
USER_NAME="$2"
PASSWORD="$3"
FOCUS="$4"

# camera-url must be scheme://host[:port] with no path
if ! printf '%s' "$CAM_URL" | grep -qE '^https?://[^/]+$'; then
    echo "ERROR: <camera-url> must be scheme://host[:port] with no path" >&2
    echo "       got: '$CAM_URL'  (e.g. http://10.107.0.221:10000)" >&2
    exit 1
fi

# focus value must be a non-negative integer
if ! printf '%s' "$FOCUS" | grep -qE '^[0-9]+$'; then
    echo "ERROR: <focus-value> must be a non-negative integer, got: '$FOCUS'" >&2
    exit 1
fi

API="${CAM_URL}/cgi-bin/api.cgi"

# Encode ONLY the characters that would break URL query parsing, and leave the
# rest literal. Reolink's query-param auth compares the password value as it
# arrives; percent-encoding password-legal punctuation like '!' makes the camera
# see the literal "%21" and reject it with rspCode -7 "login failed". So we keep
# a wide safe set (the manual working command used a raw '!'), and only escape
# the truly URL-breaking chars: space, & # ? + % and control chars.
urlencode_query() {
    # safe= keeps these literal; quote() still escapes space, &, #, ?, +, %, etc.
    python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1], safe="!$*(),/:;=@~'"'"'-._"))' "$1"
}
USER_ENC="$(urlencode_query "$USER_NAME")"
PASS_ENC="$(urlencode_query "$PASSWORD")"

# token can contain URL-breaking chars too; encode it the same conservative way.
urlencode() { urlencode_query "$1"; }

# ── step 0: read the current focus + valid range (query-param auth) ──
echo ">> Reading current focus + range from $CAM_URL ..."
RANGE_JSON="$(curl -s --max-time 15 \
    "${API}?cmd=GetZoomFocus&user=${USER_ENC}&password=${PASS_ENC}" \
    -H "Content-Type: application/json" \
    -d '[{"cmd":"GetZoomFocus","action":1,"param":{"channel":0}}]' || true)"

if [ -z "$RANGE_JSON" ]; then
    echo "ERROR: no response from camera at $CAM_URL (unreachable/timeout)" >&2
    exit 2
fi

# Parse min/max/current with python (robust to firmware field layout).
read -r FMIN FMAX FCUR <<EOF
$(printf '%s' "$RANGE_JSON" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)[0]
except Exception as e:
    sys.stderr.write("ERROR: could not parse GetZoomFocus response: %s\n" % e); sys.exit(2)
if d.get("code") != 0:
    sys.stderr.write("ERROR: GetZoomFocus returned error: %s\n" % json.dumps(d.get("error", d))); sys.exit(2)
try:
    rng = d["range"]["ZoomFocus"]["focus"]["pos"]
    cur = d["value"]["ZoomFocus"]["focus"]["pos"]
    print(rng["min"], rng["max"], cur)
except Exception as e:
    sys.stderr.write("ERROR: focus range not present in response: %s\n" % e); sys.exit(2)
')
EOF

if [ -z "${FMAX:-}" ]; then
    echo "ERROR: failed to read focus range (see message above)" >&2
    echo "Raw response: $RANGE_JSON" >&2
    exit 2
fi

echo "   current focus=$FCUR   valid range=[$FMIN..$FMAX]   requested=$FOCUS"

# ── validate requested value against the live range (fail fast) ─────
if [ "$FOCUS" -lt "$FMIN" ] || [ "$FOCUS" -gt "$FMAX" ]; then
    echo "ERROR: focus value $FOCUS is out of range [$FMIN..$FMAX]" >&2
    exit 4
fi

# ── step 1: login -> session token ──────────────────────────────────
echo ">> Logging in to obtain a session token ..."
LOGIN_JSON="$(curl -s --max-time 15 "${API}?cmd=Login" \
    -H "Content-Type: application/json" \
    -d "[{\"cmd\":\"Login\",\"param\":{\"User\":{\"userName\":\"${USER_NAME}\",\"password\":\"${PASSWORD}\"}}}]" || true)"

if [ -z "$LOGIN_JSON" ]; then
    echo "ERROR: no response to Login from $CAM_URL" >&2
    exit 2
fi

TOKEN="$(printf '%s' "$LOGIN_JSON" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)[0]
except Exception as e:
    sys.stderr.write("ERROR: could not parse Login response: %s\n" % e); sys.exit(3)
if d.get("code") != 0:
    sys.stderr.write("ERROR: Login failed: %s\n" % json.dumps(d.get("error", d))); sys.exit(3)
try:
    print(d["value"]["Token"]["name"])
except Exception as e:
    sys.stderr.write("ERROR: no token in Login response: %s\n" % e); sys.exit(3)
' )" || {
    echo "Raw Login response: $LOGIN_JSON" >&2
    exit 3
}

if [ -z "$TOKEN" ]; then
    echo "ERROR: empty token from Login (bad credentials?)" >&2
    echo "Raw Login response: $LOGIN_JSON" >&2
    exit 3
fi
echo "   token acquired: ${TOKEN:0:8}..."

TOKEN_ENC="$(urlencode "$TOKEN")"

# ── step 2: disable autofocus (switch to manual) so the focus LOCKS ──
# Skipped with --keep-autofocus. Without this, autofocus can re-drive the lens
# after we set it and undo the value.
if [ "$DISABLE_AF" -eq 1 ]; then
    echo ">> Disabling autofocus (manual focus) so the value will hold ..."
    AF_JSON="$(curl -s --max-time 15 "${API}?cmd=SetAutoFocus&token=${TOKEN_ENC}" \
        -H "Content-Type: application/json" \
        -d '[{"cmd":"SetAutoFocus","action":0,"param":{"AutoFocus":{"channel":0,"disable":1}}}]' || true)"

    if [ -z "$AF_JSON" ]; then
        echo "ERROR: no response to SetAutoFocus" >&2
        exit 2
    fi

    printf '%s' "$AF_JSON" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)[0]
except Exception as e:
    sys.stderr.write("ERROR: could not parse SetAutoFocus response: %s\n" % e); sys.exit(6)
if d.get("code") == 0:
    sys.exit(0)
sys.stderr.write("ERROR: camera rejected SetAutoFocus (autofocus disable): %s\n" % json.dumps(d.get("error", d)))
sys.exit(6)
' || {
        echo "Raw response: $AF_JSON" >&2
        echo "HINT: focus/autofocus control is ADMIN-only; a guest account returns -26." >&2
        exit 6
    }
    echo "   autofocus disabled (manual focus mode)"
else
    echo ">> --keep-autofocus: leaving autofocus ENABLED (focus may drift back)"
fi

# ── step 3: set focus using the token ───────────────────────────────
echo ">> Setting focus to $FOCUS ..."
SET_JSON="$(curl -s --max-time 15 "${API}?cmd=StartZoomFocus&token=${TOKEN_ENC}" \
    -H "Content-Type: application/json" \
    -d "[{\"cmd\":\"StartZoomFocus\",\"action\":0,\"param\":{\"ZoomFocus\":{\"channel\":0,\"op\":\"FocusPos\",\"pos\":${FOCUS}}}}]" || true)"

if [ -z "$SET_JSON" ]; then
    echo "ERROR: no response to StartZoomFocus" >&2
    exit 2
fi

# Check the camera accepted it (code 0).
printf '%s' "$SET_JSON" | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)[0]
except Exception as e:
    sys.stderr.write("ERROR: could not parse StartZoomFocus response: %s\n" % e); sys.exit(5)
if d.get("code") == 0:
    sys.exit(0)
sys.stderr.write("ERROR: camera rejected StartZoomFocus: %s\n" % json.dumps(d.get("error", d)))
sys.exit(5)
' || {
    echo "Raw response: $SET_JSON" >&2
    exit 5
}

if [ "$DISABLE_AF" -eq 1 ]; then
    echo "OK: autofocus disabled and focus set to pos=$FOCUS. The lens moves"
    echo "    asynchronously and may settle a few counts off the commanded value"
    echo "    (lens back-lash) but will then HOLD. Re-run GetZoomFocus to confirm."
else
    echo "OK: focus command accepted (pos=$FOCUS), autofocus left ENABLED so the"
    echo "    value may drift back. Use without --keep-autofocus to lock it."
fi
exit 0
