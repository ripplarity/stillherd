# Egress changelog

One section per comparison of two result files. Counts are proxy-visible requests; bytes are request bytes.

## 2026-11-08: opencode_1.19.5_run1.jsonl vs opencode_1.19.5_run2.jsonl

Hosts added: none.
Hosts removed: none.
Total requests 89 -> 10 (-79); request bytes 128205 -> 127745 (-460).

| host | requests A | requests B | change | bytes A | bytes B | change |
|---|---|---|---|---|---|---|
| local model server (private address) | 5 | 5 | 0 | 127423 | 126963 | -460 |
| models.opencode.ai | 1 | 1 | 0 | 0 | 0 | 0 |
| registry.npmjs.org | 83 | 4 | -79 | 782 | 782 | 0 |

## 2026-11-08: claude-code_2.1.304_run1.jsonl vs claude-code_2.1.304_optout_run1.jsonl

Hosts added: none.
Hosts removed: http-intake.logs.us5.datadoghq.com.
Total requests 58 -> 13 (-45); request bytes 2664308 -> 927062 (-1737246).

| host | requests A | requests B | change | bytes A | bytes B | change |
|---|---|---|---|---|---|---|
| api.anthropic.com | 51 | 13 | -38 | 2327943 | 927062 | -1400881 |
| http-intake.logs.us5.datadoghq.com | 7 | 0 | -7 | 336365 | 0 | -336365 |

