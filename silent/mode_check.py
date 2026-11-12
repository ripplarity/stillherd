#!/usr/bin/env python3
"""mode_check.py FILE [--require-mode silent|observe]: print the mode of a stillherd attestation and check it is consistent.
Run by verify.sh AFTER the signature result (attest_verify.py is the shared verifier and is left untouched).
Prints one line: `mode: silent` | `mode: observe (...)` | `mode: silent (legacy, unlabelled)`.
Exit 0 ok, 4 inconsistent or unknown mode, 8 --require-mode not met.
A missing `mode` is a legacy silent attestation (written before observe mode existed); it satisfies --require-mode silent only."""

import base64
import json
import sys

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


def predicate_of(path):
    d = json.load(open(path, encoding="utf-8"))
    if "payload" in d:
        return json.loads(base64.b64decode(d["payload"]))["predicate"]
    return d  # legacy format: no mode key


def check(pred, require=None):
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


if __name__ == "__main__":
    a = sys.argv[1:]
    req = None
    if "--require-mode" in a:
        i = a.index("--require-mode")
        req = a[i + 1] if i + 1 < len(a) else None
        del a[i : i + 2]
        if req not in ("silent", "observe"):
            sys.exit("usage: --require-mode silent|observe")
    if len(a) != 1:
        sys.exit("usage: mode_check.py FILE [--require-mode silent|observe]")
    rc, label, err = check(predicate_of(a[0]), req)
    print(label)
    if err:
        print(err, file=sys.stderr)
    sys.exit(rc)
