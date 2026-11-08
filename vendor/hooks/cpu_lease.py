"""cpu_lease.py — CORE-BUDGET admission control for CPU work on a shared box.

READ THIS FIRST — THIS IS NOT A MUTEX.
`gpu_lease.py` is a one-holder mutex because the GPU is a genuinely SERIAL
resource (two jobs thrash VRAM; a render went 45s/step -> 1637s/step). CPU is
the opposite: this box has 24 cores / 32 threads and the standing fleet
doctrine is "parallel CPU work is encouraged and nearly free". A CPU mutex
would be a self-inflicted outage — it would serialize the one resource we have
in abundance.

So this is a BUDGET ALLOCATOR, not a lock. Many holders coexist. A request is
refused only when the machine would be genuinely oversubscribed. In steady
state almost every `acquire` succeeds; that is the intended behavior, not a
sign the tool is doing nothing.

WHY IT EXISTS (2026-08-16). Eleven processes were each pinned to a full core
(ten peer research jobs + one orphan), the box sat at 63-73% sustained load,
and the fans ran hot for hours. Nothing was wrong with any individual job and
nothing was over the old rules — the ledger is a VISIBILITY board with no
admission control, so there was no point at which anyone asked "does the
machine have room for this?". Two concrete failures fell out of that:
  * No back-pressure. An eleventh job starts exactly as readily as the first.
  * Non-participants are invisible to a pure bookkeeping scheme. Four ledger
    rows had been STALLED for 22-30h and mapped to no live process at all,
    while the real load came from jobs that had taken no lease.
The second one is why admission also consults MEASURED system load (below) and
not just the sum of declared reservations.

TWO ADMISSION GATES — a request must pass BOTH:
  1. BUDGET: sum(cores of live leases) + requested <= GRANTABLE.
     GRANTABLE = total logical CPUs - INTERACTIVE_RESERVE. The reserve is not
     slack; it is what keeps VS Code, a browser, and the agent's own shell
     responsive while the fleet is saturated.
  2. MEASURED LOAD: current system-wide CPU load <= LOAD_CEILING.
     This is the gate that survives non-participants. Leases only describe
     work that opted in; load describes the machine. Today's incident would
     have been caught by (2) and missed entirely by (1).

WHAT THIS DELIBERATELY DOES NOT DO — thermal gating. The obvious design is
"refuse when the CPU is hot", and it is not implemented because there is no
unprivileged CPU temperature source on this box: MSAcpi_ThermalZoneTemperature
raises "Access to a CIM resource was not available to the client" without
elevation (verified 2026-08-16), and nvidia-smi reports the GPU only. Rather
than ship a thermal check that silently degrades to always-true — the exact
failure mode reap.py's `_norm_path` hit twice — load is used as the proxy and
the limitation is written down here. If an unprivileged temp source is ever
found, add it as gate (3); do not fake it.

FAILURE POLICY IS THE OPPOSITE OF gpu_lease.py, ON PURPOSE.
gpu_lease fails CLOSED on a corrupt file: unknown state is not free state, and
wrongly granting the GPU costs a 36x slowdown plus a WDDM VRAM leak that only
a reboot clears. Here the blast radii are reversed. Wrongly refusing every CPU
request halts the entire fleet at the one thing it is supposed to do in
parallel; wrongly granting merely returns the machine to its pre-existing
"unrestricted parallel" baseline, which is self-correcting and was the status
quo for months. So a corrupt file is QUARANTINED (moved aside, loudly) and we
start fresh, rather than blocking all CPU work until a human intervenes.

CONCURRENCY. Unlike gpu.lease.json (one object, one writer) this file is
multi-holder, so admission is a read-modify-write and a naive concurrent
write would over-grant. All mutations happen under a cross-process lock
(O_CREAT|O_EXCL sentinel with stale-lock breaking); the lock is held for
milliseconds. This mirrors the cross-process lock the recover ledger uses for
the same reason.

CLI:
  python cpu_lease.py status [--json]
  python cpu_lease.py budget
  python cpu_lease.py free --cores 4                 # exit 0 = grantable now
  python cpu_lease.py acquire --project X --purpose "..." --cores 4 \
      --eta 1800 [--pid <real job pid>]              # prints lease id
  python cpu_lease.py heartbeat --id <ID> [--eta N] [--note "..."]
  python cpu_lease.py release --id <ID>
  python cpu_lease.py gc                             # drop stale leases

Exit codes for `acquire`/`free`: 0 = granted, 3 = refused (do less, or wait).
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

COORD = Path.home() / ".claude" / "coord"
LEASES = COORD / "cpu.leases.json"
LOCK = COORD / "cpu.leases.lock"

# A lease past eta+GRACE with no heartbeat is presumed abandoned. Matches
# gpu_lease.GRACE / activity_ledger.DEAD_GRACE so all three tools agree on
# what "abandoned" means.
GRACE = 1800
MAX_LEASE_SECONDS = 14 * 3600

# The lock is held for milliseconds around a small read-modify-write. Anything
# older than this is a crashed holder, not a slow one.
LOCK_STALE_SECONDS = 30

TOTAL_CORES = os.cpu_count() or 8
# Never allocate these. Not slack — this is what keeps the editor, a browser
# and the agent's own shell usable while the fleet is saturated. A quarter of
# the box was chosen because at 32 threads it leaves 8, which comfortably
# covers an IDE + browser + shell without meaningfully starving batch work.
INTERACTIVE_RESERVE = int(
    os.environ.get("CLAUDE_CPU_RESERVE", max(2, TOTAL_CORES // 4))
)
GRANTABLE = max(1, TOTAL_CORES - INTERACTIVE_RESERVE)

# Measured system load above which we refuse regardless of the budget. Set
# below 100 deliberately: by the time the box pegs, interactive latency is
# already gone. 85 leaves room to notice before it hurts.
LOAD_CEILING = int(os.environ.get("CLAUDE_CPU_LOAD_CEILING", 85))


def _warn(msg: str) -> None:
    """Human-facing output goes to STDERR — always.

    STDOUT IS A DATA CHANNEL: `acquire` prints the lease id and nothing else,
    because the documented usage is `ID=$(cpu_lease.py acquire ...)`. Caught by
    the first real run of this file (2026-08-16): a refusal message printed to
    stdout was captured INTO `$id1`, and every downstream `--id "$id1"` then
    died on `unrecognized arguments`. The activity ledger has the same
    `print(aid)` capture contract, so the same rule applies here.
    """
    print(msg, file=sys.stderr)


# ── process identity (duplicated, not imported) ──────────────────────────
# gpu_lease.py and activity_ledger.py each carry their own copy of these; the
# latter documents the convention explicitly ("kept as a sibling function
# rather than a shared import, matching this file's existing pattern"). These
# tools must keep working if a sibling is edited or moved, so the duplication
# is deliberate. Keep the three copies behaviorally identical.


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
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
            timeout=20,
        )
        return out.stdout
    except Exception:
        return ""


def _parse_cim_date(created) -> float | None:
    """CIM renders CreationDate as `/Date(ms)/` or ISO depending on host."""
    if not isinstance(created, str):
        return None
    if created.startswith("/Date("):
        try:
            ms = int(created[6:].split(")")[0].split("+")[0])
            return ms / 1000
        except Exception:
            return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _pid_creation_ts_many(pids: set[int]) -> dict[int, float | None]:
    """Creation times for MANY pids in ONE PowerShell call.

    WHY THIS EXISTS: `_is_stale` calls the single-pid version once per TRACKED
    lease, and each call spawns a PowerShell process costing ~0.6s. Measured
    2026-08-16: 8 tracked leases took **5.20s and 8 spawned processes** on
    every `status` AND every `acquire`. An admission controller whose whole
    purpose is to reduce CPU contention must not spawn N subprocesses per
    invocation on an already-loaded box — at 20 leases that is ~13s of added
    latency and 20 more processes competing with the work it is arbitrating.

    Batching makes the cost O(1) in subprocesses regardless of lease count.
    Pids absent from the result are dead (CIM returns no row for them).
    """
    if not pids or os.name != "nt":
        return {}
    filt = " or ".join(f"ProcessId={int(p)}" for p in sorted(pids))
    raw = _ps(
        f'Get-CimInstance Win32_Process -Filter "{filt}" | '
        f"Select-Object ProcessId,CreationDate | ConvertTo-Json -Compress"
    )
    if not raw.strip():
        return {}
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if isinstance(rows, dict):
        rows = [rows]
    out: dict[int, float | None] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        pid = r.get("ProcessId")
        if pid is None:
            continue
        out[int(pid)] = _parse_cim_date(r.get("CreationDate"))
    return out


def _pid_creation_ts(pid: int) -> float | None:
    """Epoch seconds this pid was created, or None if unknown.

    Windows recycles PIDs aggressively, so `_pid_alive` alone cannot tell the
    real holder from an unrelated live process that inherited its number — a
    stale GPU lease holding explorer.exe's recycled PID once reported
    'held/live' for exactly this reason.
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
        return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _pid_is_current(
    pid: int,
    recorded_start_ts: float | None,
    prefetch: dict[int, float | None] | None = None,
) -> bool:
    """Alive AND the same process we recorded — not a PID-reuse impostor.

    Falls back to alive-only when the recorded timestamp is absent or
    uncoercible, matching the siblings: guessing "reused" would wrongly evict
    a live holder, and comparing a str to a float once raised TypeError and
    took the whole guard down with it.
    """
    if recorded_start_ts is None:
        # Legacy row with no recorded creation time — alive-only, as before.
        return _pid_alive(pid)
    try:
        recorded = float(recorded_start_ts)
    except (TypeError, ValueError):
        return _pid_alive(pid)
    # A batched prefetch answers BOTH liveness and identity in one lookup: CIM
    # returns no row for a dead pid, so absence == dead. Falling back to the
    # per-pid path only when no prefetch was supplied keeps single-lease
    # callers working unchanged.
    if prefetch is not None:
        cur = prefetch.get(pid)
        if cur is None:
            return pid in prefetch and _pid_alive(pid)
        return abs(cur - recorded) < 0.05
    if not _pid_alive(pid):
        return False
    cur = _pid_creation_ts(pid)
    if cur is None:
        return True
    # Both sides come from the same CIM CreationDate source, so a genuine
    # match is exact; the epsilon only absorbs JSON float round-tripping.
    return abs(cur - recorded) < 0.05


