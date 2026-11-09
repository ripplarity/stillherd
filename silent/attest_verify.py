#!/usr/bin/env python3
"""attest_verify: offline verifier for DSSE / in-toto Statement v1 attestations (see SPEC.md).

Usage: python attest_verify.py <envelope.dsse.json> --key <hex pubkey | keys.json>
                               --expect-keyid HEX        (REQUIRED: the pinned trusted keyid)
                               [--check-subjects DIR] [--check-anchors [--rekor-key PEM]]
Exit: 0 valid; 2 usage/unpinned, bad signature or pin mismatch; 3 subject digest mismatch;
      4 unreadable envelope, malformed statement or missing mandatory fields;
      5 unknown predicateType (after VALID-SIGNATURE); 6 anchor failure (--check-anchors).
Precedence (first failing check wins): 4 unreadable, 2 signature/pin, 4 malformed statement,
3 subject, 5 unknown type, 6 anchors. No network access. Deps: stdlib + cryptography.
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
    BASE + "dejaspawn/evidence/v1",
    BASE + "mictest/participant/v1",
    BASE + "halflife/receipt/v1",
    BASE + "canceltogether/campaign/v1",
    BASE + "buyatheorem/lemma/v1",
    "https://udoot.com/attestation/egress/v1",  # separate developer identity; full URI, not under BASE
}
RUN_MODES = {"REAL", "MOCK", "FIXTURE", "DRY-RUN"}
MAX_INT = 2**53 - 1
REKOR_KEY_SHA256 = "dce5ef715502ec9f3cdfd11f8cc384b31a6141023d3e7595e9908a81cb6241bd"


# ---- RFC 8785 (JCS) canonical JSON, restricted to strings/integers/booleans/null/arrays/objects ----
def jcs(o) -> bytes:
    return _jcs(o).encode("utf-8")


def _jcs(o) -> str:
    if o is None:
        return "null"
    if o is True:
        return "true"
    if o is False:
        return "false"
    if isinstance(o, int):
        if abs(o) > MAX_INT:
            raise ValueError("integer outside +-(2^53-1)")
        return str(o)
    if isinstance(o, float):
        raise ValueError("floats are not allowed in attestation JSON")
    if isinstance(o, str):
        o.encode("utf-8")  # raises on lone surrogates
        return json.dumps(o, ensure_ascii=False)
    if isinstance(o, list):
        return "[" + ",".join(_jcs(v) for v in o) + "]"
    if isinstance(o, dict):
        keys = sorted(o, key=lambda k: k.encode("utf-16-be"))  # UTF-16 code unit order
        return "{" + ",".join(_jcs(k) + ":" + _jcs(o[k]) for k in keys) + "}"
    raise ValueError(f"unsupported type {type(o).__name__}")


# ---- strict ed25519 (RFC 8032): canonical S < L, canonical on-curve non-small-order A and R ----
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRTM1 = pow(2, (_P - 1) // 4, _P)


def decompress(b: bytes):
    """Return affine (x, y) or None if non-canonical / not on the curve."""
    if len(b) != 32:
        return None
    n = int.from_bytes(b, "little")
    sign, y = n >> 255, n & ((1 << 255) - 1)
    if y >= _P:
        return None
    u, v = (y * y - 1) % _P, (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRTM1 % _P
    if (x * x - x2) % _P:
        return None
    if x == 0 and sign:
        return None
    if x & 1 != sign:
        x = _P - x
    return x, y


def _add(p, q):
    x1, y1 = p
    x2, y2 = q
    t = _D * x1 * x2 * y1 * y2 % _P
    return (
        (x1 * y2 + y1 * x2) * pow(1 + t, _P - 2, _P) % _P,
        (y1 * y2 + x1 * x2) * pow(1 - t, _P - 2, _P) % _P,
    )


def small_order(pt) -> bool:
    for _ in range(3):
        pt = _add(pt, pt)
    return pt == (0, 1)


def _mul(k: int, pt):
    r = (0, 1)
    while k:
        if k & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        k >>= 1
    return r


def prime_order(pt) -> bool:
    """[L]P = O (the identity also passes; small_order() excludes it separately)."""
    return _mul(_L, pt) == (0, 1)


def encode_point(pt) -> bytes:
    return (pt[1] | ((pt[0] & 1) << 255)).to_bytes(32, "little")


_B = decompress(bytes.fromhex("58" + "66" * 31))
_DOM_PH = b"SigEd25519 no Ed25519 collisions" + bytes([1, 0])  # dom2(1, "") for Ed25519ph


def ed25519ph_verify(pub: bytes, digest64: bytes, sig: bytes) -> bool:
    """RFC 8032 Ed25519ph, empty context, where `digest64` is already PH(M) = SHA-512(M).
    Cofactorless: [S]B == R + [k]A, with canonical S and prime-order A and R."""
    if len(pub) != 32 or len(sig) != 64 or len(digest64) != 64 or strict_ed25519_ok(pub, sig):
        return False
    a, r = decompress(pub), decompress(sig[:32])
    k = int.from_bytes(hashlib.sha512(_DOM_PH + sig[:32] + pub + digest64).digest(), "little") % _L
    return _mul(int.from_bytes(sig[32:], "little"), _B) == _add(r, _mul(k, a))


def strict_ed25519_ok(pub: bytes, sig: bytes) -> str:
    """Return "" if the encodings are acceptable, else the reason."""
    if len(pub) != 32 or len(sig) != 64:
        return "bad key or signature length"
    if int.from_bytes(sig[32:], "little") >= _L:
        return "non-canonical S (S >= L)"
    a = decompress(pub)
    if a is None or small_order(a):
        return "non-canonical or small-order public key"
    if not prime_order(a):
        return "public key is not prime-order ([L]A != O)"
    r = decompress(sig[:32])
    if r is None or small_order(r):
        return "malformed or small-order R"
    if not prime_order(r):
        return "R is not prime-order ([L]R != O)"
    return ""


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


def _hex64(v) -> bool:
    return (
        isinstance(v, str) and len(v) == 64 and all(c in "0123456789abcdef" for c in v)
    )


def subject_name_ok(n) -> bool:
    """POSIX relative path: no leading slash, no backslash, no empty/./.. segments."""
    if not isinstance(n, str) or not n or n.startswith("/") or "\\" in n or "\x00" in n:
        return False
    return all(seg not in ("", ".", "..") for seg in n.split("/"))


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
        "--expect-keyid",
        metavar="HEX",
        help="REQUIRED pin: the trusted signing keyid (SPEC s7)",
    )
    ap.add_argument(
        "--check-anchors",
        action="store_true",
        help="verify embedded Rekor anchors offline",
    )
    ap.add_argument(
        "--rekor-key",
        metavar="PEM",
        help="pinned Rekor public key (default: rekor.pub beside this script)",
    )
    ap.add_argument(
        "--online",
        action="store_true",
        help="reserved: freshness check (not implemented)",
    )
    a = ap.parse_args(argv)
    if not a.expect_keyid:
        print(
            "pinned keyid required (SPEC s7): pass --expect-keyid <hex>",
            file=sys.stderr,
        )
        return 2
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
    """sha512 (hex) over JCS(envelope minus `anchors`); unknown top-level keys ARE included."""
    core = {k: v for k, v in env.items() if k != "anchors"}
    return hashlib.sha512(jcs(core)).hexdigest()


def _h(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


def verify_inclusion(
    index: int, size: int, leaf_hash: bytes, proof: list, root: bytes
) -> bool:
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
    h = spec["spec"]["data"]["hash"]
    if spec.get("kind") != "hashedrekord" or h["algorithm"] != "sha512" or h["value"] != digest:
        return "anchored digest does not match this envelope"
    pem = base64.b64decode(spec["spec"]["signature"]["publicKey"]["content"])
    pub = serialization.load_pem_public_key(pem).public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if not ed25519ph_verify(pub, bytes.fromhex(digest), base64.b64decode(spec["spec"]["signature"]["content"])):
        return "entry signature (Ed25519ph over the sha512 digest) does not verify"
    pr = an["inclusionProof"]
    leaf = _h(b"\x00" + body)
    ok = verify_inclusion(
        int(pr["logIndex"]),
        int(pr["treeSize"]),
        leaf,
        [bytes.fromhex(x) for x in pr["hashes"]],
        bytes.fromhex(pr["rootHash"]),
    )
    if not ok:
        return "inclusion proof does not reach the root"
    cp = pr.get("checkpoint")
    if cp:
        lines = cp.split("\n")
        if (
            len(lines) < 3
            or int(lines[1]) != int(pr["treeSize"])
            or base64.b64decode(lines[2]).hex() != pr["rootHash"]
        ):
            return "checkpoint disagrees with inclusion proof"
    set_payload = json.dumps(
        {
            "body": an["body"],
            "integratedTime": int(an["integratedTime"]),
            "logID": an["logID"],
            "logIndex": int(an["logIndex"]),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    rk = serialization.load_pem_public_key(rekor_pem)
    rk.verify(
        base64.b64decode(an["signedEntryTimestamp"]),
        set_payload,
        ec.ECDSA(hashes.SHA256()),
    )
    return ""


def check_anchors(a) -> int:
    """0 ok or no anchors; 6 on any anchor failure."""
    env = json.load(open(a.envelope, encoding="utf-8"))
    anchors = env.get("anchors") or []
    if not anchors:
        print("NO-ANCHORS")
        return 0
    kp = a.rekor_key or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "rekor.pub"
    )
    if not os.path.isfile(kp):
        print("ANCHOR-FAIL: no pinned Rekor public key (--rekor-key)", file=sys.stderr)
        return 6
    pem = open(kp, "rb").read()
    kh = hashlib.sha256(pem).hexdigest()
    print("rekor key sha256:", kh)
    if not a.rekor_key and kh != REKOR_KEY_SHA256:
        print("ANCHOR-FAIL: default rekor.pub does not match the pinned sha256", file=sys.stderr)
        return 6
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
            raise ValueError("payloadType must equal " + PAYLOAD_TYPE)
        payload = base64.b64decode(env["payload"], validate=True)
        sigs = env["signatures"]
        if not isinstance(sigs, list) or not sigs:
            raise ValueError("no signatures")
        entries = [
            (s["keyid"], base64.b64decode(s["sig"], validate=True)) for s in sigs
        ]
        keys = load_keys(a.key)
    except Exception as e:
        print(f"UNREADABLE: envelope or key: {e}", file=sys.stderr)
        return 4
    by_id = {keyid(k): k for k in keys}
    pin = a.expect_keyid.strip().lower()
    if pin not in by_id:
        print("INVALID: key does not match pin", file=sys.stderr)
        return 2
    msg = pae(env["payloadType"], payload)
    pinned_seen = False
    for kid, sig in entries:
        raw = by_id.get(kid)
        if raw is None:
            print(
                f"INVALID: signature keyid {kid} is not a supplied key", file=sys.stderr
            )
            return 2
        why = strict_ed25519_ok(raw, sig)
        if why:
            print(f"INVALID: {why}", file=sys.stderr)
            return 2
        try:
            Ed25519PublicKey.from_public_bytes(raw).verify(sig, msg)
        except (InvalidSignature, ValueError):
            print("INVALID: signature does not verify", file=sys.stderr)
            return 2
        pinned_seen = pinned_seen or kid == pin
    if not pinned_seen:
        print("INVALID: no signature from the pinned keyid", file=sys.stderr)
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
            if not subject_name_ok(s.get("name")) or not _hex64(s["digest"]["sha256"]):
                raise ValueError(
                    "bad subject (name must be a POSIX relative path, digest lowercase hex)"
                )
        ptype = st["predicateType"]
        pred = st["predicate"]
    except Exception as e:
        print(f"MALFORMED: {e}", file=sys.stderr)
        return 4
    if a.check_subjects:
        for s in subjects:
            p = os.path.join(a.check_subjects, *s["name"].split("/"))
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
