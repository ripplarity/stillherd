"""proc_tree.py — tree-aware process cleanup (parent AND children).

WHY THIS EXISTS (2026-08-16). A Git-Bash `find / -iname '*exchange_archive*'`
ran for SEVEN HOURS, burning 24,604s of CPU. Its parent was long dead, so
nothing was reading its output and nothing would ever stop it. Two independent
defects let that happen, and this tool fixes both:

  1. reap.py is OPT-IN BY RUNNER: `CANDIDATE_NAMES = ("python","pytest",
     "pythonw")`. A runaway `find.exe` is filtered out before any orphan logic
     runs, so `reap.py scan` reported "clean" while a core burned. Compute
     leaks are not a Python-only phenomenon.

  2. NOTHING WALKED THE TREE. reap.py kills with `Stop-Process -Id <pid>`,
     which on Windows kills ONE process. It does not touch descendants.

Defect 2 is the subtle and more dangerous one, because the naive fix makes it
worse. On Windows a dead parent's PID is NOT reparented — it dangles. So
killing a parent first does not clean up its children; it CONVERTS them into
exactly the ownerless orphans this tool exists to reap. A "cleanup" that kills
top-down manufactures the next incident.

Hence the two rules that define this file:

  RULE A — SNAPSHOT FIRST, THEN KILL. Enumerate the whole tree from one
  process-table snapshot BEFORE killing anything. Once the parent dies its
  children can no longer be found by walking down from it.

  RULE B — KILL LEAVES FIRST, PARENT LAST (deepest depth first). This is the
  inverse of the intuitive order and it is the whole point: at every moment,
  any process still alive still has its real parent alive above it, so nothing
  is ever transiently orphaned. If the run is interrupted halfway, what
  survives is a smaller intact tree — never a scatter of ownerless leaves.

PID REUSE IS HANDLED, NOT ASSUMED AWAY. Windows recycles PIDs aggressively, so
"ppid == X" does not prove parenthood. A child whose creation time PRECEDES
its claimed parent's is impossible; that pairing proves the real parent is gone
and the PID was recycled. Such a process is NOT treated as a descendant — it
belongs to someone else, and adopting it into the kill set would kill an
unrelated process. This is the same proof reap.py's `is_orphan` uses, applied
in the opposite direction.

SAFETY. This box runs PEER AGENTS on other projects. The protected-name list
and the two-signal rule from reap.py apply here too: a bare orphan is
REPORTED, never killed, because a peer's deliberately-detached job looks
identical to a leak by parent-death alone. Killing requires ownership proof
AND corroborated abandonment, or an explicit operator-named target.

CLI:
  python proc_tree.py tree <pid>              # show the tree, kill nothing
  python proc_tree.py kill <pid> [--dry-run] [--children-only]
  python proc_tree.py orphans [--min-age-min 30] [--min-cpu-s 300]
  python proc_tree.py orphans --kill          # only kills corroborated ones

Exit codes: 0 = done / nothing to do, 3 = candidates found (report modes).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# Never kill these regardless of ownership proof. Mirrors reap.py's list —
# keep the two in sync. Substring match, case-insensitive, on the image name.
PROTECTED_NAMES = (
    "ollama",
    "code",
    "claude",
    "cursor",
    "node",
    "uvicorn",
    "ssh",
    "git",
    "gh",
    "docker",
    "wsl",
    "systemd",
    "explorer",
    "powershell",
    "pwsh",
    "conhost",
    "svchost",
    "csrss",
    "wininit",
    "services",
    "lsass",
    "smss",
    "winlogon",
    "dwm",
    "system",
    "registry",
    "memory compression",
)

# Long-lived services we must not reap even when the image name matches.
SERVICE_MARKERS = (
    "uvicorn",
    "http.server",
    "flask",
    "gunicorn",
    "serve_demo",
    "sandbox_launcher",
    "manage.py runserver",
)

# Runners that are pure, restartable compute — a runaway here costs a core and
# nothing else, and none of them holds state that a kill would corrupt. This
# list is what closes defect 1 WITHOUT making everything on the box killable:
# it is still opt-in, just no longer Python-only. `find` is first for a reason.
SCAN_RUNNERS = (
    "find",
    "grep",
    "rg",
    "ripgrep",
    "ag",
    "sort",
    "wc",
    "awk",
    "sed",
    "python",
    "pytest",
    "pythonw",
    "node",  # only reachable via explicit `kill <pid>`; PROTECTED blocks orphans
)

COORD_DIR = os.path.dirname(os.path.abspath(__file__))


def _ps(cmd: str) -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return out.stdout
    except Exception:
        return ""


def snapshot() -> dict[int, dict]:
    """One process-table read. See RULE A — every walk uses this, not a fresh
    query per level, so the tree cannot shift underneath us mid-enumeration."""
    raw = _ps(
        "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,"
        "Name,CreationDate,CommandLine,WorkingSetSize,UserModeTime,KernelModeTime "
        "| ConvertTo-Json -Depth 2 -Compress"
    )
    try:
        # strict=False: PowerShell's ConvertTo-Json emits RAW control characters that
        # appear inside a process's CommandLine (a tab or newline in an argument is
        # legal on Windows). Python's strict parser rejects the ENTIRE process table
        # for one such byte, which silently blinded this tool -- `snapshot()` returned
        # {} and `orphans` then printed "clean", a false all-clear, while real runaway
        # processes were burning CPU. Observed 2026-09-09.
        rows = json.loads(raw, strict=False) if raw.strip() else []
    except json.JSONDecodeError as e:
        print(
            f"proc_tree: FAILED to parse the process table ({e}). "
            "Reporting nothing is NOT the same as finding nothing.",
            file=sys.stderr,
        )
        return {}
    if isinstance(rows, dict):
        rows = [rows]

    procs: dict[int, dict] = {}
    for r in rows:
        pid = r.get("ProcessId")
        if pid is None:
            continue
        created = r.get("CreationDate")
        ts = None
        if isinstance(created, str):
            if created.startswith("/Date("):
                try:
                    ms = int(created[6:].split(")")[0].split("+")[0])
                    ts = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
                except Exception:
                    ts = None
            else:
                try:
                    ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                except Exception:
                    ts = None
        cpu_s = (
            int(r.get("UserModeTime") or 0) + int(r.get("KernelModeTime") or 0)
        ) / 1e7
        procs[int(pid)] = {
            "pid": int(pid),
            "ppid": int(r.get("ParentProcessId") or 0),
            "name": (r.get("Name") or "").lower(),
            "created": ts,
            "cmdline": r.get("CommandLine") or "",
            "cpu_s": round(cpu_s, 1),
            "rss_mb": int((r.get("WorkingSetSize") or 0) / (1024 * 1024)),
        }
    return procs


def protected_pids() -> set[int]:
    """PIDs a peer declared as live work — GPU lease, CPU leases, ledger.

    Deliberately MORE conservative than each source's own staleness logic: an
    open row protects unconditionally. A false protect costs a leaked core; a
    false unprotect kills a peer's multi-hour run.
    """
    out: set[int] = set()

    for fname, key in (("gpu.lease.json", "pid"),):
        p = os.path.join(COORD_DIR, fname)
        if os.path.exists(p):
            try:
                d = json.load(open(p, encoding="utf-8"))
                if isinstance(d, dict) and d.get(key):
                    out.add(int(d[key]))
            except Exception:
                pass

    # CPU leases (multi-holder) — every tracked pid is declared live work.
    p = os.path.join(COORD_DIR, "cpu.leases.json")
    if os.path.exists(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
            for x in (d or {}).get("leases", []):
                if isinstance(x, dict) and x.get("pid"):
                    out.add(int(x["pid"]))
        except Exception:
            pass

    # Activity ledger: a row is OPEN if the latest of {start,end} is `start`.
    # Heartbeats don't close a row. Ids are stripped — Git-Bash $(...) capture
    # can append a literal \r that silently breaks id matching.
    ledger = os.path.join(COORD_DIR, "activity_ledger.jsonl")
    if os.path.exists(ledger):
        try:
            last: dict[str, str] = {}
            pid_by_id: dict[str, int] = {}
            with open(ledger, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    aid = row.get("id")
                    if isinstance(aid, str):
                        aid = aid.strip()
                    if not aid:
                        continue
                    kind = row.get("event")
                    if kind not in ("start", "end"):
                        continue
                    last[aid] = kind
                    if kind == "start" and row.get("pid"):
                        try:
                            pid_by_id[aid] = int(row["pid"])
                        except (TypeError, ValueError):
                            pass
            for aid, kind in last.items():
                if kind == "start" and aid in pid_by_id:
                    out.add(pid_by_id[aid])
        except Exception:
            pass
    return out


def is_protected(p: dict) -> bool:
    name = p["name"]
    if any(x in name for x in PROTECTED_NAMES):
        return True
    cmd_low = p["cmdline"].lower()
    return any(m in cmd_low for m in SERVICE_MARKERS)


def _real_child(parent: dict, child: dict) -> tuple[bool, bool]:
    """(is_child, verified) for a claimed parent/child pair.

    A live parent ALWAYS predates its child. If the claimed parent started
    after the child, the pairing is impossible: the real parent exited and
    Windows recycled its PID. Adopting such a process into a kill set would
    kill a stranger, so it is excluded.

    THE SECOND RETURN VALUE EXISTS BECAUSE THE FAILURE POLARITY IS INVERTED
    HERE VS reap.py. In `is_orphan`, an unknown creation time degrades to "not
    an orphan" — i.e. don't kill, which is safe. Here an unknown degrades to
    "yes, a child" — i.e. DO kill, which is not. Simply flipping it to exclude
    unknowns is worse still: kill_tree would then skip the child, kill the
    parent anyway, and ORPHAN it — manufacturing exactly the runaway this tool
    exists to prevent.

    So membership stays inclusive (no orphans) and uncertainty is reported
    instead. Explicit `kill <pid>` proceeds — the operator named the target.
    The autonomous `orphans --kill` path refuses to auto-kill a tree whose
    membership could not be verified, because there no human vouched for it.
    """
    pc, cc = parent.get("created"), child.get("created")
    if pc and cc:
        return (pc <= cc), True
    return True, False


def descendants(root: int, procs: dict[int, dict], with_verified: bool = False):
    """[(pid, depth)] for every genuine descendant of `root`, excluding root.

    Walks one snapshot (RULE A). `seen` guards against a cycle introduced by
    PID reuse, which would otherwise hang the walk.

    `with_verified=True` returns (descendants, all_verified) where the flag is
    False if ANY parent/child link could not be confirmed by creation time —
    see `_real_child` for why that is reported rather than acted on.
    """
    kids: dict[int, list[int]] = {}
    for pid, p in procs.items():
        kids.setdefault(p["ppid"], []).append(pid)

    out: list[tuple[int, int]] = []
    seen = {root}
    frontier = [(root, 0)]
    all_verified = True
    while frontier:
        cur, depth = frontier.pop()
        for kid in kids.get(cur, []):
            if kid in seen:
                continue
            is_child, verified = _real_child(procs[cur], procs[kid])
            if not is_child:
                continue  # PID-reuse impostor, not our child
            if not verified:
                all_verified = False
            seen.add(kid)
            out.append((kid, depth + 1))
            frontier.append((kid, depth + 1))
    return (out, all_verified) if with_verified else out


def is_orphan(p: dict, procs: dict[int, dict]) -> tuple[bool, str]:
    """Proof, not guess. Mirrors reap.py's predicate."""
    ppid = p["ppid"]
    if not ppid or ppid not in procs:
        return True, f"parent pid {ppid} no longer exists"
    parent = procs[ppid]
    pc, cc = parent.get("created"), p.get("created")
    if pc and cc and pc > cc:
        return True, (
            f"parent pid {ppid} ({parent['name']}) started AFTER child "
            f"— PID reuse, real parent is gone"
        )
    return False, ""


