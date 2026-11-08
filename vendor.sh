#!/usr/bin/env bash
# Maintainer tool: refresh vendor/hooks from the maintainer's own ~/.claude, scrubbing identifying strings.
# End users never run this; install.sh installs from vendor/hooks.
set -eu
SRC="$HOME/.claude"; HERE="$(cd "$(dirname "$0")" && pwd)"; H="$HERE/vendor/hooks"
# git_guard.sh and stash_guard.sh are node-free rewrites maintained in vendor/hooks itself: keep them
KEEP_DIR="$(mktemp -d)"; cp "$H/git_guard.sh" "$H/stash_guard.sh" "$KEEP_DIR/"
rm -rf "$H"; mkdir -p "$H"
COORD="proc_tree cpu_lease gpu_lease run_leased activity_ledger pidfix"
SCRIPTS="claim_capture.py verify_claims.py check_exec.py"
for n in $COORD; do cp "$SRC/coord/$n.py" "$H/$n.py"; done
for f in $SCRIPTS; do cp "$SRC/scripts/$f" "$H/$f"; done
cp "$KEEP_DIR/git_guard.sh" "$KEEP_DIR/stash_guard.sh" "$H/"
# scrub the COPIES only (originals untouched); see SCRUB.md
for f in "$H"/*; do
  sed -i -E \
    -e 's#c--Users-[^-]+-Development-[A-Za-z0-9_]+#<project-slug>#g' \
    -e 's#[A-Za-z]:[\\/]+Users[\\/]+[^\\/]+#$HOME#g' \
    -e 's#Health[S]hield#<project>#g' \
    -e 's#Development([\\/]+)[A-Za-z0-9_]+#Development\1<project>#g' "$f"
done
# optional leak scan: terms live in a local, gitignored .scrublist (one per line)
if [ -f "$HERE/.scrublist" ] && grep -rFilf "$HERE/.scrublist" "$H" >/dev/null; then
  echo "scrub failed: identifying strings remain" >&2
  grep -rFilf "$HERE/.scrublist" "$H" >&2
  exit 1
fi
echo "vendored $(ls "$H" | wc -l) files into vendor/hooks"
