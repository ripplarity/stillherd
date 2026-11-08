"""Shared check-command execution helper for verify_claims.py and the AU
Benchmark harness (score.py).

Windows defect fix (2026-09-30): `shlex.split(cmd, posix=False)` +
`shell=False` passes literal quote characters into argv on Windows (posix
tokenizer semantics don't apply), so e.g. `grep -n "numpy" requirements.txt`
searches for the literal string `"numpy"` (quotes included) and spuriously
exits 1/2. Fix: execute the (already allowlist/denylist-validated) command
through Git Bash (`bash -c <cmd>`), which performs real shell quote removal
and globbing, while `shell=False` keeps Python from adding its own shell
layer on top. This is plumbing only -- callers must run `validate_check_command`
(or equivalent) themselves BEFORE calling `run_check_via_bash`; scoring/
verification semantics are unchanged.

Bash resolution prefers a Git for Windows bash (MINGW/MSYS) over any WSL
bash that might appear earlier on PATH, since WSL bash resolves paths in a
different filesystem namespace.
"""

import os
import shutil
import subprocess
import time

_BASH_PATH_CACHE = None
_PATH_PREPEND_CACHE = None

# Candidate paths, checked in order, before falling back to PATH search.
_CANDIDATE_BASH_PATHS = [
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files\Git\usr\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
]


