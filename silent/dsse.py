"""DSSE writer shared by attest.py and bin/today.py: signing key load/create and envelope signing."""

import base64
import hashlib
import json
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_H = os.path.expanduser("~")
_NEW = os.path.join(_H, ".stillherd", "silent_ed25519.pem")
_OLD = os.path.join(_H, ".corral", "silent_ed25519.pem")
KEY = os.environ.get(
    "SILENT_KEY", next((p for p in (_NEW, _OLD) if os.path.exists(p)), _NEW)
)
PT = "application/vnd.in-toto+json"
PRED = "https://ripplarity.com/attestation/stillherd/silent/v1"


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


def pae(t: bytes, payload: bytes) -> bytes:
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(payload), payload)


def make_envelope(statement, priv):
    payload = json.dumps(
        statement, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    raw = priv.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    sig = priv.sign(pae(PT.encode(), payload))
    return {
        "payloadType": PT,
        "payload": base64.b64encode(payload).decode(),
        "signatures": [
            {
                "keyid": hashlib.sha256(raw).hexdigest(),
                "sig": base64.b64encode(sig).decode(),
            }
        ],
    }
