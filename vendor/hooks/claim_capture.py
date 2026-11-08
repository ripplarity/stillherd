#!/usr/bin/env python3
"""
SubagentStop hook: capture a subagent's final "done" claim + its acceptance
check (if any), for the Verification Flywheel (VF) L1 layer.

Reads the hook JSON payload from stdin, extracts the task prompt (first user
message in the transcript) and the final assistant message, pulls out any
ACCEPTANCE block, finds machine-runnable check commands in it (allowlisted
prefixes only, denylisted destructive/network verbs excluded), and appends
one JSON line to claims.jsonl.

MUST NEVER exit non-zero and must never raise past main() -- this hook is not
allowed to block a subagent from completing.
"""

import hashlib
import json
import os
import re
import sys
import time

try:
    import msvcrt
except ImportError:
    msvcrt = None

COORD_DIR = os.environ.get(
    "CLAIMS_DIR",
    os.path.join(os.path.expanduser("~"), ".claude", "coord"),
)
CLAIMS_PATH = os.path.join(COORD_DIR, "claims.jsonl")
CLAIMS_DUPS_LOG_PATH = os.path.join(COORD_DIR, "claims_dups.log")
HOOK_KEYS_LOG_PATH = os.path.join(COORD_DIR, "claims_hook_keys.log")

TASKS_ROOT = os.environ.get(
    "CLAUDE_TASKS_ROOT",
    os.path.join(os.path.expanduser("~"), "AppData", "Local", "Temp", "claude"),
)

# How recent a tasks/*.output file's mtime must be to be considered "this
# invocation's" transcript when falling back to recency-based discovery.
TASKS_DIR_RECENT_WINDOW_S = 120

AGENT_TRANSCRIPT_FIELD_CANDIDATES = [
    "agent_transcript_path",
    "subagent_transcript_path",
    "agent_transcript",
]

ALLOWLIST_PREFIXES = [
    "python -m pytest",
    "python -m unittest",
    "pytest",
    "npx vitest",
    "npx playwright test",
    "npm test",
    "npm run lint",
    "ruff ",
    "eslint",
    "ls ",
    "test -f",
    "test -d",
    "test ! -f",
    "test ! -d",
    "grep ",
    "rg ",
    "wc ",
    "Get-ScheduledTask",
    "diff ",
    "cargo test",
    "go test",
]

# A1 (AU_Benchmark harness) T1-public: a CHECK may invoke a per-repo venv's
# own interpreter by absolute path (e.g.
# `"C:\...\p1-00-foo\.venv\Scripts\python.exe" -m pytest -q`) instead of the
# bare `python -m pytest` prefix -- the allowlist is otherwise prefix-based
# on the literal string "python -m pytest"/"pytest", which never matches an
# absolute interpreter path. Matches an optionally-quoted path ending in
# `python`/`python.exe`/`python3` followed by `-m pytest` or `-m unittest`.
ALLOWLIST_VENV_PYTHON_RE = re.compile(
    r'^"?[^"\s][^"]*?[\\/]python(?:\.exe|3)?"?\s+-m\s+(?:pytest|unittest)\b',
    re.IGNORECASE,
)

# Structural tokens (pipe/subshell/chain/redirect/backtick) are denied
# ANYWHERE in the command -- these can appear in any position and always
# change what actually runs.
DENYLIST_STRUCTURAL_RE = re.compile(r"(&&|\|\||;|\||>|<|`|\$\()")

# The verb denylist (destructive/network/deploy-ish words) is scoped to the
# command's first token and to any argument immediately following an
# interpreter flag (-c / -e / -Command) by command_denylisted() below -- a
# bare DENYLIST_RE.search() over the whole command string also matches
# these words inside ordinary filenames or grep patterns (e.g.
# `publish-pypi.yml`, `send_text`), which is a false positive, not a
# destructive command. Callers that need the full (structural + scoped
# verb) check must call command_denylisted(cmd), not DENYLIST_RE.search().
DENYLIST_WORD_RE = re.compile(
    r"\b(deploy|publish|push|send|stripe|payment|curl|wget|Invoke-WebRequest|rm|Remove-Item|del)\b",
    re.IGNORECASE,
)

