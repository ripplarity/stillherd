#!/usr/bin/env bash
# Opt-out rerun: same harness as run_claude.sh, one run, with DISABLE_TELEMETRY=1 and CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 exported.
# Host run (existing login reused in place; credentials are never copied). Writes results/claude-code.run{1,2}.jsonl.
# Request/response bodies are never stored: log_requests.py keeps host, path, method, byte count, content type only.
set -u
export DISABLE_TELEMETRY=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$PATH"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
command -v mitmdump >/dev/null 2>&1 || { echo "mitmdump unavailable" >&2; exit 1; }
VERSION="$(claude --version 2>/dev/null | awk '{print $1}')"
mkdir -p "$HERE/results/sessions"
PORT=8080
CA="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
for n in 1; do
  OUT="$HERE/results/claude-code_${VERSION}_optout_run$n.jsonl"; : > "$OUT"
  export EGRESS_TOOL=claude-code EGRESS_VERSION="$VERSION" EGRESS_OUT="$OUT"
  mitmdump --listen-host 127.0.0.1 --listen-port $PORT -q -s "$HERE/log_requests.py" &
  MPID=$!
  for i in $(seq 1 30); do [ -f "$CA" ] && (echo > /dev/tcp/127.0.0.1/$PORT) 2>/dev/null && break; sleep 1; done
  W="$(mktemp -d)"; ( cd "$W" && printf 'print("hello")\n' > hello.py )
  : > "$HERE/results/sessions/claude-code_optout_run$n.turns.txt"
  first=1; i=0
  while IFS= read -r line; do
    [ "$line" = "/exit" ] && break
    i=$((i+1)); A=""; [ $first -eq 0 ] && A="--continue"; first=0
    ( cd "$W" && HTTP_PROXY="http://127.0.0.1:$PORT" HTTPS_PROXY="http://127.0.0.1:$PORT" http_proxy="http://127.0.0.1:$PORT" \
        https_proxy="http://127.0.0.1:$PORT" NO_PROXY="" no_proxy="" NODE_EXTRA_CA_CERTS="$CA" SSL_CERT_FILE="$CA" \
        timeout 300 claude -p $A --setting-sources project --strict-mcp-config --disable-slash-commands \
          --allowedTools Read Edit Write Glob Grep "Bash(python -m pytest:*)" "Bash(ls:*)" -- "$line" > "$W/turn$i.out" 2>&1 < /dev/null )
    rc=$?
    echo "TURN $i rc=$rc out_bytes=$(wc -c < "$W/turn$i.out")" >> "$HERE/results/sessions/claude-code_optout_run$n.turns.txt"
  done < "$HERE/session.txt"
  sleep 3; kill $MPID 2>/dev/null; sleep 2
  (python3 "$HERE/redact.py" "$OUT" 2>/dev/null || python "$HERE/redact.py" "$OUT")  # strip query strings and ids before anything else reads the log
  echo "run$n rows=$(wc -l < "$OUT")"
  rm -rf "$W"
done
