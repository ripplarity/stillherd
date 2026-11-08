#!/usr/bin/env bash
# verify.sh attestation.json [pubkey_sha256]
HERE="$(cd "$(dirname "$0")" && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
[ $# -ge 1 ] || { echo "usage: verify.sh attestation.json [pubkey_sha256]" >&2; exit 2; }
exec "$PY" "$HERE/verify.py" "$@"
