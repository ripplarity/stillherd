#!/usr/bin/env bash
# stillherd watch: host table from a real run, empty-table message, exit-code pass-through, no body fields in the log,
# attestation verifies (mode observe), missing mitmdump / missing command / busy port messages.
# Needs network for (a); without it (a) is reported as skipped, not passed. Usage: bash tests/test_watch.sh
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0; skip=0
ok() { pass=$((pass+1)); echo "ok   $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; }
export SILENT_KEY="$T/k.pem" SILENT_OUT="$T"
freeport() { "$PY" -I -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])'; }
export PATH="$HOME/.local/bin:$PATH"
command -v mitmdump >/dev/null 2>&1 || { echo "skip: mitmdump not installed (uv tool install mitmproxy); nothing run"; exit 0; }

# (a) a real HTTPS request shows up as an external host row; the command's own exit code is passed through (0)
HAVE_NET=1; "$PY" -I -c 'import socket;socket.create_connection(("example.com",443),timeout=5)' 2>/dev/null || HAVE_NET=0
if [ $HAVE_NET -eq 1 ]; then
  SILENT_PORT="$(freeport)" out="$(bash "$HERE/stillherd" watch bash -c 'curl -s --ssl-no-revoke --cacert "$SSL_CERT_FILE" https://example.com -o /dev/null' 2>&1)"; rc=$?
  [ $rc -eq 0 ] && ok "(a) exit 0 passed through" || bad "(a) rc=$rc $out"
  printf '%s\n' "$out" | grep -E '^\| example\.com \| [1-9][0-9]* \|' >/dev/null && ok "(a) table has example.com with requests >= 1" || bad "(a) table: $out"
  printf '%s\n' "$out" | grep -E '^observed: 1 external host\(s\), [1-9][0-9]* request\(s\), [0-9]+ bytes out$' >/dev/null && ok "(a) observed: summary line" || bad "(a) observed line: $out"
  printf '%s' "$out" | grep -q "attempts=" && bad "(a) misleading attempts= line printed in observe mode"
  SILENT_PORT="$(freeport)" out2="$(bash "$HERE/stillherd" watch curl -s https://example.com 2>&1)"
  case "$out2" in *"TLS handshake(s) failed"*) printf '%s' "$out2" | grep -q 'hint: .*--cacert "$SSL_CERT_FILE".*--ssl-no-revoke' && ok "(a) TLS-failure hint printed with the fixes" || bad "(a) hint missing: $out2" ;; *) echo "info: this curl trusted the proxy CA (no TLS failure to hint on)" ;; esac
  cp "$T/watch.requests.jsonl" "$T/a.jsonl"; cp "$T/watch.attestation.dsse.json" "$T/a.dsse.json" 2>/dev/null
else
  skip=$((skip+1)); echo "skip (a): no network to example.com; a local HTTPS server would be loopback, which the table hides by design"
fi

# (b) no requests: empty-table message
SILENT_PORT="$(freeport)" out="$(bash "$HERE/stillherd" watch true 2>&1)"; rc=$?
[ $rc -eq 0 ] && printf '%s' "$out" | grep -q "no traffic reached the proxy: the command may ignore proxy settings" && ok "(b) empty table message names the proxy-bypass possibility" || bad "(b) rc=$rc $out"
cp "$T/watch.requests.jsonl" "$T/b.jsonl"; cp "$T/watch.attestation.dsse.json" "$T/b.dsse.json" 2>/dev/null

# (c) non-zero exit passed through, table still printed
SILENT_PORT="$(freeport)" out="$(bash "$HERE/stillherd" watch bash -c 'exit 7' 2>&1)"; rc=$?
[ $rc -eq 7 ] && printf '%s' "$out" | grep -q "no traffic reached the proxy" && ok "(c) exit 7 passed through, table printed" || bad "(c) rc=$rc $out"

