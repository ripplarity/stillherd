#!/usr/bin/env bash
# stillherd feed: build from committed CHANGELOG, XML well-formed, item count, banned words. Usage: bash tests/test_feed.sh
HERE="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0
ok() { pass=$((pass+1)); echo "ok   $1"; }
bad() { fail=$((fail+1)); echo "FAIL $1"; }
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cp "$HERE/egress/results/CHANGELOG.md" "$T/"
PY=python3; command -v python3 >/dev/null || PY=python
"$PY" "$HERE/egress/feed.py" "$T" >/dev/null && ok "feed builds" || bad "feed build"
N="$(grep -c '^## ' "$T/CHANGELOG.md")"
"$PY" - "$T" "$N" <<'PYEOF' && ok "xml well-formed, count, json, banned words" || bad "xml/count/banned"
import sys, json, xml.etree.ElementTree as ET
t, n = sys.argv[1], int(sys.argv[2])
root = ET.parse(t + "/feed.xml").getroot()
assert len(root.findall("./channel/item")) == n
j = json.load(open(t + "/feed.json"))
assert len(j["items"]) == n
txt = (open(t + "/feed.xml", encoding="utf-8").read() + open(t + "/feed.json", encoding="utf-8").read()).lower()
for w in ["telemetry", "spy", "leak", "exfiltrat", "hidden", "secret", "suspicious", "phone-home"]:
    assert w not in txt, w
PYEOF
cmp -s "$T/feed.xml" "$HERE/egress/results/feed.xml" && ok "committed feed.xml current" || bad "committed feed.xml stale"
# injected prose word must fail with rc != 0
mkdir "$T/inj"; sed 's/^\(Total requests.*\)$/ hidden/' "$T/CHANGELOG.md" > "$T/inj/CHANGELOG.md"
"$PY" "$HERE/egress/feed.py" "$T/inj" >/dev/null 2>&1; [ $? -ne 0 ] && ok "banned word in prose fails" || bad "banned word accepted"
# host containing a banned substring survives verbatim
mkdir "$T/host"; sed 's/registry\.npmjs\.org/api-spy.example/g' "$T/CHANGELOG.md" > "$T/host/CHANGELOG.md"
"$PY" "$HERE/egress/feed.py" "$T/host" >/dev/null 2>&1 && grep -q 'api-spy\.example' "$T/host/feed.xml" && ok "host with spy kept verbatim" || bad "host rewritten"
echo "$pass passed, $fail failed"; [ "$fail" -eq 0 ]
