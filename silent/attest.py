"""Write and sign attestation.json. Usage: attest.py LOG OUT STARTED ENDED EXIT ALLOWLIST -- cmd..."""

import base64
import hashlib
import json
import os
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
payload = {
    "version": "stillherd-silent-1",
    "started": started,
    "ended": ended,
    "cmd": cmd,
    "exit_code": int(exit_code),
    "allowlist": sorted(allowset),
    "external_attempts": attempts,
    "external_bytes_sent": forwarded_external,
    "blocked_request_bytes": blocked_bytes,
    "log_file": os.path.basename(log),
    "log_sha256": hashlib.sha256(open(log, "rb").read()).hexdigest()
    if os.path.exists(log)
    else "",
    "pubkey": base64.b64encode(pub).decode(),
    "pubkey_sha256": hashlib.sha256(pub).hexdigest(),
}
canon = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
payload["signature"] = base64.b64encode(key.sign(canon)).decode()
json.dump(payload, open(out, "w"), indent=2)
print(
    f"attestation: {out} attempts={len(attempts)} external_bytes_sent={forwarded_external} pubkey_sha256={payload['pubkey_sha256'][:16]}"
)
