#!/usr/bin/env bash
# silent.sh <cmd...>: run a command with only a local allowlist; block + log everything else; sign an attestation.
# Env: SILENT_ALLOW_EXTRA="ip1,host2" extra allowed hosts (e.g. the host Ollama IP), SILENT_OUT dir (default $PWD),
#      SILENT_PORT proxy port (default 8090), SILENT_KEY signing key path.
#      SILENT_MODE=observe (set by watch.sh): log and forward everything, block nothing; print a host table; names default to
#      watch.requests.jsonl / watch.attestation.dsse.json. If SILENT_PORT is busy the next free port is used.
set -u
MODE="${SILENT_MODE:-silent}"
[ $# -ge 1 ] || { echo "usage: $([ "$MODE" = observe ] && echo watch || echo silent).sh <cmd...>" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
command -v mitmdump >/dev/null 2>&1 || { echo "mitmdump not found (uv tool install mitmproxy)" >&2; exit 1; }
command -v "$1" >/dev/null 2>&1 || [ -x "$1" ] || { echo "command not found: $1" >&2; exit 127; }
OUT="${SILENT_OUT:-$PWD}"; PORT="${SILENT_PORT:-8090}"
for _ in $(seq 1 50); do  # port busy: take the next free one (checked by binding, then released)
  "$PY" -I -c 'import socket,sys;s=socket.socket();s.bind(("127.0.0.1",int(sys.argv[1])))' "$PORT" 2>/dev/null && break
  [ -n "${SILENT_PORT_NOTE:-}" ] || echo "port $PORT busy, trying the next one" >&2; SILENT_PORT_NOTE=1; PORT=$((PORT+1))
done
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
if [ "$MODE" = observe ]; then
  LOG="$OUT/${SILENT_LOGNAME:-watch.requests.jsonl}"; ATT="$OUT/${SILENT_ATTNAME:-watch.attestation.dsse.json}"
  ALLOW=""; ADDON="$HERE/../egress/log_requests.py"; ADDON_ENV=("EGRESS_OUT=$(native "$LOG")" "EGRESS_TOOL=watch")
else
  LOG="$OUT/${SILENT_LOGNAME:-silent.log.jsonl}"; ATT="$OUT/${SILENT_ATTNAME:-attestation.dsse.json}"
  ALLOW="127.0.0.1,localhost,::1${SILENT_ALLOW_EXTRA:+,$SILENT_ALLOW_EXTRA}"
  ADDON="$HERE/block_addon.py"; ADDON_ENV=("SILENT_LOG=$(native "$LOG")" "SILENT_ALLOW=$ALLOW")
fi
: > "$LOG"
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
env "${ADDON_ENV[@]}" \
  mitmdump --listen-host "${SILENT_LISTEN:-127.0.0.1}" --listen-port "$PORT" -q -s "$(native "$ADDON")" &
MP=$!
stop_proxy() {
  kill "$MP" 2>/dev/null
  command -v taskkill >/dev/null 2>&1 && taskkill //F //T //PID "$(cat /proc/$MP/winpid 2>/dev/null)" >/dev/null 2>&1
  true
}
trap stop_proxy EXIT
for i in $(seq 1 30); do (echo > /dev/tcp/127.0.0.1/$PORT) 2>/dev/null && [ -f "$CA" ] && break; sleep 1; done
export HTTP_PROXY="http://127.0.0.1:$PORT" HTTPS_PROXY="http://127.0.0.1:$PORT"
export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY" NO_PROXY="" no_proxy=""
export NODE_EXTRA_CA_CERTS="$(native "$CA")" SSL_CERT_FILE="$(native "$CA")" REQUESTS_CA_BUNDLE="$(native "$CA")" CURL_CA_BUNDLE="$(native "$CA")"
STARTED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
"$@"; RC=$?
ENDED="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
sleep 1
stop_proxy
if [ "$MODE" = observe ]; then
  echo; "$PY" "$HERE/../egress/watch_summary.py" "$(native "$LOG")"; echo
  SILENT_MODE=observe "$PY" "$HERE/attest.py" "$(native "$LOG")" "$(native "$ATT")" "$STARTED" "$ENDED" "$RC" "$ALLOW" -- "$@" \
    || echo "watch: attestation not written (is the 'cryptography' package installed?); the log and table above are still valid" >&2
  echo "log: $LOG"
  exit $RC
fi
"$PY" "$HERE/attest.py" "$(native "$LOG")" "$(native "$ATT")" "$STARTED" "$ENDED" "$RC" "$ALLOW" -- "$@" || exit 1
exit $RC
