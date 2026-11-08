"""Write and sign a DSSE / in-toto attestation (attest-spec v1, *.dsse.json). Usage: attest.py LOG OUT STARTED ENDED EXIT ALLOWLIST -- cmd..."""

import base64
import hashlib
import json
import os
import platform
import sys

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

log, out, started, ended, exit_code, allow = sys.argv[1:7]
cmd = sys.argv[8:] if len(sys.argv) > 8 else []
KEY = os.environ.get(
    "SILENT_KEY", next((p for p in (os.path.join(os.path.expanduser("~"), ".stillherd", "silent_ed25519.pem"), os.path.join(os.path.expanduser("~"), ".corral", "silent_ed25519.pem")) if os.path.exists(p)), os.path.join(os.path.expanduser("~"), ".stillherd", "silent_ed25519.pem"))
)


def load_key():
    if os.path.exists(KEY):
        return serialization.load_pem_private_key(open(KEY, "rb").read(), password=None)
    os.makedirs(os.path.dirname(KEY), exist_ok=True)
    k = Ed25519PrivateKey.generate()
    open(KEY, "wb").write(
        k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return k


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
PT = "application/vnd.in-toto+json"
PRED = "https://ripplarity.com/attestation/stillherd/silent/v1"


def pae(t: bytes, payload: bytes) -> bytes:
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(payload), payload)


def make_envelope(statement, priv):
    payload = json.dumps(statement, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    sig = priv.sign(pae(PT.encode(), payload))
    return {
        "payloadType": PT,
        "payload": base64.b64encode(payload).decode(),
        "signatures": [{"keyid": hashlib.sha256(raw).hexdigest(), "sig": base64.b64encode(sig).decode()}],
    }


log_bytes = open(log, "rb").read() if os.path.exists(log) else b""
predicate = {
    "producer": {"tool": "stillherd-silent", "version": "1"},
    "started": started,
    "ended": ended,
    "environment": {"host_os": platform.system() or "unknown", "run_mode": os.environ.get("SILENT_RUN_MODE", "REAL")},
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
    "subject": [{"name": os.path.basename(log), "digest": {"sha256": hashlib.sha256(log_bytes).hexdigest()}}],
    "predicateType": PRED,
    "predicate": predicate,
}
json.dump(make_envelope(statement, key), open(out, "w"), indent=2)
print(
    f"attestation: {out} attempts={len(attempts)} external_bytes_sent={forwarded_external} keyid={hashlib.sha256(pub).hexdigest()[:16]}"
)
