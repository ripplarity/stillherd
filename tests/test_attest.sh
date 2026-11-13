#!/usr/bin/env bash
# Attestation tests: legacy verify, new DSSE create+verify with a throwaway key, wrong-keyid rejection,
# verifier byte-identity with the spec copy (when ../attest-spec exists). Usage: bash tests/test_attest.sh
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0; skip=0
ok() { pass=$((pass+1)); echo "ok   $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; }
out="$(bash "$HERE/silent/verify.sh" "$HERE/silent/attestation.json" 2>&1)"
case "$out" in *"legacy format"*) ok "legacy attestation verifies" ;; *) bad "legacy: $out" ;; esac
case "$out" in *"mode: silent (legacy, unlabelled)"*) ok "legacy attestation prints the legacy mode line" ;; *) bad "legacy mode line: $out" ;; esac
bash "$HERE/silent/verify.sh" "$HERE/silent/attestation.json" --require-mode silent >/dev/null 2>&1 && ok "legacy satisfies --require-mode silent" || bad "legacy --require-mode silent"
bash "$HERE/silent/verify.sh" "$HERE/silent/attestation.json" --require-mode observe >/dev/null 2>&1 && bad "legacy accepted for --require-mode observe" || ok "legacy rejected for --require-mode observe"
printf '{"host":"example.invalid","path":"","ts":1.0,"action":"blocked","bytes":0}\n' > "$T/silent.log.jsonl"
SILENT_KEY="$T/k.pem" "$PY" "$HERE/silent/attest.py" "$T/silent.log.jsonl" "$T/a.dsse.json" 2026-01-01T00:00:00Z 2026-01-01T00:00:05Z 0 127.0.0.1 -- true >/dev/null || bad "write"
KID="$("$PY" -I -c 'import json,base64,sys,hashlib;d=json.load(open(sys.argv[1]));print(d["signatures"][0]["keyid"])' "$T/a.dsse.json")"
out="$(bash "$HERE/silent/verify.sh" "$T/a.dsse.json" "$KID" 2>&1)"
case "$out" in *VALID-SIGNATURE*OK) ok "new attestation verifies with pinned keyid" ;; *) bad "new: $out" ;; esac
case "$out" in *"mode: silent"*) ok "silent attestation prints mode: silent" ;; *) bad "mode line: $out" ;; esac
bash "$HERE/silent/verify.sh" "$T/a.dsse.json" "$KID" --require-mode silent >/dev/null 2>&1 && ok "--require-mode silent accepts a silent attestation" || bad "--require-mode silent"
bash "$HERE/silent/verify.sh" "$T/a.dsse.json" "$KID" --require-mode observe >/dev/null 2>&1 && bad "silent accepted for --require-mode observe" || ok "--require-mode observe rejects a silent attestation"
# F2: other predicate types and unlabelled/short silent payloads must not pass as silent
SILENT_KEY="$T/k.pem" "$PY" -c 'import json,base64,sys
sys.path.insert(0,sys.argv[2]); from dsse import make_envelope, load_key
d=json.load(open(sys.argv[1])); st=json.loads(base64.b64decode(d["payload"]))
k=load_key()
def out(name, st): json.dump(make_envelope(st,k),open(sys.argv[3]+"/"+name,"w"))
day=json.loads(json.dumps(st)); day["predicateType"]=day["predicateType"].replace("silent/v1","day/v1")
for f in ("mode","allowlist","external_attempts","external_bytes_sent","blocked_request_bytes"): day["predicate"].pop(f,None)
out("day.dsse.json",day)
unl=json.loads(json.dumps(st)); unl["predicate"].pop("mode",None); out("unl_ok.dsse.json",unl)
bad=json.loads(json.dumps(unl)); bad["predicate"].pop("allowlist",None); out("unl_bad.dsse.json",bad)
ty=json.loads(json.dumps(unl)); ty["predicate"]["external_bytes_sent"]="0"; out("unl_type.dsse.json",ty)' "$T/a.dsse.json" "$HERE/silent" "$T" 2>/dev/null
if [ -f "$T/day.dsse.json" ] && [ -f "$T/unl_type.dsse.json" ]; then
  out="$(bash "$HERE/silent/verify.sh" "$T/day.dsse.json" "$KID" --require-mode silent 2>&1)"; rc=$?
  [ $rc -ne 0 ] && ! printf '%s\n' "$out" | grep -q '^OK' && ok "F2 day-type attestation fails --require-mode silent" || bad "F2 day type rc=$rc $out"
  out="$(bash "$HERE/silent/verify.sh" "$T/day.dsse.json" "$KID" 2>&1)"
  case "$out" in *"mode: n/a ("*"day/v1)"*) ok "F2 day type prints mode: n/a without the flag" ;; *) bad "F2 n/a line: $out" ;; esac
  bash "$HERE/silent/verify.sh" "$T/unl_ok.dsse.json" "$KID" --require-mode silent >/dev/null 2>&1 && ok "F2 unlabelled silent payload with all four fields passes as legacy silent" || bad "F2 unlabelled ok"
  bash "$HERE/silent/verify.sh" "$T/unl_bad.dsse.json" "$KID" --require-mode silent >/dev/null 2>&1 && bad "F2 unlabelled without allowlist accepted" || ok "F2 unlabelled payload missing a silent field fails"
  bash "$HERE/silent/verify.sh" "$T/unl_type.dsse.json" "$KID" --require-mode silent >/dev/null 2>&1 && bad "F2 mistyped silent field accepted" || ok "F2 mistyped silent field fails"
