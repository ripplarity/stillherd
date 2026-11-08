#!/usr/bin/env python3
"""stillherd today [YYYY-MM-DD]: one screen for a UTC day, then a signed day attestation.
Sources (each optional, "none recorded" when absent): activity ledger, claim receipts, silent log, egress results.
Env: STILLHERD_COORD (ledger+claims dir, default ~/.claude/coord), STILLHERD_HOME (default ~/.stillherd),
STILLHERD_SILENT_LOG, STILLHERD_EGRESS_DIR."""
import datetime as dt
import glob
import hashlib
import json
import os
import platform
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
COORD = os.environ.get("STILLHERD_COORD", os.path.join(HOME, ".claude", "coord"))
SH = os.environ.get("STILLHERD_HOME", os.path.join(HOME, ".stillherd"))
for d in (os.path.join(HERE, "..", "silent"), HERE):
    sys.path.insert(0, d)
day = sys.argv[1] if len(sys.argv) > 1 else dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


def rows(path):
    out = []
    try:
        for l in open(path, encoding="utf-8", errors="replace"):
            try:
                out.append(json.loads(l))
            except ValueError:
                pass
    except OSError:
        pass
    return [r for r in out if isinstance(r, dict)]


def iso_day(v):
    try:
        if isinstance(v, (int, float)):
            return dt.datetime.fromtimestamp(v, dt.timezone.utc).strftime("%Y-%m-%d")
        return str(v)[:10]
    except (ValueError, OverflowError, OSError):
        return ""


def on_day(rs, *keys):
    return [r for r in rs if any(iso_day(r.get(k)) == day for k in keys if r.get(k) is not None)]


ledger = [r for r in on_day(rows(os.path.join(COORD, "activity_ledger.jsonl")), "ts") if r.get("event") == "start"]
claims = on_day(rows(os.path.join(COORD, "claims.jsonl")), "ts")
verified = on_day(rows(os.path.join(COORD, "claims_verified.jsonl")), "verified_ts")
silent_p = os.environ.get("STILLHERD_SILENT_LOG") or next(
    (p for p in (os.path.join(SH, "silent.log.jsonl"), os.path.join(HERE, "..", "silent", "silent.log.jsonl")) if os.path.exists(p)), "")
silent = on_day(rows(silent_p), "ts") if silent_p else []
eg_dir = os.environ.get("STILLHERD_EGRESS_DIR", os.path.join(HERE, "..", "egress", "results"))
egress = []
for f in sorted(glob.glob(os.path.join(eg_dir, "*.jsonl"))):
    egress += [dict(r, _file=os.path.basename(f)) for r in on_day(rows(f), "ts")]


def row(title, items):
    print(f"{title}:")
    for i in items or ["none recorded"]:
        print(f"  {i}")


print(f"stillherd today {day} (UTC)")
procs = sorted({(r.get("project", "?"), r.get("task", "?"), r.get("resource", "?"), r.get("pid", 0)) for r in ledger})
row("Agent processes (ledger)", [f"{p} | {t} | {res} | pid {pid}" for p, t, res, pid in procs[:15]] + ([f"... {len(procs)-15} more"] if len(procs) > 15 else []))
files = sorted({f for c in claims for f in (c.get("files") or c.get("files_touched") or []) if isinstance(f, str)})
row("Files touched (claim receipts)", files[:15])


def hosts(rs, ok=lambda r: True):
    n, b = {}, {}
    for r in rs:
        if ok(r):
            h = str(r.get("host", "?"))
            n[h] = n.get(h, 0) + 1
            b[h] = b.get(h, 0) + int(r.get("bytes") or 0)
    return [f"{h}: {n[h]} requests, {b[h]} bytes out" for h in sorted(n)]


row("Hosts and bytes out (silent log)", hosts(silent))
row("Hosts and bytes out (egress results)", hosts(egress))
vc = {}
for v in verified:
    vc[v.get("verdict", "?")] = vc.get(v.get("verdict", "?"), 0) + 1
row("Claims", [f"recorded: {len(claims)}"] + [f"verified {k}: {n}" for k, n in sorted(vc.items())] if claims or verified else [])

summary = {"day": day, "agent_processes": len(procs), "claims_recorded": len(claims), "claims_verified": len(verified),
           "silent_rows": len(silent), "egress_rows": len(egress)}
os.makedirs(os.path.join(SH, "days"), exist_ok=True)
D = os.path.join(SH, "days")
subjects = {f"{day}.summary.json": (json.dumps(summary, sort_keys=True) + "\n").encode()}
for name, rs in ((f"{day}.ledger.jsonl", ledger), (f"{day}.claims.jsonl", claims + verified),
                 (f"{day}.silent.log.jsonl", silent), (f"{day}.egress.jsonl", egress)):
    if rs:
        subjects[name] = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rs).encode()
for n, b in subjects.items():
    open(os.path.join(D, n), "wb").write(b)
out = os.path.join(D, f"{day}.dsse.json")
try:
    from cryptography.hazmat.primitives import serialization
    from dsse import load_key, make_envelope
except ImportError:
    print("attestation: skipped (python 'cryptography' not installed)")
    sys.exit(0)
key = load_key()
pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
start, end = f"{day}T00:00:00Z", f"{day}T23:59:59Z"
st = {"_type": "https://in-toto.io/Statement/v1",
      "subject": [{"name": n, "digest": {"sha256": hashlib.sha256(b).hexdigest()}} for n, b in sorted(subjects.items())],
      "predicateType": "https://ripplarity.com/attestation/stillherd/day/v1",
      "predicate": {"producer": {"tool": "stillherd-today", "version": "1"}, "started": start, "ended": end,
                    "environment": {"host_os": platform.system() or "unknown", "run_mode": "REAL"},
                    "summary": summary, "pubkey_hex": pub.hex()}}
json.dump(make_envelope(st, key), open(out, "w"), indent=2)
print(f"attestation: {out} keyid={hashlib.sha256(pub).hexdigest()[:16]}")
