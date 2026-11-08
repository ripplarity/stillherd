#!/usr/bin/env bash
# selfhost.sh <saas>: fetch the official compose file for the open replacement, validate it,
# and (if the docker daemon is up) start it under the egress allowlist proxy and attest the startup egress.
# The downloaded files are untrusted data: nothing from them is executed except via `docker compose`.
set -u
[ $# -eq 1 ] || { echo "usage: selfhost.sh <saas>" >&2; exit 2; }
HERE="$(cd "$(dirname "$0")" && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
field() { "$PY" -c "import json,sys;d=json.load(open(sys.argv[1])).get(sys.argv[2]);print(d[sys.argv[3]] if d and sys.argv[3] in d else '')" "$HERE/recipes.json" "$1" "$2"; }
SAAS="$1"
NAME="$(field "$SAAS" replacement)"
[ -n "$NAME" ] || { echo "unknown saas: $SAAS" >&2; exit 2; }
D="$HERE/recipes/$NAME"; mkdir -p "$D"
curl -fsSL --max-time 60 -o "$D/docker-compose.yml" "$(field "$SAAS" compose_url)" || { echo "compose download failed" >&2; exit 1; }
ENVU="$(field "$SAAS" env_url)"
[ -n "$ENVU" ] && curl -fsSL --max-time 60 -o "$D/.env" "$ENVU" 2>/dev/null
echo "licence: $(field "$SAAS" license)" > "$D/LICENCE.txt"

# ---- validation (works without a daemon)
V="$D/validation.txt"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  ENVARG=""; [ -f "$D/.env" ] && ENVARG="--env-file $D/.env"
  if docker compose $ENVARG -f "$D/docker-compose.yml" config -q >"$V" 2>&1; then echo "docker compose config: OK" >> "$V"; else echo "docker compose config: FAILED" >> "$V"; fi
else
  "$PY" -c "import yaml,sys;yaml.safe_load(open(sys.argv[1]));print('yaml parse: OK')" "$D/docker-compose.yml" > "$V" 2>&1
fi
echo "validation: $(tail -1 "$V")"

# ---- daemon check
INFO="$(docker info 2>&1)"
if ! docker info >/dev/null 2>&1; then
  ERR="$(printf '%s\n' "$INFO" | grep -iE 'failed to connect|error|cannot connect' | head -3)"
  {
    echo "# DOCKER_DOWN"
    echo
    echo "The docker daemon was not reachable, so no container was started and no egress attestation was produced."
    echo
    echo '`docker info` error:'
    echo
    printf '    %s\n' "$ERR"
    echo
    echo "Compose files were downloaded and validated only (see recipes/*/validation.txt)."
  } > "$HERE/DOCKER_DOWN.md"
  echo "docker down: wrote DOCKER_DOWN.md"
  exit 0
fi

# ---- daemon up: pull outside the proxy (the daemon pulls images, not the CLI), then attest startup egress
cd "$D" || exit 1
COMPOSE="docker compose -f docker-compose.yml"
for f in compose.database.yml compose.nats.yml; do [ -f "$f" ] && COMPOSE="$COMPOSE -f $f"; done
[ -f docker-compose.override.yml ] && COMPOSE="$COMPOSE -f docker-compose.override.yml"
[ -f .env ] && COMPOSE="$COMPOSE --env-file .env"
$COMPOSE ${PROFILE_ARGS:-} pull -q || echo "pull failed (continuing)" >&2
# containers get the proxy via the docker client config; CONNECT to non-allowlisted hosts is refused by the proxy
CFG="$(mktemp -d)"; PORT=8091
# per-recipe profile + smoke URL (a 2xx from the URL = healthy)
UPARGS=""; SMOKE_URL="http://localhost:3000/"
case "$NAME" in
  hoppscotch) UPARGS="--profile default"; SMOKE_URL="http://localhost:3170/ping" ;;
  appflowy)   SMOKE_URL="http://localhost/gotrue/health" ;;
  rocketchat) SMOKE_URL="http://localhost:3000/api/info" ;;
esac
# service names must bypass the proxy (inter-container traffic is internal, not egress)
SVC="$($COMPOSE $UPARGS config --services 2>/dev/null | paste -sd, -)"
"$PY" - "$CFG/config.json" "$PORT" "$SVC" <<'PYEOF'
import json, os, sys
out, port, svc = sys.argv[1], sys.argv[2], sys.argv[3]
src = os.path.expanduser("~/.docker/config.json")
cfg = json.load(open(src)) if os.path.exists(src) else {}
p = f"http://host.docker.internal:{port}"
cfg["proxies"] = {"default": {"httpProxy": p, "httpsProxy": p, "noProxy": "localhost,127.0.0.1," + svc}}
json.dump(cfg, open(out, "w"))
PYEOF
cp -r "$HOME/.docker/contexts" "$CFG/" 2>/dev/null; [ -d "$HOME/.docker/cli-plugins" ] && cp -r "$HOME/.docker/cli-plugins" "$CFG/" 2>/dev/null
RES="${SELFHOST_RESDIR:-$HERE/results}"; mkdir -p "$RES"
ATTF="${SELFHOST_ATT:-$NAME.attestation.json}"; LOGF="${SELFHOST_LOG:-$NAME.silent.log.jsonl}"; HOSTF="${SELFHOST_HOSTS:-$NAME.hosts.txt}"
# image digests (recorded before boot so the table does not depend on the run)
$COMPOSE $UPARGS config --images 2>/dev/null | sort -u | while read -r im; do
  printf '%s %s\n' "$im" "$(docker image inspect --format '{{index .RepoDigests 0}}' "$im" 2>/dev/null || echo none)"
done > "$RES/$NAME.images.txt"
# boot + health wait + smoke, all inside the attested window
cat > "$D/smoke.sh" <<SMEOF
cd selfhost/recipes/$NAME || exit 1
$COMPOSE $UPARGS up -d || exit 1
ok=0
for i in \$(seq 1 90); do
  code=\$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$SMOKE_URL")
  case "\$code" in 2*) ok=1; break;; esac
  sleep 10
done
[ \$ok = 1 ] || { echo "SMOKE failed last=\$code"; exit 1; }
echo "SMOKE ok \$code after \$((i*10))s"
cd ../../.. && ${SELFHOST_EXTRA:-true}
SMEOF
export SILENT_OUT="$RES" SILENT_LOGNAME="$LOGF" SILENT_ATTNAME="$ATTF" SILENT_PORT=$PORT SILENT_LISTEN=0.0.0.0
cd "$HERE/.." && DOCKER_CONFIG="$CFG" bash silent/silent.sh bash "selfhost/recipes/$NAME/smoke.sh"
RC=$?
cd "$D" || exit 1
$COMPOSE $UPARGS down -v
rm -rf "$CFG"
if [ -f "$RES/$ATTF" ]; then
  "$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));print(chr(10).join(sorted({a['host'] for a in d.get('external_attempts',[])})))" "$RES/$ATTF" > "$RES/$HOSTF"
fi
exit $RC
