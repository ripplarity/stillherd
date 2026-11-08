# Egress results (clean distro)

Rules are applied in the order listed (first match wins); external host = not a loopback/private address.

- R1 telemetry: host or path matches `posthog|sentry|segment|statsig|datadog|mixpanel|amplitude|telemetry|analytics`
- R3 model-call: POST whose path matches `chat|completions|generate|messages|responses|embeddings`
- R3b local-api: any other request to a loopback/private-address host (the local Ollama server)
- R2 update-check: host `pypi.org`, or a GitHub host (`github.com`, `githubusercontent.com`) with a path matching `release|latest|tags|version`
- R2b package-registry: host `registry.npmjs.org`
- R4 asset: GET of a path ending `.json|.js|.css|.txt|.yml|.yaml|.png`, or host `raw.githubusercontent.com`, or host starting `models.`
- R5 unknown: everything else

| tool | version | run | external hosts | requests | bytes | telemetry | model-call | local-api | update-check | package-registry | asset | unknown |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| aider | 0.86.2 | 1 | 2 | 28 | 24547 | 0 | 5 | 17 | 1 | 0 | 5 | 0 |
| aider | 0.86.2 | 2 | 2 | 28 | 27025 | 0 | 5 | 17 | 1 | 0 | 5 | 0 |
| claude-code | 2.1.304 | 1 | 2 | 58 | 2664308 | 7 | 12 | 0 | 0 | 0 | 0 | 39 |
| claude-code | 2.1.304 | 2 | 2 | 56 | 2614444 | 6 | 12 | 0 | 0 | 0 | 0 | 38 |
| claude-code | 2.1.304_optout | 1 | 1 | 13 | 927062 | 0 | 9 | 0 | 0 | 0 | 0 | 4 |
| goose | 1.57.0 | 1 | 1 | 12 | 72372 | 0 | 4 | 4 | 0 | 0 | 4 | 0 |
| goose | 1.57.0 | 2 | 1 | 12 | 77886 | 0 | 4 | 4 | 0 | 0 | 4 | 0 |
| opencode | 1.19.5 | 1 | 2 | 89 | 128205 | 0 | 5 | 0 | 0 | 83 | 1 | 0 |
| opencode | 1.19.5 | 2 | 2 | 10 | 127745 | 0 | 5 | 0 | 0 | 4 | 1 | 0 |

| tool | run1 vs run2 | detail |
|---|---|---|
| aider | agree | host sets identical; class counts run1 {'update-check': 1, 'asset': 5, 'local-api': 17, 'model-call': 5} run2 {'update-check': 1, 'asset': 5, 'local-api': 17, 'model-call': 5} |
| claude-code | agree | host sets identical; class counts run1 {'unknown': 39, 'model-call': 12, 'telemetry': 7} run2 {'unknown': 38, 'model-call': 12, 'telemetry': 6} |
| goose | agree | host sets identical; class counts run1 {'asset': 4, 'local-api': 4, 'model-call': 4} run2 {'asset': 4, 'local-api': 4, 'model-call': 4} |
| opencode | agree | host sets identical; class counts run1 {'asset': 1, 'model-call': 5, 'package-registry': 83} run2 {'asset': 1, 'model-call': 5, 'package-registry': 4} |

- aider run1 external hosts: pypi.org, raw.githubusercontent.com
- aider run2 external hosts: pypi.org, raw.githubusercontent.com
- claude-code run1 external hosts: api.anthropic.com, http-intake.logs.us5.datadoghq.com
- claude-code run2 external hosts: api.anthropic.com, http-intake.logs.us5.datadoghq.com
- claude-code run1 external hosts: api.anthropic.com
- goose run1 external hosts: models.dev
- goose run2 external hosts: models.dev
- opencode run1 external hosts: models.opencode.ai, registry.npmjs.org
- opencode run2 external hosts: models.opencode.ai, registry.npmjs.org
