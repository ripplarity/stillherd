# Stillherd silent

Run any coding-agent command with only a local allowlist, block and log everything else, and emit a signed attestation.

Usage:

    OLLAMA_API_BASE=http://127.0.0.1:11434 bash silent.sh aider --model ollama_chat/qwen2.5-coder:7b --message "say hi" --yes-always --no-git --no-auto-commits
    bash verify.sh attestation.dsse.json     # prints VALID-SIGNATURE, VALID <predicateType>, OK

`silent.sh` starts `mitmdump` (regular proxy mode) with `block_addon.py`, which answers HTTP 403 for every host not on the allowlist (default `127.0.0.1`, `localhost`, `::1`; add the host Ollama address with `SILENT_ALLOW_EXTRA=ip`). Blocked CONNECT tunnels are refused before any TLS. It sets the proxy and CA environment variables, runs the command, and writes `attestation.dsse.json` and `silent.log.jsonl`. The command's exit code is passed through.

`attestation.dsse.json` is a DSSE envelope around an in-toto Statement v1 (attest-spec v1; predicateType `https://ripplarity.com/attestation/stillherd/silent/v1`). The subject is the signed log file (name and sha256). The predicate holds producer, start and end time, environment (`run_mode` REAL, or set `SILENT_RUN_MODE`), command, allowlist, `external_attempts` (host, path, timestamp), `external_bytes_sent`, `blocked_request_bytes` and the public key. The signature is ed25519 over the DSSE pre-authentication encoding; `keyid` is the sha256 of the raw public key. The private key is created on first use at `~/.stillherd/silent_ed25519.pem` (an existing `~/.corral/` key is still found; override with `SILENT_KEY`) and never leaves that file. `attest_verify.py` is a byte-identical copy of the verifier in attest-spec.

## What this proves and what it does not

Proves, for the recorded run: every request that reached the proxy was either to an allowlisted host or was refused and logged, and the log is the one the signature covers.

Does not prove:

- Proxy-visible traffic only. A process that ignores proxy environment variables, opens raw sockets, uses a non-HTTP protocol, or resolves DNS itself is not seen.
- Pinned-certificate or CA-ignoring clients: if the TLS handshake fails the attempt is logged host-only (from the SNI) and nothing else is known about it.
- Child processes that scrub their environment are not covered.
- No kernel or firewall enforcement is claimed. A network-level block (container with no route, firewall rule) is a separate control.
- The signature shows the file is unchanged since signing by the holder of the key; it does not show the key holder ran the command honestly.

## Verifying against a pinned keyid
`verify.sh` always pins. It checks the signature and the log hash, and trusts a key only if its keyid equals the pin: the second argument, else `SILENT_EXPECT_KEYID`, else the maintainer keyid published here: `df051692c8467ca1eceb62d03333a02928bcb1a7ab42133efa2e2ef4287d1c42`. A key found beside the artefact is never trusted on its own. Attestations produced by other installs carry their own key: pass that install's keyid as the second argument.

Legacy format: attestations written before the shared format (format ids `corral-silent-1`, `stillherd-silent-1`; four are committed and left untouched) are still verified by `verify.sh`, which prints `OK (legacy format)`. New runs write only the DSSE file. Transparency-log (Rekor) anchoring is OFF and founder-gated; a receipt means a log inclusion proof, which these files do not carry.

Tests: `bash tests/test_attest.sh`.
