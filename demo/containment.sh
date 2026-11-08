#!/usr/bin/env bash
# Agent Containment Test: a scripted "agent" runs under silent.sh; its outbound call is refused and logged.
# Usage: bash demo/containment.sh [outdir]   (needs mitmdump; keyid pin via SILENT_EXPECT_KEYID, default maintainer key)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
OUT="${1:-$(mktemp -d)}"; mkdir -p "$OUT"
LP="${CONTAIN_LOCAL_PORT:-8765}"
SRV="$(mktemp -d)"; echo hello-local > "$SRV/index.html"
"$PY" -m http.server "$LP" --bind 127.0.0.1 --directory "$SRV" >/dev/null 2>&1 &
SP=$!; trap 'kill $SP 2>/dev/null; rm -rf "$SRV"' EXIT
sleep 1
AGENT="$SRV/agent.sh"
cat > "$AGENT" <<EOA
echo "agent: allowed local action"
curl -s -o /dev/null -w "local  http://127.0.0.1:$LP -> %{http_code}\n" http://127.0.0.1:$LP/
echo "agent: outbound to non-allowlisted host"
curl -s -o /dev/null -w "remote https://example.com -> %{http_code}\n" https://example.com/ || true
EOA
echo "== run under silent.sh (allowlist: 127.0.0.1, localhost, ::1)"
SILENT_OUT="$OUT" SILENT_LOGNAME=containment.silent.log.jsonl SILENT_ATTNAME=containment.dsse.json bash "$ROOT/silent/silent.sh" bash "$AGENT"
echo "== external attempts recorded in the log"
"$PY" -I - "$OUT/containment.silent.log.jsonl" <<'EP'
import json,sys,datetime
for l in open(sys.argv[1],encoding="utf-8"):
    r=json.loads(l)
    if r.get("action")=="blocked":
        print("BLOCKED %s %s at %s"%(r["host"],r.get("path",""),datetime.datetime.fromtimestamp(r["ts"],datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))
EP
echo "== verify (pinned keyid)"
bash "$ROOT/silent/verify.sh" "$OUT/containment.dsse.json"
echo "== tamper: flip one byte of a copy of the log"
T="$(mktemp -d)"; cp "$OUT/containment.dsse.json" "$OUT/containment.silent.log.jsonl" "$T/"
"$PY" -I - "$T/containment.silent.log.jsonl" <<'EP'
import sys
b=bytearray(open(sys.argv[1],"rb").read()); b[10]^=1; open(sys.argv[1],"wb").write(b)
EP
if bash "$ROOT/silent/verify.sh" "$T/containment.dsse.json" 2>&1; then echo "UNEXPECTED: tampered copy verified"; else echo "FAIL (tampered copy rejected)"; fi
rm -rf "$T"
