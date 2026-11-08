#!/usr/bin/env bash
# silent.sh <cmd...>: run a command with only a local allowlist; block + log everything else; sign an attestation.
# Env: SILENT_ALLOW_EXTRA="ip1,host2" extra allowed hosts (e.g. the host Ollama IP), SILENT_OUT dir (default $PWD),
#      SILENT_PORT proxy port (default 8090), SILENT_KEY signing key path.
set -u
[ $# -ge 1 ] || { echo "usage: silent.sh <cmd...>" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
command -v mitmdump >/dev/null 2>&1 || { echo "mitmdump not found (uv tool install mitmproxy)" >&2; exit 1; }
OUT="${SILENT_OUT:-$PWD}"; PORT="${SILENT_PORT:-8090}"
native() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
LOG="$OUT/${SILENT_LOGNAME:-silent.log.jsonl}"; ATT="$OUT/${SILENT_ATTNAME:-attestation.dsse.json}"
: > "$LOG"
ALLOW="127.0.0.1,localhost,::1${SILENT_ALLOW_EXTRA:+,$SILENT_ALLOW_EXTRA}"
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
SILENT_LOG="$(native "$LOG")" SILENT_ALLOW="$ALLOW" \
  mitmdump --listen-host "${SILENT_LISTEN:-127.0.0.1}" --listen-port "$PORT" -q -s "$(native "$HERE/block_addon.py")" &
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
"$PY" "$HERE/attest.py" "$(native "$LOG")" "$(native "$ATT")" "$STARTED" "$ENDED" "$RC" "$ALLOW" -- "$@" || exit 1
exit $RC