def _is_git_bash(path):
    """Returns True if `path` is a non-WSL (Git-for-Windows / MSYS/MINGW)
    bash: `bash --version` must mention Git-for-Windows-style GNU bash and
    `bash -c uname` must NOT report Linux (WSL reports 'Linux'; MSYS/MINGW
    report 'MINGW64_NT-...' or similar)."""
    try:
        ver = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        if "GNU bash" not in (ver.stdout or ""):
            return False
        uname = subprocess.run(
            [path, "-c", "uname"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        osname = (uname.stdout or "").strip()
        if osname.startswith("Linux"):
            return False  # WSL
        return True
    except Exception:
        return False


def resolve_bash():
    """Locate a Git-for-Windows (non-WSL) bash executable. Returns the path
    as a string, or raises RuntimeError if none can be found."""
    global _BASH_PATH_CACHE
    if _BASH_PATH_CACHE:
        return _BASH_PATH_CACHE

    for candidate in _CANDIDATE_BASH_PATHS:
        if os.path.isfile(candidate) and _is_git_bash(candidate):
            _BASH_PATH_CACHE = candidate
            return candidate

    # Fall back to PATH search, but validate each hit (WSL's bash.exe is
    # often earlier on PATH than Git's).
    found = shutil.which("bash")
    if found and _is_git_bash(found):
        _BASH_PATH_CACHE = found
        return found

    # Last resort: scan all `where bash` hits on Windows.
    try:
        where = subprocess.run(
            ["where", "bash"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
        for line in (where.stdout or "").splitlines():
            line = line.strip()
            if line and os.path.isfile(line) and _is_git_bash(line):
                _BASH_PATH_CACHE = line
                return line
    except Exception:
        pass

    raise RuntimeError(
        "No Git-for-Windows bash found (checked common install paths, PATH, "
        "and `where bash`). Install Git for Windows or set the bash path "
        "explicitly."
    )


def resolve_path_prepend():
    """Resolve directories of npm, node, python on Windows using `where`,
    and return as a PATH-separator-delimited string to prepend to PATH.
    Caches the result. Returns empty string if none found."""
    global _PATH_PREPEND_CACHE
    if _PATH_PREPEND_CACHE is not None:
        return _PATH_PREPEND_CACHE

    dirs = []
    for tool in ["npm", "node", "python"]:
        try:
            where = subprocess.run(
                ["where", tool],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            for line in (where.stdout or "").splitlines():
                line = line.strip()
                if line and os.path.isfile(line):
                    d = os.path.dirname(line)
                    if d and d not in dirs:
                        dirs.append(d)
                    break
        except Exception:
            pass

    result = os.pathsep.join(dirs)
    _PATH_PREPEND_CACHE = result
    return result


def _protect_unquoted_backslashes(cmd):
    """Wrap every unquoted "word" that contains a backslash in double quotes.

    Two things are true on this box and both matter here:
      1. bash's own parser treats a bare (unquoted) `\\x` as "drop the
         backslash, keep x literally" -- so an unquoted Windows path like
         `$HOME
         parsing, before the command even reaches argv.
      2. Separately, Git-for-Windows bash's MSYS argv->Win32-command-line
         marshalling has its own quirk: an UNQUOTED argument containing
         backslashes gets its backslashes silently dropped when exec'ing a
         native (non-MSYS) child process such as python.exe, even if bash's
         own parsing left them alone (verified empirically: `bash -c
         'prog $HOME
         "$HOME\\Users\\x`, correct).
    Wrapping the word in double quotes fixes BOTH: inside double quotes bash
    leaves a bare backslash-before-ordinary-char alone (point 1), and a
    quoted argument survives MSYS's marshalling intact (point 2). Already
    single/double-quoted regions are left untouched so real quote removal
    (e.g. `"class Brain"` -> `class Brain`) still happens as intended.

    Escaped-quote fix (2026-09-30, A1-5): within a DOUBLE-quoted region, a
    `\\"` is an escaped quote, not the end of the region -- e.g. PFICWeb's
    `package.json` CHECK (see AUDIT_v01.md's H3) is a grep pattern string
    containing several `\\"` sequences. Its quoted span ends at the FIRST
    unescaped `"`. Scanning with a naive `cmd[j] != q` mis-pairs the quotes
    partway through and leaves `test/\\*.test.mjs\\"` unquoted, which then
    gets a second, wrong pair of quotes wrapped around it by the word-scan
    branch below -- producing a command that behaves differently from
    plain bash. Single-quoted regions do NOT support backslash escaping in
    bash, so this only applies when q == '"'.
    """
    out = []
    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]
        if c.isspace():
            out.append(c)
            i += 1
            continue
        if c in ("'", '"'):
            q = c
            j = i + 1
            while j < n:
                if q == '"' and cmd[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if cmd[j] == q:
                    break
                j += 1
            end = min(j + 1, n)
            out.append(cmd[i:end])
            i = end
            continue
        j = i
        while j < n and not cmd[j].isspace() and cmd[j] not in ("'", '"'):
            j += 1
        word = cmd[i:j]
        if "\\" in word:
            escaped = word.replace('"', '\\"')
            out.append('"' + escaped + '"')
        else:
            out.append(word)
        i = j
    return "".join(out)


def run_check_via_bash(cmd, cwd, timeout_s, env_extra=None):
    """Execute an already-validated check command through Git Bash.

    Returns a dict: {cmd, exit, elapsed_s, stdout_head, stderr_head, passed}.
    Never raises for ordinary failures/timeouts (mirrors the previous
    subprocess.run(shlex.split(...)) call sites in score.py/verify_claims.py)
    -- only a missing bash executable raises (RuntimeError from
    resolve_bash()), since that's a configuration error, not a check
    failure.
    """
    bash_path = resolve_bash()
    env = dict(os.environ)

    # Prepend Windows tool directories (npm, node, python) to PATH
    # so they resolve in Git Bash (exit 127 fix).
    path_prepend = resolve_path_prepend()
    if path_prepend:
        existing_path = env.get("PATH", "")
        env["PATH"] = path_prepend + os.pathsep + existing_path

    if env_extra:
        env.update(env_extra)

    bash_cmd = _protect_unquoted_backslashes(cmd)

    start = time.time()
    try:
        proc = subprocess.run(
            [bash_path, "-c", bash_cmd],
            shell=False,
            cwd=cwd if cwd and os.path.isdir(cwd) else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=env,
        )
        elapsed = time.time() - start
        return {
            "cmd": cmd,
            "exit": proc.returncode,
            "elapsed_s": round(elapsed, 2),
            "stdout_head": (proc.stdout or "")[:500],
            "stderr_head": (proc.stderr or "")[:500],
            "passed": proc.returncode == 0,
        }
    except subprocess.TimeoutExpired:
        return {
            "cmd": cmd,
            "exit": None,
            "elapsed_s": round(time.time() - start, 2),
            "stdout_head": "",
            "stderr_head": "TIMEOUT",
            "passed": False,
        }
    except Exception as exc:
        return {
            "cmd": cmd,
            "exit": None,
            "elapsed_s": round(time.time() - start, 2),
            "stdout_head": "",
            "stderr_head": f"ERROR: {exc}",
            "passed": False,
        }
