#!/usr/bin/env python3
"""mode_check.py FILE [--require-mode silent|observe]: print the mode of a stillherd attestation and check it is consistent.
Run by verify.sh AFTER the signature result (attest_verify.py is the shared verifier and is left untouched).
Prints one line: `mode: silent` | `mode: observe (...)` | `mode: silent (legacy, unlabelled)` | `mode: n/a (<predicateType>)`.
Exit 0 ok, 4 inconsistent or unknown mode, 8 --require-mode not met, 2 bad arguments.
Never reads stdin. Unlabelled (no `mode`) counts as silent only for the silent predicateType with all four silent fields
present and correctly typed (or for the legacy JSON format, which predates the field)."""

import base64
import json
import sys

SILENT_TYPE = "https://ripplarity.com/attestation/stillherd/silent/v1"
SILENT_FIELDS = (
    "allowlist",
    "external_attempts",
    "external_bytes_sent",
    "blocked_request_bytes",
)
OBSERVE_FIELDS = (
    "observed_external",
    "observed_bytes_out",
    "enforcement",
    "proxy_saw_traffic",
)
SILENT_TYPES = {
    "allowlist": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v),
    "external_attempts": lambda v: isinstance(v, list),
    "external_bytes_sent": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "blocked_request_bytes": lambda v: isinstance(v, int) and not isinstance(v, bool),
}
USAGE = "usage: mode_check.py FILE [--require-mode silent|observe]"


def load(path):
    """-> (predicateType or None for the legacy JSON format, predicate)."""
    d = json.load(open(path, encoding="utf-8"))
    if "payload" in d:
        st = json.loads(base64.b64decode(d["payload"]))
        return st.get("predicateType"), st["predicate"]
    return None, d


def silent_fields_ok(pred):
    bad = [k for k in SILENT_FIELDS if k not in pred or not SILENT_TYPES[k](pred[k])]
    return bad


def check(ptype, pred, require=None):
    legacy_json = ptype is None
    if not legacy_json and ptype != SILENT_TYPE:
        label = f"mode: n/a ({ptype})"
        if require:
            return (
                8,
                label,
                f"FAIL: --require-mode {require} needs a stillherd silent/watch attestation, got predicateType {ptype}",
            )
        return 0, label, ""
    mode = pred.get("mode")
    if mode is None:
        label = "mode: silent (legacy, unlabelled)"
        bad = [k for k in OBSERVE_FIELDS if k in pred]
        if bad:
            return (
                4,
                label,
                f"FAIL: unlabelled attestation carries observe-only field(s) {bad}",
            )
        if not legacy_json:
            miss = silent_fields_ok(pred)
            if miss:
                return (
                    4,
                    label,
                    f"FAIL: unlabelled attestation lacks silent field(s) with the right type: {miss}",
                )
        eff = "silent"
    elif mode == "silent":
        label = "mode: silent"
        bad = [k for k in OBSERVE_FIELDS if k in pred]
        if bad:
            return (
                4,
                label,
                f"FAIL: mode silent but observe-only field(s) present: {bad}",
            )
        miss = silent_fields_ok(pred)
        if miss:
            return (
                4,
                label,
                f"FAIL: mode silent but silent field(s) missing or mistyped: {miss}",
            )
        eff = "silent"
    elif mode == "observe":
        label = "mode: observe (nothing was blocked; this is a record, not a containment claim)"
        bad = [k for k in SILENT_FIELDS if k in pred]
        if bad:
            return (
                4,
                label,
                f"FAIL: mode observe but silent-claim field(s) present: {bad}",
            )
        if not isinstance(pred.get("observed_external"), list):
            return 4, label, "FAIL: mode observe without an observed_external list"
        eff = "observe"
    else:
        return 4, f"mode: {mode!r} (unknown)", f"FAIL: unknown mode {mode!r}"
    if require and eff != require:
        why = (
            "attestation has no mode (legacy silent)"
            if mode is None
            else f"attestation mode is {mode}"
        )
        return 8, label, f"FAIL: --require-mode {require} not met: {why}"
    return 0, label, ""


def main(a):
    req, path = None, None
    i = 0
    while i < len(a):
        x = a[i]
        if x == "--require-mode" or x.startswith("--require-mode="):
            if req is not None:
                _die("--require-mode given twice")
            if x == "--require-mode":
                if i + 1 >= len(a):
                    _die("--require-mode needs a value")
                req, i = a[i + 1], i + 2
            else:
                req, i = x.split("=", 1)[1], i + 1
            if req not in ("silent", "observe"):
                _die(f"--require-mode must be silent or observe, got {req!r}")
        elif x.startswith("-") or path is not None:
            _die(f"unexpected argument {x!r}")
        else:
            path, i = x, i + 1
    if path is None:
        _die("missing FILE")
    ptype, pred = load(path)
    rc, label, err = check(ptype, pred, req)
    print(label)
    if err:
        print(err, file=sys.stderr)
    return rc


def _die(msg):
    print(f"mode_check: {msg}\n{USAGE}", file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
