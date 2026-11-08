"""Console entry points. Wrap the repo scripts (silent/, bin/today.py) without duplicating them."""
import base64
import json
import os
import runpy
import sys
import tempfile

PIN = "df051692c8467ca1eceb62d03333a02928bcb1a7ab42133efa2e2ef4287d1c42"
_SILENT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_silent")
_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bin")

VERIFY_USAGE = (
    "usage: stillherd-verify FILE [keyid]\n"
    "Verify a stillherd attestation. DSSE (*.dsse.json) is checked with the pinned keyid\n"
    "(arg 2, else $SILENT_EXPECT_KEYID, else the maintainer keyid); legacy format via verify.py.\n"
)
TODAY_USAGE = "usage: stillherd-today [YYYY-MM-DD]\nPrint the day screen and write ~/.stillherd/days/<date>.dsse.json\n"


def verify_main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        sys.stdout.write(VERIFY_USAGE)
        return 0
    if not argv:
        sys.stderr.write(VERIFY_USAGE)
        return 2
    f = argv[0]
    pin = argv[1] if len(argv) > 1 else os.environ.get("SILENT_EXPECT_KEYID", PIN)
    try:
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:
        d = {}
    if "payloadType" in d:
        try:
            pub = json.loads(base64.b64decode(d["payload"]))["predicate"]["pubkey_hex"]
        except Exception:
            sys.stderr.write("FAIL: no public key in predicate\n")
            return 2
        from stillherd_py._silent import attest_verify
        rc = attest_verify.main([f, "--key", pub.strip(), "--expect-keyid", pin,
                                 "--check-subjects", os.path.dirname(os.path.abspath(f))])
        if rc:
            return rc
        print("OK")
        return 0
    if "signature" in d:
        old = sys.argv
        sys.argv = ["verify.py", f, pin]
        try:
            runpy.run_path(os.path.join(_SILENT, "verify.py"), run_name="__main__")
            rc = 0
        except SystemExit as e:
            rc = e.code or 0
        finally:
            sys.argv = old
        if rc == 0:
            print("OK (legacy format)")
        return rc
    sys.stderr.write("FAIL: not an attestation\n")
    return 2


def today_main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("-h", "--help"):
        sys.stdout.write(TODAY_USAGE)
        return 0
    sys.path.insert(0, _SILENT)
    old = sys.argv
    sys.argv = ["today.py"] + list(argv)
    try:
        runpy.run_path(os.path.join(_BIN, "today.py"), run_name="__main__")
    except SystemExit as e:
        return e.code or 0
    finally:
        sys.argv = old
    return 0


def _v():
    sys.exit(verify_main())


def _t():
    sys.exit(today_main())