def kill_tree(
    root: int,
    procs: dict[int, dict],
    include_root: bool = True,
    dry_run: bool = False,
) -> tuple[list[int], list[int]]:
    """Kill `root`'s tree leaves-first (RULE B). Returns (killed, skipped)."""
    tree = descendants(root, procs)
    # Deepest first. Ordering by descending depth is what guarantees a process
    # is never orphaned even for an instant — its parent outlives it.
    ordered = [pid for pid, _ in sorted(tree, key=lambda t: -t[1])]
    if include_root:
        ordered.append(root)

    killed, skipped = [], []
    for pid in ordered:
        p = procs.get(pid)
        if not p:
            continue
        if is_protected(p):
            skipped.append(pid)
            continue
        if pid == os.getpid():
            skipped.append(pid)
            continue
        if dry_run:
            killed.append(pid)
            continue
        # Teardown is NOT synchronous: sampling once right after Stop-Process
        # catches processes mid-exit and reports them alive, making a working
        # kill look broken. Poll to a short deadline instead.
        r = _ps(
            f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue; "
            f"1..20 | ForEach-Object {{ "
            f"  if (-not (Get-Process -Id {pid} -ErrorAction SilentlyContinue)) "
            f"  {{ 'dead'; break }}; Start-Sleep -Milliseconds 100 }}"
        )
        (killed if "dead" in r else skipped).append(pid)
    return killed, skipped


