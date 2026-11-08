"""mitmproxy addon: one JSON row per outbound request."""
import json, os, time

OUT = os.environ.get("EGRESS_OUT", "requests.jsonl")
TOOL = os.environ.get("EGRESS_TOOL", "unknown")
VERSION = os.environ.get("EGRESS_VERSION", "unknown")


def _write(row):
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def request(flow):
    r = flow.request
    _write({
        "ts": time.time(), "tool": TOOL, "version": VERSION,
        "host": r.pretty_host, "path": r.path, "method": r.method,
        "bytes": len(r.raw_content or b""),
        "content_type": r.headers.get("content-type", ""),
    })


def tls_failed_client(data):
    # certificate pinning / untrusted CA: host-only fallback row
    sni = getattr(data.conn, "sni", None) or "unknown"
    _write({
        "ts": time.time(), "tool": TOOL, "version": VERSION,
        "host": sni, "path": "", "method": "CONNECT-TLS-FAILED",
        "bytes": 0, "content_type": "",
    })
