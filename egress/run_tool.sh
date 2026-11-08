#!/usr/bin/env bash
# Record every outbound request one AI coding tool (opencode) makes during a fixed session.
# Usage: bash run_tool.sh   (writes requests.jsonl next to this script)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
# re-exec under the lease wrapper unless already inside it
command -v mitmdump >/dev/null 2>&1 || uv tool install mitmproxy >/dev/null 2>&1 \
  || "$PY" -m pip install --user mitmproxy >/dev/null 2>&1
command -v mitmdump >/dev/null 2>&1 || { echo "mitmdump unavailable" >&2; exit 1; }
VERSION="$(npx --yes opencode-ai@latest --version 2>/dev/null | tail -1)"
export EGRESS_TOOL=opencode EGRESS_VERSION="$VERSION" EGRESS_OUT="$HERE/requests.jsonl"
: > "$EGRESS_OUT"
PORT=8080
mitmdump --listen-host 127.0.0.1 --listen-port $PORT -q -s "$HERE/log_requests.py" &
MPID=$!
trap 'kill $MPID 2>/dev/null' EXIT
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
for i in $(seq 1 30); do [ -f "$CA" ] && (echo > /dev/tcp/127.0.0.1/$PORT) 2>/dev/null && break; sleep 1; done
export HTTP_PROXY="http://127.0.0.1:$PORT" HTTPS_PROXY="http://127.0.0.1:$PORT"
export http_proxy="$HTTP_PROXY" https_proxy="$HTTPS_PROXY"
export NODE_EXTRA_CA_CERTS="$CA" SSL_CERT_FILE="$CA" NODE_USE_ENV_PROXY=1
export NO_PROXY="" no_proxy=""
export OPENCODE_CONFIG="$HERE/opencode.json"
# fixed fixture project
W="$(mktemp -d)"; cd "$W"
printf 'def add(a, b):\n    return a + b\n' > calc.py
printf 'from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n' > test_calc.py
first=1
while IFS= read -r line; do
  [ "$line" = "/exit" ] && break
  if [ $first -eq 1 ]; then A=""; first=0; else A="--continue"; fi
  timeout 150 npx --yes opencode-ai@latest run $A "$line" >> "$HERE/session.out" 2>&1
  echo "prompt rc=$?: $line" >&2
done < "$HERE/session.txt"
sleep 2
echo "rows: $(wc -l < "$EGRESS_OUT")"
