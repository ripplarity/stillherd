"""Resolve a --pid argument to a live Windows pid.

WHY: Git Bash `$!` and `$$` are MSYS pids, not Windows pids. A lease or ledger
row recorded with one looks dead at once, so a peer reclaims it mid-job
(2026-10-31: two GPU jobs thrashed for 2.5 h). MSYS exposes the mapping at
/proc/<pid>/winpid, readable through Git's own cat.exe.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_CATS = [
    Path(r"C:\Program Files\Git\usr\bin\cat.exe"),
    Path(r"C:\Program Files (x86)\Git\usr\bin\cat.exe"),
]


def _in_msys() -> bool:
    """True only inside an MSYS/Git Bash environment with Git's cat.exe at an absolute path."""
    return bool(os.environ.get("MSYSTEM")) and any(c.exists() for c in _CATS)


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def _msys_winpid(pid: int) -> int | None:
    for cat in _CATS:
        if not cat.exists():
            continue
        try:
            r = subprocess.run(
                [str(cat), f"/proc/{pid}/winpid"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            v = int(r.stdout.strip())
            return v if v > 0 else None
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return None


def resolve_pid(pid: int) -> tuple[int | None, str]:
    """(windows_pid, note). pid<=0 -> (0, ""). None means refuse: no live process."""
    pid = int(pid or 0)
    if pid <= 0:
        return 0, ""
    # An MSYS pid can collide with an unrelated live Windows pid, so prefer the
    # MSYS mapping when one exists and is alive.
    # Outside MSYS (no MSYSTEM, e.g. launched from PowerShell) or without Git's
    # cat.exe there is nothing to translate: keep the pid, never raise.
    if os.name == "nt" and _in_msys():
        try:
            w = _msys_winpid(pid)
            if w and w != pid and _alive(w):
                return (
                    w,
                    f"note: --pid {pid} is a Git Bash pid; recorded Windows pid {w}",
                )
        except Exception:
            pass
    if _alive(pid):
        return pid, ""
    return None, (
        f"REFUSE: --pid {pid} is not a live process (nor a live Git Bash pid). "
        "Pass the pid of the running job."
    )