def _fmt(p: dict, procs: dict[int, dict]) -> str:
    age = ""
    if p.get("created"):
        mins = int((datetime.now(timezone.utc) - p["created"]).total_seconds() / 60)
        age = f"age {mins:>5}m  "
    return (
        f"pid {p['pid']:>6}  {p['name']:<16} {age}"
        f"cpu {int(p['cpu_s']):>6}s  {p['rss_mb']:>5}MB  "
        f"{p['cmdline'][:90]}"
    )


def cmd_tree(a) -> int:
    procs = snapshot()
    if a.pid not in procs:
        print(f"pid {a.pid} not found")
        return 0
    print(_fmt(procs[a.pid], procs))
    tree = descendants(a.pid, procs)
    for pid, depth in sorted(tree, key=lambda t: (t[1], t[0])):
        print("  " * depth + "└ " + _fmt(procs[pid], procs))
    if not tree:
        print("  (no children)")
    else:
        total = sum(procs[p]["cpu_s"] for p, _ in tree) + procs[a.pid]["cpu_s"]
        print(f"\n{len(tree)} descendant(s), {int(total)}s CPU across the tree")
    return 0


def cmd_kill(a) -> int:
    procs = snapshot()
    if a.pid not in procs:
        print(f"pid {a.pid} not found")
        return 0
    killed, skipped = kill_tree(
        a.pid, procs, include_root=not a.children_only, dry_run=a.dry_run
    )
    verb = "would kill" if a.dry_run else "killed"
    print(f"{verb} {len(killed)}: {killed}")
    if skipped:
        print(f"skipped {len(skipped)} (protected or already gone): {skipped}")
    return 0


