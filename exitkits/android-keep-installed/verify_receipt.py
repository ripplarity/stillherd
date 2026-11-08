"""Verify a keep_installed receipt: ed25519 signature over the canonical payload.
Usage: verify_receipt.py receipt.json [pubkey_sha256]   (same scheme as ../../silent/verify.py)"""

import base64
import hashlib
import json
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

a = json.load(open(sys.argv[1]))
pin = sys.argv[2] if len(sys.argv) > 2 else None
sig = base64.b64decode(a.pop("signature", None) or "")
canon = json.dumps(a, sort_keys=True, separators=(",", ":")).encode()
pub = base64.b64decode(a["pubkey"])
try:
    Ed25519PublicKey.from_public_bytes(pub).verify(sig, canon)
except (InvalidSignature, ValueError):
    print("FAIL: bad signature")
    sys.exit(1)
if hashlib.sha256(pub).hexdigest() != a["pubkey_sha256"] or (
    pin and pin != a["pubkey_sha256"]
):
    print("FAIL: pubkey fingerprint mismatch")
    sys.exit(1)
print("OK")
