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
printf '{"host":"example.invalid","path":"","ts":1.0,"action":"blocked","bytes":0}\n' > "$T/silent.log.jsonl"
SILENT_KEY="$T/k.pem" "$PY" "$HERE/silent/attest.py" "$T/silent.log.jsonl" "$T/a.dsse.json" 2026-01-01T00:00:00Z 2026-01-01T00:00:05Z 0 127.0.0.1 -- true >/dev/null || bad "write"
KID="$("$PY" -I -c 'import json,base64,sys,hashlib;d=json.load(open(sys.argv[1]));print(d["signatures"][0]["keyid"])' "$T/a.dsse.json")"
out="$(bash "$HERE/silent/verify.sh" "$T/a.dsse.json" "$KID" 2>&1)"
case "$out" in *VALID-SIGNATURE*OK) ok "new attestation verifies with pinned keyid" ;; *) bad "new: $out" ;; esac
WRONG="$(printf '%064d' 1)"
bash "$HERE/silent/verify.sh" "$T/a.dsse.json" "$WRONG" >/dev/null 2>&1 && bad "wrong keyid accepted" || ok "wrong keyid rejected"
echo tamper >> "$T/silent.log.jsonl"
bash "$HERE/silent/verify.sh" "$T/a.dsse.json" "$KID" >/dev/null 2>&1 && bad "tampered log accepted" || ok "tampered subject rejected"
SPEC="$HERE/../attest-spec/attest_verify.py"
if [ -f "$SPEC" ]; then
  # compare against the spec's COMMITTED verifier, not its working copy (which may be mid-edit)
  SPEC_SHA="$(git -C "$HERE/../attest-spec" show HEAD:attest_verify.py 2>/dev/null | sha256sum)"
  [ -n "$SPEC_SHA" ] || SPEC_SHA="$(sha256sum < "$SPEC")"
  [ "$SPEC_SHA" = "$(sha256sum < "$HERE/silent/attest_verify.py")" ] && ok "verifier byte-identical to spec (committed)" || bad "verifier differs from spec (committed)"
else
  echo "skip: spec repo not present beside this checkout (verifier identity check not run)"
fi
echo "$pass passed, $fail failed"; [ $fail -eq 0 ]
