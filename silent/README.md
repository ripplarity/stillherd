# Stillherd silent

Run any coding-agent command with only a local allowlist, block and log everything else, and emit a signed attestation.

Usage:

    OLLAMA_API_BASE=http://127.0.0.1:11434 bash silent.sh aider --model ollama_chat/qwen2.5-coder:7b --message "say hi" --yes-always --no-git --no-auto-commits
    bash verify.sh attestation.json          # prints OK

`silent.sh` starts `mitmdump` (regular proxy mode) with `block_addon.py`, which answers HTTP 403 for every host not on the allowlist (default `127.0.0.1`, `localhost`, `::1`; add the host Ollama address with `SILENT_ALLOW_EXTRA=ip`). Blocked CONNECT tunnels are refused before any TLS. It sets the proxy and CA environment variables, runs the command, and writes `attestation.json` and `silent.log.jsonl`. The command's exit code is passed through.

`attestation.json` holds: start and end time, command, allowlist, `external_attempts` (host, path, timestamp), `external_bytes_sent` (bytes of requests forwarded to non-allowlisted hosts), `blocked_request_bytes`, `log_sha256`, the public key and an ed25519 signature over the canonical JSON of all other fields. The private key is created on first use at `~/.corral/silent_ed25519.pem` (override with `SILENT_KEY`); it never leaves that file. `verify.sh` checks the signature and recomputes the log hash; pass a pinned public-key SHA-256 as the second argument to check who signed.

## What this proves and what it does not

Proves, for the recorded run: every request that reached the proxy was either to an allowlisted host or was refused and logged, and the log is the one the signature covers.

Does not prove:

- Proxy-visible traffic only. A process that ignores proxy environment variables, opens raw sockets, uses a non-HTTP protocol, or resolves DNS itself is not seen.
- Pinned-certificate or CA-ignoring clients: if the TLS handshake fails the attempt is logged host-only (from the SNI) and nothing else is known about it.
- Child processes that scrub their environment are not covered.
- No kernel or firewall enforcement is claimed. A network-level block (container with no route, firewall rule) is a separate control.
- The signature shows the file is unchanged since signing by the holder of the key; it does not show the key holder ran the command honestly.

## Verifying against a pinned key
verify.sh checks the signature with the public key embedded in each attestation. To pin instead of trust-on-first-use, compare the attestation's pubkey_sha256 with this maintainer key fingerprint: `df051692c8467ca1eceb62d03333a02928bcb1a7ab42133efa2e2ef4287d1c42`. Attestations produced by other installs carry their own keys.

Attestation `version` ids written before the rename read `corral-silent-1`; new runs write `stillherd-silent-1`. `verify.sh` checks signatures and hashes and does not depend on the id.
