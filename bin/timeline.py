#!/usr/bin/env python3
"""stillherd timeline RUN_ID | --since UTC     and     stillherd explain RUN_ID

Read-only view over records Stillherd already writes. No new collection.
RUN_ID is a run_leased job key (e.g. corral/egress) or an activity-ledger id. --since picks the latest run
started at or after the given UTC time (YYYY-MM-DDTHH:MM:SSZ).

Fields read (exhaustive; every record is reduced to this whitelist the moment it is parsed, all else is dropped):
  activity_ledger.jsonl   event id job_key ts pid project resource incarnation status session
  runs/<key>.json         status incarnation restarts mem_peak_mb updated
  cpu.leases.json         id job_key cores started_at
  gpu.lease.json          job_key started_at
  claims.jsonl            id ts project
  claims_verified.jsonl   id verified_ts verdict claimed_done
  silent / egress logs    ts host action method bytes
  *.dsse.json             payloadType, signatures[0].keyid, predicate.started, predicate.ended, subject[].name
Never read: prompts, task text, commands, request paths, bodies, headers, credentials, notes, check output.
Unknown is reported as UNKNOWN, never as a pass.
Env: STILLHERD_COORD (default ~/.claude/coord), STILLHERD_HOME (default ~/.stillherd), STILLHERD_SILENT_LOG,
STILLHERD_EGRESS_DIR, STILLHERD_ATT_DIR (extra dir of *.dsse.json)."""

import base64
import datetime as dt
import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
COORD = os.environ.get("STILLHERD_COORD", os.path.join(HOME, ".claude", "coord"))
SH = os.environ.get("STILLHERD_HOME", os.path.join(HOME, ".stillherd"))

F_LEDGER = (
    "event",
    "id",
    "job_key",
    "ts",
    "pid",
    "project",
    "resource",
    "incarnation",
    "status",
    "session",
)
F_STATE = ("status", "incarnation", "restarts", "mem_peak_mb", "updated")
F_CPU = ("id", "job_key", "cores", "started_at")
F_GPU = ("job_key", "started_at")
F_CLAIM = ("id", "ts", "project")
F_VER = ("id", "verified_ts", "verdict", "claimed_done")
F_NET = ("ts", "host", "action", "method", "bytes")


def pick(r, fields):
    return {k: r[k] for k in fields if k in r} if isinstance(r, dict) else {}


def jsonl(path, fields):
    """[(lineno, whitelisted-dict)] ; missing file -> None."""
    if not os.path.isfile(path):
        return None
    out = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for n, l in enumerate(fh, 1):
            try:
                out.append((n, pick(json.loads(l), fields)))
            except ValueError:
                pass
    return out


def jfile(path, fields):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return pick(json.load(fh), fields)
    except (OSError, ValueError):
        return None


def epoch(v):
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Run:
    pass


