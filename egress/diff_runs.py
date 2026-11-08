#!/usr/bin/env python3
"""diff_runs.py A B: compare two egress result files (JSON lines from log_requests.py).
Prints hosts added, hosts removed, per-host request-count change and request-byte change as a markdown table,
and appends the same text to results/CHANGELOG.md under a dated heading. Descriptive only."""

import collections
import datetime as dt
import ipaddress
import json
import os
import sys


def is_local(h):
    if h in ("localhost", "host.docker.internal"):
        return True
    try:
        a = ipaddress.ip_address(h)
        return a.is_private or a.is_loopback
    except ValueError:
        return False


def load(path):
    n, b = collections.Counter(), collections.Counter()
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        h = "local model server (private address)" if is_local(r["host"]) else r["host"]
        n[h] += 1
        b[h] += int(r.get("bytes") or 0)
    return n, b


def sign(x):
    return f"+{x}" if x > 0 else str(x)


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: diff_runs.py A.jsonl B.jsonl")
    pa, pb = sys.argv[1], sys.argv[2]
    na, ba = load(pa)
    nb, bb = load(pb)
    ha, hb = set(na), set(nb)
    added, removed = sorted(hb - ha), sorted(ha - hb)
    la, lb = os.path.basename(pa), os.path.basename(pb)
    out = [f"## {dt.datetime.now(dt.timezone.utc):%Y-%m-%d}: {la} vs {lb}", ""]
    out.append("Hosts added: " + (", ".join(added) if added else "none") + ".")
    out.append("Hosts removed: " + (", ".join(removed) if removed else "none") + ".")
    out.append(
        f"Total requests {sum(na.values())} -> {sum(nb.values())} ({sign(sum(nb.values()) - sum(na.values()))}); "
        f"request bytes {sum(ba.values())} -> {sum(bb.values())} ({sign(sum(bb.values()) - sum(ba.values()))})."
    )
    out.append("")
    out.append(
        "| host | requests A | requests B | change | bytes A | bytes B | change |"
    )
    out.append("|---|---|---|---|---|---|---|")
    for h in sorted(ha | hb):
        out.append(
            f"| {h} | {na[h]} | {nb[h]} | {sign(nb[h] - na[h])} | {ba[h]} | {bb[h]} | {sign(bb[h] - ba[h])} |"
        )
    out.append("")
    text = "\n".join(out)
    print(text)
    cl = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "results", "CHANGELOG.md"
    )
    new = not os.path.exists(cl)
    with open(cl, "a", encoding="utf-8") as f:
        if new:
            f.write(
                "# Egress changelog\n\nOne section per comparison of two result files. Counts are proxy-visible requests; bytes are request bytes.\n\n"
            )
        f.write(text + "\n")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import feed

    feed.build()


if __name__ == "__main__":
    main()