def _agent() -> str:
    return os.environ.get("SFS_AGENT") or os.environ.get("CLAUDE_AGENT") or "unknown"


def _ident(job_key=None, incarnation=None, create_time=None) -> dict:
    """Durable-identity fields (fleet handshake). All optional/additive."""
    inc = incarnation if incarnation is not None else os.environ.get("JOB_INCARNATION")
    try:
        inc = int(inc) if inc not in (None, "") else None
    except (TypeError, ValueError):
        inc = None
    return {
        "job_key": job_key or os.environ.get("JOB_KEY") or None,
        "incarnation": inc,
        "session": _agent(),
        "model": os.environ.get("SFS_MODEL") or None,
        "pid_create_time": create_time,
    }


def _total_ram_gb() -> float:
    try:
        import psutil

        return psutil.virtual_memory().total / 2**30
    except Exception:
        return 64.0


def _used_ram_gb() -> float | None:
    try:
        import psutil

        return psutil.virtual_memory().used / 2**30
    except Exception:
        return None


MEM_RESERVE_GB = float(os.environ.get("CLAUDE_MEM_RESERVE_GB", 12))
MEM_MEASURED_RESERVE_GB = float(os.environ.get("CLAUDE_MEM_MEASURED_RESERVE_GB", 6))


def _top_ram(n: int = 3) -> str:
    try:
        import psutil

        rows = []
        for p in psutil.process_iter(["name", "pid", "memory_info"]):
            mi = p.info.get("memory_info")
            if mi:
                rows.append((mi.rss, p.info.get("name"), p.info.get("pid")))
        rows.sort(reverse=True)
        return ", ".join(
            f"{nm}[{pid}] {rss / 2**30:.1f}GB" for rss, nm, pid in rows[:n]
        )
    except Exception:
        return "(unavailable)"


