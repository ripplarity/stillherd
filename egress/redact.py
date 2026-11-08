"""Redact request logs before publication: drop query strings, replace UUIDs and opaque ids with <id>.
Usage: python egress/redact.py <file.jsonl> [...]  (rewrites in place; idempotent)"""
import json, re, sys
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
OPAQUE = re.compile(r"(?<=/)(?:sdk-)?[A-Za-z0-9_-]{16,}(?=/|$)")
def redact_path(p):
    p = p.split("?", 1)[0]
    p = UUID.sub("<id>", p)
    return OPAQUE.sub("<id>", p)
for fn in sys.argv[1:]:
    out = []
    for line in open(fn, encoding="utf-8"):
        line = line.strip()
        if not line: continue
        d = json.loads(line)
        for k in ("path", "url"):
            if isinstance(d.get(k), str): d[k] = redact_path(d[k])
        out.append(json.dumps(d))
    open(fn, "w", encoding="utf-8", newline="\n").write("\n".join(out) + "\n")
    print("redacted", fn, len(out))
