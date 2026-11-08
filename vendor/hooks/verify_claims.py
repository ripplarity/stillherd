#!/usr/bin/env python3
"""
VF L1 runner: execute runnable checks attached to captured claims and record
the verdict. Never modifies repos (no git commands are ever run here).

Usage:
    python verify_claims.py [--dry-run] [--limit N] [--since ISO8601]
    python verify_claims.py --reverify-ids ID[,ID...]
    python verify_claims.py --reverify-failed-hook

A-VF-2 (2026-10-01) environment discipline, applied here:
  * A check runs ONLY in the agent's stated Work dir (the check's own cwd,
    which claim_capture.py takes from the prompt's "Work dir" phrase, else the
    captured task_prompt_workdir). If that directory no longer exists the
    check is recorded with skip_reason "worktree_missing" and is NEVER run
    somewhere else -- check_exec's `cwd=None` fallback silently ran such
    checks in the verifier's own process cwd and produced false failures.
  * python/pytest checks run under a pinned interpreter: the Work dir's own
    venv (`.venv`/`venv`, or the main checkout's when the Work dir is a git
    worktree) if one exists, else the interpreter running this script. The
    interpreter used is recorded on every check as "interpreter".
  * pytest always gets `-p no:cacheprovider` so verification never writes a
    `.pytest_cache` into a repo and never reads a stale one.
  * An acceptance phrased as alternatives ("`python -m pytest -q` exits 0 ...
    or `npm test` if the repo is Node") is adjudicated any-of: the claim
    survives if any applicable alternative exits 0; an alternative that is
    inapplicable to the repo (npm test with no package.json / no test script,
    pytest collecting zero tests) is recorded as skipped, not failed.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

# Import allowlist and denylist from claim_capture
from claim_capture import ALLOWLIST_PREFIXES  # noqa: F401
from claim_capture import command_denylisted  # noqa: F401
from claim_capture import ANY_OF_ACCEPTANCE_RE
from check_exec import run_check_via_bash

COORD_DIR = os.environ.get(
    "CLAIMS_DIR",
    os.path.join(os.path.expanduser("~"), ".claude", "coord"),
)
CLAIMS_PATH = os.path.join(COORD_DIR, "claims.jsonl")
VERIFIED_PATH = os.path.join(COORD_DIR, "claims_verified.jsonl")
STATUS_PATH = os.path.join(COORD_DIR, "claims_status.jsonl")

CHECK_TIMEOUT_S = int(os.environ.get("VF_CHECK_TIMEOUT_S", "300"))
CHECK_ENV_EXTRA = {
    "PRIMITIVE_MESH_OFFLINE": "1",
    "LLM_PROVIDER": "fake",
    "NO_NETWORK": "1",
}

# pytest exit code 5 == "no tests were collected".
PYTEST_NO_TESTS_EXIT = 5


def read_jsonl(path):
    rows = []
    if not os.path.isfile(path):
        return rows
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def append_jsonl(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def already_verified_ids():
    ids = set()
    for row in read_jsonl(VERIFIED_PATH):
        rid = row.get("id")
        if rid:
            ids.add(rid)
    for row in read_jsonl(STATUS_PATH):
        rid = row.get("id")
        if rid and row.get("status") == "verified":
            ids.add(rid)
    return ids


def latest_status_by_id():
    """Read claims_status.jsonl and return {id: last_status}, last write wins."""
    latest = {}
    for row in read_jsonl(STATUS_PATH):
        rid = row.get("id")
        status = row.get("status")
        if rid and status:
            latest[rid] = status
    return latest


# Matches an absolute Windows path (C:\foo\bar or C:/foo/bar), used as a
# last-resort cwd fallback when scanning task_prompt_head for a usable
# directory hint.
_ABS_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/](?:[^\s`\"']+)")

# Matches paths following specific phrases: "Work dir", "Working dir",
# "work dir:", "cwd", "run from", "from the ... dir"
_PROMPT_WORKDIR_PHRASES_RE = re.compile(
    r"(?:Work\s+dir|Working\s+dir|work\s+dir|cwd|run\s+from|from\s+the\s+\w+\s+dir)"
    r"\s*[:=]?\s*[`\"']?([A-Za-z]:\\[^\s`\"'\n]+|/[^\s`\"'\n]+)",
    re.IGNORECASE,
)

# Home/Development base used to resolve a claim's "project" field (e.g.
# "<project>") to a real directory on disk.
_DEV_ROOT = os.path.join(os.path.expanduser("~"), "Development")

_GITDIR_RE = re.compile(r"^gitdir:\s*(.+?)\s*$")

_PYTHON_CMD_RE = re.compile(r"^python(?:3)?(?=\s)")
_PYTEST_CMD_RE = re.compile(r"^pytest(?=\s|$)")


def _validate_check_command(cmd):
    """Validate that a command is allowed. Returns (is_allowed, reason)."""
    if command_denylisted(cmd):
        return False, "DENYLIST_MATCH"
    if not any(cmd.startswith(p) for p in ALLOWLIST_PREFIXES):
        return False, "NOT_ALLOWLISTED"
    return True, None


def stated_workdir(check, row):
    """The agent's STATED Work dir for a check, whether or not it exists:
    the check's own cwd (claim_capture.py fills it from the prompt's
    "Work dir" phrase), else the captured task_prompt_workdir. Returns
    (path_or_None, source_label)."""
    cwd = check.get("cwd")
    if cwd:
        return str(cwd), "explicit"
    tpw = row.get("task_prompt_workdir")
    if tpw:
        return str(tpw), "prompt_workdir"
    return None, None


def resolve_fallback_cwd(row):
    """When a check has NO stated Work dir at all, find a usable directory
    to run it in. Returns (cwd_or_None, source_label).

    Order: (b) path following specific phrases in task_prompt_head;
    (c) first absolute Windows path anywhere in task_prompt that is an
    existing directory; (d) capture_cwd; (e) ~/Development/<project> if
    exists; (f) skip with reason no_cwd. Records cwd_source on each result.
    (task_prompt_workdir, when present, is handled by stated_workdir() and
    is not a fallback: if it is stated and missing the check is skipped as
    worktree_missing rather than run somewhere else.)
    """
    task_prompt_workdir = row.get("task_prompt_workdir")
    if task_prompt_workdir and os.path.isdir(str(task_prompt_workdir)):
        return str(task_prompt_workdir), "prompt_workdir"

    head = row.get("task_prompt_head") or ""
    m = _PROMPT_WORKDIR_PHRASES_RE.search(head)
    if m:
        p = m.group(1).strip().rstrip(".,:;")
        if p and os.path.isdir(p):
            return p, "prompt_workdir"

    # (c) first absolute Windows path anywhere in task_prompt that is an
    # existing directory
    m = _ABS_WIN_PATH_RE.search(head)
    if m:
        p = m.group(0).rstrip(".,:;")
        if os.path.isfile(p):
            p = os.path.dirname(p)
        if p and os.path.isdir(p):
            return p, "prompt_path"

    # (d) capture_cwd
    capture_cwd = row.get("capture_cwd")
    if capture_cwd and os.path.isdir(str(capture_cwd)):
        return str(capture_cwd), "capture_cwd"

    # (e) ~/Development/<project> if exists
    project = row.get("project")
    if project:
        candidate = os.path.join(_DEV_ROOT, str(project))
        if os.path.isdir(candidate):
            return candidate, "project"

    # (f) skip with reason no_cwd
    return None, "no_cwd"


def _venv_python_in(root):
    for rel in (
        (".venv", "Scripts", "python.exe"),
        ("venv", "Scripts", "python.exe"),
        (".venv", "bin", "python"),
        ("venv", "bin", "python"),
    ):
        p = os.path.join(root, *rel)
        if os.path.isfile(p):
            return p
    return None


def _worktree_main_checkout(cwd):
    """If `cwd` is a git WORKTREE (its `.git` is a file, not a directory,
    pointing at `<main>/.git/worktrees/<name>`), return `<main>`; else None.
    Reads the file directly -- no git command is run."""
    dotgit = os.path.join(cwd, ".git")
    if not os.path.isfile(dotgit):
        return None
    try:
        with open(dotgit, "r", encoding="utf-8", errors="replace") as f:
            first = f.readline()
    except Exception:
        return None
    m = _GITDIR_RE.match(first)
    if not m:
        return None
    gitdir = os.path.normpath(m.group(1))
    # <main>/.git/worktrees/<name>  ->  <main>
    parts = gitdir.replace("/", os.sep).split(os.sep)
    if len(parts) >= 3 and parts[-2] == "worktrees" and parts[-3] == ".git":
        main = os.sep.join(parts[:-3])
        if main and os.path.isdir(main):
            return main
    return None


_PYTEST_CONFIG_FILES = ("pytest.ini", "tox.ini", "setup.cfg", "conftest.py")
_PYTEST_SUBDIR_CANDIDATES = ("Backend", "backend", "api", "server", "python")


def _has_pytest_root_markers(d):
    for name in _PYTEST_CONFIG_FILES:
        if os.path.isfile(os.path.join(d, name)):
            return True
    if os.path.isdir(os.path.join(d, "tests")):
        return True
    pp = os.path.join(d, "pyproject.toml")
    if os.path.isfile(pp):
        try:
            with open(pp, "r", encoding="utf-8", errors="replace") as f:
                if "[tool.pytest" in f.read():
                    return True
        except Exception:
            pass
    return False


def resolve_pytest_root(cwd):
    """A repo whose Work dir is a monorepo ROOT with no pytest config or
    tests/ of its own, but exactly one conventional python subdir
    (Backend/ ...) that has them, runs pytest from that subdir -- that is
    where the agent ran it and where pyproject's [tool.pytest] applies.
    Returns (dir, adjusted_bool)."""
    if _has_pytest_root_markers(cwd):
        return cwd, False
    hits = [
        os.path.join(cwd, sub)
        for sub in _PYTEST_SUBDIR_CANDIDATES
        if os.path.isdir(os.path.join(cwd, sub))
        and _has_pytest_root_markers(os.path.join(cwd, sub))
    ]
    # case-insensitive filesystems make "Backend" and "backend" one dir
    uniq = {}
    for h in hits:
        uniq.setdefault(os.path.normcase(os.path.realpath(h)), h)
    if len(uniq) == 1:
        return next(iter(uniq.values())), True
    return cwd, False


def resolve_interpreter(cwd):
    """Pinned Python interpreter for a check in `cwd`: the Work dir's own
    venv, then the main checkout's venv when `cwd` is a worktree, then the
    interpreter running this script. Returns (path, source_label)."""
    if cwd and os.path.isdir(cwd):
        for root in (cwd, os.path.join(cwd, "Backend")):
            p = _venv_python_in(root)
            if p:
                return p, "workdir_venv"
        main = _worktree_main_checkout(cwd)
        if main:
            for root in (main, os.path.join(main, "Backend")):
                p = _venv_python_in(root)
                if p:
                    return p, "main_checkout_venv"
    return sys.executable, "system"


def pin_command(cmd, interpreter):
    """Rewrite `cmd` so python/pytest invocations use `interpreter` and
    pytest never touches a repo's .pytest_cache. Non-python commands are
    returned unchanged. Returns the rewritten command string."""
    out = cmd.strip()
    quoted = '"' + interpreter.replace('"', '\\"') + '"'
    if _PYTHON_CMD_RE.match(out):
        out = quoted + out[_PYTHON_CMD_RE.match(out).end() :]
    elif _PYTEST_CMD_RE.match(out):
        out = quoted + " -m pytest" + out[_PYTEST_CMD_RE.match(out).end() :]
    if re.search(r"(?:^|\s)(?:-m\s+pytest|pytest)(?:\s|$)", out) and (
        "no:cacheprovider" not in out
    ):
        out = out + " -p no:cacheprovider"
    return out


def is_any_of_acceptance(row):
    if row.get("check_mode") == "any_of":
        return True
    acc = row.get("acceptance") or ""
    return bool(ANY_OF_ACCEPTANCE_RE.search(acc))


def _inapplicable_reason(cmd, cwd, result):
    """Under any-of adjudication, decide whether a failed alternative was
    simply inapplicable to this repo. Returns a skip-reason label or None."""
    c = cmd.strip()
    if c.startswith("npm test") or c.startswith("npm run"):
        if not os.path.isfile(os.path.join(cwd, "package.json")):
            return "inapplicable_not_node"
        blob = (result.get("stderr_head") or "") + (result.get("stdout_head") or "")
        if "Missing script" in blob or "no test specified" in blob:
            return "inapplicable_no_test_script"
        return None
    if "pytest" in c and result.get("exit") == PYTEST_NO_TESTS_EXIT:
        return "inapplicable_no_tests"
    return None


def run_check(cmd, cwd):
    start = time.time()

    # Validate command before running
    is_allowed, reason = _validate_check_command(cmd)
    if not is_allowed:
        elapsed = time.time() - start
        return {
            "cmd": cmd,
            "cwd": cwd,
            "exit": None,
            "elapsed_s": round(elapsed, 2),
            "stdout_head": "",
            "stderr_head": f"SKIPPED_DISALLOWED: {reason}",
            "skip_reason": "DISALLOWED",
            "skipped_reason": "DISALLOWED",
        }

    # A-VF-2: never run a check anywhere but the stated/resolved directory.
    if not cwd or not os.path.isdir(cwd):
        return {
            "cmd": cmd,
            "cwd": cwd,
            "exit": None,
            "elapsed_s": 0,
            "stdout_head": "",
            "stderr_head": "SKIPPED_WORKTREE_MISSING",
            "skip_reason": "worktree_missing",
            "skipped_reason": "worktree_missing",
        }

    run_dir, adjusted = cwd, False
    if re.search(
        r"(?:^|\s)(?:-m\s+pytest|pytest)(?:\s|$)", cmd.strip()
    ) and not re.search(r"\S+\.py\b|\btests?[/\\]", cmd):
        # bare suite run (no explicit path argument): locate the pytest root
        run_dir, adjusted = resolve_pytest_root(cwd)
    interpreter, interp_source = resolve_interpreter(run_dir)
    cmd_executed = pin_command(cmd, interpreter)

    result = run_check_via_bash(
        cmd_executed, run_dir, CHECK_TIMEOUT_S, env_extra=CHECK_ENV_EXTRA
    )
    out = {
        "cmd": cmd,
        "cmd_executed": cmd_executed,
        "interpreter": interpreter,
        "interpreter_source": interp_source,
        "cwd": run_dir,
        "stated_cwd": cwd,
        "cwd_adjusted": adjusted,
        "exit": result["exit"],
        "elapsed_s": result["elapsed_s"],
        "stdout_head": result["stdout_head"],
        "stderr_head": result["stderr_head"],
    }
    # A-VF-2: the verifier giving up (timeout / launcher error) is a
    # verifier-environment outcome, not evidence against the claim.
    if result["exit"] is None:
        head = result.get("stderr_head") or ""
        reason = "verifier_timeout" if head == "TIMEOUT" else "verifier_error"
        out["skip_reason"] = reason
        out["skipped_reason"] = reason
        out["timeout_s"] = CHECK_TIMEOUT_S
    return out


def verify_row(row, dry_run=False, status_row=None):
    checks_spec = row.get("runnable_checks") or []

    # If status_row has re-extracted checks, use them instead
    if status_row and status_row.get("status") == "checks_reextracted":
        checks_spec = status_row.get("runnable_checks") or checks_spec

    # Filter out checks marked as non-runnable (e.g., those with placeholders)
    runnable = [
        c for c in checks_spec if c.get("cmd") and c.get("runnable") is not False
    ]
    placeholders = [
        c for c in checks_spec if c.get("cmd") and c.get("runnable") is False
    ]
    any_of = is_any_of_acceptance(row)
    results = []

    if not runnable:
        verdict = "unverifiable"
        for c in placeholders:
            results.append(
                {
                    "cmd": c.get("cmd"),
                    "cwd": c.get("cwd"),
                    "exit": None,
                    "elapsed_s": 0,
                    "stdout_head": "",
                    "stderr_head": "SKIPPED_PLACEHOLDER",
                    "skip_reason": "command_placeholder",
                    "skipped_reason": "command_placeholder",
                }
            )
    else:
        if dry_run:
            for c in runnable:
                results.append(
                    {
                        "cmd": c.get("cmd"),
                        "cwd": c.get("cwd"),
                        "exit": None,
                        "elapsed_s": 0,
                        "stdout_head": "",
                        "stderr_head": "DRY-RUN",
                    }
                )
            verdict = "unverifiable"
        else:
            for c in runnable:
                cwd, cwd_source = stated_workdir(c, row)
                if cwd and not os.path.isdir(cwd):
                    # Stated Work dir no longer exists: record, never
                    # relocate the check (A-VF-2).
                    print(
                        f"SKIP id={row.get('id')} cmd={c.get('cmd')!r} "
                        f"reason=worktree_missing ({cwd_source} cwd {cwd!r} "
                        f"does not exist)"
                    )
                    results.append(
                        {
                            "cmd": c.get("cmd"),
                            "cwd": cwd,
                            "cwd_source": cwd_source,
                            "exit": None,
                            "elapsed_s": 0,
                            "stdout_head": "",
                            "stderr_head": "SKIPPED_WORKTREE_MISSING",
                            "skip_reason": "worktree_missing",
                            "skipped_reason": "worktree_missing",
                        }
                    )
                    continue
                if not cwd:
                    cwd, cwd_source = resolve_fallback_cwd(row)
                if not cwd:
                    # No cwd resolvable by any fallback -- record the check
                    # as skipped (never silently drop it from "checks").
                    print(
                        f"SKIP id={row.get('id')} cmd={c.get('cmd')!r} "
                        f"reason=no_cwd (no explicit cwd, capture_cwd, "
                        f"resolvable project dir, or path hint in prompt)"
                    )
                    results.append(
                        {
                            "cmd": c.get("cmd"),
                            "cwd": None,
                            "exit": None,
                            "elapsed_s": 0,
                            "stdout_head": "",
                            "stderr_head": "SKIPPED_NO_CWD",
                            "skip_reason": "no_cwd",
                            "skipped_reason": "no_cwd",
                        }
                    )
                    continue
                result = run_check(c["cmd"], cwd)
                result["cwd_source"] = cwd_source
                if (
                    any_of
                    and result.get("exit") not in (0, None)
                    and not result.get("skip_reason")
                ):
                    reason = _inapplicable_reason(c["cmd"], cwd, result)
                    if reason:
                        result["skip_reason"] = reason
                        result["skipped_reason"] = reason
                if result.get("skip_reason") or result.get("skipped_reason"):
                    print(
                        f"SKIP id={row.get('id')} cmd={c.get('cmd')!r} "
                        f"reason={result.get('skip_reason')}"
                    )
                results.append(result)
            if not results:
                verdict = "unverifiable"
            else:
                # Exclude skipped checks from verdict calculation
                runnable_results = [
                    r
                    for r in results
                    if not r.get("skip_reason") and not r.get("skipped_reason")
                ]
                if not runnable_results:
                    verdict = "unverifiable"
                elif any_of:
                    ok = any(r.get("exit") == 0 for r in runnable_results)
                    verdict = "survived" if ok else "failed"
                else:
                    ok = all(r.get("exit") == 0 for r in runnable_results)
                    verdict = "survived" if ok else "failed"

    out = {
        "id": row.get("id"),
        "verified_ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "checks": results,
        "verdict": verdict,
        "claimed_done": row.get("claimed_done"),
    }
    if any_of:
        out["check_mode"] = "any_of"
    skips = sorted({r.get("skip_reason") for r in results if r.get("skip_reason")})
    if skips:
        out["skip_reasons"] = skips
    return out


def maybe_acquire_cpu_lease(n_checks):
    if n_checks <= 3:
        return None
    lease_script = os.path.join(COORD_DIR, "cpu_lease.py")
    if not os.path.isfile(lease_script):
        return None
    try:
        proc = subprocess.run(
            [
                sys.executable,
                lease_script,
                "acquire",
                "--project",
                "VF",
                "--purpose",
                "verify_claims",
                "--cores",
                "4",
                "--eta",
                "1800",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
    except Exception:
        pass
    return None


def maybe_release_cpu_lease(lease_id):
    if not lease_id:
        return
    lease_script = os.path.join(COORD_DIR, "cpu_lease.py")
    if not os.path.isfile(lease_script):
        return
    try:
        subprocess.run(
            [sys.executable, lease_script, "release", "--id", lease_id],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        pass


def latest_hook_failed_ids():
    """Ids whose LATEST verified row (non-benchmark) has verdict failed."""
    latest = {}
    for v in read_jsonl(VERIFIED_PATH):
        if v.get("source") == "au_benchmark":
            continue
        rid = v.get("id")
        if rid:
            latest[rid] = v
    return [rid for rid, v in latest.items() if v.get("verdict") == "failed"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--since", type=str, default=None)
    parser.add_argument(
        "--reverify-ids",
        type=str,
        default=None,
        help="Comma-separated claim ids to re-verify (appends a new verified "
        "row with reverify=true; never rewrites).",
    )
    parser.add_argument(
        "--reverify-failed-hook",
        action="store_true",
        help="Re-verify every hook-captured claim whose latest verdict is failed.",
    )
    args = parser.parse_args()

    rows = read_jsonl(CLAIMS_PATH)
    done_ids = already_verified_ids()
    status_by_id = latest_status_by_id()

    # Read full status rows to pass re-extracted checks to verify_row
    status_rows = read_jsonl(STATUS_PATH)
    status_row_by_id = {}
    for sr in status_rows:
        rid = sr.get("id")
        if rid:
            status_row_by_id[rid] = sr

    reverify = bool(args.reverify_ids or args.reverify_failed_hook)
    if reverify:
        wanted = set()
        if args.reverify_ids:
            wanted.update(x.strip() for x in args.reverify_ids.split(",") if x.strip())
        if args.reverify_failed_hook:
            wanted.update(latest_hook_failed_ids())
        # last claim row per id wins (ids can collide; the newest capture is
        # the one the latest verified row refers to)
        by_id = {}
        for r in rows:
            if r.get("id") in wanted:
                by_id[r["id"]] = r
        pending = [
            r
            for r in by_id.values()
            if status_by_id.get(r.get("id")) != "invalid_parent_capture"
        ]
    else:
        pending = [
            r
            for r in rows
            if r.get("status") == "pending"
            and r.get("id") not in done_ids
            and status_by_id.get(r.get("id")) != "invalid_parent_capture"
        ]
    if args.since:
        pending = [r for r in pending if (r.get("ts") or "") >= args.since]
    if args.limit:
        pending = pending[: args.limit]

    total_checks = sum(len(r.get("runnable_checks") or []) for r in pending)
    lease_id = None
    if not args.dry_run and total_checks > 3:
        lease_id = maybe_acquire_cpu_lease(total_checks)

    n_verified = 0
    try:
        for row in pending:
            row_id = row.get("id")
            status_row = status_row_by_id.get(row_id)
            result = verify_row(row, dry_run=args.dry_run, status_row=status_row)
            if reverify:
                result["reverify"] = True
                result["reverify_reason"] = "A-VF-2"
            append_jsonl(VERIFIED_PATH, result)
            append_jsonl(
                STATUS_PATH,
                {"id": row_id, "status": "reverified" if reverify else "verified"},
            )
            n_verified += 1
            print(
                f"{'RE' if reverify else ''}VERIFIED id={row_id} "
                f"verdict={result['verdict']} "
                f"exits={[c.get('exit') for c in result['checks']]} "
                f"skips={result.get('skip_reasons')}"
            )
    finally:
        if lease_id:
            maybe_release_cpu_lease(lease_id)

    print(
        f"verified {n_verified} claim(s) of {len(pending)} pending "
        f"(dry_run={args.dry_run}, reverify={reverify})"
    )


if __name__ == "__main__":
    main()