def _mem_admit(live: list, mem_gb: float) -> tuple[bool, str]:
    total = _total_ram_gb()
    budget = total - MEM_RESERVE_GB
    declared = sum(float(x.get("mem_gb") or 0) for x in live)
    used = _used_ram_gb()
    why = None
    if declared + mem_gb > budget:
        why = (
            f"memory budget: {declared:.1f}GB declared + {mem_gb:.1f}GB request "
            f"> {budget:.1f}GB (total {total:.0f} - {MEM_RESERVE_GB:.0f} reserve)"
        )
    elif used is not None and used + mem_gb > total - MEM_MEASURED_RESERVE_GB:
        why = (
            f"memory measured: {used:.1f}GB used + {mem_gb:.1f}GB request "
            f"> {total - MEM_MEASURED_RESERVE_GB:.1f}GB (total {total:.0f} - "
            f"{MEM_MEASURED_RESERVE_GB:.0f})"
        )
    if why:
        return False, (
            f"{why}. Top RAM: {_top_ram()}. Check `ollama ps` and free a model "
            f"with `ollama stop <model>`."
        )
    return True, "mem ok"


def system_load_pct() -> int | None:
    """Current system-wide CPU load, or None if unreadable.

    None must NOT be treated as 0. An unreadable load is unknown, and this
    gate exists to catch load from processes that took no lease — silently
    passing on None would make the gate a no-op precisely when the process
    table is under stress. Callers treat None as "gate unavailable" and say so.

    MEASURED WITH A SAMPLED COUNTER, NOT A SINGLE WMI READ. The obvious
    implementation — `Win32_Processor.LoadPercentage` — is an instantaneous
    ~1-second sample and is far too noisy to gate on. Ten consecutive reads on
    an unchanged workload gave [33, 8, 39, 74, 27, 42, 11, 27, 71, 49]
    (2026-08-16). Against an 85% ceiling that makes admission a coin flip, and
    worse, an 8% reading during genuine 70% load waves a large request
    straight through — defeating the gate precisely when it matters.

    `\\Processor(_Total)\\% Processor Time` is a real perf counter averaged
    over its sample interval. Three samples, median, in ONE PowerShell call so
    the fix costs no extra subprocesses. Median (not mean) so one spike or one
    trough cannot swing the decision.

    COST: ~4s per call, so `acquire`/`free`/`status` each take ~4-5s. That is
    deliberate — accuracy over latency. A lease is only warranted for work
    running >60s in the first place, so this is <1% overhead on anything that
    should be taking a lease, and a gate that answers wrongly in 0.1s is worth
    less than one that answers correctly in 4s. Do NOT drop the sample count
    to speed this up without re-measuring the noise (see the numbers above).
    """
    # Test-only override (conftest.py sets it) so peers' real load can never fail the suite.
    # Honoured only under pytest (PYTEST_CURRENT_TEST is inherited by test subprocesses),
    # so a live agent cannot set it to bypass the load gate.
    _ov = os.environ.get("SFS_CPU_LOAD_OVERRIDE")
    if _ov not in (None, "") and os.environ.get("PYTEST_CURRENT_TEST"):
        try:
            return max(0, min(100, int(float(_ov))))
        except ValueError:
            pass
    raw = _ps(
        "try { "
        "  $s = (Get-Counter '\\Processor(_Total)\\% Processor Time' "
        "        -SampleInterval 1 -MaxSamples 3 -ErrorAction Stop"
        "       ).CounterSamples.CookedValue; "
        "  [int](($s | Sort-Object)[1]) "
        "} catch { "
        "  [int](Get-CimInstance Win32_Processor | "
        "        Measure-Object -Property LoadPercentage -Average).Average "
        "}"
    )
    raw = raw.strip()
    if not raw:
        return None
    try:
        # Clamp: a counter can momentarily exceed 100 across logical CPUs.
        return max(0, min(100, int(float(raw))))
    except ValueError:
        return None