def _owner_hint(session_cwd, cmdline: str) -> bool:
    """Does `cmdline` reference this session's workspace?

    ANNOTATION ONLY — deliberately not a kill gate. Ownership of an orphan is
    fundamentally unprovable (the parent that proved it is gone, which is what
    "orphan" means), and this heuristic fails for exactly the case that hurts:
    an unscoped `find / -iname ...` names no workspace path at all, so gating
    on it would make the 24,604s incident permanently unkillable. Authorization
    stays with the two-signal rule; this only enriches the report and the log.

    Reuses reap.py's implementation rather than copying it: that function has
    silently degraded twice on path normalization and is pinned by regression
    tests in test_reap.py. A second copy would drift with only one tested.
    """
    if not session_cwd:
        return False
    try:
        from reap import _cwd_matches
    except Exception:
        return False
    return _cwd_matches(session_cwd, cmdline)


def _log_kills(entries: list[dict]) -> None:
    """Append one audit line per auto-kill.

    A kill nobody can trace is not acceptable on a box running peer agents:
    hook stdout is discarded, so without this an auto-kill leaves no evidence.
    Failures are caught (this must never break a Stop hook) but are REPORTED on
    stderr — an earlier silent `except: pass` here hid a formatting bug that
    created empty log files while every kill went unrecorded.
    """
    if not entries:
        return
    try:
        d = os.path.join(COORD_DIR, "health_logs")
        os.makedirs(d, exist_ok=True)
        stamp = datetime.now(timezone.utc)
        fn = os.path.join(d, stamp.strftime("%Y-%m-%d") + ".log")
        with open(fn, "a", encoding="utf-8") as f:
            f.writelines(" ".join(
                        [
                            stamp.isoformat(),
                            "proc_tree AUTOKILL",
                            "pid=" + str(e["pid"]),
                            "name=" + str(e["name"]),
                            "cpu_s=" + str(e["cpu_s"]),
                            "rss_mb=" + str(e["rss_mb"]),
                            "workspace_ref=" + str(e["owned"]),
                            "tree=" + repr(e["killed"]),
                            "reason=" + repr(e["why"]),
                            "cmd=" + repr(e["cmdline"][:300]),
                        ]
                    )
                    + chr(10) for e in entries)
    except Exception as exc:  # never break the caller, but never hide it either
        print("proc_tree: WARNING kill-audit log failed: %r" % (exc,), file=sys.stderr)


