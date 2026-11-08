#!/usr/bin/env bash
# verify.sh FILE [keyid]
# New format (*.dsse.json, DSSE + in-toto): checked by attest_verify.py, pinned with --expect-keyid
#   (arg 2, else $SILENT_EXPECT_KEYID, else the maintainer keyid published in silent/README.md).
#   The public key is read from the predicate only to be matched against that pin; an unpinned key is never trusted.
# Legacy format (stillherd-silent-1 / corral-silent-1): checked by verify.py, prints "OK (legacy format)".
HERE="$(cd "$(dirname "$0")" && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
[ $# -ge 1 ] || { echo "usage: verify.sh FILE [keyid]" >&2; exit 2; }
F="$1"; PIN="${2:-${SILENT_EXPECT_KEYID:-df051692c8467ca1eceb62d03333a02928bcb1a7ab42133efa2e2ef4287d1c42}}"
KIND="$("$PY" -I -c 'import json,sys
try: d=json.load(open(sys.argv[1],encoding="utf-8"))
except Exception: print("bad"); sys.exit()
print("dsse" if "payloadType" in d else "legacy" if "signature" in d else "bad")' "$F")"
case "$KIND" in
  legacy)
    OUT="$("$PY" "$HERE/verify.py" "$F" "$PIN")"; RC=$?
    if [ $RC -eq 0 ]; then echo "OK (legacy format)"; else echo "$OUT"; fi
    exit $RC ;;
  dsse)
    TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
    "$PY" -I -c 'import json,base64,sys
d=json.load(open(sys.argv[1],encoding="utf-8")); print(json.loads(base64.b64decode(d["payload"]))["predicate"]["pubkey_hex"])' "$F" > "$TMP" 2>/dev/null \
      || { echo "FAIL: no public key in predicate" >&2; exit 2; }
    "$PY" "$HERE/attest_verify.py" "$F" --key "$(cat "$TMP" | tr -d '\r\n')" --expect-keyid "$PIN" --check-subjects "$(dirname "$F")" || exit $?
    echo "OK" ;;
  *) echo "FAIL: not an attestation" >&2; exit 2 ;;
esac