# (d) the log has only metadata fields, never bodies
if [ -f "$T/a.jsonl" ]; then L="$T/a.jsonl"; else L="$T/b.jsonl"; fi
"$PY" -I -c 'import json,sys
ok={"ts","tool","version","host","path","method","bytes","content_type","canary","had_query"}
rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
bad=[k for r in rows for k in r if k not in ok or k in ("body","content","text","response","headers","raw")]
sys.exit(1 if bad else 0)' "$L" && ok "(d) log rows carry no body/header/response fields" || bad "(d) unexpected fields in $L"

# (e) the attestation verifies with its own pinned keyid and says mode observe; else the documented fallback applies
D="$T/b.dsse.json"; [ -f "$T/a.dsse.json" ] && D="$T/a.dsse.json"
if [ -f "$D" ]; then
  KID="$("$PY" -I -c 'import json,sys;print(json.load(open(sys.argv[1]))["signatures"][0]["keyid"])' "$D")"
  cp "$D" "$T/watch.attestation.dsse.json"; cp "$L" "$T/watch.requests.jsonl"
  out="$(bash "$HERE/silent/verify.sh" "$T/watch.attestation.dsse.json" "$KID" 2>&1)"
  case "$out" in *VALID-SIGNATURE*OK) ok "(e) attestation verifies" ;; *) bad "(e) verify: $out" ;; esac
  m="$("$PY" -I -c 'import json,base64,sys;p=json.loads(base64.b64decode(json.load(open(sys.argv[1]))["payload"]))["predicate"];print(p["mode"],p["enforcement"])' "$D")"
  [ "$m" = "observe none" ] && ok "(e) predicate says mode observe, enforcement none" || bad "(e) predicate mode: $m"
  # silent-claim fields are ABSENT in observe mode; renamed byte count present; proxy_saw_traffic recorded
  "$PY" -I -c 'import json,base64,sys;p=json.loads(base64.b64decode(json.load(open(sys.argv[1]))["payload"]))["predicate"]
bad=[k for k in ("allowlist","external_attempts","external_bytes_sent","blocked_request_bytes") if k in p]
sys.exit(1 if bad or "observed_bytes_out" not in p or "proxy_saw_traffic" not in p else 0)' "$D" && ok "(e) observe attestation omits allowlist/external_attempts/external_bytes_sent/blocked_request_bytes" || bad "(e) silent-claim fields present"
  saw="$("$PY" -I -c 'import json,base64,sys;print(json.loads(base64.b64decode(json.load(open(sys.argv[1]))["payload"]))["predicate"]["proxy_saw_traffic"])' "$T/b.dsse.json")"
  [ "$saw" = "False" ] && ok "(b) no-request run records proxy_saw_traffic false" || bad "(b) proxy_saw_traffic=$saw"
  # verify.sh prints the mode line; --require-mode
  out="$(bash "$HERE/silent/verify.sh" "$T/watch.attestation.dsse.json" "$KID" 2>&1)"
  case "$out" in *"mode: observe (nothing was blocked; this is a record, not a containment claim)"*) ok "(e) verify prints the observe mode line" ;; *) bad "(e) mode line: $out" ;; esac
  out="$(bash "$HERE/silent/verify.sh" "$T/watch.attestation.dsse.json" "$KID" --require-mode silent 2>&1)"; rc=$?
  [ $rc -ne 0 ] && printf '%s' "$out" | grep -q "require-mode silent not met" && ok "(e) --require-mode silent rejects an observe attestation" || bad "(e) require silent rc=$rc $out"
  bash "$HERE/silent/verify.sh" "$T/watch.attestation.dsse.json" "$KID" --require-mode observe >/dev/null 2>&1 && ok "(e) --require-mode observe accepts it" || bad "(e) require observe"
  # hand-edited relabel (mode silent, not re-signed) fails on the signature
  "$PY" -I -c 'import json,base64,sys
