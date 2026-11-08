# Week 1 egress table

| tool | version | external hosts | requests run1/run2 | bytes run1/run2 | completed turns run1/run2 | classification per host (rule, requests run1/run2) | telemetry hosts matched |
|---|---|---|---|---|---|---|---|
| aider | 0.86.2 | pypi.org, raw.githubusercontent.com | 28/28 | 24547/27025 | n/a/n/a | pypi.org: R2 update-check (1/1); raw.githubusercontent.com: R4 asset (5/5); local Ollama host: (22/22) | none |
| claude-code | 2.1.304 | api.anthropic.com, http-intake.logs.us5.datadoghq.com | 58/56 | 2664308/2614444 | n/a/n/a | api.anthropic.com: R3 model-call, R5 unknown (51/50); http-intake.logs.us5.datadoghq.com: R1 telemetry (7/6); local Ollama host: (0/0) | http-intake.logs.us5.datadoghq.com |
| claude-code | 2.1.304_optout | api.anthropic.com | 13/0 | 927062/0 | n/a/n/a | api.anthropic.com: R3 model-call, R5 unknown (13/0); local Ollama host: (0/0) | none |
| goose | 1.57.0 | models.dev | 12/12 | 72372/77886 | 4/4 | models.dev: R4 asset (4/4); local Ollama host: (8/8) | none |
| opencode | 1.19.5 | models.opencode.ai, registry.npmjs.org | 89/10 | 128205/127745 | 4/4 | models.opencode.ai: R4 asset (1/1); registry.npmjs.org: R2b package-registry (83/4); local Ollama host: (5/5) | none |

## Statements

- aider 0.86.2 sent 1 requests (run 1) and 1 requests (run 2) to pypi.org; rule(s): R2 update-check.
- aider 0.86.2 sent 5 requests (run 1) and 5 requests (run 2) to raw.githubusercontent.com; rule(s): R4 asset.
- aider 0.86.2 sent 22 requests (run 1) and 22 requests (run 2) to the local Ollama host.
- aider 0.86.2 external host sets in run 1 and run 2: identical.
- claude-code 2.1.304 sent 51 requests (run 1) and 50 requests (run 2) to api.anthropic.com; rule(s): R3 model-call, R5 unknown.
- claude-code 2.1.304 sent 7 requests (run 1) and 6 requests (run 2) to http-intake.logs.us5.datadoghq.com; rule(s): R1 telemetry.
- claude-code 2.1.304 sent 0 requests (run 1) and 0 requests (run 2) to the local Ollama host.
- claude-code 2.1.304 external host sets in run 1 and run 2: identical.
- claude-code 2.1.304_optout sent 13 requests (run 1) and 0 requests (run 2) to api.anthropic.com; rule(s): R3 model-call, R5 unknown.
- claude-code 2.1.304_optout sent 0 requests (run 1) and 0 requests (run 2) to the local Ollama host.
- claude-code 2.1.304_optout external host sets in run 1 and run 2: different (api.anthropic.com).
- goose 1.57.0 sent 4 requests (run 1) and 4 requests (run 2) to models.dev; rule(s): R4 asset.
- goose 1.57.0 sent 8 requests (run 1) and 8 requests (run 2) to the local Ollama host.
- goose 1.57.0 external host sets in run 1 and run 2: identical.
- opencode 1.19.5 sent 1 requests (run 1) and 1 requests (run 2) to models.opencode.ai; rule(s): R4 asset.
- opencode 1.19.5 sent 83 requests (run 1) and 4 requests (run 2) to registry.npmjs.org; rule(s): R2b package-registry.
- opencode 1.19.5 sent 5 requests (run 1) and 5 requests (run 2) to the local Ollama host.
- opencode 1.19.5 external host sets in run 1 and run 2: identical.

## Method

- Distro image: Ubuntu 24.04.5 WSL amd64 (official release image); SHA256 bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e.
- Proxy: mitmproxy 12.3.1, regular proxy mode inside the distro, CA installed in the system store.
- Tools: opencode 1.19.5 (npm), aider 0.86.2 (pip, earlier run, same session.txt), goose 1.57.0 (release CLI).
- Model server: Ollama on the host, model qwen2.5-coder:7b, reached over the distro's default gateway address.
- Session: `session.txt` (list files, add a function, run tests, explain a file, exit); each prompt is one non-interactive tool invocation; tool state is wiped and the proxy restarted before each run.
- Runs: two independent runs per tool; agreement is stated per tool in the Statements section.
- Classifier rules (`classify.py`):

  - R1 telemetry: host or path matches `posthog|sentry|segment|statsig|datadog|mixpanel|amplitude|telemetry|analytics`
  - R3 model-call: POST whose path matches `chat|completions|generate|messages|responses|embeddings`
  - R3b local-api: any other request to a loopback/private-address host (the local Ollama server)
  - R2 update-check: host `pypi.org`, or a GitHub host (`github.com`, `githubusercontent.com`) with a path matching `release|latest|tags|version`
  - R2b package-registry: host `registry.npmjs.org`
  - R4 asset: GET of a path ending `.json|.js|.css|.txt|.yml|.yaml|.png`, or host `raw.githubusercontent.com`, or host starting `models.`
  - R5 unknown: everything else

## Limits

- Proxy-visible traffic only: connections that bypass the proxy environment variables are not recorded.
- No pinned-certificate hosts were seen (no TLS-failure rows in any run).
- The session is 4 prompts.
- With the local 7B model, the captured replies are mostly tool-call JSON printed as text, so tool-execution traffic (file edits, test runs) was not exercised; the counts cover startup, model calls and metadata fetches.
- The model server is local Ollama, so no vendor model endpoints were exercised.
- Request counts depend on cache state at the start of a run; compare host sets as well as counts.

## Notes per tool (appended verbatim by classify.py; not derived from the logs)

- Claude Code row: host run, not the clean distro. The existing login was reused in place (no credentials copied, no API key); session `session.txt` (4 prompts) in an empty scratch directory containing only hello.py, `claude -p` per prompt with `--continue` after the first, `--setting-sources project --strict-mcp-config --disable-slash-commands`, tools limited to Read, Edit, Write, Glob, Grep, `python -m pytest`, `ls`. Model: the account default. Only host, path, method, request byte count and content type were stored; no request or response bodies. Two runs, proxy restarted between them, tool state not wiped (host config directory in use), so counts include any cache effects. Completed turns 4/4 were observed in the session output and are not recorded in the logs (table shows n/a). Of the api.anthropic.com requests, `grep -c '"path": "/v1/messages"'` gives 12 per run (rule R3 model-call); the rest (39/38) are bootstrap, settings, event-logging and other paths (rule R5 unknown).
