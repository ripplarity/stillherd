#!/usr/bin/env bash
# stillherd installer: install.sh TARGET
# Self-contained: installs from vendor/hooks and bin/ in this repo; assumes nothing from the host's own ~/.claude.
set -eu
[ $# -eq 1 ] || { echo "usage: install.sh TARGET" >&2; exit 2; }
TARGET="$1"; H="$TARGET/.claude/hooks"
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -d "$HERE/vendor/hooks" ] || { echo "vendor/hooks missing: run vendor.sh on a maintainer machine" >&2; exit 1; }
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
command -v "$PY" >/dev/null 2>&1 || { echo "python3 is required (apt install python3)" >&2; exit 1; }
mkdir -p "$H" "$TARGET/.claude/bin"
find "$HERE/vendor/hooks" -maxdepth 1 -type f -exec cp {} "$H"/ \;
cp "$HERE/stillherd" "$TARGET/.claude/bin/stillherd"; chmod +x "$TARGET/.claude/bin/stillherd"
cp "$HERE/bin/today.py" "$HERE/silent/dsse.py" "$TARGET/.claude/bin/"
chmod +x "$H"/*.sh 2>/dev/null || true
"$PY" "$HERE/merge_settings.py" "$TARGET/.claude/settings.json" "$PY"
echo "stillherd 0.1.0 installed to $TARGET/.claude"
