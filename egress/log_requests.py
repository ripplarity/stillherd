"""mitmproxy addon: one JSON row per outbound request."""
import json, os, time

OUT = os.environ.get("EGRESS_OUT", "requests.jsonl")
TOOL = os.environ.get("EGRESS_TOOL", "unknown")
VERSION = os.environ.get("EGRESS_VERSION", "unknown")
STRIP_QUERY = os.environ.get("EGRESS_STRIP_QUERY") == "1"  # watch mode: never store query strings


def _write(row):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def request(flow):
    r = flow.request
    path, had_query = r.path, "?" in r.path
    if STRIP_QUERY:
        path = path.split("?", 1)[0]
    _write({
        "ts": time.time(), "tool": TOOL, "version": VERSION,
        "host": r.pretty_host, "path": path, "method": r.method,
        "bytes": len(r.raw_content or b""),
        "content_type": r.headers.get("content-type", ""),
        **({"had_query": had_query} if STRIP_QUERY else {}),
    })


def tls_failed_client(data):
    # certificate pinning / untrusted CA: host-only fallback row
    sni = getattr(data.conn, "sni", None) or "unknown"
    _write({
        "ts": time.time(), "tool": TOOL, "version": VERSION,
        "host": sni, "path": "", "method": "CONNECT-TLS-FAILED",
        "bytes": 0, "content_type": "",
    })
