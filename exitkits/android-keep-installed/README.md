<!-- FOUNDER-TEXT-PENDING -->
<!-- GATE 1 what it does: fetch, hash-check, ADB-install, signed receipt -->
<!-- GATE 2 legal line: supported, user-initiated ADB installs only; no circumvention -->
<!-- GATE 3 timeline: cited only from Google's own pages, quoted below -->
<!-- GATE 4 limits: no device test done; split APKs not handled -->

# android-keep-installed: install open-source Android apps from their GitHub releases, only after the publisher's SHA-256 matches, with a signed receipt

## What it does

`keep_installed.py` reads an `apps.json` list of `{name, github_repo, apk_pattern, expected_sha256 | expected_sha256_url}`. For each app it (1) fetches the latest GitHub release asset whose name matches `apk_pattern` (glob), (2) computes SHA-256 and compares it with the publisher-published hash (a literal, a URL, or the name pattern of a checksums asset in the same release; bare-hash and `hash  filename` formats), refusing to install on no hash or a mismatch, (3) runs `adb install -r` over an ADB connection you authorised (USB or wireless debugging), (4) writes `results/receipt_*.json` with `started, ended, device_serial_sha256, apps[{name, repo, version, asset, sha256, verified, installed}], adb_version`, signed with ed25519 (same scheme as `../../silent/attest.py`; key at `~/.stillherd/keep_installed_ed25519.pem`, created on first run; needs the `cryptography` package, otherwise the receipt is written unsigned and says so).

Verify a receipt: `python verify_receipt.py results/receipt_*.json [pinned_pubkey_sha256]`. `../../silent/verify.sh` does not fit: it also requires a proxy log file named in the attestation.

## Legal line

Supported, user-initiated ADB installs only. It does not root, does not bypass signature checks or any verification, and does not circumvent any platform control. It runs plain `adb install -r` on a device whose owner enabled debugging and approved the computer.

## Google developer verification timeline (quoted from Google's pages, fetched 2026-11-07)

- developer.android.com/developer-verification: "Effective September 30, 2026 Protections begin for users installing apps from participating stores in select regions on certified Android devices." Milestone table: "August 2026 Developer APIs, limited distribution accounts, and power user advanced flow launch."; "September 30, 2026 Protections begin for all users who install apps from participating stores in Brazil, Indonesia, Singapore, and Thailand on certified devices running Android 7+."; "2027 and beyond Verification capability will be expanded globally for all apps on certified Android devices in 2027."
- android-developers.googleblog.com/2025/08/elevating-android-security.html: "March 2026: Verification opens for all developers."; "September 2026: These requirements go into effect in Brazil, Indonesia, Singapore, and Thailand."; "2027 and beyond: We will continue to roll out these requirements globally."; update line: "Update 4/21/2026: Early access is closed and verification is open to all developers."

How that applies to ADB installs of any given app is UNTESTED here; this kit makes no claim about it.

## Limits

- No device test was done (no adb binary or device on the build machine); the `adb install -r` path and `installed=true` receipts are untested.
- Split APKs / app bundles are not handled (single-file assets only).
- Only the `latest` GitHub release is used; pre-releases are skipped by GitHub's `latest`.
- A hash published in the same release as the file proves integrity against corruption and a swapped mirror, not against a compromised publisher account.
- The sample list uses two non-APK release archives (a linux arm64 tarball from each of two projects) because they ship checksum files; it demonstrates fetch and hash verification, not an install.
- Prior art exists for each ingredient (release-fetch installers, hash checks, signed receipts); the bundle is not claimed as first.

## Dry-run transcript

`python keep_installed.py --dry-run --apps sample_apps.json` (exit 0; files in `results/`):

```
gh-cli   v2.103.3   gh_2.103.3_linux_arm64.tar.gz verified=True installed=False
hugo     v0.169.0   hugo_0.169.0_linux-arm64.tar.gz verified=True installed=False
receipt: results/receipt_20261108T074036Z_dryrun.json
verified 2/2
```

`python verify_receipt.py results/receipt_20261108T074036Z_dryrun.json` prints `OK`.
