# SCRUB

Edits applied by vendor.sh (maintainer tool; its output is committed in vendor/hooks and installed by install.sh) to the COPIED files only (originals untouched). Scan covered all copied files for absolute user paths, emails, tokens/keys and project names. If a local, gitignored `.scrublist` exists (one term per line), vendor.sh aborts when any listed term remains in the copies.

| File | Edit |
|---|---|
| hooks/claim_capture.py (docstring) | example user/project paths rewritten to `$HOME\Development\<project>` and `<project-slug>` |
| hooks/verify_claims.py (comment, ~line 126) | a literal product name rewritten to `<project>` (generic word rule applied to all hooks) |
| all hooks | generic rule: `<drive>:\Users\<name>` -> `$HOME` (no other matches found at scan time) |

Scan findings that needed no edit: "token"/"secret" hits are parser tokens and secret-detection regexes in git_guard.sh (no real credentials); no emails found.
