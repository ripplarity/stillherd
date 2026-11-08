"""mitmproxy addon: block (HTTP 403) every host not on the allowlist; log every attempt as JSONL."""

import json
import os
import time

from mitmproxy import http

LOG = os.environ.get("SILENT_LOG", "silent.log.jsonl")
ALLOW = {
    h.strip().lower()
    for h in os.environ.get("SILENT_ALLOW", "127.0.0.1,localhost,::1").split(",")
    if h.strip()
}


def _write(row):
    row["ts"] = time.time()
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")


def _allowed(host):
    return host.lower() in ALLOW


def http_connect(flow: http.HTTPFlow):
    # CONNECT (HTTPS tunnel) to a non-allowlisted host: refuse before any TLS or upstream connection.
    h = flow.request.host
    if not _allowed(h):
        _write(
            {
                "host": h,
                "path": "",
                "method": "CONNECT",
                "bytes": 0,
                "action": "blocked",
                "stage": "connect",
            }
        )
        flow.response = http.Response.make(
            403, b"blocked by stillherd silent", {"Content-Type": "text/plain"}
        )


def request(flow: http.HTTPFlow):
    r = flow.request
    h = r.pretty_host
    n = len(r.raw_content or b"")
    if _allowed(h):
        _write(
            {
                "host": h,
                "path": r.path,
                "method": r.method,
                "bytes": n,
                "action": "allowed",
                "stage": "request",
            }
        )
    else:
        _write(
            {
                "host": h,
                "path": r.path,
                "method": r.method,
                "bytes": n,
                "action": "blocked",
                "stage": "request",
            }
        )
        flow.response = http.Response.make(
            403, b"blocked by stillherd silent", {"Content-Type": "text/plain"}
        )


def tls_failed_client(data):
    sni = getattr(data.conn, "sni", None) or "unknown"
    if not _allowed(sni):
        _write(
            {
                "host": sni,
                "path": "",
                "method": "TLS-FAILED",
                "bytes": 0,
                "action": "tls-failed",
                "stage": "tls",
            }
        )
