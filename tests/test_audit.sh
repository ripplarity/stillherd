#!/usr/bin/env bash
# stillherd audit: fixture diff mode on committed result files, refusal without mitmdump, usage errors. Usage: bash tests/test_audit.sh
HERE="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
ok() { pass=$((pass+1)); echo "ok   $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; }
R="$HERE/egress/results"; T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT; export AUDITS_OUT="$T/AUDITS.md"; BEFORE=0
out="$(bash "$HERE/stillherd" audit claude --from "$R/claude-code_2.1.304_run1.jsonl" "$R/claude-code_2.1.304_optout_run1.jsonl" 2>&1)"
for w in "| api.anthropic.com |" "DISABLE_TELEMETRY=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" "requests default"; do
  printf '%s\n' "$out" | grep -qF "$w" && ok "fixture output has: $w" || bad "missing: $w"
done
AFTER="$(wc -l < "$AUDITS_OUT")"; [ "$AFTER" -gt "$BEFORE" ] && ok "AUDITS.md appended" || bad "AUDITS.md not appended"
out="$(bash "$HERE/stillherd" audit opencode --from "$R/opencode_1.19.5_run1.jsonl" "$R/opencode_1.19.5_run2.jsonl" 2>&1)"
printf '%s\n' "$out" | grep -qF "no documented opt-out" && ok "opencode: no documented opt-out stated" || bad "opencode text"
out="$(env PATH=/nonexistent HOME=/nonexistent "$BASH" "$HERE/egress/audit.sh" opencode 2>&1)"; rc=$?
[ $rc -eq 1 ] && printf '%s' "$out" | grep -q "uv tool install mitmproxy" && ok "refuses without mitmdump, says how to install" || bad "mitmdump refusal rc=$rc $out"
bash "$HERE/stillherd" audit nosuchtool >/dev/null 2>&1; [ $? -eq 2 ] && ok "unknown tool rejected" || bad "unknown tool"
echo "$pass passed, $fail failed"; [ $fail -eq 0 ]
