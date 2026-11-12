"""Shared egress rules: RULES table, is_local, classify(row) -> (rule, class). Used by classify.py and watch_summary.py."""

import ipaddress
import re

RULES = [
    (
        "R1",
        "telemetry",
        "host or path matches `posthog|sentry|segment|statsig|datadog|mixpanel|amplitude|telemetry|analytics`",
    ),
    (
        "R3",
        "model-call",
        "POST whose path matches `chat|completions|generate|messages|responses|embeddings`",
    ),
    (
        "R3b",
        "local-api",
        "any other request to a loopback/private-address host (the local Ollama server)",
    ),
    (
        "R2",
        "update-check",
        "host `pypi.org`, or a GitHub host (`github.com`, `githubusercontent.com`) with a path matching `release|latest|tags|version`",
    ),
    ("R2b", "package-registry", "host `registry.npmjs.org`"),
    (
        "R4",
        "asset",
        "GET of a path ending `.json|.js|.css|.txt|.yml|.yaml|.png`, or host `raw.githubusercontent.com`, or host starting `models.`",
    ),
    ("R5", "unknown", "everything else"),
]
CLASSES = [c for _, c, _ in RULES]
RULE_TEXT = (
    "Rules are applied in the order listed (first match wins); external host = not a loopback/private address.\n\n"
    + "\n".join(f"- {r} {c}: {d}" for r, c, d in RULES)
)


def is_local(h):
    try:
        ip = ipaddress.ip_address(h)
        return ip.is_private or ip.is_loopback
    except ValueError:
        return h == "localhost"


def classify(r):
    h, p, m = r["host"], r["path"], r["method"]
    if re.search(
        r"posthog|sentry|segment|statsig|datadog|mixpanel|amplitude|telemetry|analytics",
        h + p,
        re.IGNORECASE,
    ):
        return "R1", "telemetry"
    if m == "POST" and re.search(
        r"chat|completions|generate|messages|responses|embeddings", p, re.IGNORECASE
    ):
        return "R3", "model-call"
    if is_local(h):
        return "R3b", "local-api"
    if h == "pypi.org" or (
        re.search(r"github(usercontent)?\.com$", h)
        and re.search(r"release|latest|tags|version", p, re.IGNORECASE)
    ):
        return "R2", "update-check"
    if h == "registry.npmjs.org":
        return "R2b", "package-registry"
    if (
        (m == "GET" and re.search(r"\.(json|js|css|txt|ya?ml|png)(\?|$)", p))
        or h == "raw.githubusercontent.com"
        or h.startswith("models.")
    ):
        return "R4", "asset"
    return "R5", "unknown"

