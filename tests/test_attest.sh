#!/usr/bin/env bash
# Attestation tests: legacy verify, new DSSE create+verify with a throwaway key, wrong-keyid rejection,
# verifier byte-identity with the spec copy (when ../attest-spec exists). Usage: bash tests/test_attest.sh
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
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
echo "$pass passed, $fail failed"; [ $fail -eq 0 ]
