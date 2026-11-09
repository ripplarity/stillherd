#!/usr/bin/env bash
# Timeline/explain tests: golden output from committed fixture records, UNKNOWN when a record is removed,
# and a grep proving the new code never touches body/prompt/content/headers/authorization/token/key fields.
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
FX="$HERE/tests/fixtures/timeline"; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
pass=0; fail=0
ok() { pass=$((pass+1)); echo "ok   $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; }
cp -r "$FX/coord" "$FX/home" "$FX/egress" "$T/"
run() { STILLHERD_COORD="$T/coord" STILLHERD_HOME="$T/home" STILLHERD_SILENT_LOG="$T/home/silent.log.jsonl" STILLHERD_EGRESS_DIR="$T/egress" "$PY" -I "$HERE/bin/timeline.py" "$@"; }
run timeline fx/demo | tr -d '\r' > "$T/tl.txt"; run explain fx/demo | tr -d '\r' > "$T/ex.txt"
tr -d "" < "$FX/golden_timeline.txt" > "$T/g1.txt"; tr -d "" < "$FX/golden_explain.txt" > "$T/g2.txt"
diff -u "$T/g1.txt" "$T/tl.txt" >/dev/null && ok "timeline matches golden" || { bad "timeline differs from golden"; diff -u "$T/g1.txt" "$T/tl.txt" | head -20; }
diff -u "$T/g2.txt" "$T/ex.txt" >/dev/null && ok "explain matches golden" || { bad "explain differs from golden"; diff -u "$T/g2.txt" "$T/ex.txt" | head -20; }
run timeline --since 2027-01-15T07:00:00Z | tr -d '\r' | grep -q "job_key=fx/demo" && ok "--since picks the run" || bad "--since"
grep -q "SECRET" "$T/tl.txt" "$T/ex.txt" && bad "fixture secret leaked into output" || ok "no fixture secret in output"
# delete the verify record -> claim is UNKNOWN, never PASS
: > "$T/coord/claims_verified.jsonl"
run explain fx/demo | tr -d '\r' > "$T/ex2.txt"
grep -q "claim c1: UNKNOWN" "$T/ex2.txt" && ! grep -q "PASS" "$T/ex2.txt" && ok "missing verify record gives UNKNOWN" || bad "missing verify record: $(cat "$T/ex2.txt")"
# delete the end row and run state -> why terminated UNKNOWN
grep -v '"event": "end"' "$FX/coord/activity_ledger.jsonl" > "$T/coord/activity_ledger.jsonl"; rm "$T/coord/runs/fx__demo.json"
run explain fx/demo | tr -d '\r' | grep -q "^why terminated: UNKNOWN" && ok "missing end record gives UNKNOWN" || bad "why terminated not UNKNOWN"
# no code path reads sensitive fields
if grep -nE "[\"'](body|prompt|content|headers|authorization|token|key|path|note|task|cmd|task_prompt_head|stdout_head)[\"']" "$HERE/bin/timeline.py" | grep -v '^[0-9]*:\s*#' | grep -vE '^[0-9]+:(Never read|F_)' >/dev/null; then
  bad "timeline.py mentions a sensitive field name: $(grep -nE "[\"'](body|prompt|content|headers|authorization|token|key|path|note|task|cmd)[\"']" "$HERE/bin/timeline.py" | head -3)"
else ok "no sensitive field names in timeline.py"; fi
echo "$pass passed, $fail failed"; [ $fail -eq 0 ]
