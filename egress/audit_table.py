#!/usr/bin/env python3
"""audit_table.py TOOL "VARS" A.jsonl B.jsonl: per-host request counts, default run (A) vs opt-out run (B), as markdown.
Prints the table and appends it to results/AUDITS.md. Counts are proxy-visible requests; local addresses are grouped."""
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from diff_runs import load  # noqa: E402

if len(sys.argv) != 5:
    sys.exit('usage: audit_table.py TOOL "VARS" A.jsonl B.jsonl')
tool, variables, pa, pb = sys.argv[1:]
na, ba = load(pa)
nb, bb = load(pb)
out = [f"## {dt.datetime.now(dt.timezone.utc):%Y-%m-%d}: {tool}", "",
       f"Default run: {os.path.basename(pa)}", f"Opt-out run: {os.path.basename(pb)}",
       f"Variables used for the opt-out run: {variables}", "",
       f"Total requests {sum(na.values())} -> {sum(nb.values())}; request bytes {sum(ba.values())} -> {sum(bb.values())}.", "",
       "| host | requests default | requests opt-out | bytes default | bytes opt-out |", "|---|---|---|---|---|"]
for h in sorted(set(na) | set(nb)):
    out.append(f"| {h} | {na[h]} | {nb[h]} | {ba[h]} | {bb[h]} |")
out.append("")
text = "\n".join(out)
print(text)
p = os.environ.get("AUDITS_OUT") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "AUDITS.md")
new = not os.path.exists(p)
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, "a", encoding="utf-8") as f:
    if new:
        f.write("# Egress audits\n\nOne section per `stillherd audit` run: default run against the documented opt-out run.\n\n")
    f.write(text + "\n")
