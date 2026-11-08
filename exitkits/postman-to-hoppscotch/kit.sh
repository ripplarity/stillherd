#!/usr/bin/env bash
# kit.sh: boot the self-hosted Hoppscotch recipe (selfhost/selfhost.sh), import the sample Postman collection + environment through
# the backend API, verify the 5 requests exist, all inside one silent/silent.sh window; then tear down with volumes removed.
# Output: results/attestation.json, results/hosts.txt, results/run.log. Needs: docker, mitmdump, python.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
RES="$HERE/results"; mkdir -p "$RES"
RAW="$(mktemp)"
# local SMTP sink: the backend sends its sign-in mail here and nothing leaves the machine
"$PY" "$HERE/smtp_sink.py" & SINK=$!
trap 'kill $SINK 2>/dev/null' EXIT
export SELFHOST_RESDIR="$RES" SELFHOST_ATT=attestation.json SELFHOST_LOG=silent.log.jsonl SELFHOST_HOSTS=hosts.txt
export SELFHOST_EXTRA="$PY -I exitkits/postman-to-hoppscotch/kit_import.py"
PROFILE_ARGS="--profile default" bash "$ROOT/selfhost/selfhost.sh" postman > "$RAW" 2>&1
RC=$?
echo "kit exit code: $RC" >> "$RAW"
V="$(bash "$ROOT/silent/verify.sh" "$RES/attestation.json" 2>&1 | tail -1)"
echo "verify.sh: $V" >> "$RAW"
# keep local paths and user names out of the stored log
"$PY" - "$RAW" "$RES/run.log" "$ROOT" <<'PYEOF'
import re, sys
t = open(sys.argv[1], encoding="utf-8", errors="replace").read()
root = sys.argv[3]
for r in {root, root.replace("\\", "/"), re.sub(r"^/([a-z])/", r"\1:/", root.replace("\\", "/"))}:
    t = t.replace(r, "<repo>")
t = re.sub(r"(?i)/?c/Users/[^/\s]+|C:[/\\]Users[/\\][^/\\\s]+", "<home>", t)
open(sys.argv[2], "w", encoding="utf-8").write(t)
PYEOF
rm -f "$RAW"
exit $RC
