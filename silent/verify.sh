#!/usr/bin/env bash
# verify.sh FILE [keyid] [--require-mode silent|observe]   (also --require-mode=VALUE; bad arguments exit 2 before any output)
# New format (*.dsse.json, DSSE + in-toto): checked by attest_verify.py, pinned with --expect-keyid
#   (arg 2, else $SILENT_EXPECT_KEYID, else the maintainer keyid published in silent/README.md).
#   The public key is read from the predicate only to be matched against that pin; an unpinned key is never trusted.
# Legacy format (stillherd-silent-1 / corral-silent-1): checked by verify.py, prints "OK (legacy format)".
HERE="$(cd "$(dirname "$0")" && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
usage() { echo "usage: verify.sh FILE [keyid] [--require-mode silent|observe]" >&2; exit 2; }
REQ=""; HAVEREQ=0; POS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --require-mode|--require-mode=*)
      [ $HAVEREQ -eq 0 ] || { echo "verify.sh: --require-mode given twice" >&2; usage; }
      HAVEREQ=1
      if [ "$1" = --require-mode ]; then
        [ $# -ge 2 ] || { echo "verify.sh: --require-mode needs a value (silent or observe)" >&2; usage; }
        REQ="$2"; shift 2
      else REQ="${1#--require-mode=}"; shift; fi
      case "$REQ" in silent|observe) ;; *) echo "verify.sh: --require-mode must be silent or observe, got '$REQ'" >&2; usage ;; esac ;;
    -*) echo "verify.sh: unknown argument: $1" >&2; usage ;;
    *) POS+=("$1"); shift ;;
  esac
done
[ ${#POS[@]} -ge 1 ] && [ ${#POS[@]} -le 2 ] || { echo "verify.sh: expected FILE [keyid]" >&2; usage; }
for x in "${POS[@]}"; do [ -n "$x" ] || { echo "verify.sh: empty argument" >&2; usage; }; done
exec </dev/null
F="${POS[0]}"; PIN="${POS[1]:-${SILENT_EXPECT_KEYID:-df051692c8467ca1eceb62d03333a02928bcb1a7ab42133efa2e2ef4287d1c42}}"
MODEARG=(); [ $HAVEREQ -eq 1 ] && MODEARG=(--require-mode "$REQ")
KIND="$("$PY" -I -c 'import json,sys
try: d=json.load(open(sys.argv[1],encoding="utf-8"))
except Exception: print("bad"); sys.exit()
print("dsse" if "payloadType" in d else "legacy" if "signature" in d else "bad")' "$F")"
case "$KIND" in
  legacy)
    OUT="$("$PY" "$HERE/verify.py" "$F" "$PIN")"; RC=$?
    if [ $RC -ne 0 ]; then echo "$OUT"; exit $RC; fi
    MC="$("$PY" -I "$HERE/mode_check.py" "$F" "${MODEARG[@]}" 2>&1)"; MRC=$?
    [ $MRC -eq 0 ] && echo "OK (legacy format)"; echo "$MC"; exit $MRC ;;
  dsse)
    TMP="$(mktemp)"; trap 'rm -f "$TMP"' EXIT
    "$PY" -I -c 'import json,base64,sys
d=json.load(open(sys.argv[1],encoding="utf-8")); print(json.loads(base64.b64decode(d["payload"]))["predicate"]["pubkey_hex"])' "$F" > "$TMP" 2>/dev/null \
      || { echo "FAIL: no public key in predicate" >&2; exit 2; }
    "$PY" "$HERE/attest_verify.py" "$F" --key "$(cat "$TMP" | tr -d '\r\n')" --expect-keyid "$PIN" --check-subjects "$(dirname "$F")" || exit $?
    "$PY" -I "$HERE/mode_check.py" "$F" "${MODEARG[@]}" || exit $?
    echo "OK" ;;
  *) echo "FAIL: not an attestation" >&2; exit 2 ;;
esac
