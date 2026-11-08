#!/usr/bin/env python3
"""attest_verify: offline verifier for DSSE / in-toto Statement v1 attestations (see SPEC.md).

Usage: python attest_verify.py <envelope.dsse.json> --key <hex pubkey | keys.json>
                               [--check-subjects DIR] [--expect-keyid HEX]
Exit: 0 valid, 2 bad signature/envelope, 3 subject digest mismatch,
      4 malformed statement or missing mandatory fields, 5 unknown predicateType
      (after printing VALID-SIGNATURE). No network access. Deps: stdlib + cryptography.
"""

import argparse
import base64
import hashlib
import json
import os
import sys

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PAYLOAD_TYPE = "application/vnd.in-toto+json"
STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
BASE = "https://ripplarity.com/attestation/"
KNOWN = {
    BASE + "sovkit/inventory/v1",
    BASE + "sovkit/drill/v1",
    BASE + "aiescrow/manifest/v1",
    BASE + "dejaspawn/mirror/v1",
    BASE + "nukekit/mirror/v1",  # deprecated alias of dejaspawn/mirror/v1; still known
    BASE + "stillherd/silent/v1",
}
RUN_MODES = {"REAL", "MOCK", "FIXTURE", "DRY-RUN"}


def pae(payload_type: str, payload: bytes) -> bytes:
    t = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(payload), payload)


def keyid(raw_pub: bytes) -> str:
    return hashlib.sha256(raw_pub).hexdigest()


def load_keys(arg: str) -> list:
    """Return list of raw 32-byte public keys from a hex string, a hex file, or keys.json."""
    if os.path.isfile(arg):
        txt = open(arg, encoding="utf-8").read().strip()
        try:
            doc = json.loads(txt)
        except ValueError:
            return [bytes.fromhex(txt)]
        items = doc["keys"] if isinstance(doc, dict) else doc
        return [bytes.fromhex(k["public_key_hex"]) for k in items]
    return [bytes.fromhex(arg.strip())]


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def check_mandatory(pred) -> str:
    if not isinstance(pred, dict):
        return "predicate is not an object"
    p = pred.get("producer")
    if not (isinstance(p, dict) and p.get("tool") and p.get("version")):
        return "missing producer{tool,version}"
    for k in ("started", "ended"):
        v = pred.get(k)
        if not (isinstance(v, str) and v.endswith("Z") and len(v) >= 20):
            return f"missing or non-UTC {k}"
    e = pred.get("environment")
    if not (
        isinstance(e, dict) and e.get("host_os") and e.get("run_mode") in RUN_MODES
    ):
        return "missing environment{host_os,run_mode}"
    return ""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("envelope")
    ap.add_argument("--key", required=True)
    ap.add_argument("--check-subjects", metavar="DIR")
    ap.add_argument(
        "--expect-keyid", metavar="HEX", help="pin: signing key must have this keyid"
    )
    a = ap.parse_args(argv)
    try:
        env = json.load(open(a.envelope, encoding="utf-8"))
        if env["payloadType"] != PAYLOAD_TYPE:
            raise ValueError("wrong payloadType")
        payload = base64.b64decode(env["payload"], validate=True)
        sigs = env["signatures"]
        keys = load_keys(a.key)
    except Exception as e:
        print(f"INVALID: unreadable envelope or key: {e}", file=sys.stderr)
        return 2
    by_id = {keyid(k): k for k in keys}
    if a.expect_keyid:
        # a key found beside the artefact is untrusted unless it matches the pinned keyid
        by_id = {i: k for i, k in by_id.items() if i == a.expect_keyid.lower()}
        if not by_id:
            print(
                "INVALID: supplied key does not match --expect-keyid", file=sys.stderr
            )
            return 2
    elif os.path.dirname(os.path.abspath(a.key)) == os.path.dirname(
        os.path.abspath(a.envelope)
    ) and os.path.isfile(a.key):
        print(
            "WARNING: key file sits beside the envelope and is not pinned; pass --expect-keyid",
            file=sys.stderr,
        )
    msg = pae(env["payloadType"], payload)
    ok = False
    for s in sigs:
        raw = by_id.get(s.get("keyid"))
        if raw is None:
            continue
        try:
            Ed25519PublicKey.from_public_bytes(raw).verify(
                base64.b64decode(s["sig"]), msg
            )
            ok = True
            break
        except (InvalidSignature, ValueError, KeyError):
            pass
    if not ok:
        print(
            "INVALID: no signature verifies under the supplied key(s)", file=sys.stderr
        )
        return 2
    print("VALID-SIGNATURE")
    try:
        st = json.loads(payload)
        if st["_type"] != STATEMENT_TYPE:
            raise ValueError("wrong _type")
        subjects = st["subject"]
        if not subjects:
            raise ValueError("empty subject")
        for s in subjects:
            if not (s.get("name") and len(s["digest"]["sha256"]) == 64):
                raise ValueError("bad subject")
        ptype = st["predicateType"]
        pred = st["predicate"]
    except Exception as e:
        print(f"MALFORMED: {e}", file=sys.stderr)
        return 4
    if a.check_subjects:
        for s in subjects:
            p = os.path.join(a.check_subjects, s["name"])
            if not os.path.isfile(p) or sha256_file(p) != s["digest"]["sha256"]:
                print(f"SUBJECT-MISMATCH: {s['name']}", file=sys.stderr)
                return 3
    if ptype not in KNOWN:
        print(
            f"WARNING: unknown predicateType {ptype}; signature checked, predicate not",
            file=sys.stderr,
        )
        return 5
    why = check_mandatory(pred)
    if why:
        print(f"MALFORMED: {why}", file=sys.stderr)
        return 4
    print(f"VALID {ptype}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