# Backward-compatible alias: DENYLIST_RE now matches structural tokens only.
# Kept for any caller that just wants the "definitely unsafe regardless of
# position" check; full command validation goes through command_denylisted().
DENYLIST_RE = DENYLIST_STRUCTURAL_RE

_INTERPRETER_FLAG_TOKENS = {"-c", "-e", "-Command"}


def _verb_check_tokens(cmd):
    """Tokens of `cmd` that the verb denylist should actually be checked
    against: the first token (the program/command name) and any argument
    immediately following -c/-e/-Command (an inline script/command
    string), not every word in the command line."""
    parts = cmd.split()
    if not parts:
        return []
    toks = [parts[0]]
    for i, p in enumerate(parts):
        if p in _INTERPRETER_FLAG_TOKENS and i + 1 < len(parts):
            toks.append(parts[i + 1])
    return toks


def is_allowlisted_check(cmd):
    """True if `cmd` matches ALLOWLIST_PREFIXES OR the venv-python-by-
    absolute-path rule (ALLOWLIST_VENV_PYTHON_RE). Shared by every call
    site that previously did `any(cmd.startswith(p) for p in
    ALLOWLIST_PREFIXES)` directly -- do not re-inline that check, it misses
    the venv rule."""
    if any(cmd.startswith(p) for p in ALLOWLIST_PREFIXES):
        return True
    return bool(ALLOWLIST_VENV_PYTHON_RE.match(cmd))


def command_denylisted(cmd):
    """True if `cmd` is denylisted: structural tokens anywhere, OR a
    denylisted verb as the first token / as a -c/-e/-Command argument."""
    if DENYLIST_STRUCTURAL_RE.search(cmd):
        return True
    for tok in _verb_check_tokens(cmd):
        if DENYLIST_WORD_RE.search(tok):
            return True
    return False


DONE_RE = re.compile(
    r"\b(done|complete[d]?|all (acceptance )?criteria met|passed|verified)\b",
    re.IGNORECASE,
)

NEGATION_RE = re.compile(
    r"\b(not|cannot|couldn't|unable|failed to|blocked)\b",
    re.IGNORECASE,
)


def is_declared_done(text):
    """Check if text matches a done-pattern, but NOT if a negation appears
    within 6 words before the matched term.

    Examples:
    - "All acceptance criteria met" -> True
    - "The acceptance criterion cannot be met" -> False
    - "Cannot do the task, help me" -> False (negation before 'help')
    """
    if not text:
        return False

    for match in DONE_RE.finditer(text):
        done_pos = match.start()
        # Extract text up to this match (last 200 chars to be safe)
        context = text[max(0, done_pos - 200) : done_pos]

        # Check if there's a negation within the last 6 words
        words = context.split()
        last_words = words[-6:] if len(words) >= 6 else words
        negation_context = " ".join(last_words).lower()

        if NEGATION_RE.search(negation_context):
            continue  # This done-pattern is negated, skip it

        # Found a done-pattern with no negation nearby
        return True

    return False


ACCEPTANCE_RE = re.compile(r"ACCEPTANCE", re.IGNORECASE)

# An acceptance phrased as alternatives, e.g.
#   "`python -m pytest -q` exits 0 in the Work dir (or `npm test` if the
#    repo is Node; state which)"
# Its runnable checks are adjudicated any-of by verify_claims.py (A-VF-2):
# requiring BOTH to exit 0 manufactured a failure on every Python repo
# (npm test with no package.json) and every Node repo (pytest: no tests).
ANY_OF_ACCEPTANCE_RE = re.compile(
    r"\b(?:or|otherwise)\s+`[^`]+`\s+(?:if|when|for)\b", re.IGNORECASE
)