def load(run_id, since):
    R = Run()
    led = jsonl(os.path.join(COORD, "activity_ledger.jsonl"), F_LEDGER) or []
    starts = [(n, r) for n, r in led if r.get("event") == "start"]
    if since:
        t0 = epoch(since)
        cand = [(n, r) for n, r in starts if r.get("ts", 0) >= (t0 or 0)]
    else:
        cand = [
            (n, r)
            for n, r in starts
            if r.get("id") == run_id or r.get("job_key") == run_id
        ]
    R.start = cand[-1] if cand else None
    R.run_id = run_id
    if not R.start:
        return R
    s = R.start[1]
    R.id = s["id"]
    R.job_key = s.get("job_key")
    R.rows = [(n, r) for n, r in led if r.get("id") == R.id]
    R.end = next(((n, r) for n, r in R.rows if r.get("event") == "end"), None)
    R.state_file = None
    R.state = None
    if R.job_key:
        R.state_file = "runs/%s.json" % R.job_key.replace("/", "__")
        R.state = jfile(
            os.path.join(COORD, "runs", R.job_key.replace("/", "__") + ".json"), F_STATE
        )
    R.t0 = s["ts"]
    R.t1 = R.end[1]["ts"] if R.end else None
    R.cpu = None
    try:
        with open(
            os.path.join(COORD, "cpu.leases.json"), encoding="utf-8", errors="replace"
        ) as fh:
            for L in json.load(fh).get("leases", []):
                L = pick(L, F_CPU)
                if L.get("id") == R.id or (R.job_key and L.get("job_key") == R.job_key):
                    R.cpu = L
    except (OSError, ValueError):
        pass
    R.gpu = None
    g = jfile(os.path.join(COORD, "gpu.lease.json"), F_GPU)
    if g and R.job_key and g.get("job_key") == R.job_key:
        R.gpu = g

    def inw(t):
        return (
            isinstance(t, (int, float))
            and t >= R.t0
            and (R.t1 is None or t <= R.t1 + 5)
        )

    R.net = []
    sp = os.environ.get("STILLHERD_SILENT_LOG") or next(
        (
            p
            for p in (
                os.path.join(SH, "silent.log.jsonl"),
                os.path.join(HERE, "..", "silent", "silent.log.jsonl"),
            )
            if os.path.exists(p)
        ),
        "",
    )
    srcs = [sp] if sp else []
    ed = os.environ.get(
        "STILLHERD_EGRESS_DIR", os.path.join(HERE, "..", "egress", "results")
    )
    srcs += sorted(glob.glob(os.path.join(ed, "*.jsonl")))
    for p in srcs:
        for n, r in jsonl(p, F_NET) or []:
            if inw(r.get("ts")):
                R.net.append((r["ts"], "%s:%d" % (os.path.basename(p), n), r))
    R.net_sources = bool(srcs)
    R.proj = s.get("project")
    cl = jsonl(os.path.join(COORD, "claims.jsonl"), F_CLAIM)
    ver = jsonl(os.path.join(COORD, "claims_verified.jsonl"), F_VER)
    R.claims_missing = cl is None or ver is None
    vmap = {r.get("id"): (n, r) for n, r in ver or []}
    R.claims = []
    for n, r in cl or []:
        t = epoch(r.get("ts"))
        if R.proj and r.get("project") == R.proj and t is not None and inw(t):
            R.claims.append((n, t, r, vmap.get(r.get("id"))))
    R.atts = []
    for d in (os.path.join(SH, "days"), os.environ.get("STILLHERD_ATT_DIR", "")):
        for p in sorted(glob.glob(os.path.join(d, "*.dsse.json"))) if d else []:
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    env = json.load(fh)
                st = json.loads(base64.b64decode(env["payload"]))
                pr = st.get("predicate", {})
                a, b = epoch(pr.get("started")), epoch(pr.get("ended"))
                kid = (env.get("signatures") or [{}])[0].get("keyid", "")
                names = [
                    x.get("name") for x in st.get("subject", []) if isinstance(x, dict)
                ]
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if (
                a is not None
                and b is not None
                and a >= R.t0 - 1
                and (R.t1 is None or a <= R.t1)
            ):
                R.atts.append((os.path.basename(p), a, str(kid)[:16], names[:3]))
    return R


def timeline(R):
    ev = []
    n, s = R.start
    lf = "activity_ledger.jsonl"
    ev.append(
        (
            R.t0,
            "start",
            "run started: id=%s job_key=%s project=%s resource=%s pid=%s"
            % (
                R.id,
                R.job_key or "none",
                s.get("project", "UNKNOWN"),
                s.get("resource", "UNKNOWN"),
                s.get("pid", "UNKNOWN"),
            ),
            "%s:%d" % (lf, n),
        )
    )
    for n2, r in R.rows:
        if r.get("event") == "rebind":
            ev.append(
                (
                    r["ts"],
                    "proc-tree",
                    "pid rebound to %s" % r.get("pid", "UNKNOWN"),
                    "%s:%d" % (lf, n2),
                )
            )
    if R.cpu:
        ev.append(
            (
                R.cpu["started_at"],
                "lease",
                "cpu lease claimed: cores=%s id=%s"
                % (R.cpu.get("cores", "UNKNOWN"), R.cpu.get("id")),
                "cpu.leases.json:id=%s" % R.cpu.get("id"),
            )
        )
    if R.gpu:
        ev.append(
            (
                R.gpu["started_at"],
                "lease",
                "gpu lease claimed (still held)",
                "gpu.lease.json:job_key",
            )
        )
    for t, src, r in R.net:
        act = r.get("action") or ("request:" + str(r.get("method", "?")))
        ev.append(
            (
                t,
                "egress",
                "%s %s bytes=%s"
                % (act, r.get("host", "UNKNOWN"), r.get("bytes", "UNKNOWN")),
                src,
            )
        )
    for n2, t, r, v in R.claims:
        ev.append((t, "claim", "recorded id=%s" % r.get("id"), "claims.jsonl:%d" % n2))
        if v:
            vt = epoch(v[1].get("verified_ts")) or t
            ev.append(
                (
                    vt,
                    "claim",
                    "verified id=%s verdict=%s claimed_done=%s"
                    % (
                        r.get("id"),
                        v[1].get("verdict", "UNKNOWN"),
                        v[1].get("claimed_done", "UNKNOWN"),
                    ),
                    "claims_verified.jsonl:%d" % v[0],
                )
            )
    for f, a, kid, names in R.atts:
        ev.append(
            (
                a,
                "attest",
                "attestation keyid=%s subjects=%s (signature not checked here; use verify.sh)"
                % (kid, ",".join(x for x in names if x)),
                f,
            )
        )
    if R.end:
        ev.append(
            (
                R.t1,
                "end",
                "run ended: status=%s" % R.end[1].get("status", "UNKNOWN"),
                "%s:%d" % (lf, R.end[0]),
            )
        )
    if R.state:
        ev.append(
            (
                R.state.get("updated", R.t1 or R.t0),
                "run-state",
                "state=%s incarnation=%s restarts=%s mem_peak_mb=%s"
                % (
                    R.state.get("status", "UNKNOWN"),
                    R.state.get("incarnation", "UNKNOWN"),
                    R.state.get("restarts", "UNKNOWN"),
                    R.state.get("mem_peak_mb", "UNKNOWN"),
                ),
                R.state_file,
            )
        )
    ev.sort(key=lambda e: (e[0], e[1], e[3]))
    return ev