def cmd_orphans(a) -> int:
    """Report orphaned compute of ANY runner, not just Python (defect 1).

    Killing still needs two independent signals — ownership proof (orphan)
    AND corroborated abandonment (sustained CPU in a flat working set). A bare
    orphan is a peer's detached job until proven otherwise.
    """
    procs = snapshot()
    protected = protected_pids()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=a.min_age_min)

    found = []
    for pid, p in procs.items():
        if is_protected(p) or pid in protected or pid == os.getpid():
            continue
        stem = p["name"].rsplit(".", 1)[0]
        if not any(stem == r or stem.startswith(r) for r in SCAN_RUNNERS):
            continue
        created = p.get("created")
        if not created or created > cutoff:
            continue
        orphan, why = is_orphan(p, procs)
        if not orphan:
            continue

        corroboration = []
        if p["cpu_s"] >= a.min_cpu_s and p["rss_mb"] < 250:
            corroboration.append(
                f"burned {int(p['cpu_s'])}s CPU in {p['rss_mb']}MB "
                f"(spinning compute, working set never grew)"
            )
        tree, verified = descendants(pid, procs, with_verified=True)
        # Autonomous killing requires membership we could actually confirm.
        # Explicit `kill <pid>` has an operator behind it; this path does not.
        owned = _owner_hint(getattr(a, "session_cwd", None), p["cmdline"])
        found.append(
            {
                "p": p,
                "why": why,
                "owned": owned,
                "corroboration": corroboration,
                "tree": tree,
                "verified": verified,
                "killable": bool(corroboration) and verified,
            }
        )

    if not procs:
        print(
            "proc_tree: COULD NOT READ THE PROCESS TABLE — this is not an all-clear. "
            "Orphan status is UNKNOWN.",
            file=sys.stderr,
        )
        return 2
    if not found:
        print(f"proc_tree: clean — no orphaned compute older than {a.min_age_min}m")
        return 0

    for c in found:
        tag = "KILLABLE" if c["killable"] else "report-only"
        print(f"[{tag}] {_fmt(c['p'], procs)}")
        print(f"         - {c['why']}")
        for r in c["corroboration"]:
            print(f"         - {r}")
        if c["owned"]:
            print("         - cmdline references this session's workspace")
        if c["tree"]:
            print(f"         - {len(c['tree'])} descendant(s) would go with it")
        if not c.get("verified", True):
            print(
                "         - tree membership UNVERIFIED (missing creation time); "
                "not auto-killing — use `kill <pid>` if you vouch for it"
            )
        elif not c["killable"]:
            print(
                "         - no abandonment corroboration; could be a peer's detached job"
            )

    if a.kill:
        total_killed = []
        audit = []
        for c in found:
            if not c["killable"]:
                continue
            killed, _ = kill_tree(c["p"]["pid"], procs, include_root=True)
            total_killed.extend(killed)
            audit.append(
                {
                    "pid": c["p"]["pid"],
                    "name": c["p"]["name"],
                    "cpu_s": c["p"]["cpu_s"],
                    "rss_mb": c["p"]["rss_mb"],
                    "owned": c["owned"],
                    "why": c["why"],
                    "killed": killed,
                    "cmdline": c["p"]["cmdline"],
                }
            )
        _log_kills(audit)
        left = sum(1 for c in found if not c["killable"])
        print(chr(10) + f"killed {len(total_killed)}: {total_killed}")
        if left:
            print(f"left {left} report-only orphan(s) alone (could be a peer's job)")
        # rc 3 still means "orphaned compute is present". Returning 0 because we
        # killed SOME would blind every rc==3 caller (health_sweep) to the
        # orphans we deliberately refused to kill.
        return 3 if left else 0

    return 3