else
  skip=$((skip+1)); echo "skip F2: fixtures could not be built (cryptography missing for this python?); NOT counted as passed"
fi
WRONG="$(printf '%064d' 1)"
bash "$HERE/silent/verify.sh" "$T/a.dsse.json" "$WRONG" >/dev/null 2>&1 && bad "wrong keyid accepted" || ok "wrong keyid rejected"
echo tamper >> "$T/silent.log.jsonl"
bash "$HERE/silent/verify.sh" "$T/a.dsse.json" "$KID" >/dev/null 2>&1 && bad "tampered log accepted" || ok "tampered subject rejected"
# argv paths are reduced to basenames in the signed payload
: > "$T/empty.jsonl"
SILENT_KEY="$T/k.pem" "$PY" "$HERE/silent/attest.py" "$T/empty.jsonl" "$T/p.dsse.json" 2026-01-01T00:00:00Z 2026-01-01T00:00:05Z 0 127.0.0.1 -- bash "C:/Users/someone/AppData/Local/Temp/tmp.AbC/agent.sh" /tmp/xyz.123/a.sh --flag https://example.com/x >/dev/null
pl="$("$PY" -I -c 'import json,base64,sys;print(json.loads(base64.b64decode(json.load(open(sys.argv[1]))["payload"]))["predicate"]["cmd"])' "$T/p.dsse.json")"
case "$pl" in *Users*|*Temp*|*tmp.*|*xyz*) bad "path leaked in cmd: $pl" ;; *"'agent.sh', 'a.sh', '--flag', 'https://example.com/x'"*) ok "argv paths reduced to basename" ;; *) bad "cmd unexpected: $pl" ;; esac
SPEC="$HERE/../attest-spec/attest_verify.py"
if [ -f "$SPEC" ]; then
  # the shipped verifier must be byte-identical to the spec REVISION this repo tracks (pinned below),
  # not to whatever the spec repo's HEAD is today; a moved spec HEAD is reported, not failed
  SHIPPED_SPEC_SHA="51da005156826521b9a55ecf2ad3b5eb2aeb8cf85fb9ba70f231bfce55403687"   # attest-spec revision 1.4
  MINE="$(sha256sum < "$HERE/silent/attest_verify.py" | cut -d' ' -f1)"
  [ "$MINE" = "$SHIPPED_SPEC_SHA" ] && ok "verifier byte-identical to the pinned spec revision" || bad "verifier differs from the pinned spec revision"
  HEAD_SHA="$(git -C "$HERE/../attest-spec" show HEAD:attest_verify.py 2>/dev/null | sha256sum | cut -d' ' -f1)"
  [ -n "$HEAD_SHA" ] && [ "$HEAD_SHA" != "$SHIPPED_SPEC_SHA" ] && echo "info: attest-spec HEAD has moved past the pinned revision (${HEAD_SHA:0:16}); review before adopting"
else
  echo "skip: spec repo not present beside this checkout (verifier identity check not run)"
fi
echo "$pass passed, $fail failed, $skip skipped"; [ $fail -eq 0 ]
