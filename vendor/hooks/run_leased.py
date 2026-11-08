"""run_leased.py - run a long command as a REAL Windows child process with a CPU lease and an activity-ledger row
bound to its real PID, heartbeat both while it runs, and release/close on exit. Survives the parent agent exiting:
the orphan reaper never touches a PID held by a live lease or an open ledger row.

Why (2026-10-13): agents kept launching searches from MSYS bash with `$!` (an MSYS pid, not a Windows pid) and then
exiting their claude -p session. The lease pointed at a non-existent Windows pid, so it looked dead; the parent was
gone, so the Stop-hook reaper treated the search as an orphan and killed it (D3 cage cell, D4 min-modulus n=8).

Usage:
  python ~/.claude/coord/run_leased.py --project X --purpose "what" --cores 8 --eta 28800 [--log out.log] -- <cmd> [args...]
Fleet handshake (2026-11-01) options:
  --job-key K            durable identity (project/slug); writes RUN_STATE runs/<K with / -> __>.json
  --resume-cmd "..."     command used to relaunch after a restart (default: the original cmd)
  --mem-gb G             memory lease + Job Object memory limit of G*1.25
  --restart {no,on-oom,on-fail} --max-restarts 3 --backoff 60
  --no-resume-ok         allow --eta > 1800 with --job-key but no --resume-cmd
Exit code = the command's exit code (3 = lease refused, 4 = duplicate live job key, 2 = usage).
Writes "<log>.pid" with the Windows pid.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time

COORD = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
OOM_RC = (0xC0000017, 3221225495)
HB_SECONDS = float(os.environ.get("SFS_HB_SECONDS", 60))

if os.name == "nt":
    # No console windows: when this runs detached, every console child (pwsh, python
    # heartbeats) would otherwise pop its own visible window.
    _NOWIN = subprocess.CREATE_NO_WINDOW
    _run, _Popen = subprocess.run, subprocess.Popen

    class _QuietPopen(_Popen):
        def __init__(self, *a, **k):
            k["creationflags"] = k.get("creationflags", 0) | _NOWIN
            super().__init__(*a, **k)

    subprocess.Popen = _QuietPopen


# ── RUN_STATE ────────────────────────────────────────────────────────────


def runs_dir():
    root = os.environ.get("SFS_COORD_DIR") or COORD
    return os.path.join(root, "runs")


def run_state_path(job_key):
    return os.path.join(
        runs_dir(), job_key.replace("/", "__").replace("\\", "__") + ".json"
    )


def proc_create_time(pid):
    try:
        import psutil

        return psutil.Process(pid).create_time()
    except Exception:
        return None


def pid_is_current(pid, create_time):
    """Alive AND the same process (pid + create_time) - defeats PID reuse."""
    try:
        import psutil

        if not pid or pid <= 0 or not psutil.pid_exists(pid):
            return False
        if create_time is None:
            return True
        return abs(psutil.Process(pid).create_time() - float(create_time)) < 1.0
    except Exception:
        return False


def read_run_state(job_key):
    try:
        with open(run_state_path(job_key), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def state_is_live(st):
    if not st:
        return False
    if st.get("status") not in ("running", "restarting"):
        return False
    return pid_is_current(int(st.get("pid") or 0), st.get("pid_create_time"))


def write_run_state(path, st):
    st["updated"] = time.time()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, path)


# ── Job Object ───────────────────────────────────────────────────────────


def _job_structs():
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [
            ("a", ctypes.c_int64),
            ("b", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("c", ctypes.c_size_t),
            ("d", ctypes.c_size_t),
            ("e", wintypes.DWORD),
            ("f", ctypes.c_size_t),
            ("g", wintypes.DWORD),
            ("h", wintypes.DWORD),
        ]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in "abcdef"]

    class EXT(ctypes.Structure):
        _fields_ = [
            ("Basic", BASIC),
            ("Io", IO),
            ("p", ctypes.c_size_t),
            ("j", ctypes.c_size_t),  # JobMemoryLimit
            ("pp", ctypes.c_size_t),
            ("pj", ctypes.c_size_t),  # PeakJobMemoryUsed
        ]

    return EXT


def _kill_on_close_job(pid, mem_limit_bytes=None):
    """Put pid's tree in a Job Object that kills every member when the handle
    closes: on normal exit (closed in run's finally) AND when this process dies
    for any reason (Windows closes the handle). Workers the command spawned can
    never outlive it. With mem_limit_bytes, also sets JOB_OBJECT_LIMIT_JOB_MEMORY
    so a runaway job fails alone. Returns the handle, or None."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.OpenProcess.restype = wintypes.HANDLE
    EXT = _job_structs()

    job = k.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXT()
    info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if mem_limit_bytes:
        info.Basic.LimitFlags |= 0x200  # JOB_OBJECT_LIMIT_JOB_MEMORY
        info.j = int(mem_limit_bytes)
    k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
    h = k.OpenProcess(0x0101, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    ok = bool(h) and k.AssignProcessToJobObject(job, h)
    if h:
        k.CloseHandle(h)
    if not ok:
        k.CloseHandle(job)
        return None
    return job


def _job_peak_bytes(job):
    if not job or os.name != "nt":
        return 0
    try:
        import ctypes

        EXT = _job_structs()
        info = EXT()
        if ctypes.windll.kernel32.QueryInformationJobObject(
            job, 9, ctypes.byref(info), ctypes.sizeof(info), None
        ):
            return int(info.pj)
    except Exception:
        pass
    return 0


def _close_job(job):
    if job:
        import ctypes

        ctypes.windll.kernel32.CloseHandle(job)


def _log_tail(path, offset, n=8192):
    try:
        with open(path, "rb") as f:
            f.seek(max(offset, os.path.getsize(path) - n))
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _helper(script, *argv, **kw):
    return subprocess.run(
        [PY, os.path.join(COORD, script), *argv], capture_output=True, text=True, **kw
    )


def _kill_tree(pid):
    subprocess.run(
        [PY, os.path.join(COORD, "proc_tree.py"), "kill", str(pid)],
        capture_output=True,
        timeout=120,
    )


# ── one incarnation ──────────────────────────────────────────────────────


def _is_io_exe(cmd):
    return bool(cmd) and os.path.basename(cmd[0]).lower() in (
        "robocopy",
        "robocopy.exe",
        "xcopy",
        "xcopy.exe",
    )


def _io_disk_gate(cmd):
    """io admission: destination free space must cover the source size (robocopy SRC DST ...)."""
    try:
        if not _is_io_exe(cmd) or len(cmd) < 3 or not os.path.isdir(cmd[1]):
            return True, ""
        import disk_steward as _ds

        need = _ds.dirsize(cmd[1], 60)
        dst = cmd[2]
        while dst and not os.path.exists(dst):
            nd = os.path.dirname(dst)
            if nd == dst:
                return True, ""
            dst = nd
        free = __import__("shutil").disk_usage(dst).free
        if free < need:
            return False, "io disk gate: dest free %.1f GB < source %.1f GB" % (
                free / 2**30,
                need / 2**30,
            )
    except Exception:
        pass
    return True, ""


def run_once(args, cmd, mem_gb, inc, st, st_path, log):
    """Run cmd once. Returns (rc, oom, refused). rc 3 + refused=True = lease refused."""
    os.environ["JOB_INCARNATION"] = str(inc)
    if args.job_key:
        os.environ["JOB_KEY"] = args.job_key
        os.environ["RUN_STATE"] = st_path
    # Leases and the ledger row are taken BEFORE spawn, bound to this launcher's own
    # pid (alive for the whole run), then the CPU lease/ledger row are rebound to the
    # child pid. A child that dies instantly still gets a row with an exit status.
    me = os.getpid()
    gpu = False
    lease_id = led_id = ""
    if args.gpu:
        g = _helper(
            "gpu_lease.py",
            "acquire",
            "--project",
            args.project,
            "--purpose",
            args.purpose,
            "--eta",
            str(args.eta),
            "--pid",
            str(me),
        )
        if g.returncode != 0:
            sys.stderr.write("gpu lease refused: %s\n" % (g.stdout + g.stderr).strip())
            return 3, False, True
        gpu = True

    def _release_pre():
        if gpu:
            subprocess.run(
                [PY, os.path.join(COORD, "gpu_lease.py"), "release"],
                capture_output=True,
                env={**os.environ, "SFS_RELEASE_PID": str(me)},
            )
        if lease_id:
            _helper("cpu_lease.py", "release", "--id", lease_id)

    lease_argv = [
        "acquire",
        "--project",
        args.project,
        "--purpose",
        args.purpose,
        "--cores",
        str(args.cores),
        "--eta",
        str(args.eta),
        "--pid",
        str(me),
    ]
    if mem_gb:
        lease_argv += ["--mem-gb", "%.3f" % mem_gb]
    if getattr(args, "io", False) or _is_io_exe(cmd):
        lease_argv += ["--io"]
    lease = _helper("cpu_lease.py", *lease_argv)
    if lease.returncode != 0:
        sys.stderr.write("lease refused: %s\n" % lease.stderr.strip())
        _release_pre()
        return 3, False, True
    lease_id = lease.stdout.strip()
    led = _helper(
        "activity_ledger.py",
        "start",
        "--resource",
        "both" if gpu else "cpu",
        "--project",
        args.project,
        "--task",
        args.purpose,
        "--eta",
        str(args.eta),
        "--pid",
        str(me),
    )
    led_id = led.stdout.strip()
    if not led_id:
        sys.stderr.write(
            "ledger start failed (rc %s): %s\n"
            % (led.returncode, (led.stdout + led.stderr).strip()[-400:])
        )
    out = open(log, "ab", buffering=0)
    log_off = os.path.getsize(log)
    creation = 0
    if os.name == "nt":
        creation = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(
            subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0
        )
    try:
        try:
            p = subprocess.Popen(
                cmd, stdout=out, stderr=subprocess.STDOUT, creationflags=creation
            )
        except PermissionError:  # parent job (e.g. Task Scheduler) forbids breakaway
            p = subprocess.Popen(
                cmd,
                stdout=out,
                stderr=subprocess.STDOUT,
                creationflags=creation
                & ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0),
            )
    except Exception as exc:  # spawn failed: close the row, free the leases
        sys.stderr.write("spawn failed: %s\n" % exc)
        out.close()
        _release_pre()
        if led_id:
            _helper(
                "activity_ledger.py",
                "end",
                "--id",
                led_id,
                "--status",
                "failed",
                "--note",
                "spawn failed: %s" % exc,
            )
        return 127, False, False
    limit = int(mem_gb * 1.25 * 2**30) if mem_gb else None
    job = _kill_on_close_job(p.pid, limit)
    with open(log + ".pid", "w") as f:
        f.write(str(p.pid))
    if p.poll() is None:  # rebind from the launcher pid to the child pid
        _helper("cpu_lease.py", "rebind", "--id", lease_id, "--pid", str(p.pid))
        if led_id:
            _helper("activity_ledger.py", "rebind", "--id", led_id, "--pid", str(p.pid))
    print(
        "pid=%s lease=%s ledger=%s log=%s inc=%s" % (p.pid, lease_id, led_id, log, inc),
        flush=True,
    )

    if st is not None:
        st.update(
            pid=p.pid,
            pid_create_time=proc_create_time(p.pid),
            status="running",
            incarnation=inc,
            mem_gb=mem_gb or None,
            log=log,
        )
        write_run_state(st_path, st)

    t0 = time.time()
    last_hb = t0
    rc = None
    peak = 0
    try:
        while True:
            try:
                rc = p.wait(timeout=min(1.0, HB_SECONDS))
                break
            except subprocess.TimeoutExpired:
                pass
            peak = max(peak, _job_peak_bytes(job))
            if time.time() - last_hb < HB_SECONDS:
                continue
            last_hb = time.time()
            left = max(60, int(args.eta - (time.time() - t0)))
            if lease_id:
                _helper(
                    "cpu_lease.py", "heartbeat", "--id", lease_id, "--eta", str(left)
                )
            if led_id:
                _helper(
                    "activity_ledger.py",
                    "heartbeat",
                    "--id",
                    led_id,
                    "--eta",
                    str(left),
                )
            if st is not None:
                st["mem_peak_mb"] = max(st.get("mem_peak_mb") or 0, int(peak / 2**20))
                write_run_state(st_path, st)
    finally:
        peak = max(peak, _job_peak_bytes(job))
        _close_job(job)
        if gpu:
            subprocess.run(
                [PY, os.path.join(COORD, "gpu_lease.py"), "release"],
                capture_output=True,
                env={**os.environ, "SFS_RELEASE_PID": str(me)},
            )
        if lease_id:
            _helper("cpu_lease.py", "release", "--id", lease_id)
        if led_id:
            stt = (
                "concluded"
                if rc in args.ok_set
                else ("failed" if rc is not None else "quit")
            )
            _helper(
                "activity_ledger.py",
                "end",
                "--id",
                led_id,
                "--status",
                stt,
                "--note",
                "exit=%s" % rc,
            )
        out.close()
    oom = False
    if rc is not None and rc not in args.ok_set:
        tail = _log_tail(log, log_off)
        oom = (
            rc in OOM_RC
            or "MemoryError" in tail
            or (limit is not None and peak >= 0.97 * limit)
        )
    if st is not None:
        st["mem_peak_mb"] = max(st.get("mem_peak_mb") or 0, int(peak / 2**20))
    return rc, oom, False


WIN_FLAG_HINT = (
    "Windows tools with /FLAGS (robocopy, xcopy, icacls) must be launched from the "
    "PowerShell tool: Git Bash rewrites /E into E:/, and MSYS_NO_PATHCONV=1 then "
    "breaks the ~ script path."
)


def parse_ok_codes(spec, exe=""):
    """'0-7' / '0,1,3' / '0,2-4' -> set of ints. Default {0}; robocopy defaults to 0-7."""
    if spec is None:
        spec = "0-7" if exe in ("robocopy", "robocopy.exe") else "0"
    out = set()
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.update(range(int(lo), int(hi) + 1))
        else:
            out.add(int(part))
    if not out:
        raise ValueError("empty set")
    return out


def _run_inner(args, cmd):
    if not hasattr(args, "ok_set"):
        args.ok_set = parse_ok_codes(
            getattr(args, "ok_codes", None),
            os.path.basename(cmd[0]).lower() if cmd else "",
        )
    log = args.log or os.path.join(os.getcwd(), "run_leased.log")
    open(log, "ab").close()
    st = st_path = None
    if args.job_key:
        st_path = run_state_path(args.job_key)
        st = {
            "job_key": args.job_key,
            "owner_session": os.environ.get("SFS_AGENT") or "unknown",
            "model": os.environ.get("SFS_MODEL"),
            "pid": 0,
            "pid_create_time": None,
            "cmd": cmd,
            "cwd": os.getcwd(),
            "log": log,
            "status": "running",
            "incarnation": 1,
            "restarts": 0,
            "mem_gb": args.mem_gb or None,
            "mem_peak_mb": 0,
            "resume_cmd": args.resume_cmd,
        }
        if state_is_live(read_run_state(args.job_key)):
            sys.stderr.write(
                "job key %s already has a live process (RUN_STATE %s)\n"
                % (args.job_key, st_path)
            )
            return 4
        write_run_state(st_path, st)

    mem = float(args.mem_gb or 0)
    inc = 1
    restarts = 0
    cur_cmd = cmd
    while True:
        rc, oom, refused = run_once(args, cur_cmd, mem, inc, st, st_path, log)
        if refused:
            if inc > 1 and st is not None:
                st["status"] = "parked-oom"
                write_run_state(st_path, st)
                _park_notice(args, "mem lease refused on restart")
            elif st is not None:
                st["status"] = "failed"
                write_run_state(st_path, st)
            return 3
        if rc in args.ok_set:
            if st is not None:
                st["status"] = "concluded"
                write_run_state(st_path, st)
            return 0
        want = (args.restart == "on-fail") or (args.restart == "on-oom" and oom)
        if rc is None or not want:
            if st is not None:
                st["status"] = "failed"
                write_run_state(st_path, st)
            return rc if rc is not None else 1
        if restarts >= args.max_restarts:
            if st is not None:
                st["status"] = "parked-oom" if oom else "failed"
                st["restarts"] = restarts
                write_run_state(st_path, st)
            if oom:
                _park_notice(args, "max restarts reached")
            return rc
        restarts += 1
        inc += 1
        if oom and mem:
            mem = round(mem * 1.5, 3)
        if st is not None:
            st.update(
                status="restarting",
                incarnation=inc,
                restarts=restarts,
                mem_gb=mem or None,
                pid=0,
            )
            write_run_state(st_path, st)
        time.sleep(args.backoff * (2 ** (restarts - 1)))
        if args.resume_cmd:
            cur_cmd = shlex.split(args.resume_cmd, posix=(os.name != "nt"))


def run(args, cmd):
    """Disk admission (--disk-gb) and path claims (--writes) around the job."""
    import claims as _cl
    import disk_steward as _ds

    rid = None
    if getattr(args, "io", False) or _is_io_exe(cmd):
        ok, msg = _io_disk_gate(cmd)
        if not ok:
            sys.stderr.write("refused: " + msg + chr(10))
            return 3
    gb = float(getattr(args, "disk_gb", 0) or 0)
    if gb:
        ok, msg = _ds.admit(gb)
        if not ok:
            sys.stderr.write("refused: %s\n" % msg)
            return 3
        rid = _ds.reserve(gb)
    writes = list(getattr(args, "writes", None) or [])
    handle = os.environ.get("SFS_AGENT") or "unknown"
    note = "run_leased %s" % (args.job_key or args.purpose)
    for w in writes:
        _cl.set_claim(w, "live", handle=handle, note=note)
    try:
        return _run_inner(args, cmd)
    finally:
        for w in writes:
            try:
                _cl.set_claim(w, "keep", handle=handle, note=note + " (ended)")
            except Exception:
                pass
        if rid:
            _ds.unreserve(rid)


def _park_notice(args, why):
    """Durable record is RUN_STATE status=parked-oom; this only leaves a stderr line for the operator."""
    sys.stderr.write(
        "PARKED %s (%s); owner=%s; RUN_STATE %s; resume_cmd=%s\n"
        % (
            args.job_key,
            why,
            os.environ.get("SFS_AGENT") or "unknown",
            run_state_path(args.job_key),
            args.resume_cmd,
        )
    )


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--purpose", required=True)
    ap.add_argument("--cores", type=int, default=4)
    ap.add_argument("--eta", type=int, default=None)  # 1800 with --job-key else 3600
    ap.add_argument("--log", default=None)
    ap.add_argument("--gpu", action="store_true", help="also hold the GPU lease")
    ap.add_argument("--job-key", default=None)
    ap.add_argument("--resume-cmd", default=None)
    ap.add_argument("--mem-gb", type=float, default=0.0)
    ap.add_argument("--restart", choices=["no", "on-oom", "on-fail"], default="no")
    ap.add_argument("--max-restarts", type=int, default=3)
    ap.add_argument("--backoff", type=float, default=60)
    ap.add_argument(
        "--writes",
        action="append",
        default=[],
        help="path claimed live for the job, keep after",
    )
    ap.add_argument(
        "--disk-gb",
        type=float,
        default=0.0,
        help="disk admission; exit 3 if under floor",
    )
    ap.add_argument(
        "--io",
        action="store_true",
        help="I/O-bound job: 0.5-core lease, skips the CPU-load gate (auto for robocopy/xcopy)",
    )
    ap.add_argument("--no-resume-ok", action="store_true")
    ap.add_argument(
        "--ok-codes",
        default=None,
        help='exit codes counted as success, e.g. "0-7" or "0,1,3" (default 0; 0-7 for robocopy)',
    )
    return ap


if __name__ == "__main__":
    if "--" not in sys.argv:
        print(__doc__)
        sys.exit(2)
    i = sys.argv.index("--")
    a = build_parser().parse_args(sys.argv[1:i])
    if a.eta is None:  # keyed jobs default under the resume threshold
        a.eta = 1800 if a.job_key else 3600
    # Rule (fleet handshake): a keyed job longer than 30 min needs a resume path.
    # Scoped to --job-key so pre-existing un-keyed callers keep working unchanged.
    if a.job_key and a.eta > 1800 and not a.resume_cmd and not a.no_resume_ok:
        sys.stderr.write(
            "refused: --eta > 1800 needs --resume-cmd (or --no-resume-ok)\n"
        )
        sys.exit(2)
    cmd_ = sys.argv[i + 1 :]
    exe = os.path.basename(cmd_[0]).lower() if cmd_ else ""
    if exe in ("robocopy", "robocopy.exe", "xcopy", "xcopy.exe") and any(
        re.match(r"^[A-Z]:/([A-Z]+)?$", x) for x in cmd_[1:]
    ):
        sys.stderr.write(
            "refused: mangled /FLAG argument (e.g. 'E:/' for /E). "
            + WIN_FLAG_HINT
            + "\n"
        )
        sys.exit(2)
    try:
        a.ok_set = parse_ok_codes(a.ok_codes, exe)
    except ValueError as exc:
        sys.stderr.write("bad --ok-codes: %s\n" % exc)
        sys.exit(2)
    sys.exit(run(a, cmd_))
