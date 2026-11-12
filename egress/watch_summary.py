#!/usr/bin/env python3
"""watch_summary.py LOG.jsonl: table of external hosts (host, requests, bytes out, rule) from a log_requests.py log.
No request or response bodies are read: the log has only host, path, method and byte counts. Local addresses are not listed.
Importable: summarize(rows) -> list of dicts, table(rows) -> str."""
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rules import classify, is_local  # noqa: E402


def load(path):
    if not os.path.exists(path):
        return []
    return [json.loads(l) for l in open(path, encoding="utf-8") if l.strip()]


def summarize(rows):
    hosts = collections.OrderedDict()
    for r in rows:
        h = r["host"]
        if is_local(h):
            continue
        e = hosts.setdefault(h, {"host": h, "requests": 0, "bytes_out": 0, "rules": collections.Counter(), "tls_failed": 0})
        e["requests"] += 1
        e["bytes_out"] += int(r.get("bytes", 0))
        if r.get("method") == "CONNECT-TLS-FAILED":
            e["tls_failed"] += 1
        code, cls = classify(r)
        e["rules"][f"{code} {cls}"] += 1
    out = []
    for e in hosts.values():
        out.append({"host": e["host"], "requests": e["requests"], "bytes_out": e["bytes_out"],
                    "rule": ", ".join(sorted(e["rules"])), "tls_failed": e["tls_failed"]})
    return sorted(out, key=lambda x: (-x["requests"], x["host"]))


def table(rows):
    s = summarize(rows)
    local = sum(1 for r in rows if is_local(r["host"]))
    if not s:
        note = f" ({local} local request(s) not listed)" if local else ""
        return "no external requests observed" + note
    out = ["| host | requests | bytes out | rule |", "|---|---|---|---|"]
    out += [f"| {e['host']} | {e['requests']} | {e['bytes_out']} | {e['rule']} |" for e in s]
    failed = sum(e["tls_failed"] for e in s)
    if failed:
        out.append(f"\n{failed} TLS handshake(s) failed (the client does not trust the proxy CA): those rows show the host only.")
    if local:
        out.append(f"{local} local request(s) not listed.")
    return "\n".join(out)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: watch_summary.py LOG.jsonl")
    print(table(load(sys.argv[1])))