d=json.load(open(sys.argv[1])); st=json.loads(base64.b64decode(d["payload"])); st["predicate"]["mode"]="silent"
d["payload"]=base64.b64encode(json.dumps(st).encode()).decode(); json.dump(d,open(sys.argv[2],"w"))' "$T/watch.attestation.dsse.json" "$T/relabel.dsse.json"
  bash "$HERE/silent/verify.sh" "$T/relabel.dsse.json" "$KID" >/dev/null 2>&1 && bad "(e) relabelled attestation accepted" || ok "(e) hand-edited relabel (observe -> silent) fails the signature"
  # consistently SIGNED but inconsistent payloads fail the consistency check
  for BADMODE in silent observe; do
    SILENT_KEY="$T/k.pem" "$PY" -c 'import json,base64,sys
sys.path.insert(0,sys.argv[3]); from dsse import make_envelope, load_key
d=json.load(open(sys.argv[1])); st=json.loads(base64.b64decode(d["payload"])); p=st["predicate"]
if sys.argv[4]=="silent": p["mode"]="silent"            # mode silent + observed_external present
else: p["external_attempts"]=[]                         # mode observe + a silent-claim field present
json.dump(make_envelope(st,load_key()),open(sys.argv[2],"w"))' "$T/watch.attestation.dsse.json" "$T/inc_$BADMODE.dsse.json" "$HERE/silent" "$BADMODE"
    if [ ! -f "$T/inc_$BADMODE.dsse.json" ]; then skip=$((skip+1)); echo "skip (e) inconsistent payload ($BADMODE): fixture could not be built (cryptography missing for this python?); NOT counted as passed"; continue; fi
    cp "$T/inc_$BADMODE.dsse.json" "$T/chk.dsse.json"
    out="$(bash "$HERE/silent/verify.sh" "$T/chk.dsse.json" "$KID" 2>&1)"; rc=$?
    [ $rc -ne 0 ] && printf '%s' "$out" | grep -q "^FAIL" && ok "(e) signed but inconsistent payload ($BADMODE) fails the consistency check" || bad "(e) inconsistent $BADMODE rc=$rc $out"
  done
else
  echo "info (e): no attestation written (documented fallback: log and table only); cryptography package missing?"; bad "(e) no attestation file"
fi

# F1: argument parsing fails closed, before any verification output
if [ -f "$T/watch.attestation.dsse.json" ]; then
  A="$T/watch.attestation.dsse.json"
  chk() { # name expected_rc args...
    local n="$1" want="$2"; shift 2
    out="$(timeout 20 bash "$HERE/silent/verify.sh" "$@" 2>&1 </dev/null)"; rc=$?
    if [ $rc -eq "$want" ] && ! printf '%s\n' "$out" | grep -q '^OK'; then ok "F1 $n: rc $want, no OK line"; else bad "F1 $n: rc=$rc (want $want) $out"; fi
  }
  chk "equals form is honoured (observe file, silent required)" 8 "$A" "$KID" --require-mode=silent
  chk "empty value" 2 "$A" "$KID" --require-mode ""
  chk "empty value, equals form" 2 "$A" "$KID" --require-mode=
  chk "repeated flag" 2 "$A" "$KID" --require-mode silent --require-mode observe
  chk "trailing flag with no value (no hang)" 2 "$A" "$KID" --require-mode
  chk "unknown flag" 2 "$A" "$KID" --bogus
  chk "third positional" 2 "$A" "$KID" extra
  chk "flag before the file" 8 --require-mode silent "$A" "$KID"
  bash "$HERE/silent/verify.sh" "$A" "$KID" --require-mode=observe >/dev/null 2>&1 </dev/null && ok "F1 equals form accepts a matching mode" || bad "F1 equals form observe"
fi

# F3: no credentials in the log or the signed payload (query string, bash -c wrapper, bearer token)
SECRET='https://example.com/?api_key=SECRETTOKEN123'
if [ $HAVE_NET -eq 1 ]; then
  SILENT_PORT="$(freeport)" bash "$HERE/stillherd" watch bash -c "curl -s --ssl-no-revoke --cacert \"\$SSL_CERT_FILE\" \"$SECRET\" -H \"Authorization: Bearer sk-LIVE123\" -o /dev/null" >/dev/null 2>&1
else
  SILENT_PORT="$(freeport)" bash "$HERE/stillherd" watch bash -c "echo $SECRET Bearer sk-LIVE123 > /dev/null" >/dev/null 2>&1
  echo "info F3: no network, so only the command-line half is covered"
