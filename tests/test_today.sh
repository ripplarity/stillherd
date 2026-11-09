#!/usr/bin/env bash
# stillherd today: runs with empty data and with a fixture log; the day attestation verifies (pinned keyid).
# Also checks the CLI installed by install.sh into a temp dir. Usage: bash tests/test_today.sh
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok() { pass=$((pass+1)); echo "ok   $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; }
export STILLHERD_COORD="$T/coord" STILLHERD_HOME="$T/home" SILENT_KEY="$T/k.pem" STILLHERD_EGRESS_DIR="$T/eg"
mkdir -p "$T/coord" "$T/eg"
export STILLHERD_SILENT_LOG="$T/none.jsonl"
DAY=2026-01-02
# The pinned verifier is byte-identical to the spec copy and does not yet list the day predicateType, so it exits 5
# (signature and subject digests valid, predicate not registered). Accept 0 or 5 with VALID-SIGNATURE.
vday() { o="$(bash "$HERE/silent/verify.sh" "$1" "$(kid "$1")" 2>&1)"; r=$?; [ $r -eq 0 ] && printf '%s' "$o" | grep -q VALID-SIGNATURE; }
kid() { "$PY" -I -c 'import json,sys;print(json.load(open(sys.argv[1]))["signatures"][0]["keyid"])' "$1" | tr -d ''; }
out="$(bash "$HERE/stillherd" today $DAY 2>&1)"; rc=$?
[ $rc -eq 0 ] && [ "$(printf '%s\n' "$out" | grep -c 'none recorded')" -ge 4 ] && ok "empty data: none recorded, no crash" || bad "empty: rc=$rc $out"
F="$STILLHERD_HOME/days/$DAY.dsse.json"
vday "$F" && ok "empty-day attestation verifies" || bad "empty attestation"
TS=1767312000  # 2026-01-02T00:00:00Z
printf '{"action":"blocked","bytes":0,"host":"example.invalid","method":"CONNECT","path":"","ts":%s}\n{"action":"allowed","bytes":42,"host":"127.0.0.1","method":"POST","path":"/x","ts":%s}\n' $TS $TS > "$T/silent.jsonl"
printf '{"event":"start","ts":%s,"id":"a","project":"demo","task":"build","resource":"cpu","pid":123}\n' $TS > "$T/coord/activity_ledger.jsonl"
printf '{"id":"c1","ts":"%sT01:00:00+00:00","files":["a.py"]}\n' $DAY > "$T/coord/claims.jsonl"
printf '{"id":"c1","verified_ts":"%sT02:00:00+00:00","verdict":"passed"}\n' $DAY > "$T/coord/claims_verified.jsonl"
export STILLHERD_SILENT_LOG="$T/silent.jsonl"
out="$(bash "$HERE/stillherd" today $DAY 2>&1)"
for w in "demo | build" "example.invalid: 1 requests, 0 bytes" "127.0.0.1: 1 requests, 42 bytes" "recorded: 1" "verified passed: 1" "a.py"; do
  printf '%s\n' "$out" | grep -qF "$w" && ok "fixture shows: $w" || bad "missing: $w"
done
vday "$F" && ok "fixture attestation verifies" || bad "fixture attestation"
echo x >> "$STILLHERD_HOME/days/$DAY.silent.log.jsonl"
vday "$F" && bad "tampered subject accepted" || ok "tampered subject rejected"
# installed copy
bash "$HERE/install.sh" "$T/proj" >/dev/null 2>&1 && out="$(bash "$T/proj/.claude/bin/stillherd" today $DAY 2>&1)"
printf '%s\n' "$out" | grep -q "stillherd today $DAY" && ok "installed CLI prints today screen" || bad "installed: $out"
echo "$pass passed, $fail failed"; [ $fail -eq 0 ]
