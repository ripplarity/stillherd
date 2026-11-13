"""Write and sign a DSSE / in-toto attestation (attest-spec v1, *.dsse.json). Usage: attest.py LOG OUT STARTED ENDED EXIT ALLOWLIST -- cmd..."""

import hashlib
import json
import os
import platform
import sys

from cryptography.hazmat.primitives import serialization

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dsse import PRED, load_key, make_envelope, scrub_arg

log, out, started, ended, exit_code, allow = sys.argv[1:7]
cmd = [scrub_arg(a) for a in sys.argv[8:]]

rows = (
    [json.loads(l) for l in open(log, encoding="utf-8") if l.strip()]
    if os.path.exists(log)
    else []
)
allowset = {h.strip().lower() for h in allow.split(",") if h.strip()}
mode = os.environ.get("SILENT_MODE", "silent")
if mode not in ("silent", "observe"):
    sys.exit(f"attest: SILENT_MODE must be silent or observe, got {mode!r}; nothing signed")
observed = []
if mode == "observe":
    # watch.sh: nothing was blocked; rows come from egress/log_requests.py (no "action" field)
    sys.path.insert(
        0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "egress")
    )
    from watch_summary import summarize

    observed = summarize(rows)
    attempts = []
    forwarded_external = sum(o["bytes_out"] for o in observed)
    blocked_bytes = 0
else:
    attempts = [
        {"host": r["host"], "path": r["path"], "ts": r["ts"]}
        for r in rows
        if r["action"] in ("blocked", "tls-failed")
    ]
    forwarded_external = sum(
        r["bytes"]
        for r in rows
        if r["action"] == "allowed" and r["host"].lower() not in allowset
    )
    blocked_bytes = sum(r["bytes"] for r in rows if r["action"] == "blocked")
key = load_key()
pub = key.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw
)

log_bytes = open(log, "rb").read() if os.path.exists(log) else b""
predicate = {
    "producer": {
        "tool": "stillherd-watch" if mode == "observe" else "stillherd-silent",
        "version": "1",
    },
    "started": started,
    "ended": ended,
    "environment": {
        "host_os": platform.system() or "unknown",
        "run_mode": os.environ.get("SILENT_RUN_MODE", "REAL"),
    },
    "exit_code": int(exit_code),
    "mode": mode,
    "pubkey_hex": pub.hex(),
}
if mode == "observe":
    # the command line can hold credentials (tokens in -c strings, --flag=/home/user/x): record only argv[0]'s basename and argc
    predicate["cmd"] = [os.path.basename(sys.argv[8].replace("\\", "/"))] if len(sys.argv) > 8 else []
    predicate["argc"] = len(sys.argv) - 8
    # observe mode makes NO containment claim: allowlist, external_attempts, external_bytes_sent and
    # blocked_request_bytes are ABSENT (not empty or zero), so a consumer cannot read a clean-looking value.
    predicate["enforcement"] = "none"
    predicate["observed_external"] = observed
    predicate["observed_bytes_out"] = forwarded_external
    predicate["proxy_saw_traffic"] = len(rows) > 0  # any request at all, loopback included
else:
    predicate["cmd"] = cmd
    predicate["allowlist"] = sorted(allowset)
    predicate["external_attempts"] = attempts
    predicate["external_bytes_sent"] = forwarded_external
    predicate["blocked_request_bytes"] = blocked_bytes
statement = {
    "_type": "https://in-toto.io/Statement/v1",
    "subject": [
        {
            "name": os.path.basename(log),
            "digest": {"sha256": hashlib.sha256(log_bytes).hexdigest()},
        }
    ],
    "predicateType": PRED,
    "predicate": predicate,
}
json.dump(make_envelope(statement, key), open(out, "w"), indent=2)
_kid = hashlib.sha256(pub).hexdigest()[:16]
if mode == "observe":
    _req = sum(o["requests"] for o in observed)
    print(
        f"observed: {len(observed)} external host(s), {_req} request(s), {forwarded_external} bytes out"
        f"\nattestation: {out} keyid={_kid}"
    )
else:
    print(
        f"attestation: {out} attempts={len(attempts)} external_bytes_sent={forwarded_external} keyid={_kid}"
    )
