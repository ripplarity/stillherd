"""keep_installed: fetch latest GitHub release assets, verify publisher SHA-256, adb install -r, write a signed receipt.

Usage: python keep_installed.py --apps apps.json [--dry-run] [--serial SERIAL] [--out results/]
apps.json: list of {name, github_repo, apk_pattern (glob on asset name),
  expected_sha256 (hex) OR expected_sha256_url (http URL, or a glob naming a release asset)}.
The hash source may be a bare hash or a checksums file ("<hash>  <filename>" lines).
Never roots, never bypasses signature or Play/verification checks; plain `adb install -r` only.
"""

import argparse
import base64
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timezone

KEY = os.environ.get(
    "KEEP_INSTALLED_KEY",
    os.path.join(os.path.expanduser("~"), ".stillherd", "keep_installed_ed25519.pem"),
)
UA = {"User-Agent": "keep-installed/1", "Accept": "application/vnd.github+json"}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def find_hash(text, asset_name):
    """Return the hex digest for asset_name from a bare-hash or checksums-file body."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for l in lines:
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", l)
        if m and os.path.basename(m.group(2)) == asset_name:
            return m.group(1).lower()
    if len(lines) == 1:
        m = re.match(r"^([0-9a-fA-F]{64})(\s|$)", lines[0])
        if m:
            return m.group(1).lower()
    return None


_FD = {}


def fdroid_latest(pkg):
    """Newest version of pkg from the F-Droid index: (versionName, file name, sha256)."""
    if "idx" not in _FD:
        _FD["idx"] = json.loads(
            get(
                "https://f-droid.org/repo/index-v2.json",
                {"User-Agent": "keep-installed/1"},
            )
        )
    vs = _FD["idx"]["packages"][pkg]["versions"]
    x = vs[max(vs, key=lambda h: vs[h]["manifest"]["versionCode"])]
    return (
        x["manifest"]["versionName"],
        x["file"]["name"].lstrip("/"),
        x["file"]["sha256"].lower(),
    )


def _download_check(rec, url, path, expected):
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "keep-installed/1"})
    with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            h.update(b)
            f.write(b)
    rec["sha256"] = h.hexdigest()
    rec["verified"] = rec["sha256"] == expected
    if not rec["verified"]:
        rec["error"] = "sha256 mismatch; refusing"
        return rec, None
    return rec, path


def process_fdroid(app, tmp):
    rec = {
        "name": app.get("name"),
        "repo": "fdroid:%s" % app.get("fdroid_package"),
        "version": None,
        "asset": None,
        "sha256": None,
        "verified": False,
        "installed": False,
    }
    try:
        rec["version"], rec["asset"], expected = fdroid_latest(app["fdroid_package"])
        return _download_check(
            rec,
            "https://f-droid.org/repo/" + rec["asset"],
            os.path.join(tmp, rec["asset"]),
            expected,
        )
    except Exception as e:
        rec["error"] = "%s: %s" % (type(e).__name__, e)
        return rec, None


def process(app, tmp):
    if app.get("expected_sha256_source") == "fdroid":
        return process_fdroid(app, tmp)
    rec = {
        "name": app.get("name"),
        "repo": app.get("github_repo"),
        "version": None,
        "asset": None,
        "sha256": None,
        "verified": False,
        "installed": False,
    }
    try:
        rel = json.loads(
            get("https://api.github.com/repos/%s/releases/latest" % app["github_repo"])
        )
        rec["version"] = rel.get("tag_name")
        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        hits = sorted(n for n in assets if fnmatch.fnmatch(n, app["apk_pattern"]))
        if not hits:
            rec["error"] = "no asset matches apk_pattern"
            return rec, None
        rec["asset"] = hits[0]
        expected = (app.get("expected_sha256") or "").lower() or None
        if not expected and app.get("expected_sha256_url"):
            u = app["expected_sha256_url"]
            if not u.startswith("http"):
                m = sorted(n for n in assets if fnmatch.fnmatch(n, u))
                u = assets[m[0]] if m else None
            if u:
                expected = find_hash(get(u).decode("utf-8", "replace"), rec["asset"])
        if not expected:
            rec["error"] = "no publisher hash found; refusing"
            return rec, None
        h = hashlib.sha256()
        path = os.path.join(tmp, rec["asset"])
        req = urllib.request.Request(
            assets[rec["asset"]], headers={"User-Agent": "keep-installed/1"}
        )
        with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                h.update(b)
                f.write(b)
        rec["sha256"] = h.hexdigest()
        rec["verified"] = rec["sha256"] == expected
        if not rec["verified"]:
            rec["error"] = "sha256 mismatch; refusing"
            return rec, None
        return rec, path
    except Exception as e:  # network/parse errors never install
        rec["error"] = "%s: %s" % (type(e).__name__, e)
        return rec, None


def adb(args, serial=None):
    here = os.path.dirname(os.path.abspath(__file__))
    local = os.path.join(
        here, "tools", "platform-tools", "adb.exe" if os.name == "nt" else "adb"
    )
    cmd = (
        [local if os.path.exists(local) else "adb"]
        + (["-s", serial] if serial else [])
        + args
    )
    return subprocess.run(cmd, capture_output=True, text=True, timeout=600)


def sign(payload):
    """Same scheme as silent/attest.py: ed25519 over canonical sorted JSON, pubkey embedded."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if os.path.exists(KEY):
        key = serialization.load_pem_private_key(open(KEY, "rb").read(), password=None)
    else:
        os.makedirs(os.path.dirname(KEY), exist_ok=True)
        key = Ed25519PrivateKey.generate()
        open(KEY, "wb").write(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    payload["pubkey"] = base64.b64encode(pub).decode()
    payload["pubkey_sha256"] = hashlib.sha256(pub).hexdigest()
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["signature"] = base64.b64encode(key.sign(canon)).decode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apps", required=True)
    ap.add_argument(
        "--dry-run", action="store_true", help="fetch + verify only; installed=false"
    )
    ap.add_argument("--serial")
    ap.add_argument(
        "--out",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "results"),
    )
    a = ap.parse_args()
    apps = json.load(open(a.apps, encoding="utf-8"))
    started = now()
    serial_hash, adb_version = None, None
    try:
        adb_version = adb(["version"]).stdout.splitlines()[0]
    except Exception:
        adb_version = None
    if not a.dry_run:
        try:
            s = a.serial or adb(["get-serialno"]).stdout.strip()
        except (FileNotFoundError, subprocess.SubprocessError):
            s = ""
        if not s or s == "unknown":
            print(
                "no authorised adb device attached; continuing as --dry-run (fetch + verify only, nothing installed)"
            )
            a.dry_run = True
        else:
            a.serial = s
            serial_hash = hashlib.sha256(s.encode()).hexdigest()
    out_apps, ok = [], 0
    with tempfile.TemporaryDirectory() as tmp:
        for app in apps:
            rec, path = process(app, tmp)
            if rec["verified"]:
                ok += 1
                if path and not a.dry_run:
                    r = adb(["install", "-r", path], a.serial)
                    rec["installed"] = r.returncode == 0 and "Success" in r.stdout
                    if not rec["installed"]:
                        rec["error"] = (r.stdout + r.stderr).strip()[-200:]
            print(
                "%-8s %-10s %s verified=%s installed=%s%s"
                % (
                    rec["name"],
                    rec["version"],
                    rec["asset"],
                    rec["verified"],
                    rec["installed"],
                    " ERR " + rec["error"] if rec.get("error") else "",
                )
            )
            out_apps.append(rec)
    payload = {
        "version": "stillherd-keep-installed-1",
        "started": started,
        "ended": now(),
        "dry_run": a.dry_run,
        "device_serial_sha256": serial_hash,
        "apps": out_apps,
        "adb_version": adb_version,
    }
    try:
        sign(payload)
    except ImportError:
        payload["signature"] = None
        print("note: 'cryptography' not installed; receipt is UNSIGNED")
    os.makedirs(a.out, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    op = os.path.join(
        a.out, "receipt_%s%s.json" % (stamp, "_dryrun" if a.dry_run else "")
    )
    json.dump(payload, open(op, "w"), indent=2)
    print("receipt: %s" % os.path.relpath(op))
    print("verified %d/%d" % (ok, len(apps)))
    return 0 if ok == len(apps) else 1


if __name__ == "__main__":
    sys.exit(main())
