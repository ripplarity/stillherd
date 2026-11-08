#!/usr/bin/env bash
# Clean-machine egress runs: fresh WSL distro, 2 runs per tool, then teardown.
# Needs data/rootfs.tar.gz (official Ubuntu WSL image; verify its SHA256 first, never execute it).
# Env: TOOLS="opencode aider goose" (default "opencode goose"), REUSE=1 reuse an existing distro,
#      KEEP=1 skip teardown (debugging only).
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
export MSYS_NO_PATHCONV=1
D=stillherd-clean
TOOLS="${TOOLS:-opencode goose}"
W() { wsl.exe -d $D -u root --exec "$@" 2>&1 | tr -d '\000'; }
teardown() { [ "${KEEP:-0}" = 1 ] || wsl.exe --unregister $D 2>&1 | tr -d '\000'; }
trap teardown EXIT
mkdir -p "$HERE/results/sessions" "$HERE/data"
WIN="$(cygpath -m "$HERE")"
if [ "${REUSE:-0}" != 1 ]; then
  wsl.exe --unregister $D >/dev/null 2>&1
  wsl.exe --import $D "$(cygpath -w "$HERE/data/distro")" "$(cygpath -w "$HERE/data/rootfs.tar.gz")" --version 2 2>&1 | tr -d '\000'
fi
W sh -c 'mkdir -p /opt/stillherd && cd "$(wslpath -a "$1")" && cp inner.sh log_requests.py session.txt opencode.json /opt/stillherd/' _ "$WIN" || exit 1
[ "${REUSE:-0}" = 1 ] || W bash /opt/stillherd/inner.sh provision | tail -5
HIP="$(W sh -c "ip route show default | awk '{print \$3}'" | head -1)"
echo "host ip: $HIP"
ok=0
for try in 1 2; do
  W curl -sS -m 10 "http://$HIP:11434/api/tags" | grep -q qwen2.5-coder && ok=1 && break
done
[ "$ok" = 1 ] || { echo "host Ollama unreachable from WSL at $HIP:11434" >&2; exit 3; }
for tool in $TOOLS; do
  for n in 1 2; do
    echo "== $tool run$n"
    out="$(W bash /opt/stillherd/inner.sh run $tool $n "$HIP")"
    printf '%s\n' "$out" | tail -12
    ver="$(printf '%s\n' "$out" | grep -o 'VERSION=[^ ]*' | tail -1 | cut -d= -f2 | tr -c 'A-Za-z0-9.\n' '_')"
    W cat /tmp/res_${tool}_${n}.jsonl > "$HERE/results/${tool}_${ver:-unknown}_run${n}.jsonl"
    printf '%s\n' "$out" > "$HERE/results/sessions/${tool}_run${n}.turns.txt"
    W sh -c "for f in /tmp/s_${tool}_${n}_*.out; do echo \"--- \$f\"; cat \"\$f\"; done" > "$HERE/results/sessions/${tool}_run${n}.out"
  done
done
"$PY" "$HERE/classify.py"