def cmd_timeline(R):
    print("stillherd timeline %s (UTC)" % R.run_id)
    for t, kind, text, src in timeline(R)[:200]:
        print("%s  %-9s %s  [%s]" % (iso(t), kind, text, src))
    if not R.end:
        print("(no end row for this run: end time UNKNOWN)")
    if not R.net_sources:
        print("(no silent/egress log found: egress UNKNOWN)")
    if not R.cpu:
        print("(no cpu lease record for this run: lease release time is not recorded)")


def cmd_explain(R):
    print("stillherd explain %s" % R.run_id)
    parts = []
    if R.end:
        parts.append(
            "ledger end status=%s [activity_ledger.jsonl:%d]"
            % (R.end[1].get("status", "UNKNOWN"), R.end[0])
        )
    if R.state:
        parts.append(
            "run state=%s restarts=%s [%s]"
            % (
                R.state.get("status", "UNKNOWN"),
                R.state.get("restarts", "UNKNOWN"),
                R.state_file,
            )
        )
    print(
        "why terminated: "
        + ("; ".join(parts) if parts else "UNKNOWN (no end row, no run state)")
    )
    blocked = [
        (s, r) for t, s, r in R.net if r.get("action") in ("blocked", "tls-failed")
    ]
    if blocked:
        print(
            "why blocked: %d request(s) refused by the allowlist proxy: %s [%s]"
            % (
                len(blocked),
                ", ".join(sorted({r.get("host", "UNKNOWN") for s, r in blocked})),
                blocked[0][0],
            )
        )
    elif not R.net_sources:
        print("why blocked: UNKNOWN (no silent/egress log)")
    elif not R.net:
        print("why blocked: UNKNOWN (no silent/egress record covers this run)")
    else:
        print(
            "why blocked: nothing blocked; %d request(s) logged, all allowed [%s]"
            % (len(R.net), R.net[0][1])
        )
    h = []
    if R.cpu:
        h.append(
            "cpu cores=%s [cpu.leases.json:id=%s]"
            % (R.cpu.get("cores", "UNKNOWN"), R.cpu.get("id"))
        )
    if R.gpu:
        h.append("gpu held [gpu.lease.json]")
    if not h:
        h.append(
            "UNKNOWN (no lease record; ledger declared resource=%s)"
            % (R.start[1].get("resource") or "UNKNOWN")
        )
    print("who held GPU/cores: " + "; ".join(h))
    if R.claims_missing:
        print("which claims passed: UNKNOWN (claims record file missing)")
    elif not R.claims:
        print("which claims passed: UNKNOWN (no claim recorded for this run)")
    else:
        for n, t, r, v in R.claims:
            if v and v[1].get("verdict") == "survived":
                res = "PASS verdict=survived [claims_verified.jsonl:%d]" % v[0]
            elif v:
                res = "NOT PASS verdict=%s [claims_verified.jsonl:%d]" % (
                    v[1].get("verdict", "UNKNOWN"),
                    v[0],
                )
            else:
                res = "UNKNOWN (no verify result)"
            print(
                "which claims passed: claim %s: %s (matched by project and time window)"
                % (r.get("id"), res)
            )
    print(
        "Unknown is reported as UNKNOWN, never as a pass; no prompts, code or bodies are collected."
    )


def main(argv):
    if not argv or argv[0] not in ("timeline", "explain") or len(argv) < 2:
        sys.stderr.write("usage: timeline.py timeline|explain RUN_ID | --since UTC\n")
        return 2
    mode, rest = argv[0], argv[1:]
    since = rid = None
    if rest[0] == "--since":
        since = rest[1] if len(rest) > 1 else None
        if since is None:
            return 2
    else:
        rid = rest[0]
    R = load(rid, since)
    if not R.start:
        print("%s: UNKNOWN (no ledger start row matches %s)" % (mode, rid or since))
        return 1
    R.run_id = rid or R.job_key or R.id
    (cmd_timeline if mode == "timeline" else cmd_explain)(R)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
