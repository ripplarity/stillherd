# egress: record what an AI coding tool sends out

Reproducible harness that records every outbound HTTP(S) request one AI coding tool makes during a fixed scripted session.

## Method

1. `bash run_tool.sh` re-executes itself under the lease wrapper (2 cores, eta 1800 s), installs `mitmproxy` if missing (`uv tool install`, falling back to `pip install --user`), and starts `mitmdump` in regular proxy mode on `127.0.0.1:8080` with the addon `log_requests.py`.
2. It exports `HTTP_PROXY`/`HTTPS_PROXY` (and lowercase forms), `NODE_USE_ENV_PROXY=1`, and `NODE_EXTRA_CA_CERTS`/`SSL_CERT_FILE` pointing at the mitmproxy CA, with `NO_PROXY` empty so even loopback traffic goes through the proxy.
3. It creates a throwaway fixture project (`calc.py`, one test) and runs the tool, `opencode`, against local Ollama `qwen2.5-coder:7b` (`opencode.json`; no API keys, no paid services).
4. The session is `session.txt`: list files, add a function, run tests, explain a file, exit. Each prompt runs with a 150 s timeout, so the session is bounded well under 10 minutes.
5. Output: `requests.jsonl`, one row per request: `{ts, tool, version, host, path, method, bytes, content_type}`. Model output goes to `session.out` (not tracked).

Check: `python -c "import json;[json.loads(l) for l in open('requests.jsonl')]"`.

## Reading the output

Rows to `127.0.0.1` are the local model server. Everything else is egress. Rows from unrelated developer-machine shims (for example a package-scanner wrapper that fires when the tool runs `pip`) can appear; filter by host and note it when publishing numbers. The bundled sample run is one such run, not a clean-room measurement.

## What transparent mode would add

Regular proxy mode only sees clients that honour proxy environment variables. A client that ignores them (hard-coded direct sockets, some native runtimes) is invisible. Transparent mode (firewall redirect of all outbound 80/443 to the proxy, or a network namespace/container with a forced route) captures those too, and also shows traffic from child processes that scrub their environment. It needs elevated rights and a Linux container or VM; it is not done here.

## Clean-machine runs

`clean_run.sh` imports a fresh Ubuntu WSL distro from the official rootfs (verify its SHA256 against the published sums; place it at `data/rootfs.tar.gz`, never execute it), installs only curl, ca-certificates, python3, nodejs, the proxy and its CA, and the tool under test, then runs `session.txt` twice per tool against the host's Ollama. Tool state is wiped between runs; each run starts a fresh proxy. The distro is unregistered at the end. `classify.py` builds `results/TABLE.md`, including the classification rules and a run 1 vs run 2 agreement check. Request counts can differ between runs when a first run populates a package cache (see the opencode rows); compare host sets, not just counts.

## Certificate-pinning fallback

If a client pins certificates or does not trust the mitmproxy CA, the TLS handshake fails and no request body is visible. `log_requests.py` then records a host-only row (`method: CONNECT-TLS-FAILED`, host from the SNI) so the destination is still logged without path, size or content type. Report such hosts as "destination known, content not inspected".
