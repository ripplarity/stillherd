#!/usr/bin/env bash
# watch.sh <cmd...>: run a command through the local logging proxy WITHOUT blocking anything; print a table of external
# hosts (host, requests, bytes out, rule); write watch.requests.jsonl (no bodies) and a signed watch.attestation.dsse.json.
# Same proxy, CA and attestation code as silent.sh (mode observe). Env as silent.sh: SILENT_OUT, SILENT_PORT, SILENT_KEY.
HERE="$(cd "$(dirname "$0")" && pwd)"
SILENT_MODE=observe exec bash "$HERE/silent.sh" "$@"
