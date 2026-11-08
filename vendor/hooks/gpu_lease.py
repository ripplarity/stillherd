"""Shared-GPU lease — coordination for multiple agents on ONE machine.

This box has ONE GPU (RTX 5000 Ada, 16 GB VRAM) but 24 CPU cores / 64 GB RAM.
CPU work parallelizes nearly free; the GPU is a SERIAL resource — only one heavy
job (LoRA training, video render, big model load) fits at a time. Hard lessons
that motivate this (from real incidents):
  • Two agents both loading models → VRAM thrash → minutes-per-step instead of
    seconds (a render went 45s/step → 1637s/step under contention).
  • Killing a peer's GPU process does NOT reclaim its VRAM on Windows (WDDM
    leaks the dead context until REBOOT) — so killing peers is both rude AND
    ineffective AND destroys their work.

So: claim a lease before heavy GPU use; if it's held, do CPU work / wait /
offload to Kaggle. NEVER kill a peer's process to take the GPU.

Lease file: ~/.claude/coord/gpu.lease.json
A lease is STALE (auto-reclaimable) if its PID is dead, or its heartbeat is
older than eta_seconds + GRACE, or older than MAX_LEASE_SECONDS.

CLI:
  python gpu_lease.py status
  python gpu_lease.py acquire --project SongForge --purpose "flux lora" --eta 7200
  python gpu_lease.py heartbeat
  python gpu_lease.py release
  python gpu_lease.py free        # is the GPU claimable right now? (exit 0=yes)
Exit code of `acquire`: 0 = acquired, 3 = held by a live peer (do not proceed).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

LEASE = Path.home() / ".claude" / "coord" / "gpu.lease.json"
GRACE = 1800  # 30 min past eta before a lease is considered abandoned
MAX_LEASE_SECONDS = 14 * 3600  # absolute ceiling regardless of eta/heartbeat


class LeaseUnreadable(Exception):
    """Raised when gpu.lease.json exists but can't be parsed as a lease.

    Deliberately NOT the same thing as "no lease file" (see _read). A mutex
    must fail CLOSED: corruption is unknown state, not free state.
    """


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not h:
            return False
        # distinguish "alive" from "exited-but-handle": check exit code
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _ps(cmd: str) -> str:
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return out.stdout
    except Exception:
        return ""


def _pid_creation_ts(pid: int) -> float | None:
    """Epoch seconds this pid was created, or None if unknown/unqueryable.

    WHY THIS EXISTS: `_pid_alive(pid)` only proves *a* process with that PID
    exists — Windows recycles PIDs aggressively, and a live-but-unrelated
    process (proven: explorer.exe's PID written into a stale lease made
    `status` report 'held/live') will pass `_pid_alive` even though the real
    holder is long dead. Creation time is the only way to tell "same process"
    from "coincidentally-same-PID". Parsing mirrors reap.py's snapshot() —
    CIM renders CreationDate as either `/Date(ms)/` or ISO depending on host.
    """
    if os.name != "nt" or pid <= 0:
        return None
    raw = _ps(
        f'Get-CimInstance Win32_Process -Filter "ProcessId={pid}" | '
        f"Select-Object CreationDate | ConvertTo-Json -Compress"
    )
    if not raw.strip():
        return None
    try:
        row = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if isinstance(row, list):
        row = row[0] if row else None
    if not isinstance(row, dict):
        return None
    created = row.get("CreationDate")
    if not isinstance(created, str):
        return None
    if created.startswith("/Date("):
        try:
            ms = int(created[6:].split(")")[0].split("+")[0])
            return ms / 1000
        except Exception:
            return None
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return None


def _pid_is_current(pid: int, recorded_start_ts: float | None) -> bool:
    """True if `pid` is alive AND is (as best we can tell) the SAME process
    that was recorded at acquire/start time — not a PID-reuse impostor.

    BACKWARD COMPATIBILITY: rows/leases written before this fix have no
    `pid_start_ts` (recorded_start_ts is None). Those keep the OLD behavior
    (pid-alive only) exactly — we cannot retroactively know their creation
    time, and guessing would misclassify live legacy leases as stale.
    Likewise if we can't query the current creation time (PowerShell hiccup,
    non-Windows), we don't false-positive-reclaim a possibly-live holder —
    we fall back to alive-only rather than assume reuse.
    """
    if not _pid_alive(pid):
        return False
    if recorded_start_ts is None:
        return True
    # The recorded value may be corrupt, or a string written by an older/other
    # writer. Comparing it raw raised TypeError and took the guard down with
    # it (caught by an end-to-end run against real processes, 2026-07-19 —
    # the monkeypatched unit tests always fed it a clean float). Coerce, and
    # on anything uncoercible fall back to the documented legacy behavior
    # rather than crashing or guessing "reused".
    try:
        recorded = float(recorded_start_ts)
    except (TypeError, ValueError):
        return True
    cur_ts = _pid_creation_ts(pid)
    if cur_ts is None:
        return True
    # Both sides come from the SAME source (Win32_Process CreationDate via
    # CIM), recorded at acquire and re-read here, so a genuine match is exact.
    # The epsilon only absorbs float round-tripping through JSON. An earlier
    # 2.0s slack was far too loose: it would accept a recycled PID whose new
    # process happened to start within two seconds of the original's start,
    # which is precisely the case this check exists to catch.
    return abs(cur_ts - recorded) < 0.05


def _write_atomic(data: dict) -> None:
    """Write the lease via temp+rename so a crash mid-write can never leave a
    truncated/corrupt gpu.lease.json in place — a truncated write is exactly
    the corruption scenario BUG 2 has to defend against. An orphaned
    `.tmp.<pid>` from this same write-then-crash pattern was found elsewhere
    in the portfolio (10.7MB, sat for 2 days), so the cleanup is not
    hypothetical: always unlink the temp file in `finally` (a no-op once
    os.replace has already moved it away).
    """
    LEASE.parent.mkdir(parents=True, exist_ok=True)
    tmp = LEASE.with_name(f"{LEASE.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, LEASE)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def _read() -> dict | None:
    """None = genuinely no lease file (free — safe to grant).

    Raises LeaseUnreadable for a PRESENT-but-corrupt file instead of
    swallowing the error and returning None. The old `except Exception:
    return None` made a truncated/corrupt gpu.lease.json indistinguishable
    from "no lease", so `status` reported free and a subsequent `acquire`
    silently discarded a real holder. A mutex must fail CLOSED.
    """
    try:
        text = LEASE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        raise LeaseUnreadable(f"{LEASE} exists but is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise LeaseUnreadable(f"{LEASE} exists but does not contain a JSON object")
    return data


def _is_stale(lease: dict) -> tuple[bool, str]:
    now = time.time()
    pid = int(lease.get("pid", 0) or 0)
    # Only treat a dead pid as stale when a REAL job pid was recorded (>0). The
    # `acquire` command runs as an ephemeral process whose pid dies immediately,
    # so a lease that didn't pass --pid is "untracked": fall through to the
    # heartbeat/eta checks instead of instantly reading stale.
    if pid > 0 and not _pid_is_current(pid, lease.get("pid_start_ts")):
        reason = (
            "is dead"
            if not _pid_alive(pid)
            else "was reused by a different process (creation time mismatch)"
        )
        return True, f"holder pid {pid} {reason}"
    hb = float(lease.get("heartbeat", lease.get("started_at", 0)))
    eta = float(lease.get("eta_seconds", 0))
    if now - hb > eta + GRACE:
        return True, f"heartbeat {int(now - hb)}s old > eta+{GRACE}"
    if now - float(lease.get("started_at", now)) > MAX_LEASE_SECONDS:
        return True, "exceeded MAX_LEASE_SECONDS"
    return False, "live"


def status() -> dict:
    try:
        lease = _read()
    except LeaseUnreadable as e:
        return {"state": "unknown", "why": str(e)}
    if not lease:
        return {"state": "free"}
    stale, why = _is_stale(lease)
    return {"state": "stale" if stale else "held", "why": why, **lease}


def acquire(
    project: str, purpose: str, eta_seconds: int, agent: str | None = None, pid: int = 0
) -> int:
    # A --pid that is not a live Windows process would make the lease look
    # stale at once and let a peer reclaim it mid-job. Git Bash `$!` is an MSYS
    # pid, not a Windows pid (2026-10-31: a lease taken with one was reclaimed
    # and two GPU jobs thrashed for 2.5 h). Refuse rather than record it.
    if pid and int(pid) > 0 and not _pid_alive(int(pid)):
        print(
            f"REFUSE: --pid {pid} is not a live process. In Git Bash, $! is an MSYS pid;"
            " pass the Windows pid (e.g. from Get-CimInstance Win32_Process)."
        )
        return 2
    LEASE.parent.mkdir(parents=True, exist_ok=True)
    try:
        cur = _read()
    except LeaseUnreadable as e:
        # FAIL CLOSED: a corrupt lease file is unknown state, not free state.
        # Granting a lease on top of it could silently discard a real holder
        # (the exact BUG 2 failure). Never auto-delete/auto-repair — that's
        # a human decision once they've looked at what's actually in the file.
        print(
            f"REFUSE to acquire: {e}\n"
            f"The lease file is corrupt, not absent. Inspect {LEASE} by hand, "
            f"or run `release` if you're certain no job holds the GPU. This "
            f"tool will not auto-repair or delete it for you."
        )
        return 3
    if cur:
        stale, why = _is_stale(cur)
        if not stale:
            print(
                f"GPU HELD by {cur.get('project')}/{cur.get('purpose')} "
                f"(pid {cur.get('pid')}, agent {cur.get('agent')}); "
                f"eta ~{int(cur.get('eta_seconds', 0))}s. Do CPU work, wait, "
                f"or use Kaggle. Do NOT kill it."
            )
            return 3
        print(f"reclaiming stale lease ({why})")
    now = time.time()
    # Record the REAL job pid when given (--pid) for crash auto-reclaim; default
    # 0 = untracked (reclaimed via heartbeat/eta + MAX_LEASE, not pid-death).
    # Also record its creation time (pid_start_ts) so a future PID-reuse
    # impostor can be told apart from the real holder (see _pid_is_current).
    tracked_pid = int(pid or 0)
    lease = {
        "holder": agent or os.environ.get("SFS_AGENT", "unknown"),
        "agent": agent or os.environ.get("SFS_AGENT", "unknown"),
        "project": project,
        "purpose": purpose,
        "pid": tracked_pid,
        "pid_start_ts": _pid_creation_ts(tracked_pid) if tracked_pid > 0 else None,
        "pid_create_time": _pid_creation_ts(tracked_pid) if tracked_pid > 0 else None,
        "job_key": os.environ.get("JOB_KEY") or None,
        "incarnation": int(os.environ["JOB_INCARNATION"])
        if os.environ.get("JOB_INCARNATION", "").isdigit()
        else None,
        "session": os.environ.get("SFS_AGENT", "unknown"),
        "model": os.environ.get("SFS_MODEL") or None,
        "started_at": now,
        "heartbeat": now,
        "eta_seconds": int(eta_seconds),
    }
    _write_atomic(lease)
    print(
        f"ACQUIRED GPU lease: {project}/{purpose} eta~{eta_seconds}s pid {tracked_pid}"
    )
    return 0


def acquire_for_pid(pid: int, project: str, purpose: str, eta_seconds: int) -> int:
    """Register a lease on behalf of an already-running GPU process (e.g. a
    training job launched separately). Used to retroactively mark the GPU busy."""
    LEASE.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    lease = {
        "holder": os.environ.get("SFS_AGENT", "unknown"),
        "agent": os.environ.get("SFS_AGENT", "unknown"),
        "project": project,
        "purpose": purpose,
        "pid": int(pid),
        "pid_start_ts": _pid_creation_ts(int(pid)),
        "pid_create_time": _pid_creation_ts(int(pid)),
        "job_key": os.environ.get("JOB_KEY") or None,
        "session": os.environ.get("SFS_AGENT", "unknown"),
        "model": os.environ.get("SFS_MODEL") or None,
        "started_at": now,
        "heartbeat": now,
        "eta_seconds": int(eta_seconds),
    }
    _write_atomic(lease)
    print(f"registered lease for pid {pid}: {project}/{purpose}")
    return 0


def heartbeat() -> int:
    try:
        lease = _read()
    except LeaseUnreadable as e:
        print(f"cannot heartbeat: {e}")
        return 1
    if not lease:
        return 1
    lease["heartbeat"] = time.time()
    _write_atomic(lease)
    return 0


def release() -> int:
    try:
        lease = _read()
    except LeaseUnreadable as e:
        # Don't touch a file we can't parse — could be a peer's in-progress
        # write or genuine corruption; either way this tool won't guess.
        print(
            f"REFUSE to release: {e}\n"
            f"Inspect {LEASE} by hand; this tool will not delete a file it can't parse."
        )
        return 3
    # Release if: it's an untracked lease (pid 0 — cooperative, anyone can clear
    # their own), or the pid matches this/the SFS_RELEASE_PID process.
    owners = {0, os.getpid(), int(os.environ.get("SFS_RELEASE_PID", -1))}
    if lease and int(lease.get("pid", 0) or 0) in owners:
        LEASE.unlink(missing_ok=True)
        print("released GPU lease")
        return 0
    # allow forced release of a stale lease
    if lease:
        stale, why = _is_stale(lease)
        if stale:
            LEASE.unlink(missing_ok=True)
            print(f"force-released stale lease ({why})")
            return 0
        print(f"refuse: lease held by live pid {lease.get('pid')} — not yours")
        return 3
    print("no lease to release")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "cmd", choices=["status", "acquire", "heartbeat", "release", "free", "register"]
    )
    ap.add_argument("--project", default="?")
    ap.add_argument("--purpose", default="?")
    ap.add_argument("--eta", type=int, default=3600)
    ap.add_argument("--pid", type=int, default=0)
    a = ap.parse_args()
    if int(getattr(a, "pid", 0) or 0) > 0:
        from pidfix import resolve_pid  # Git Bash $! -> Windows pid; refuse dead pids

        _p, _note = resolve_pid(a.pid)
        if _note:
            print(_note, file=sys.stderr)
        if _p is None:
            return 2
        a.pid = _p
    if a.cmd == "status":
        print(json.dumps(status(), indent=2))
        return 0
    if a.cmd == "free":
        s = status()
        ok = s["state"] in ("free", "stale")
        print("FREE" if ok else "HELD")
        return 0 if ok else 3
    if a.cmd == "acquire":
        return acquire(a.project, a.purpose, a.eta, pid=a.pid)
    if a.cmd == "register":
        return acquire_for_pid(a.pid, a.project, a.purpose, a.eta)
    if a.cmd == "heartbeat":
        return heartbeat()
    if a.cmd == "release":
        return release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