HOG_IGNORE = ("system idle process", "system", "memory compression", "idle")


def _toast(title: str, body: str) -> None:
    """Best-effort Windows balloon. Never raises — a failed toast must not hide the log."""
    body = body.replace("'", "''")[:250]
    title = title.replace("'", "''")
    _ps(
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n=New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon=[System.Drawing.SystemIcons]::Warning;$n.Visible=$true;"
        f"$n.ShowBalloonTip(15000,'{title}','{body}','Warning');"
        "Start-Sleep 16;$n.Dispose()"
    )


def find_hogs(procs_a, procs_b, dt_s, min_age_min, min_cores, protected, now):
    """Pure core of `hogs`, split out for tests.

    A hog = alive in both samples, older than min_age_min, averaging >= min_cores
    over its WHOLE lifetime, still burning >= 1 core right now, and with neither
    itself nor any ancestor declared via a lease/ledger row. Orphan status is
    irrelevant — that is the blind spot this closes (2026-09-28: an emulator with
    a live parent ran ~14 cores for 8 h while `orphans` reported clean).
    """
    out = []
    for pid, b in procs_b.items():
        a = procs_a.get(pid)
        if not a or a.get("created") != b.get("created") or not b.get("created"):
            continue
        if b["name"] in HOG_IGNORE or pid == os.getpid():
            continue
        age_s = (now - b["created"]).total_seconds()
        if age_s < min_age_min * 60:
            continue
        life = b["cpu_s"] / max(age_s, 1)
        recent = (b["cpu_s"] - a["cpu_s"]) / max(dt_s, 0.1)
        if life < min_cores or recent < 1.0:
            continue
        chain, cur, seen = [], pid, set()
        while cur and cur not in seen and cur in procs_b:
            seen.add(cur)
            chain.append(cur)
            cur = procs_b[cur]["ppid"]
        if any(c in protected for c in chain):
            continue
        out.append(
            {
                "pid": pid,
                "name": b["name"],
                "age_h": round(age_s / 3600, 1),
                "life_cores": round(life, 1),
                "now_cores": round(recent, 1),
                "cpu_s": b["cpu_s"],
                "cmd": b["cmdline"][:160],
            }
        )
    return sorted(out, key=lambda h: -h["life_cores"])


