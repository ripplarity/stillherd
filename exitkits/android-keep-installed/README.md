# android-keep-installed: install open-source Android apps from GitHub releases or F-Droid, only after the publisher's SHA-256 matches, with a signed receipt

## What it does

`keep_installed.py` reads an `apps.json` list of `{name, github_repo, apk_pattern, expected_sha256 | expected_sha256_url}`. For each app it (1) fetches the latest GitHub release asset, or the F-Droid-published APK when `expected_sha256_source` is `fdroid`, whose name matches `apk_pattern` (glob), (2) computes SHA-256 and compares it with the publisher-published hash (a literal, a URL, or the name pattern of a checksums asset in the same release; bare-hash and `hash  filename` formats), refusing to install on no hash or a mismatch, (3) runs `adb install -r` over an ADB connection you authorised (USB or wireless debugging), (4) writes `results/receipt_*.json` with `started, ended, device_serial_sha256, apps[{name, repo, version, asset, sha256, verified, installed}], adb_version`, signed with ed25519 (same scheme as `../../silent/attest.py`; key at `~/.stillherd/keep_installed_ed25519.pem`, created on first run; needs the `cryptography` package, otherwise the receipt is written unsigned and says so).

- Only the `latest` GitHub release is used. Two hash sources: a publisher `.sha256` asset in the release (`expected_sha256_url`), or `"expected_sha256_source": "fdroid"` with `fdroid_package`, which reads the hash from f-droid.org `index-v2.json` (about 72 MB download per run) and downloads the APK from f-droid.org/repo.

Verify a receipt: `python verify_receipt.py results/receipt_*.json [pinned_pubkey_sha256]`. `../../silent/verify.sh` does not fit: it also requires a proxy log file named in the attestation.

## Legal line

Supported, user-initiated ADB installs only. It does not root, does not bypass signature checks or any verification, and does not circumvent any platform control. It runs plain `adb install -r` on a device whose owner enabled debugging and approved the computer.

## Google developer verification timeline (quoted from Google's pages, fetched 2026-11-07)

- developer.android.com/developer-verification: "Effective September 30, 2026 Protections begin for users installing apps from participating stores in select regions on certified Android devices." Milestone table: "August 2026 Developer APIs, limited distribution accounts, and power user advanced flow launch."; "September 30, 2026 Protections begin for all users who install apps from participating stores in Brazil, Indonesia, Singapore, and Thailand on certified devices running Android 7+."; "2027 and beyond Verification capability will be expanded globally for all apps on certified Android devices in 2027."
- android-developers.googleblog.com/2025/08/elevating-android-security.html: "March 2026: Verification opens for all developers."; "September 2026: These requirements go into effect in Brazil, Indonesia, Singapore, and Thailand."; "2027 and beyond: We will continue to roll out these requirements globally."; update line: "Update 4/21/2026: Early access is closed and verification is open to all developers."

How that applies to ADB installs of any given app is UNTESTED here; this kit makes no claim about it.

## Limits

- No Android device test was done; the `adb install -r` path and `installed=true` receipts are untested. A local `adb` (platform-tools 37.0.1) runs (`adb version` exits 0), but no device was attached.
- With no authorised device attached, a run without `--dry-run` prints a message, falls back to fetch + verify only, writes `installed=false`, and exits 0.
- Split APKs / app bundles are not handled (single-file assets only).
- A hash published beside the file proves integrity against corruption and a swapped mirror, not against a compromised publisher account.
- `tools/` (gitignored) holds platform-tools from dl.google.com/android/repository/platform-tools-latest-windows.zip. Google's platform-tools release page lists no SHA-256 (searched the fetched page), so the observed hash is recorded instead: 45f4d63113e895ebde0c90f194099a4676b6ac653bd28d54314a9e022bbc1a99 (fetched 2026-11-07). It is not a publisher-verified value.
- Prior art exists for each ingredient (release-fetch installers, hash checks, signed receipts); the bundle is not claimed as first.

## Dry-run transcript

`python keep_installed.py --dry-run --apps sample_apps.json` (exit 0; files in `results/`). Samples: Obtainium (GitHub release, `.sha256` asset) and NewPipe (F-Droid index hash):

```
obtainium v1.7.4     app-arm64-v8a-release.apk verified=True installed=False
newpipe  0.29.1     org.schabi.newpipe_1015_cb84069.apk verified=True installed=False
receipt: results/receipt_20261108T074239Z_dryrun.json
verified 2/2
```

`python verify_receipt.py results/receipt_20261108T074239Z_dryrun.json` prints `OK`.
