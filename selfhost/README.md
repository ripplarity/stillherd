# corral selfhost

`bash selfhost.sh <saas>` brings up an open replacement for a SaaS and attests what it sent out during startup.

| SaaS | Replacement | Licence | Official compose file |
|---|---|---|---|
| notion | appflowy | AGPL-3.0 | AppFlowy-Cloud `docker-compose.yml` |
| postman | hoppscotch | MIT | hoppscotch `docker-compose.yml` |
| slack | rocketchat | MIT (core; enterprise directories separately licensed) | rocketchat-compose `compose.yml` |

`recipes.json` holds the table, the licence per recipe, and the alternatives considered but not chosen (a source-available one and a desktop-only one).

## What it does

1. Downloads the compose file (and the env template, kept out of git) into `recipes/<name>/`. These are untrusted data; nothing from them is run except through `docker compose`.
2. Validates the compose file with `docker compose config` (YAML parse if the CLI is missing). Result in `recipes/<name>/validation.txt`.
3. If the docker daemon is not reachable, writes `DOCKER_DOWN.md` with the exact `docker info` error and stops.
4. If it is reachable: pulls images (outside the proxy), then runs `docker compose up -d` under `../silent/silent.sh` for 120 s with the container proxy settings injected through a temporary docker client config, writes `recipes/<name>/attestation.json`, then runs `docker compose down -v`.

## What it proves

Compose-level egress during startup only: which hosts the started containers tried to reach through the proxy in the first 120 seconds, and that the non-allowlisted ones were refused and logged, in a log covered by the signature.

## Limits

- Status of this build: steps 1 to 3 were run (all three compose files validated; the daemon was down). The container run in step 4 has not been executed, so no attestation exists yet.
- Startup window only; later behaviour is not covered.
- Image pulls are done by the daemon, outside the proxy, and are not attested.
- Only traffic that honours the injected proxy settings is seen; containers that ignore them, use raw sockets or non-HTTP protocols are not. The proxy has no CA in the containers, so HTTPS to non-allowlisted hosts is refused at CONNECT; allowlisted HTTPS content is not inspected.
- Traffic between containers of the same stack is not routed through the proxy.
- No kernel or firewall enforcement is claimed.