def cmd_hogs(a) -> int:
    """REPORT-ONLY: sustained multi-core burners with no lease/ledger declaration.

    Never kills — a hog with a live parent is somebody's job; the fix is to
    lease it or stop it, which is a human/owner call. Exit 3 when any found.
    """
    import time

    p1 = snapshot()
    t1 = time.time()
    time.sleep(a.sample_s)
    p2 = snapshot()
    if not p1 or not p2:
        print("hogs: process table unreadable — NOT a clean result", file=sys.stderr)
        return 2
    hogs = find_hogs(
        p1,
        p2,
        time.time() - t1,
        a.min_age_min,
        a.min_cores,
        protected_pids(),
        datetime.now(timezone.utc),
    )
    if not hogs:
        print(
            f"hogs: clean — nothing >= {a.min_cores} cores avg for >= {a.min_age_min}m undeclared"
        )
        return 0
    lines = [
        f"HOG pid={h['pid']} {h['name']} age={h['age_h']}h avg={h['life_cores']} cores "
        f"now={h['now_cores']} cpu_s={h['cpu_s']} :: {h['cmd']}"
        for h in hogs
    ]
    print("\n".join(lines))
    print(
        "hogs: no lease/ledger row declares these. Stop them "
        "(proc_tree.py kill <pid>) or lease them (cpu_lease.py acquire --pid)."
    )
    if a.notify:
        logd = os.path.join(COORD_DIR, "health_logs")
        os.makedirs(logd, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        with open(os.path.join(logd, "hogs.log"), "a", encoding="utf-8") as f:
            f.writelines(f"{stamp} {ln}\n" for ln in lines)
        _toast(
            "CPU hog running undeclared",
            "; ".join(
                f"{h['name']} {h['life_cores']} cores {h['age_h']}h" for h in hogs[:3]
            ),
        )
    return 3


def closed_projects() -> list[str]:
    """Lowercased project names from closed_projects.txt (one per line, # comments)."""
    p = os.path.join(COORD_DIR, "closed_projects.txt")
    if not os.path.exists(p):
        return []
    with open(p, encoding="utf-8") as f:
        return [
            ln.split("#")[0].strip().lower() for ln in f if ln.split("#")[0].strip()
        ]


def find_stale(procs, min_days, closed, now):
    """Pure core of `stale`: long-lived processes whose command line names a CLOSED project."""
    out = []
    for pid, p in procs.items():
        if not p.get("created") or not closed:
            continue
        age_d = (now - p["created"]).total_seconds() / 86400
        if age_d < min_days:
            continue
        cmd = p["cmdline"].lower()
        hit = next((c for c in closed if c in cmd), None)
        if hit:
            out.append(
                {
                    "pid": pid,
                    "name": p["name"],
                    "age_d": round(age_d, 1),
                    "project": hit,
                    "cpu_s": p["cpu_s"],
                    "cmd": p["cmdline"][:160],
                }
            )
    return out


def cmd_stale(a) -> int:
    """REPORT-ONLY: daemons older than N days belonging to a closed project. Exit 3 if any."""
    procs = snapshot()
    if not procs:
        print("stale: process table unreadable — NOT a clean result", file=sys.stderr)
        return 2
    found = find_stale(procs, a.days, closed_projects(), datetime.now(timezone.utc))
    if not found:
        print(f"stale: clean — no closed-project daemon older than {a.days}d")
        return 0
    for s in found:
        print(
            f"STALE pid={s['pid']} {s['name']} age={s['age_d']}d project={s['project']} "
            f"cpu_s={s['cpu_s']} :: {s['cmd']}"
        )
    print(
        "stale: project is listed in coord/closed_projects.txt. Keep (remove it from "
        "the list) or stop it (proc_tree.py kill <pid>)."
    )
    return 3


def main() -> int:
    # cp1252 consoles cannot print the tree glyphs; never crash a cleanup tool on output.
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("tree")
    t.add_argument("pid", type=int)
    t.set_defaults(fn=cmd_tree)

    k = sub.add_parser("kill")
    k.add_argument("pid", type=int)
    k.add_argument("--dry-run", action="store_true")
    k.add_argument(
        "--children-only",
        action="store_true",
        help="kill descendants but leave the root alive",
    )
    k.set_defaults(fn=cmd_kill)

    o = sub.add_parser("orphans")
    o.add_argument("--min-age-min", type=int, default=30)
    o.add_argument("--min-cpu-s", type=int, default=300)
    o.add_argument("--kill", action="store_true")
    o.add_argument(
        "--session-cwd",
        default=None,
        help="annotate findings that reference this workspace (not a kill gate)",
    )
    o.set_defaults(fn=cmd_orphans)

    h = sub.add_parser(
        "hogs", help="report-only: undeclared sustained multi-core burners"
    )
    h.add_argument("--min-age-min", type=int, default=60)
    h.add_argument("--min-cores", type=float, default=4.0)
    h.add_argument("--sample-s", type=float, default=10.0)
    h.add_argument(
        "--notify", action="store_true", help="append to health_logs/hogs.log + toast"
    )
    h.set_defaults(fn=cmd_hogs)

    st = sub.add_parser("stale", help="report-only: old daemons of closed projects")
    st.add_argument("--days", type=float, default=7.0)
    st.set_defaults(fn=cmd_stale)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
