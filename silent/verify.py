"""Verify attestation.json: ed25519 signature over the canonical payload, and the log hash."""

import base64
import hashlib
import json
import os
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

path = sys.argv[1]
expect_fp = sys.argv[2] if len(sys.argv) > 2 else None  # optional pinned pubkey sha256
a = json.load(open(path))
sig = base64.b64decode(a.pop("signature", ""))
canon = json.dumps(a, sort_keys=True, separators=(",", ":")).encode()
pub = base64.b64decode(a["pubkey"])
try:
    Ed25519PublicKey.from_public_bytes(pub).verify(sig, canon)
except (InvalidSignature, ValueError):
    print("FAIL: bad signature")
    sys.exit(1)
if hashlib.sha256(pub).hexdigest() != a["pubkey_sha256"] or (
    expect_fp and expect_fp != a["pubkey_sha256"]
):
    print("FAIL: pubkey fingerprint mismatch")
    sys.exit(1)
lp = os.path.join(os.path.dirname(os.path.abspath(path)), a["log_file"])
if (
    not os.path.exists(lp)
    or hashlib.sha256(open(lp, "rb").read()).hexdigest() != a["log_sha256"]
):
    print("FAIL: log hash mismatch")
    sys.exit(1)
print("OK")
