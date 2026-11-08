# D5 clean run

Fresh Ubuntu 24.04 WSL distro (`corral-d5`, official image), the repo copied in with `git archive` of a commit (not the working tree), run as a stranger would. Host Ollama reached over the distro's default gateway. Distro unregistered at the end. Driver: `d5_run.sh` (lease: 4 cores, eta 2400); in-distro steps: `d5_inner.sh`.

Provisioned in the distro: curl, ca-certificates, python3, python3-venv, python3-pip, git, and a venv with mitmproxy, cryptography and aider-chat. No node.

## Failures and the fix applied to the repo

| # | Symptom in the clean distro | Cause | Fix |
|---|---|---|---|
| 1 | `install.sh` could not copy hooks (first run on the earlier revision, which sourced files from the installer's own `~/.claude`) | install.sh assumed the maintainer's `~/.claude/coord` and `~/.claude/scripts` exist on every machine | Hooks are now vendored in `vendor/hooks/` (scrubbed); `install.sh` copies from there. `vendor.sh` refreshes them on a maintainer machine only. |
| 2 | `install.sh: line 3: set: -: invalid option`, `silent.sh` and `verify.sh` syntax errors (`$'do\r'`) | `git archive` emitted CRLF line endings on a Windows checkout | `.gitattributes` forces `eol=lf` for the tree, so archives and checkouts carry LF. |
| 3 | settings.json hook command was `python ...`; `python` does not exist on Ubuntu | hard-coded interpreter name | `install.sh` picks `python3` or `python` at install time and `merge_settings.py` writes that into the hook command. |
| 4 | Warning: node not found | `git_guard.sh` and `stash_guard.sh` call node helper scripts and allow everything when node is missing | `install.sh` now prints a warning; documented as a requirement. Not fixed: the guards still fail open without node. |
| 5 | `corral status`: "COULD NOT READ THE PROCESS TABLE" | the orphan check reads the Windows process table | Not fixed: the orphan section is Windows-only; the command still exits 0 and says the status is unknown. |

Requirements a stranger must install for `silent/`: `mitmproxy`, `python3-cryptography` (or pip), the tool under test, and the host Ollama address in `SILENT_ALLOW_EXTRA` (without it the model server is blocked as an external host).

## First run on the earlier revision (before fixes 1 to 3)

All four commands failed (rc=2, rc=127, rc=2, rc=2 in order; no hook files installed). Output in `egress/data/d5_e5409c3.txt` (untracked).

## FINAL run (fresh distro, revision 5479ba4)

```
$ bash install.sh ~/proj
WARNING: node not found; git_guard.sh and stash_guard.sh need it and will fail open (allow everything) without it. Install nodejs.
corral 0.1.0 installed to /root/proj/.claude
rc=0
$ ~/proj/.claude/bin/corral status
corral status
== CPU leases / GPU lease / Ledger: empty or free
== Orphan candidates: proc_tree could not read the process table (status unknown)
rc=0
$ bash silent/silent.sh aider --model ollama_chat/qwen2.5-coder:7b --message "say hi" --yes-always --no-git
Understood. ... Tokens: 594 sent, 39 received.
attestation: /root/corral/attestation.json attempts=3 external_bytes_sent=0
rc=0
$ bash silent/verify.sh attestation.json
OK
rc=0
```

## D6: guards without node (fresh distro, revision cabfa43, no node installed)

Failure 4 above is fixed: `vendor/hooks/git_guard.sh` and `vendor/hooks/stash_guard.sh` are rewritten (vendored copies only; originals untouched). They read the hook JSON with python3 (sed fallback if python is missing) and decide in bash. Behaviour kept: `git stash` denied except `list`/`show`; `git_guard.sh` keeps its secret scan (deny) and main/master confirmation (ask). The three node helper scripts are no longer vendored. `vendor.sh` preserves the two rewritten guards when refreshing.

`tests/test_guards.sh` pipes 6 fixtures (stash push, stash pop, stash list, plain commit, push with a runtime-built fake AWS-style key, harmless ls) through both guards.

```
$ command -v node || echo "node: absent"
node: absent
rc=0
$ bash tests/test_guards.sh
ok   stash push -> deny
ok   stash pop -> deny
ok   stash list -> allow
ok   plain commit -> allow
ok   push w/ key -> deny
ok   harmless ls -> allow
6/6 passed
rc=0
$ PATH=/tmp/nopy bash tests/test_guards.sh      # python3 not on PATH: sed fallback
(same six results)
6/6 passed
rc=0
```

Host run: `6/6 passed`. The distro was unregistered after the run.