# ── cross-process lock ───────────────────────────────────────────────────


class _Lock:
    """Cross-process mutex around the read-modify-write of the lease file.

    O_CREAT|O_EXCL is atomic on Windows and POSIX alike. A crashed holder
    would deadlock every future writer, so a sentinel older than
    LOCK_STALE_SECONDS is broken — safe because the lock is only ever held
    for a few milliseconds, so any older sentinel is definitionally a crash.
    """

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self.acquired = False

    def __enter__(self):
        COORD.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, f"{os.getpid()} {time.time()}".encode())
                os.close(fd)
                self.acquired = True
                return self
            except OSError as e:
                if e.errno != errno.EEXIST:
                    raise
                if self._break_if_stale():
                    continue
                if time.time() > deadline:
                    # Never block a caller forever on a lock we cannot take.
                    # Proceeding unlocked risks a rare over-grant; refusing to
                    # run risks a fleet-wide stall. Given this allocator's
                    # fail-open posture, the over-grant is the cheaper error.
                    _warn(
                        f"WARN: could not take {LOCK.name} within {self.timeout}s; "
                        f"proceeding without it (a concurrent acquire may over-grant)"
                    )
                    return self
                time.sleep(0.05)

    def _break_if_stale(self) -> bool:
        try:
            age = time.time() - LOCK.stat().st_mtime
        except FileNotFoundError:
            return True  # vanished — retry immediately
        if age > LOCK_STALE_SECONDS:
            try:
                LOCK.unlink(missing_ok=True)
                _warn(f"broke stale lock ({int(age)}s old)")
                return True
            except OSError:
                return False
        return False

    def __exit__(self, *exc):
        if self.acquired:
            try:
                LOCK.unlink(missing_ok=True)
            except OSError:
                pass
        return False


