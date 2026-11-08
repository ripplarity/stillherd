<p align="center">
  <img src="assets/logo.svg" width="128" height="128" alt="Stillherd logo: a ring of fence posts around three agents, with a check mark">
</p>

<h1 align="center">Stillherd</h1>

<p align="center"><b>Run fleets of coding agents that cannot wreck each other, and prove what they sent out.</b></p>

<p align="center">
  <code>bash install.sh /path/to/project</code> &nbsp;·&nbsp; <code>bash silent/silent.sh &lt;any agent command&gt;</code> &nbsp;·&nbsp; <code>bash silent/verify.sh attestation.json</code>
</p>

---

## What you get

| | You run | You get |
|---|---|---|
| **Guardrails** | `install.sh` into any project that uses Claude Code hooks | Kill-tree, CPU and GPU leases, a stash ban, and claim receipts, enforced by hooks on the default path. The CLI is `stillherd status`. |
| **Silent runs** | `silent/silent.sh aider ...` (any agent, any model server) | Every host except your local allowlist is refused, every attempt is logged, and the run ends with an ed25519-signed attestation of hosts contacted and bytes sent. |
| **Receipts for leaving SaaS** | `selfhost/selfhost.sh postman`, `exitkits/postman-to-hoppscotch/kit.sh` | An open replacement boots, your data is imported, and the whole window is attested. |
| **Egress tables** | `egress/run_tool.sh` | A reproducible record of what a named AI coding tool sends out during a fixed scripted session. |

## Evidence in this repository

Every number below is produced by a file in this repository and can be re-checked with the commands shown.

| Result | Where | Check |
|---|---|---|
| Four AI coding tools, two scripted runs each: hosts, request counts, bytes, classification per host | [egress/results/WEEK1_TABLE.md](egress/results/WEEK1_TABLE.md) and the `egress/results/*.jsonl` request logs (metadata only, query strings and ids redacted by `egress/redact.py`) | `python egress/classify.py` rebuilds the table from the logs |
| Hoppscotch and Rocket.Chat booted under the attestation proxy: external hosts, bytes out, signed | [selfhost/results/SELFHOST_TABLE.md](selfhost/results/SELFHOST_TABLE.md) | `bash silent/verify.sh selfhost/results/hoppscotch.attestation.json` |
| A Postman collection imported into self-hosted Hoppscotch with zero external bytes, signed | [exitkits/postman-to-hoppscotch/README.md](exitkits/postman-to-hoppscotch/README.md) | `bash silent/verify.sh exitkits/postman-to-hoppscotch/results/attestation.json` |
| The guard hooks work without node, on a fresh distro | [tests/test_guards.sh](tests/test_guards.sh), [D5_CLEANRUN.md](D5_CLEANRUN.md) | `bash tests/test_guards.sh` |

What the tables do not say: a tool that contacted no telemetry host in a ten-minute scripted session may do so in other sessions; a proxy sees only traffic that honours proxy settings. Limits are listed in each component's README.

## Sixty seconds

```bash
git clone https://github.com/ripplarity/stillherd.git && cd stillherd
bash install.sh /path/to/your/project          # hooks + the stillherd CLI into .claude/
/path/to/your/project/.claude/bin/stillherd status # leases, ledger, orphans, claims on one screen

# run any agent command with only localhost allowed, and get a signed receipt
OLLAMA_API_BASE=http://127.0.0.1:11434 bash silent/silent.sh \
  aider --model ollama_chat/qwen2.5-coder:7b --message "say hi" --yes-always --no-git
bash silent/verify.sh attestation.json          # prints OK, or which check failed (signature, key fingerprint, log hash)
```

Requirements: bash, python3, git. `silent.sh` needs `mitmdump` on PATH (`uv tool install mitmproxy`, or `pip install --user mitmproxy`); `egress/run_tool.sh` installs it itself the same way if missing. Docker is needed only for `selfhost/` and `exitkits/`.

## Five laws and the incidents that wrote them

The figures below come from the maintainer's own incident log on one shared workstation and are not reproduced in this repository.

1. **Kill-tree.** A killed parent orphaned a filesystem scan that burned 24,604 CPU-seconds over seven hours; 14 search processes leaked the same day. Law: kill process trees, leaves first, never a bare parent.
2. **GPU lease.** Two GPU jobs sharing one card took a render from 45 s/step to 1,637 s/step. Law: one heavy GPU job at a time, claimed by lease.
3. **Stash ban.** A shared stash stack let one agent pop another's entry; the work was not recoverable. Law: `git stash` is denied by hook; read other revisions with `git show` or a worktree.
4. **CPU lease.** Eleven core-pinned jobs ran unbudgeted for hours, and a headless emulator ran 8 h at 14 cores unseen. Law: heavy work claims cores from a shared budget first, and everything long-running is on the board.
5. **Claim receipts.** A worker's "done" is a claim. A hook records it, and acceptance commands verify it.

| Guardrail | What loss this prevents |
|---|---|
| Kill-tree | Ownerless runaway processes left behind by killing a parent alone. |
| Stash ban | One agent destroying another agent's uncommitted work. |
| CPU lease | Agents starving each other of cores. |
| GPU lease | Two jobs thrashing one GPU until both crawl. |
| Claim receipts | Unverified "done" claims shipping as fact. |

## How the pieces fit

- **Leases are property.** You hold cores or the GPU by claim; a stale claim expires, a live one is respected.
- **Receipts are courts.** A claim is only as good as the acceptance check that confirms it.
- **Messaging is mail.** Agents ask each other; they do not reach into each other's work.
- **Kill-tree is death.** When a process ends, everything it spawned ends with it, leaves first.
- **Silence is signed.** A run's network behaviour is a signed artefact, not a policy page.

## Verifying a receipt

`verify.sh` recomputes the log hash and checks the ed25519 signature with the public key embedded in the attestation. To pin instead of trusting the embedded key, compare `pubkey_sha256` with the maintainer fingerprint published in [silent/README.md](silent/README.md). Attestations from other installs carry their own keys. Attestation format ids written before v0.2 read `corral-silent-1`; new runs write `stillherd-silent-1`. Signed files are stored byte-exact (`.gitattributes`), so a fresh clone verifies.

## What Stillherd never does

- Kill a peer's process.
- Raise a cap to make something run.
- Push for you.
- Store request or response bodies from the tools it observes.

## Layout

| Path | Contents |
|---|---|
| `install.sh`, `merge_settings.py`, `stillherd` | Installer, settings merge, status CLI |
| `vendor/hooks/` | The hooks and tools that get installed: `proc_tree.py`, `cpu_lease.py`, `gpu_lease.py`, `run_leased.py`, `activity_ledger.py`, `claim_capture.py`, `verify_claims.py`, `git_guard.sh`, `stash_guard.sh` |
| `silent/` | Allowlist proxy, signed attestation, `verify.sh` |
| `egress/` | Egress harness, classifier, redaction, week-1 results |
| `selfhost/` | Compose recipes and boot attestations |
| `exitkits/` | Data migration into a self-hosted replacement, attested |
| `tests/` | Guard tests (run on a distro without node) |

## Licence

Apache-2.0 for everything written here. The compose recipes under `selfhost/recipes/` keep their upstream licences; see [NOTICE](NOTICE).
