#!/usr/bin/env bash
# watch_versions.sh [--run]
# For each tracked tool, fetch the latest published version and compare it with results/versions.json.
# Prints one line per tool: "NEW <tool> <old> -> <new>", "SAME <tool> <version>" or "ERROR <tool> <reason>".
# With --run, a NEW version triggers one egress run with the existing harness and writes results/<tool>_<ver>_run1.jsonl.
# claude-code is never run by this script (it needs the account login and an explicit decision); it only reports.
# Exit code is 0 unless the script itself cannot start.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
RES="$HERE/results"; VJ="$RES/versions.json"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
RUN=0; [ "${1:-}" = "--run" ] && RUN=1
mkdir -p "$RES"
if [ ! -f "$VJ" ]; then
  # seed from the versions in the existing result file names: <tool>_<version>_run<n>.jsonl
  "$PY" - "$RES" "$VJ" <<'PYEOF'
import glob, json, os, re, sys
res, out = sys.argv[1], sys.argv[2]
v = {}
for f in sorted(glob.glob(os.path.join(res, "*_run*.jsonl"))):
    m = re.match(r"(.+?)_(\d[^_]*)_run\d\.jsonl$", os.path.basename(f))
    if m:
        v[m.group(1)] = m.group(2)
json.dump(v, open(out, "w"), indent=1, sort_keys=True)
PYEOF
fi
json_get() { "$PY" -c "import json,sys;d=json.load(sys.stdin);print(eval(sys.argv[1]))" "$1" 2>/dev/null; }
latest() {
  case "$1" in
    aider)       curl -fsS --max-time 30 https://pypi.org/pypi/aider-chat/json | json_get "d['info']['version']" ;;
    goose)       curl -fsSL --max-time 30 https://api.github.com/repos/block/goose/releases/latest | json_get "d['tag_name'].lstrip('v')" ;;
    opencode)    curl -fsS --max-time 30 https://registry.npmjs.org/opencode-ai/latest | json_get "d['version']" ;;
    claude-code) curl -fsS --max-time 30 https://registry.npmjs.org/@anthropic-ai/claude-code/latest | json_get "d['version']" ;;
  esac
}
known() { "$PY" -c "import json,sys;print(json.load(open(sys.argv[1])).get(sys.argv[2],''))" "$VJ" "$1"; }
for tool in aider goose opencode claude-code; do
  new="$(latest "$tool")"
  old="$(known "$tool")"
  if [ -z "$new" ]; then echo "ERROR $tool could not read the latest version"; continue; fi
  if [ "$new" = "$old" ]; then echo "SAME $tool $new"; continue; fi
  echo "NEW $tool ${old:-none} -> $new"
  [ "$RUN" = 1 ] || continue
  case "$tool" in
    opencode)
      if bash "$HERE/run_tool.sh" >/dev/null 2>&1 && cp "$HERE/requests.jsonl" "$RES/${tool}_${new}_run1.jsonl"; then
        echo "RAN $tool $new -> results/${tool}_${new}_run1.jsonl"
        "$PY" -c "import json,sys;p=sys.argv[1];d=json.load(open(p));d[sys.argv[2]]=sys.argv[3];json.dump(d,open(p,'w'),indent=1,sort_keys=True)" "$VJ" "$tool" "$new"
      else echo "ERROR $tool harness failed"; fi ;;
    aider|goose)
      echo "SKIP $tool no host harness; use: TOOLS=$tool bash egress/clean_run.sh" ;;
    claude-code)
      echo "SKIP claude-code not run by this script; run egress/run_claude.sh after a decision" ;;
  esac
done
exit 0
