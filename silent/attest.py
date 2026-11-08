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
    "producer": {"tool": "stillherd-silent", "version": "1"},
    "started": started,
    "ended": ended,
    "environment": {
        "host_os": platform.system() or "unknown",
        "run_mode": os.environ.get("SILENT_RUN_MODE", "REAL"),
    },
    "cmd": cmd,
    "exit_code": int(exit_code),
    "allowlist": sorted(allowset),
    "external_attempts": attempts,
    "external_bytes_sent": forwarded_external,
    "blocked_request_bytes": blocked_bytes,
    "pubkey_hex": pub.hex(),
}
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
print(
    f"attestation: {out} attempts={len(attempts)} external_bytes_sent={forwarded_external} keyid={hashlib.sha256(pub).hexdigest()[:16]}"
)