CWD_HINT_RE = re.compile(
    r"(?:Work dir|Working directory|cwd|run from)\s*[:=]?\s*[`\"']?([A-Za-z]:\\[^\s`\"'\n]+|/[^\s`\"'\n]+)",
    re.IGNORECASE,
)

# Matches paths following specific phrases: "Work dir", "Working dir",
# "work dir:", "cwd", "run from", "from the ... dir"
PROMPT_WORKDIR_PHRASES_RE = re.compile(
    r"(?:Work\s+dir|Working\s+dir|work\s+dir|cwd|run\s+from|from\s+the\s+\w+\s+dir)"
    r"\s*[:=]?\s*[`\"']?([A-Za-z]:\\[^\s`\"'\n]+|/[^\s`\"'\n]+)",
    re.IGNORECASE,
)

# Matches absolute Windows paths
ABS_WIN_PATH_RE = re.compile(r"[A-Za-z]:[\\/](?:[^\s`\"']+)")

CODEBLOCK_RE = re.compile(r"```(?:[a-zA-Z0-9_-]*)\n(.*?)```", re.DOTALL)
BACKTICK_RE = re.compile(r"`([^`\n]+)`")

# Matches a "CLAIM: <text>" line immediately followed (possibly after blank
# lines) by a "CHECK: <cmd>" line -- the benchmark task-card format used by
# AU_Benchmark (see PROMPT.txt) and reused verbatim by subagent final
# messages that report claim/check pairs.
CLAIM_CHECK_RE = re.compile(
    r"^[ \t>*-]*CLAIM:\s*(?P<claim>.+?)\s*\n"
    r"(?:^[ \t>*-]*\n)*"
    r"^[ \t>*-]*CHECK:\s*(?P<check>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def read_stdin_json():
    try:
        raw = sys.stdin.read()
        if not raw or not raw.strip():
            return {}
        return json.loads(raw)
    except Exception:
        return {}


def load_transcript_messages(path):
    """Return list of (role, text) tuples parsed from a transcript JSONL."""
    msgs = []
    if not path or not os.path.isfile(path):
        return msgs
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                msgs.append(obj)
    except Exception:
        pass
    return msgs


def extract_text(content):
    """Extract plain text from an Anthropic-style message content field."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif "text" in block:
                    parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return str(content)


def find_first_user_message(msgs):
    for obj in msgs:
        msg = obj.get("message") if isinstance(obj, dict) else None
        role = None
        content = None
        if isinstance(msg, dict):
            role = msg.get("role")
            content = msg.get("content")
        elif isinstance(obj, dict):
            role = obj.get("role")
            content = obj.get("content")
        if role == "user":
            text = extract_text(content)
            if text:
                return text
    return ""


def find_last_assistant_message(msgs):
    last_text = ""
    for obj in msgs:
        msg = obj.get("message") if isinstance(obj, dict) else None
        role = None
        content = None
        if isinstance(msg, dict):
            role = msg.get("role")
            content = msg.get("content")
        elif isinstance(obj, dict):
            role = obj.get("role")
            content = obj.get("content")
        if role == "assistant":
            text = extract_text(content)
            if text:
                last_text = text
    return last_text


def extract_acceptance(task_prompt):
    if not task_prompt:
        return None
    lines = task_prompt.splitlines()
    for i, line in enumerate(lines):
        if ACCEPTANCE_RE.search(line):
            block = "\n".join(lines[i : i + 40])
            return block.strip()
    return None


def extract_cwd(task_prompt):
    if not task_prompt:
        return None
    m = CWD_HINT_RE.search(task_prompt)
    if m:
        return m.group(1).strip()
    return None


def extract_prompt_workdir(task_prompt):
    """Extract task_prompt_workdir by resolving rule (b)/(c) at capture time.

    Returns the first existing directory found following specific phrases,
    or the first absolute Windows path that exists, from the full task_prompt.
    This field is stored in the claim for use by verify_claims.py as a
    pre-resolved path.
    """
    if not task_prompt:
        return None

    # Rule (b): path following specific phrases
    m = PROMPT_WORKDIR_PHRASES_RE.search(task_prompt)
    if m:
        p = m.group(1).strip().rstrip(".,:;")
        if p and os.path.isdir(p):
            return p

    # Rule (c): first absolute Windows path that is an existing directory
    for m in ABS_WIN_PATH_RE.finditer(task_prompt):
        p = m.group(0).rstrip(".,:;")
        if os.path.isfile(p):
            p = os.path.dirname(p)
        if p and os.path.isdir(p):
            return p

    return None


def extract_runnable_checks(acceptance_text, cwd):
    if not acceptance_text:
        return []
    candidates = []
    for block in CODEBLOCK_RE.findall(acceptance_text):
        for ln in block.splitlines():
            ln = ln.strip().lstrip("$ ").strip()
            if ln:
                candidates.append((ln, False))  # (cmd, has_placeholder)
    for m in BACKTICK_RE.findall(acceptance_text):
        m = m.strip()
        has_placeholder = bool(re.search(r"<[^>]+>", m))
        candidates.append((m, has_placeholder))

    checks = []
    seen = set()
    for cmd, has_placeholder in candidates:
        if not cmd or cmd in seen:
            continue
        # Commands with placeholders are not checked for denylist
        # (placeholders use < > which are structural tokens)
        if not has_placeholder and command_denylisted(cmd):
            continue
        if is_allowlisted_check(cmd):
            seen.add(cmd)
            chk = {"cmd": cmd, "cwd": cwd}
            if has_placeholder:
                chk["runnable"] = False
                chk["reason"] = "placeholder"
            checks.append(chk)
    return checks


def extract_claim_check_pairs(claim_text, cwd):
    """Parse "CLAIM: <text>\\nCHECK: <cmd>" pairs out of the subagent's final
    message (the benchmark task-card format). Applies the same
    allowlist/denylist as extract_runnable_checks and attaches the CLAIM
    text to each resulting check as "claim_text". Marks checks with
    placeholders as non-runnable."""
    if not claim_text:
        return []
    checks = []
    seen = set()
    for m in CLAIM_CHECK_RE.finditer(claim_text):
        claim = m.group("claim").strip()
        cmd = m.group("check").strip().strip("`")
        if not cmd or cmd in seen:
            continue
        has_placeholder = bool(re.search(r"<[^>]+>", cmd))
        # Commands with placeholders are not checked for denylist
        if not has_placeholder and command_denylisted(cmd):
            continue
        if is_allowlisted_check(cmd):
            seen.add(cmd)
            chk = {"cmd": cmd, "cwd": cwd, "claim_text": claim}
            if has_placeholder:
                chk["runnable"] = False
                chk["reason"] = "placeholder"
            checks.append(chk)
    return checks


def append_jsonl_locked(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        if msvcrt is not None:
            try:
                # Lock a byte region for exclusive append; retry briefly.
                for _ in range(20):
                    try:
                        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            except Exception:
                pass
        os.write(fd, line)
        os.fsync(fd)
        if msvcrt is not None:
            try:
                os.lseek(fd, -1, os.SEEK_CUR)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except Exception:
                pass
    finally:
        os.close(fd)


def log_hook_keys(hook_json):
    try:
        keys = sorted(hook_json.keys())
        os.makedirs(os.path.dirname(HOOK_KEYS_LOG_PATH), exist_ok=True)
        with open(HOOK_KEYS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
                        "keys": keys,
                    }
                )
                + "\n"
            )
    except Exception:
        pass


def compute_capture_key(agent_id, subagent_transcript_path, claim_text):
    """Compute capture_key as sha256(identifier + sha256(claim_text))."""
    ident = (
        agent_id
        or (
            os.path.basename(subagent_transcript_path)
            if subagent_transcript_path
            else ""
        )
        or ""
    )
    claim_hash = hashlib.sha256((claim_text or "").encode("utf-8")).hexdigest()
    combined = f"{ident}{claim_hash}".encode("utf-8")
    return hashlib.sha256(combined).hexdigest()


def capture_key_exists(capture_key):
    """Check if capture_key already exists in claims.jsonl."""
    if not os.path.isfile(CLAIMS_PATH):
        return False
    try:
        with open(CLAIMS_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("capture_key") == capture_key:
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def log_duplicate(ts, agent_id):
    """Log a duplicate detection to claims_dups.log."""
    try:
        os.makedirs(os.path.dirname(CLAIMS_DUPS_LOG_PATH), exist_ok=True)
        with open(CLAIMS_DUPS_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "ts": ts,
                        "agent_id": agent_id,
                        "dup": True,
                    }
                )
                + "\n"
            )
    except Exception:
        pass


def find_agent_transcript_field(hook_json):
    """Look for a hook JSON field that points at the SUBAGENT's own transcript
    (as opposed to transcript_path, which is the PARENT session transcript).

    Returns (field_name, path) or (None, None).
    """
    for name in AGENT_TRANSCRIPT_FIELD_CANDIDATES:
        val = hook_json.get(name)
        if val and os.path.isfile(str(val)):
            return name, str(val)
    # Generic scan: any key containing both "agent" and "transcript".
    for key, val in hook_json.items():
        if not isinstance(key, str):
            continue
        kl = key.lower()
        if "agent" in kl and "transcript" in kl:
            if val and os.path.isfile(str(val)):
                return key, str(val)
    return None, None


def sanitize_cwd_for_tasks_dir(cwd):
    """Reproduce the ~/AppData/Local/Temp/claude/<sanitized-cwd>/ convention:
    drive letter lowercased, ':' and '\\'/'/' replaced with '-'.
    e.g. $HOME\\Development\\<project>
      -> <project-slug>
    """
    if not cwd:
        return None
    s = str(cwd).rstrip("\\/")
    if len(s) >= 2 and s[1] == ":":
        s = s[0].lower() + s[1:]
    s = s.replace(":", "-").replace("\\", "-").replace("/", "-")
    return s


def find_tasks_dir_transcript(hook_json):
    """Fallback transcript discovery: locate the subagent's tasks/*.output
    file under the session's tasks dir, by agent_id (preferred) or by
    recency (most-recently-modified file within the recent window).

    Returns (source_label, path) or (None, None).
    """
    cwd = hook_json.get("cwd")
    session_id = hook_json.get("session_id")
    if not cwd or not session_id:
        return None, None

    sanitized = sanitize_cwd_for_tasks_dir(cwd)
    if not sanitized:
        return None, None

    tasks_dir = os.path.join(TASKS_ROOT, sanitized, str(session_id), "tasks")
    if not os.path.isdir(tasks_dir):
        return None, None

    agent_id = hook_json.get("agent_id")
    if agent_id:
        candidate = os.path.join(tasks_dir, f"{agent_id}.output")
        if os.path.isfile(candidate):
            return "tasks_dir_agent_id", candidate

    try:
        entries = [
            os.path.join(tasks_dir, name)
            for name in os.listdir(tasks_dir)
            if name.endswith(".output")
        ]
    except Exception:
        entries = []

    if not entries:
        return None, None

    now = time.time()
    recent = []
    for p in entries:
        try:
            mtime = os.path.getmtime(p)
        except OSError:
            continue
        if now - mtime <= TASKS_DIR_RECENT_WINDOW_S:
            recent.append((mtime, p))

    if not recent:
        return None, None

    recent.sort(key=lambda t: t[0], reverse=True)
    return "tasks_dir_recent", recent[0][1]


def resolve_subagent_transcript(hook_json):
    """Fallback chain: hook field -> tasks-dir agent_id/recency -> none.

    Returns (capture_source, path_or_None).
    """
    field_name, path = find_agent_transcript_field(hook_json)
    if path:
        return f"hook_field:{field_name}", path

    source, path = find_tasks_dir_transcript(hook_json)
    if path:
        return source, path

    return "none", None


def guess_project(hook_json, task_prompt):
    cwd = hook_json.get("cwd")
    if cwd:
        base = os.path.basename(str(cwd).rstrip("\\/"))
        if base:
            return base
    if task_prompt:
        m = re.search(r"Development[\\/]+([A-Za-z0-9_\-\.]+)", task_prompt)
        if m:
            return m.group(1)
    return None


def main():
    hook_json = read_stdin_json()

    _safe(lambda: log_hook_keys(hook_json))

    session_id = hook_json.get("session_id") or "unknown"
    agent_id = hook_json.get("agent_id")

    capture_source, subagent_transcript_path = resolve_subagent_transcript(hook_json)

    if subagent_transcript_path:
        msgs = load_transcript_messages(subagent_transcript_path)
        task_prompt = find_first_user_message(msgs)
        claim_text = find_last_assistant_message(msgs)[:4000]
    else:
        task_prompt = ""
        claim_text = ""

    # Compute capture_key for deduplication
    capture_key = compute_capture_key(agent_id, subagent_transcript_path, claim_text)

    # Check for duplicate and return early if found
    if capture_key_exists(capture_key):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        _safe(lambda: log_duplicate(ts, agent_id))
        return

    acceptance = extract_acceptance(task_prompt)
    cwd = extract_cwd(task_prompt)
    runnable_checks = extract_runnable_checks(acceptance, cwd)

    # Also parse CLAIM:/CHECK: pairs out of the subagent's own final
    # message (benchmark task-card format) -- additive, same
    # allowlist/denylist, deduped against the ACCEPTANCE-derived checks.
    existing_cmds = {c["cmd"] for c in runnable_checks}
    for chk in extract_claim_check_pairs(claim_text, cwd):
        if chk["cmd"] not in existing_cmds:
            runnable_checks.append(chk)
            existing_cmds.add(chk["cmd"])

    claimed_done = is_declared_done(claim_text)
    task_prompt_workdir = extract_prompt_workdir(task_prompt)
    check_mode = (
        "any_of" if acceptance and ANY_OF_ACCEPTANCE_RE.search(acceptance) else "all_of"
    )

    ts = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    task_prompt_sha256 = hashlib.sha256((task_prompt or "").encode("utf-8")).hexdigest()
    # id collision fix: include agent_id, capture_transcript, and claim_text prefix
    # to avoid same-second collisions
    capture_transcript_name = (
        os.path.basename(subagent_transcript_path) if subagent_transcript_path else ""
    )
    identifier = agent_id or capture_transcript_name or ""
    id_src = f"{identifier}{ts}{(claim_text or '')[:200]}".encode("utf-8")
    row_id = hashlib.sha256(id_src).hexdigest()[:16]

    row = {
        "id": row_id,
        "ts": ts,
        "session_id": session_id,
        "project": guess_project(hook_json, task_prompt),
        "task_prompt_sha256": task_prompt_sha256,
        "task_prompt_head": (task_prompt or "")[:300],
        "task_prompt_workdir": task_prompt_workdir,
        "acceptance": acceptance,
        "claim_text": claim_text,
        "claimed_done": claimed_done,
        "runnable_checks": runnable_checks,
        "check_mode": check_mode,
        "status": "pending",
        "capture_source": capture_source,
        "capture_transcript": os.path.basename(subagent_transcript_path)
        if subagent_transcript_path
        else None,
        # The hook JSON's own cwd at capture time -- used by verify_claims.py
        # as a fallback when an individual check's "cwd" is null (e.g. no
        # "Work dir:" hint was present in the task prompt).
        "capture_cwd": hook_json.get("cwd"),
        "capture_key": capture_key,
        "agent_id": agent_id,
    }

    _safe(lambda: append_jsonl_locked(CLAIMS_PATH, row))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