# ── state ────────────────────────────────────────────────────────────────


def _quarantine(reason: str) -> None:
    """Move a corrupt lease file aside and start fresh. See module docstring
    for why this fails OPEN where gpu_lease.py fails closed."""
    stamp = int(time.time())
    dest = COORD / f"cpu.leases.corrupt.{stamp}.json"
    try:
        LEASES.replace(dest)
        _warn(f"WARN: {LEASES.name} was corrupt ({reason}); quarantined to {dest.name}")
    except OSError as e:
        _warn(f"WARN: {LEASES.name} corrupt ({reason}) and unmovable: {e}")


def _read() -> list[dict]:
    try:
        text = LEASES.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as e:
        _quarantine(str(e))
        return []
    if not isinstance(data, dict) or not isinstance(data.get("leases"), list):
        _quarantine("not a lease object")
        return []
    return [x for x in data["leases"] if isinstance(x, dict)]


def _write_atomic(leases: list[dict]) -> None:
    """temp+rename so a crash mid-write cannot leave a truncated file.

    Always unlink the temp in `finally` — an orphaned `.tmp.<pid>` from this
    exact write-then-crash pattern was found elsewhere in the portfolio
    (10.7MB, sat two days), so the cleanup is not hypothetical.
    """
    COORD.mkdir(parents=True, exist_ok=True)
    tmp = LEASES.with_name(f"{LEASES.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "leases": leases}, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, LEASES)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _is_stale(
    lease: dict, prefetch: dict[int, float | None] | None = None
) -> tuple[bool, str]:
    now = time.time()
    pid = int(lease.get("pid", 0) or 0)
    # Only a TRACKED pid (>0) can be pid-checked. `acquire` itself is an
    # ephemeral process whose pid dies immediately, so an untracked lease
    # (pid 0) must fall through to the heartbeat/eta checks instead of
    # instantly reading stale.
    if pid > 0 and not _pid_is_current(pid, lease.get("pid_start_ts"), prefetch):
        why = (
            "is dead"
            if not _pid_alive(pid)
            else "was reused by a different process (creation time mismatch)"
        )
        return True, f"pid {pid} {why}"
    hb = float(lease.get("heartbeat") or lease.get("started_at") or 0)
    eta = float(lease.get("eta_seconds") or 0)
    if now - hb > eta + GRACE:
        return True, f"no heartbeat for {int(now - hb)}s (> eta+{GRACE})"
    if now - float(lease.get("started_at") or now) > MAX_LEASE_SECONDS:
        return True, "exceeded MAX_LEASE_SECONDS"
    return False, "live"


