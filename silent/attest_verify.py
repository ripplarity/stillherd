#!/usr/bin/env python3
"""attest_verify: offline verifier for DSSE / in-toto Statement v1 attestations (see SPEC.md).

Usage: python attest_verify.py <envelope.dsse.json> --key <hex pubkey | keys.json>
                               [--check-subjects DIR] [--expect-keyid HEX]
                               [--check-anchors [--rekor-key PEM]]
Exit: 0 valid, 6 anchor failure (--check-anchors), 2 bad signature/envelope, 3 subject digest mismatch,
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
    BASE + "stillherd/day/v1",
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
    ap.add_argument("--check-anchors", action="store_true", help="verify embedded Rekor anchors offline")
    ap.add_argument("--rekor-key", metavar="PEM", help="pinned Rekor public key (default: rekor.pub beside this script)")
    ap.add_argument("--online", action="store_true", help="reserved: freshness check against Rekor (not implemented)")
    a = ap.parse_args(argv)
    if a.online:
        print("--online is not implemented in this release", file=sys.stderr)
        return 2
    rc = _verify(a)
    if a.check_anchors and rc in (0, 5):
        arc = check_anchors(a)
        if arc:
            return arc
    return rc


# ---- offline Rekor anchor verification (SPEC section 8); no network ----
def envelope_digest(env: dict) -> str:
    """sha256 over canonical JSON of the envelope with `anchors` removed."""
    core = {k: v for k, v in env.items() if k != "anchors"}
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def _h(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def verify_inclusion(index: int, size: int, leaf_hash: bytes, proof: list, root: bytes) -> bool:
    """RFC 6962 / RFC 9162 section 2.1.3.2 inclusion proof check."""
    if index < 0 or index >= size:
        return False
    fn, sn, r = index, size - 1, leaf_hash
    for p in proof:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            r = _h(b"\x01" + p + r)
            if not fn & 1:
                while not fn & 1 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = _h(b"\x01" + r + p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == root


def _check_one_anchor(env: dict, an: dict, rekor_pem: bytes) -> str:
    """Return "" if the anchor verifies, else a reason."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    digest = envelope_digest(env)
    body = base64.b64decode(an["body"], validate=True)
    spec = json.loads(body)
    if spec.get("kind") != "hashedrekord" or spec["spec"]["data"]["hash"]["value"] != digest:
        return "anchored digest does not match this envelope"
    pem = base64.b64decode(spec["spec"]["signature"]["publicKey"]["content"])
    ed = serialization.load_pem_public_key(pem)
    ed.verify(base64.b64decode(spec["spec"]["signature"]["content"]), bytes.fromhex(digest))
    pr = an["inclusionProof"]
    leaf = _h(b"\x00" + body)
    ok = verify_inclusion(
        int(pr["logIndex"]), int(pr["treeSize"]), leaf,
        [bytes.fromhex(x) for x in pr["hashes"]], bytes.fromhex(pr["rootHash"]),
    )
    if not ok:
        return "inclusion proof does not reach the root"
    cp = pr.get("checkpoint")
    if cp:
        lines = cp.split("\n")
        if len(lines) < 3 or int(lines[1]) != int(pr["treeSize"]) or base64.b64decode(lines[2]).hex() != pr["rootHash"]:
            return "checkpoint disagrees with inclusion proof"
    set_payload = json.dumps(
        {"body": an["body"], "integratedTime": int(an["integratedTime"]),
         "logID": an["logID"], "logIndex": int(an["logIndex"])},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    rk = serialization.load_pem_public_key(rekor_pem)
    rk.verify(base64.b64decode(an["signedEntryTimestamp"]), set_payload, ec.ECDSA(hashes.SHA256()))
    return ""


def check_anchors(a) -> int:
    """0 ok or no anchors; 6 on any anchor failure."""
    env = json.load(open(a.envelope, encoding="utf-8"))
    anchors = env.get("anchors") or []
    if not anchors:
        print("NO-ANCHORS")
        return 0
    kp = a.rekor_key or os.path.join(os.path.dirname(os.path.abspath(__file__)), "rekor.pub")
    if not os.path.isfile(kp):
        print("ANCHOR-FAIL: no pinned Rekor public key (--rekor-key)", file=sys.stderr)
        return 6
    pem = open(kp, "rb").read()
    print("rekor key sha256:", hashlib.sha256(pem).hexdigest())
    for i, an in enumerate(anchors):
        try:
            why = _check_one_anchor(env, an, pem)
        except Exception as e:  # any parse/verify error is an anchor failure
            why = f"{type(e).__name__}: {e}"
        if why:
            print(f"ANCHOR-FAIL[{i}]: {why}", file=sys.stderr)
            return 6
    print(f"ANCHORS-OK {len(anchors)}")
    return 0


def _verify(a) -> int:
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