fi
"$PY" -I -c 'import json,base64,sys
d=json.load(open(sys.argv[1])); open(sys.argv[2],"w").write(base64.b64decode(d["payload"]).decode())' "$T/watch.attestation.dsse.json" "$T/payload.json"
if grep -q -e SECRETTOKEN123 -e sk-LIVE123 -e Bearer "$T/watch.requests.jsonl" "$T/watch.attestation.dsse.json" "$T/payload.json"; then bad "F3 credential text found in log or attestation"; else ok "F3 no query string, token or bearer text in the log or the signed payload"; fi
if [ $HAVE_NET -eq 1 ]; then
  grep -q '"had_query": true' "$T/watch.requests.jsonl" && grep -q '"path": "/"' "$T/watch.requests.jsonl" && ok "F3 path stored without query, had_query true" || bad "F3 path/had_query: $(cat "$T/watch.requests.jsonl")"
fi
"$PY" -I -c 'import json,sys,base64
p=json.loads(base64.b64decode(json.load(open(sys.argv[1]))["payload"]))["predicate"]
sys.exit(0 if p["cmd"]==["bash"] and p["argc"]==3 else 1)' "$T/watch.attestation.dsse.json" && ok "F3 cmd is the command name only, plus argc" || bad "F3 cmd/argc: $(head -c 600 "$T/payload.json")"

# N1: SILENT_MODE set but empty refuses (nothing signed)
out="$(SILENT_MODE= bash "$HERE/silent/silent.sh" true 2>&1)"; rc=$?
[ $rc -ne 0 ] && printf '%s' "$out" | grep -q "SILENT_MODE must be silent or observe" && ok "N1 empty SILENT_MODE refused by silent.sh" || bad "N1 empty mode rc=$rc $out"
: > "$T/e.jsonl"; rm -f "$T/e.dsse.json"
out="$(SILENT_MODE= SILENT_KEY="$T/k.pem" "$PY" "$HERE/silent/attest.py" "$T/e.jsonl" "$T/e.dsse.json" 2026-01-01T00:00:00Z 2026-01-01T00:00:05Z 0 "" -- true 2>&1)"; rc=$?
[ $rc -ne 0 ] && [ ! -f "$T/e.dsse.json" ] && printf '%s' "$out" | grep -q "nothing signed" && ok "N1 attest.py refuses an empty mode and signs nothing" || bad "N1 attest rc=$rc $out"

# failure messages
out="$(env PATH=/nonexistent HOME=/nonexistent SILENT_MODE=observe "$BASH" "$HERE/silent/silent.sh" true 2>&1)"; rc=$?
[ $rc -eq 1 ] && printf '%s' "$out" | grep -q "uv tool install mitmproxy" && ok "missing mitmdump: says how to install" || bad "mitmdump refusal rc=$rc $out"
out="$(bash "$HERE/stillherd" watch no-such-command-xyz 2>&1)"; rc=$?
[ $rc -eq 127 ] && printf '%s' "$out" | grep -q "command not found: no-such-command-xyz" && ok "unknown command: clear message, rc 127" || bad "unknown command rc=$rc $out"
BUSY="$(freeport)"
"$PY" -I -c 'import socket,sys,time;s=socket.socket();s.bind(("127.0.0.1",int(sys.argv[1])));s.listen(1);time.sleep(60)' "$BUSY" & HOLD=$!
sleep 1
out="$(SILENT_PORT="$BUSY" bash "$HERE/stillherd" watch true 2>&1)"; rc=$?
kill $HOLD 2>/dev/null
[ $rc -eq 0 ] && printf '%s' "$out" | grep -q "port $BUSY busy" && ok "busy port: next free port used" || bad "busy port rc=$rc $out"
bash "$HERE/stillherd" watch >/dev/null 2>&1; [ $? -eq 2 ] && ok "watch without a command: usage, rc 2" || bad "usage rc"
echo "$pass passed, $fail failed, $skip skipped"; [ $fail -eq 0 ]
