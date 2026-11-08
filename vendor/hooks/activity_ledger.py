"""Activity ledger — a shared VISIBILITY board for load-bearing CPU/GPU work.

Complements `gpu_lease.py`. The lease is a GPU *mutex* (one heavy GPU job at a
time). This ledger is the broader *who-is-doing-what* board: every agent running
a load-bearing task — GPU (train/render/model-load) OR CPU (long build, full
test sweep, big audio/data analysis, model download) — records when it started,
its ETA, and what it is, then marks it concluded/quit when done. The motivating
incident: another agent saw an unlabeled `python.exe` pegging resources and had
to reverse-engineer (via the command line) that it was an audio-mix analysis —
a one-line ledger entry would have said so.

DESIGN: append-only JSONL event log (`activity_ledger.jsonl`). Each agent only
ever APPENDS its own line, so concurrent writers never clobber a shared object
(unlike a read-modify-write JSON blob). Current state is reconstructed by folding
events by activity id. A "running" entry whose PID is dead and was never ended is
shown as `abandoned` — so a crash self-heals the board.

CLI (timestamps are real wall-clock; safe to run from a normal shell):
  python activity_ledger.py start  --resource gpu|cpu|both --project X \
      --task "flux lora train" --eta 7200 [--pid <pid>]      # prints an ID
  python activity_ledger.py heartbeat --id <ID> [--eta <sec>] [--note "..."]
  python activity_ledger.py end    --id <ID> --status concluded|quit|failed [--note "..."]
  python activity_ledger.py list   [--all] [--json]          # the board
  python activity_ledger.py gc     [--days 3]                # prune old ended rows

Rule of thumb for "load-bearing": any task expected to run > ~60s OR that holds
the GPU/lots of RAM/cores — log it. Quick one-off commands don't need an entry.
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

LEDGER = Path.home() / ".claude" / "coord" / "activity_ledger.jsonl"
DEAD_GRACE = 1800  # a running entry past eta+grace with no heartbeat = likely abandoned


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
            timeout=15,
        )
        return out.stdout
    except Exception:
        return ""


def _pid_creation_ts(pid: int) -> float | None:
    """Epoch seconds this pid was created, or None if unknown/unqueryable.

    Same rationale as gpu_lease.py's copy (kept as a sibling function rather
    than a shared import, matching this file's existing pattern of
    duplicating `_pid_alive` rather than cross-importing): `_pid_alive` alone
    can't tell a live-but-unrelated process from the real tracked one — proven
    when an unrelated live PID (explorer.exe) written into a stale lease made
    `status` report 'held/live'. Parsing mirrors reap.py's snapshot().
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
    recorded at `start` time — not a PID-reuse impostor.

    BACKWARD COMPATIBILITY: rows written before this fix carry no
    `pid_start_ts` (recorded_start_ts is None, since it's simply absent from
    the JSONL event dict) and keep the OLD behavior — pid-alive only — so
    upgrading this file cannot reclassify existing live rows as abandoned.
    """
    if not _pid_alive(pid):
        return False
    if recorded_start_ts is None:
        return True
    # Corrupt or foreign-format values must not crash the guard: comparing a
    # str against a float raised TypeError in the sibling implementation
    # (found by an end-to-end run against real processes, 2026-07-19). Fall
    # back to the legacy alive-only behavior on anything uncoercible.
    try:
        recorded = float(recorded_start_ts)
    except (TypeError, ValueError):
        return True
    cur_ts = _pid_creation_ts(pid)
    if cur_ts is None:
        return True
    # Both values come from the same CIM CreationDate source, so a real match
    # is exact; the epsilon only covers JSON float round-tripping. A 2.0s
    # tolerance would accept a PID recycled within two seconds of the
    # original's start — the exact impostor this check must reject.
    return abs(cur_ts - recorded) < 0.05


def _agent() -> str:
    return os.environ.get("SFS_AGENT") or os.environ.get("CLAUDE_AGENT") or "unknown"


def _append(event: dict) -> None:
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _read_events() -> list[dict]:
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _fold() -> dict[str, dict]:
    """Fold the event log into current state per activity id.

    IDS ARE STRIPPED BEFORE GROUPING (2026-07-26): ~21 `end` events written
    2026-07-25 carry an id with a literal trailing carriage return inside the
    JSON string (e.g. "38800-1784334547\r" — a Git-Bash `$(...)` command-
    substitution artifact), which then failed to match the clean id on their
    `start` event, leaving the row stuck "running" forever. Grouping on the
    stripped id closes those rows correctly regardless of which event(s)
    picked up the stray `\r`.
    """
    state: dict[str, dict] = {}
    for ev in _read_events():
        aid = ev.get("id")
        if not aid:
            continue
        if isinstance(aid, str):
            aid = aid.strip()
            if not aid:
                continue
        cur = state.setdefault(aid, {})
        kind = ev.get("event")
        if kind == "start":
            cur.update(
                {
                    k: ev.get(k)
                    for k in (
                        "id",
                        "agent",
                        "project",
                        "task",
                        "resource",
                        "pid",
                        "pid_start_ts",
                        "started_at",
                        "eta_seconds",
                    )
                }
            )
            cur["id"] = aid  # normalized, not the possibly-\r-suffixed raw value
            cur["status"] = "running"
            cur["last_ts"] = ev.get("ts")
        elif kind == "heartbeat":
            cur["last_ts"] = ev.get("ts")
            if ev.get("eta_seconds") is not None:
                cur["eta_seconds"] = ev.get("eta_seconds")
            if ev.get("note"):
                cur["note"] = ev.get("note")
        elif kind == "rebind":
            cur["pid"] = ev.get("pid")
            cur["pid_start_ts"] = ev.get("pid_start_ts")
            cur["last_ts"] = ev.get("ts")
        elif kind == "end":
            cur["status"] = ev.get("status", "concluded")
            cur["ended_at"] = ev.get("ts")
            cur["last_ts"] = ev.get("ts")
            if ev.get("note"):
                cur["note"] = ev.get("note")
    # post-process self-heal. A TRACKED pid (>0) that's dead → abandoned. An
    # UNTRACKED entry (pid 0) can't be pid-checked, so it only goes "stalled"
    # when it blows past eta+grace with no heartbeat/end (likely done-but-unlogged).
    now = time.time()
    for cur in state.values():
        if cur.get("status") == "running":
            pid = int(cur.get("pid", 0) or 0)
            hb = float(cur.get("last_ts") or cur.get("started_at") or 0)
            eta = float(cur.get("eta_seconds") or 0)
            if pid > 0 and not _pid_is_current(pid, cur.get("pid_start_ts")):
                reason = (
                    "dead"
                    if not _pid_alive(pid)
                    else "PID reused by a different process (creation time mismatch)"
                )
                cur["status"] = "abandoned"
                cur["why"] = f"pid {pid} {reason}, never ended"
            elif now - hb > eta + DEAD_GRACE:
                cur["status"] = "stalled"
                cur["why"] = f"no heartbeat for {int(now - hb)}s (> eta+{DEAD_GRACE})"
    return state


def _fmt_dur(s: float) -> str:
    s = int(max(0, s))
    if s < 90:
        return f"{s}s"
    if s < 5400:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def cmd_start(a) -> int:
    now = time.time()
    # The id is unique (start-command pid + ts). The TRACKED pid is separate and
    # OPTIONAL: pass --pid <real task pid> for crash auto-detection; default 0 =
    # untracked (end it explicitly; stall-detection is the only safety net).
    aid = f"{os.getpid()}-{int(now)}"
    tracked_pid = int(a.pid or 0)
    # Record creation time alongside the pid so a later PID-reuse impostor
    # (Windows recycles PIDs aggressively) can be told apart from the real
    # tracked process — see _pid_is_current.
    pid_start_ts = _pid_creation_ts(tracked_pid) if tracked_pid > 0 else None
    inc = (
        a.incarnation
        if a.incarnation is not None
        else os.environ.get("JOB_INCARNATION")
    )
    try:
        inc = int(inc) if inc not in (None, "") else None
    except (TypeError, ValueError):
        inc = None
    _append(
        {
            "event": "start",
            "ts": now,
            "id": aid,
            "agent": a.agent or _agent(),
            "project": a.project,
            "task": a.task,
            "resource": a.resource,
            "pid": tracked_pid,
            "pid_start_ts": pid_start_ts,
            "pid_create_time": pid_start_ts,
            "job_key": a.job_key or os.environ.get("JOB_KEY") or None,
            "incarnation": inc,
            "session": _agent(),
            "model": os.environ.get("SFS_MODEL") or None,
            "started_at": now,
            "eta_seconds": int(a.eta),
        }
    )
    print(aid)
    return 0


def cmd_heartbeat(a) -> int:
    # .strip(): a Git-Bash `$(...)` capture of a `start`-printed id can carry
    # a stray trailing \r, which would otherwise never match the start
    # event's clean id when _fold() groups by id.
    ev = {"event": "heartbeat", "ts": time.time(), "id": a.id.strip()}
    if a.eta is not None:
        ev["eta_seconds"] = int(a.eta)
    if a.note:
        ev["note"] = a.note
    _append(ev)
    return 0


def cmd_rebind(a) -> int:
    pid = int(a.pid)
    ts = _pid_creation_ts(pid) if pid > 0 else None
    _append(
        {
            "event": "rebind",
            "ts": time.time(),
            "id": a.id.strip(),
            "pid": pid,
            "pid_start_ts": ts,
            "pid_create_time": ts,
        }
    )
    return 0


def cmd_end(a) -> int:
    aid = a.id.strip()  # see cmd_heartbeat for why
    _append(
        {
            "event": "end",
            "ts": time.time(),
            "id": aid,
            "status": a.status,
            "note": a.note,
        }
    )
    print(f"ended {aid}: {a.status}")
    return 0


def cmd_list(a) -> int:
    state = _fold()
    rows = list(state.values())
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    now = time.time()
    active = [r for r in rows if r.get("status") in ("running", "stalled")]
    other = [r for r in rows if r.get("status") not in ("running", "stalled")]
    active.sort(key=lambda r: r.get("started_at") or 0)
    if not rows:
        print("ledger empty — no load-bearing activity recorded")
        return 0
    print(f"=== ACTIVE ({len(active)}) ===")
    for r in active:
        el = _fmt_dur(now - float(r.get("started_at") or now))
        eta = float(r.get("eta_seconds") or 0)
        rem = _fmt_dur(eta - (now - float(r.get("started_at") or now))) if eta else "?"
        flag = "  ** STALLED **" if r.get("status") == "stalled" else ""
        print(
            f"  [{r.get('resource', '?'):>4}] {r.get('project', '?')}/{r.get('task', '?')} "
            f"| agent={r.get('agent', '?')} pid={r.get('pid')} "
            f"| {el} elapsed, ~{rem} left{flag}"
        )
        if r.get("note"):
            print(f"         note: {r['note']}")
    if a.all and other:
        print(f"=== ENDED/ABANDONED ({len(other)}) ===")
        for r in sorted(
            other,
            key=lambda r: r.get("ended_at") or r.get("last_ts") or 0,
            reverse=True,
        )[:20]:
            tag = r.get("status", "?")
            why = f" ({r['why']})" if r.get("why") else ""
            print(
                f"  [{r.get('resource', '?'):>4}] {r.get('project', '?')}/{r.get('task', '?')} "
                f"| {tag}{why}"
            )
    return 0


def cmd_autoclose(a) -> int:
    """Append synthetic `end` events for rows that are provably finished.

    WHY THIS EXISTS (2026-08-18): the board is a VISIBILITY surface, and it
    had started lying. `cmd_gc`'s keep-set retains `status in ("running",
    "stalled")` UNCONDITIONALLY, so a stalled row could never be pruned no
    matter how old — phantoms accumulated monotonically (4 stalled rows on
    08-16, 12 by 08-18, none mapping to a live process). A peer glancing at
    `list` saw "12 ACTIVE" on a fully idle machine, which is exactly the
    reverse-engineer-the-command-line problem the ledger was built to end.

    APPEND-ONLY, NEVER A REWRITE. `cmd_gc` compacts by rewriting the whole
    file, which races with a concurrent appender and can silently drop its
    line — the file is JSONL precisely so writers never clobber each other.
    Auto-closing by APPENDING an `end` event keeps that invariant, preserves
    the row's history instead of deleting it, and needs no lock.

    Conservative by design: only rows already self-healed to stalled/abandoned
    (so the fold logic has independently concluded they are done) AND silent
    for longer than --older-than. Never touches a `running` row, and never
    touches a process — this is bookkeeping only.
    """
    state = _fold()
    now = time.time()
    closed = []
    for aid, r in state.items():
        if r.get("status") not in ("stalled", "abandoned"):
            continue
        last = float(r.get("last_ts") or r.get("started_at") or 0)
        idle = now - last
        if idle < a.older_than:
            continue
        pid = int(r.get("pid", 0) or 0)
        # A tracked pid that is somehow still current means the fold was wrong
        # about this row; leave it for a human rather than closing live work.
        if pid > 0 and _pid_is_current(pid, r.get("pid_start_ts")):
            continue
        if not a.dry_run:
            _append(
                {
                    "event": "end",
                    "ts": now,
                    "id": aid,
                    "status": "quit",
                    "note": (
                        f"auto-closed by `autoclose`: {r.get('status')} for "
                        f"{_fmt_dur(idle)} with no heartbeat"
                    ),
                }
            )
        closed.append((aid, r, idle))
    verb = "would close" if a.dry_run else "closed"
    for aid, r, idle in closed:
        print(
            f"  {verb} {r.get('project', '?')}/{r.get('task', '?')} "
            f"({r.get('status')}, silent {_fmt_dur(idle)})"
        )
    print(f"autoclose: {verb} {len(closed)} row(s)")
    return 0


def cmd_gc(a) -> int:
    """Compact: keep only events for activities ended < `days` ago or still running."""
    events = _read_events()
    state = _fold()
    cutoff = time.time() - a.days * 86400
    # `stalled` is NOT pinned here. It used to be, alongside `running`, which
    # meant gc could never prune a stalled row however old — the board filled
    # with phantoms permanently (see cmd_autoclose). Only genuinely `running`
    # rows are pinned now; a stalled row ages out on the same clock as an
    # ended one, and `autoclose` is the graceful way to retire it first.
    keep_ids = {
        aid
        for aid, r in state.items()
        if r.get("status") == "running"
        or float(r.get("ended_at") or r.get("last_ts") or 0) > cutoff
    }
    kept = [e for e in events if e.get("id") in keep_ids]
    LEDGER.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in kept),
        encoding="utf-8",
    )
    print(f"gc: kept {len(kept)}/{len(events)} events for {len(keep_ids)} activities")
    return 0


def cmd_compact(a) -> int:
    """Keep open (running) rows + everything from the last `days` days; append the rest
    to activity_ledger.YYYYMM.jsonl.gz (by event month). Archive and ledger are both
    replaced atomically (tmp + os.replace); lines appended during the run are carried over."""
    import gzip

    if not LEDGER.exists():
        print("compact: no ledger")
        return 0
    raw = LEDGER.read_bytes()
    size0 = len(raw)
    state = _fold()
    cutoff = time.time() - a.days * 86400
    keep_ids = {
        aid
        for aid, r in state.items()
        if r.get("status") == "running"
        or float(r.get("ended_at") or r.get("last_ts") or r.get("started_at") or 0)
        > cutoff
    }
    kept: list[bytes] = []
    arch: dict[str, list[bytes]] = {}
    for line in raw.splitlines(keepends=True):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
            aid = ev.get("id")
            aid = aid.strip() if isinstance(aid, str) else aid
            ts = float(ev.get("ts") or 0)
        except Exception:
            kept.append(line)  # unparseable: never destroy
            continue
        if (aid and aid in keep_ids) or (not aid and ts > cutoff):
            kept.append(line if line.endswith(b"\n") else line + b"\n")
        else:
            ym = time.strftime("%Y%m", time.localtime(ts)) if ts else "unknown"
            arch.setdefault(ym, []).append(
                line if line.endswith(b"\n") else line + b"\n"
            )
    # Collapse heartbeat chatter in kept rows: _fold only needs the last heartbeat
    # overall, the last carrying eta_seconds, and the last carrying a note.
    parsed = []
    for ln in kept:
        try:
            parsed.append(json.loads(ln))
        except Exception:
            parsed.append(None)
    last: dict[tuple, int] = {}
    for i, ev in enumerate(parsed):
        if ev and ev.get("event") == "heartbeat":
            aid = str(ev.get("id", "")).strip()
            last[(aid, "any")] = i
            if ev.get("eta_seconds") is not None:
                last[(aid, "eta")] = i
            if ev.get("note"):
                last[(aid, "note")] = i
    need = set(last.values())
    dropped = [
        ln
        for i, (ln, ev) in enumerate(zip(kept, parsed))
        if ev and ev.get("event") == "heartbeat" and i not in need
    ]
    if dropped:
        kept = [
            ln
            for i, (ln, ev) in enumerate(zip(kept, parsed))
            if not (ev and ev.get("event") == "heartbeat" and i not in need)
        ]
        for ln in dropped:
            ts = float(json.loads(ln).get("ts") or 0)
            ym = time.strftime("%Y%m", time.localtime(ts)) if ts else "unknown"
            arch.setdefault(ym, []).append(ln)
    pid = os.getpid()
    moved = 0
    for ym, lines in arch.items():
        dest = LEDGER.parent / f"activity_ledger.{ym}.jsonl.gz"
        tmp = dest.with_name(dest.name + f".tmp.{pid}")
        old = dest.read_bytes() if dest.exists() else b""
        tmp.write_bytes(old + gzip.compress(b"".join(lines)))
        os.replace(tmp, dest)
        moved += len(lines)
    cur = LEDGER.read_bytes()
    tail = cur[size0:] if len(cur) > size0 else b""
    tmp = LEDGER.with_name(LEDGER.name + f".tmp.{pid}")
    tmp.write_bytes(b"".join(kept) + tail)
    os.replace(tmp, LEDGER)
    print(
        f"compact: kept {len(kept)} lines, archived {moved} into {len(arch)} month file(s)"
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start")
    s.add_argument("--resource", choices=["gpu", "cpu", "both"], required=True)
    s.add_argument("--project", default="?")
    s.add_argument("--task", default="?")
    s.add_argument("--eta", type=int, default=3600)
    s.add_argument("--pid", type=int, default=0)
    s.add_argument("--agent", default=None)
    s.add_argument("--job-key", default=None, dest="job_key")
    s.add_argument("--incarnation", type=int, default=None)
    s.set_defaults(fn=cmd_start)
    h = sub.add_parser("heartbeat")
    h.add_argument("--id", required=True)
    h.add_argument("--eta", type=int, default=None)
    h.add_argument("--note", default=None)
    h.set_defaults(fn=cmd_heartbeat)
    rb = sub.add_parser("rebind")
    rb.add_argument("--id", required=True)
    rb.add_argument("--pid", type=int, required=True)
    rb.set_defaults(fn=cmd_rebind)
    e = sub.add_parser("end")
    e.add_argument("--id", required=True)
    e.add_argument(
        "--status",
        choices=["concluded", "quit", "failed", "abandoned-legacy"],
        default="concluded",
    )
    e.add_argument("--note", default=None)
    e.set_defaults(fn=cmd_end)
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)
    ac = sub.add_parser("autoclose")
    ac.add_argument(
        "--older-than",
        type=int,
        default=6 * 3600,
        help="seconds of silence before a stalled row is auto-closed",
    )
    ac.add_argument("--dry-run", action="store_true")
    ac.set_defaults(fn=cmd_autoclose)
    cp = sub.add_parser("compact")
    cp.add_argument("--days", type=int, default=7)
    cp.set_defaults(fn=cmd_compact)
    g = sub.add_parser("gc")
    g.add_argument("--days", type=int, default=3)
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
