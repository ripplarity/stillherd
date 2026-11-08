#!/usr/bin/env bash
# Re-runs demo/containment.sh and asserts BLOCKED + OK + FAIL. Skips if mitmdump is absent.
HERE="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="$HOME/.local/bin:$PATH"
command -v mitmdump >/dev/null 2>&1 || { echo "skip: mitmdump not found (containment demo not run)"; exit 0; }
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
out="$(SILENT_PORT="${SILENT_PORT:-8091}" bash "$HERE/demo/containment.sh" "$T" 2>&1)"
fail=0
for p in '^BLOCKED example.com' '^OK$' '^FAIL (tampered'; do
  echo "$out" | grep -q "$p" && echo "ok   $p" || { echo "FAIL missing $p"; fail=1; }
done
exit $fail
