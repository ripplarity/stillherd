#!/usr/bin/env bash
# audit.sh TOOL            run the host harness twice (default, then documented opt-out variables) and tabulate
# audit.sh TOOL --from A B  fixture mode: tabulate two existing result files, no harness, no mitmdump needed
# TOOL: claude | aider | opencode | goose. Appends to results/AUDITS.md. Exit 2 usage, 1 refused/failed.
set -u
HERE="${BASH_SOURCE[0]%/*}"; HERE="$(cd "$HERE" && pwd)"
tool="${1:-}"
# Opt-out variables, each taken from the tool's official docs (URL and fetch date beside each line).
case "$tool" in
  claude)   VARS="DISABLE_TELEMETRY=1 CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1" ;;  # variables named in the harness run_claude_optout.sh; official docs not fetched here
  aider)    VARS="AIDER_ANALYTICS_DISABLE=1" ;;  # flag --analytics-disable; https://aider.chat/docs/config/options.html and /docs/more/analytics.html, fetched 2026-11-08
  goose)    VARS="GOOSE_TELEMETRY_ENABLED=false" ;;  # https://goose-docs.ai/docs/guides/environment-variables and /docs/guides/usage-data, fetched 2026-11-08
  opencode) VARS="" ;;  # no documented opt-out (checked https://opencode.ai/docs/config/, /docs/cli/, /docs/troubleshooting/ on 2026-11-08)
  *) echo "usage: stillherd audit <claude|aider|opencode|goose> [--from RUN1 RUN2]" >&2; exit 2 ;;
esac
SHOWN="${VARS:-no documented opt-out (checked https://opencode.ai/docs/config/ on 2026-11-08)}"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
if [ "${2:-}" = "--from" ]; then
  [ -f "${3:-}" ] && [ -f "${4:-}" ] || { echo "usage: stillherd audit $tool --from RUN1 RUN2 (two existing result files)" >&2; exit 2; }
  exec "$PY" "$HERE/audit_table.py" "$tool" "$SHOWN" "$3" "$4"
fi
if ! command -v mitmdump >/dev/null 2>&1 && ! [ -x "$HOME/.local/bin/mitmdump" ]; then
  echo "mitmdump is missing. Install it: uv tool install mitmproxy   (or: python -m pip install --user mitmproxy), then rerun." >&2
  exit 1
fi
case "$tool" in
  aider|goose) echo "$tool has no host harness (it runs in the clean WSL machine: TOOLS=$tool bash egress/clean_run.sh). Run it there twice, then: stillherd audit $tool --from RUN1 RUN2" >&2; exit 1 ;;
esac
R="$HERE/results"; mkdir -p "$R"
case "$tool" in
  claude)
    bash "$HERE/run_claude.sh" || exit 1
    bash "$HERE/run_claude_optout.sh" || exit 1
    A="$(ls -t "$R"/claude-code_*_run1.jsonl | grep -v optout | head -1)"; B="$(ls -t "$R"/claude-code_*_optout_run1.jsonl | head -1)" ;;
  opencode)
    bash "$HERE/run_tool.sh" >/dev/null || exit 1; A="$R/opencode_audit_default.jsonl"; cp "$HERE/requests.jsonl" "$A"
    bash "$HERE/run_tool.sh" >/dev/null || exit 1; B="$R/opencode_audit_optout.jsonl"; cp "$HERE/requests.jsonl" "$B" ;;
esac
exec "$PY" "$HERE/audit_table.py" "$tool" "$SHOWN" "$A" "$B"