def live_leases(leases: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """Split into (live, stale) without writing anything."""
    rows = _read() if leases is None else leases
    # ONE batched identity query for every tracked pid, instead of one
    # PowerShell spawn per lease (measured 5.20s / 8 processes for 8 leases).
    tracked = {int(x.get("pid") or 0) for x in rows if int(x.get("pid") or 0) > 0}
    prefetch = _pid_creation_ts_many(tracked) if tracked else {}
    live, stale = [], []
    for x in rows:
        is_stale, why = _is_stale(x, prefetch)
        if is_stale:
            stale.append({**x, "why": why})
        else:
            live.append(x)
    return live, stale


def reserved_cores(live: list[dict]) -> int:
    return sum(float(x.get("cores") or 0) for x in live)


# ── commands ─────────────────────────────────────────────────────────────


def cmd_status(a) -> int:
    live, stale = live_leases()
    used = reserved_cores(live)
    load = system_load_pct()
    payload = {
        "total_cores": TOTAL_CORES,
        "interactive_reserve": INTERACTIVE_RESERVE,
        "grantable": GRANTABLE,
        "reserved": used,
        "available": max(0, GRANTABLE - used),
        "load_pct": load,
        "load_ceiling": LOAD_CEILING,
        "live": live,
        "stale": stale,
    }
    if a.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(
        f"cores {used}/{GRANTABLE} reserved "
        f"(total {TOTAL_CORES}, reserve {INTERACTIVE_RESERVE})   "
        f"load {load if load is not None else '?'}%/{LOAD_CEILING}%"
    )
    for x in live:
        el = int(time.time() - float(x.get("started_at") or time.time()))
        print(
            f"  [{x.get('cores')}c] {x.get('project')}/{x.get('purpose')} "
            f"| id={x.get('id')} agent={x.get('agent')} pid={x.get('pid')} "
            f"| {el // 60}m elapsed"
        )
    for x in stale:
        print(
            f"  STALE {x.get('id')} {x.get('project')}/{x.get('purpose')} ({x['why']})"
        )
    if not live and not stale:
        print("  (no CPU leases held)")
    return 0


def cmd_budget(a) -> int:
    live, _ = live_leases()
    print(
        json.dumps(
            {
                "total_cores": TOTAL_CORES,
                "interactive_reserve": INTERACTIVE_RESERVE,
                "grantable": GRANTABLE,
                "reserved": reserved_cores(live),
                "available": max(0, GRANTABLE - reserved_cores(live)),
            },
            indent=2,
        )
    )
    return 0


def _admit(cores, io: bool = False) -> tuple[bool, str]:
    """Both gates. Returns (ok, human-readable reason)."""
    live, _ = live_leases()
    used = reserved_cores(live)
    if used + cores > GRANTABLE:
        return False, (
            f"budget: {used}/{GRANTABLE} cores reserved, request {cores} "
            f"would exceed it by {used + cores - GRANTABLE}"
        )
    if io:  # I/O-bound job (robocopy etc.): the CPU-load gate does not apply
        return True, f"io job: budget ok ({used + cores}/{GRANTABLE}), load gate skipped"
    load = system_load_pct()
    if load is None:
        # Unknown load is not zero load. Admit on the budget gate alone, but
        # say so — a silent pass here would hide the loss of the only gate
        # that sees non-participating work.
        return True, f"budget ok ({used + cores}/{GRANTABLE}); LOAD GATE UNAVAILABLE"
    if load > LOAD_CEILING:
        return False, f"measured load {load}% > ceiling {LOAD_CEILING}%"
    return True, f"budget ok ({used + cores}/{GRANTABLE}), load {load}%"


def cmd_free(a) -> int:
    ok, why = _admit(a.cores)
    print(("FREE — " if ok else "REFUSED — ") + why)
    return 0 if ok else 3


def cmd_acquire(a) -> int:
    io = bool(getattr(a, "io", False))
    if not io and a.cores < 1:
        _warn("REFUSED — --cores must be >= 1")
        return 3
    if a.cores > GRANTABLE:
        _warn(
            f"REFUSED — request {a.cores} exceeds the whole grantable budget "
            f"({GRANTABLE}). Split the job or lower CLAUDE_CPU_RESERVE."
        )
        return 3
    with _Lock():
        ok, why = _admit(0.5 if io else a.cores, io)
        if not ok:
            _warn(
                f"CPU REFUSED — {why}\n"
                f"Do less work in parallel, wait for a peer to release, or run "
                f"`cpu_lease.py status` to see who holds what. Do NOT kill peers."
            )
            return 3
        mem_gb = float(getattr(a, "mem_gb", 0) or 0)
        if mem_gb > 0:
            mok, mwhy = _mem_admit(live_leases()[0], mem_gb)
            if not mok:
                _warn(f"MEM REFUSED — {mwhy}")
                return 3
        now = time.time()
        lease_id = f"{os.getpid()}-{int(now)}"
        tracked = int(a.pid or 0)
        lease = {
            "id": lease_id,
            "agent": a.agent or _agent(),
            "project": a.project,
            "purpose": a.purpose,
            "cores": 0.5 if io else int(a.cores),
            "io": io,
            "pid": tracked,
            "pid_start_ts": _pid_creation_ts(tracked) if tracked > 0 else None,
            "started_at": now,
            "heartbeat": now,
            "eta_seconds": int(a.eta),
        }
        lease.update(
            _ident(
                getattr(a, "job_key", None),
                getattr(a, "incarnation", None),
                lease["pid_start_ts"],
            )
        )
        if mem_gb > 0:
            lease["mem_gb"] = mem_gb
        live, _ = live_leases()  # drops stale rows as a side effect of rewriting
        _write_atomic(live + [lease])
    print(lease_id)
    return 0


def cmd_heartbeat(a) -> int:
    # .strip(): a Git-Bash $(...) capture of a printed id can carry a stray
    # trailing \r, which silently broke id matching in the activity ledger
    # (~21 rows stuck "running" forever).
    wanted = a.id.strip()
    with _Lock():
        rows = _read()
        hit = False
        for x in rows:
            if str(x.get("id", "")).strip() == wanted:
                x["heartbeat"] = time.time()
                if a.eta is not None:
                    x["eta_seconds"] = int(a.eta)
                if a.note:
                    x["note"] = a.note
                hit = True
        if not hit:
            _warn(f"no such lease: {wanted}")
            return 1
        _write_atomic(rows)
    return 0


def cmd_rebind(a) -> int:
    """Rebind a lease to another pid (launcher pid -> child pid after spawn)."""
    wanted = a.id.strip()
    ts = _pid_creation_ts(int(a.pid)) if int(a.pid) > 0 else None
    with _Lock():
        rows = _read()
        hit = False
        for x in rows:
            if str(x.get("id", "")).strip() == wanted:
                x["pid"] = int(a.pid)
                x["pid_start_ts"] = ts
                x["pid_create_time"] = ts
                x["heartbeat"] = time.time()
                hit = True
        if not hit:
            _warn(f"no such lease: {wanted}")
            return 1
        _write_atomic(rows)
    return 0


def cmd_release(a) -> int:
    wanted = a.id.strip()
    with _Lock():
        rows = _read()
        keep = [x for x in rows if str(x.get("id", "")).strip() != wanted]
        if len(keep) == len(rows):
            _warn(f"no such lease: {wanted}")
            return 1
        _write_atomic(keep)
    print(f"released {wanted}")
    return 0


def cmd_gc(a) -> int:
    with _Lock():
        live, stale = live_leases()
        if stale:
            _write_atomic(live)
    for x in stale:
        print(
            f"reclaimed {x.get('id')} {x.get('project')}/{x.get('purpose')} ({x['why']})"
        )
    print(f"gc: {len(stale)} reclaimed, {len(live)} live")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_status)

    b = sub.add_parser("budget")
    b.set_defaults(fn=cmd_budget)

    f = sub.add_parser("free")
    f.add_argument("--cores", type=int, default=1)
    f.set_defaults(fn=cmd_free)

    ac = sub.add_parser("acquire")
    ac.add_argument("--project", default="?")
    ac.add_argument("--purpose", default="?")
    ac.add_argument("--cores", type=int, required=True)
    ac.add_argument("--eta", type=int, default=1800)
    ac.add_argument("--pid", type=int, default=0)
    ac.add_argument("--agent", default=None)
    ac.add_argument("--mem-gb", type=float, default=0.0, dest="mem_gb")
    ac.add_argument("--job-key", default=None, dest="job_key")
    ac.add_argument("--incarnation", type=int, default=None)
    ac.add_argument("--io", action="store_true", help="I/O-bound: 0.5-core reservation, skips the load gate")
    ac.set_defaults(fn=cmd_acquire)

    h = sub.add_parser("heartbeat")
    h.add_argument("--id", required=True)
    h.add_argument("--eta", type=int, default=None)
    h.add_argument("--note", default=None)
    h.set_defaults(fn=cmd_heartbeat)

    rb = sub.add_parser("rebind")
    rb.add_argument("--id", required=True)
    rb.add_argument("--pid", type=int, required=True)
    rb.set_defaults(fn=cmd_rebind)

    r = sub.add_parser("release")
    r.add_argument("--id", required=True)
    r.set_defaults(fn=cmd_release)

    g = sub.add_parser("gc")
    g.set_defaults(fn=cmd_gc)

    a = ap.parse_args()
    if int(getattr(a, "pid", 0) or 0) > 0:
        from pidfix import resolve_pid  # Git Bash $! -> Windows pid; refuse dead pids

        _p, _note = resolve_pid(a.pid)
        if _note:
            print(_note, file=sys.stderr)
        if _p is None:
            return 2
        a.pid = _p
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
