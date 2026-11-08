#!/usr/bin/env bash
# D5: run the corral repo end to end in a fresh WSL distro, as a stranger would.
# Env: REVS="rev1 rev2" git revisions to run in order (default HEAD); REUSE=1 reuse the existing distro;
#      KEEP=1 skip teardown.
# Needs egress/data/rootfs.tar.gz (official Ubuntu WSL image, verified by SHA256).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
export MSYS_NO_PATHCONV=1
D=corral-d5
W() { wsl.exe -d $D -u root --exec "$@" 2>&1 | tr -d '\000'; }
teardown() { [ "${KEEP:-0}" = 1 ] || wsl.exe --unregister $D 2>&1 | tr -d '\000'; }
trap teardown EXIT
mkdir -p "$HERE/egress/data/d5"
if [ "${REUSE:-0}" != 1 ]; then
  wsl.exe --unregister $D >/dev/null 2>&1
  wsl.exe --import $D "$(cygpath -w "$HERE/egress/data/d5-distro")" "$(cygpath -w "$HERE/egress/data/rootfs.tar.gz")" --version 2 2>&1 | tr -d '\000'
fi
W sh -c 'mkdir -p /opt/d5 && cp "$(wslpath -a "$1")" /opt/d5/d5_inner.sh' _ "$(cygpath -m "$HERE/d5_inner.sh")" || exit 1
[ "${REUSE:-0}" = 1 ] || W bash /opt/d5/d5_inner.sh provision | tail -3
HIP="$(W sh -c "ip route show default | awk '{print \$3}'" | head -1)"
echo "host ip: $HIP"
W curl -sS -m 10 "http://$HIP:11434/api/tags" | grep -q qwen2.5-coder || { echo "host Ollama unreachable from WSL at $HIP:11434" >&2; exit 3; }
for rev in ${REVS:-HEAD}; do
  echo "##### revision $rev ($(git -C "$HERE" rev-parse --short "$rev"))"
  git -C "$HERE" archive "$rev" -o "$HERE/egress/data/d5/corral.tar" || exit 1
  W sh -c 'cp "$(wslpath -a "$1")" /opt/d5/corral.tar' _ "$(cygpath -m "$HERE/egress/data/d5/corral.tar")" || exit 1
  W bash /opt/d5/d5_inner.sh stranger "$HIP" | tee "$HERE/egress/data/d5_$(git -C "$HERE" rev-parse --short "$rev").txt"
done
