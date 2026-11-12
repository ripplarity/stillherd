"""Build results/TABLE.md and results/WEEK1_TABLE.md from results/*.jsonl."""

import collections
import glob
import ipaddress
import json
import os
import re


from rules import CLASSES, RULE_TEXT, RULES, classify, is_local  # noqa: F401


here = os.path.dirname(os.path.abspath(__file__))
runs = {}
for f in sorted(glob.glob(os.path.join(here, "results", "*.jsonl"))):
    m = re.match(r"(.+?)_(.+)_run(\d)\.jsonl$", os.path.basename(f))
    tool, ver, n = m.group(1), m.group(2), m.group(3)
    runs.setdefault((tool, ver), {})[n] = [json.loads(l) for l in open(f) if l.strip()]


def turns(tool, n):
    """Turns completed = invocations with exit code 0 whose captured output holds at least one
    reply line beyond the tool banner (counted from the saved session output)."""
    base = os.path.join(here, "results", "sessions", f"{tool}_run{n}")
    if not (os.path.exists(base + ".out") and os.path.exists(base + ".turns.txt")):
        return "n/a"
    rcs = [
        int(x)
        for x in re.findall(r"TURN \d+ rc=(\d+)", open(base + ".turns.txt").read())
    ]
    secs = re.split(
        r"^--- /tmp/",
        open(base + ".out", encoding="utf-8", errors="replace").read(),
        flags=re.MULTILINE,
    )[1:]
    banner = re.compile(
        r"^\s*$|^> build|goose is ready|__\(|\\____\)|^\s*L L|^[^\n]*/tmp/s_"
    )
    done = 0
    for rc, sec in zip(rcs, secs):
        body = [
            l
            for l in re.sub(r"\x1b\[[0-9;]*m", "", sec).splitlines()[1:]
            if not banner.search(l)
        ]
        done += rc == 0 and bool(body)
    return str(done)


# ---- TABLE.md
out = [
    "# Egress results (clean distro)",
    "",
    RULE_TEXT,
    "",
    "| tool | version | run | external hosts | requests | bytes | "
    + " | ".join(CLASSES)
    + " |",
    "|---|---|---|---|---|---|" + "---|" * len(CLASSES),
]
agree = ["", "| tool | run1 vs run2 | detail |", "|---|---|---|"]
detail = []
for (tool, ver), rs in sorted(runs.items()):
    sets, cls = {}, {}
    for n, rows in sorted(rs.items()):
        ext = sorted({r["host"] for r in rows if not is_local(r["host"])})
        c = collections.Counter(classify(r)[1] for r in rows)
        sets[n], cls[n] = set(ext), c
        out.append(
            f"| {tool} | {ver} | {n} | {len(ext)} | {len(rows)} | {sum(r['bytes'] for r in rows)} | "
            + " | ".join(str(c.get(k, 0)) for k in CLASSES)
            + " |"
        )
        detail.append(f"- {tool} run{n} external hosts: {', '.join(ext) or '(none)'}")
    if len(sets) == 2:
        same = sets["1"] == sets["2"]
        diff = (
            "identical"
            if same
            else "differ: " + ", ".join(sorted(sets["1"] ^ sets["2"]))
        )
        agree.append(
            f"| {tool} | {'agree' if same else 'DISAGREE'} | host sets {diff}; class counts run1 "
            f"{dict(cls['1'])} run2 {dict(cls['2'])} |"
        )
out += agree + [""] + detail + [""]
open(
    os.path.join(here, "results", "TABLE.md"), "w", encoding="utf-8", newline="\n"
).write("\n".join(out))

# ---- WEEK1_TABLE.md
meta_p = os.path.join(here, "results", "method.json")
meta = json.load(open(meta_p)) if os.path.exists(meta_p) else {}
w = [
    "# Week 1 egress table",
    "",
    "| tool | version | external hosts | requests run1/run2 | bytes run1/run2 | completed turns run1/run2 | classification per host (rule, requests run1/run2) | telemetry hosts matched |",
    "|---|---|---|---|---|---|---|---|",
]
stmts = []
for (tool, ver), rs in sorted(runs.items()):
    r1, r2 = rs.get("1", []), rs.get("2", [])
    hosts = sorted({r["host"] for r in r1 + r2 if not is_local(r["host"])})
    cells = []
    for h in hosts:
        rules = sorted(
            {classify(r)[0] + " " + classify(r)[1] for r in r1 + r2 if r["host"] == h}
        )
        a = sum(1 for r in r1 if r["host"] == h)
        b = sum(1 for r in r2 if r["host"] == h)
        cells.append(f"{h}: {', '.join(rules)} ({a}/{b})")
        stmts.append(
            f"- {tool} {ver} sent {a} requests (run 1) and {b} requests (run 2) to {h}; rule(s): {', '.join(rules)}."
        )
    loc1 = sum(1 for r in r1 if is_local(r["host"]))
    loc2 = sum(1 for r in r2 if is_local(r["host"]))
    cells.append(f"local Ollama host: ({loc1}/{loc2})")
    stmts.append(
        f"- {tool} {ver} sent {loc1} requests (run 1) and {loc2} requests (run 2) to the local Ollama host."
    )
    tel = sorted({r["host"] for r in r1 + r2 if classify(r)[0] == "R1"})
    w.append(
        f"| {tool} | {ver} | {', '.join(hosts) or 'none'} | {len(r1)}/{len(r2)} | "
        f"{sum(r['bytes'] for r in r1)}/{sum(r['bytes'] for r in r2)} | {turns(tool, 1)}/{turns(tool, 2)} | "
        f"{'; '.join(cells)} | {', '.join(tel) or 'none'} |"
    )
    s1 = {r["host"] for r in r1 if not is_local(r["host"])}
    s2 = {r["host"] for r in r2 if not is_local(r["host"])}
    stmts.append(
        f"- {tool} {ver} external host sets in run 1 and run 2: {'identical' if s1 == s2 else 'different (' + ', '.join(sorted(s1 ^ s2)) + ')'}."
    )
w += ["", "## Statements", ""] + stmts
w += (
    [
        "",
        "## Method",
        "",
        f"- Distro image: {meta.get('image', 'n/a')}; SHA256 {meta.get('image_sha256', 'n/a')}.",
        f"- Proxy: mitmproxy {meta.get('mitmproxy', 'n/a')}, regular proxy mode inside the distro, CA installed in the system store.",
        f"- Tools: {meta.get('tools', 'n/a')}.",
        "- Model server: Ollama on the host, model qwen2.5-coder:7b, reached over the distro's default gateway address.",
        "- Session: `session.txt` (list files, add a function, run tests, explain a file, exit); each prompt is one non-interactive tool invocation; tool state is wiped and the proxy restarted before each run.",
        "- Runs: two independent runs per tool; agreement is stated per tool in the Statements section.",
        "- Classifier rules (`classify.py`):",
        "",
    ]
    + [f"  - {r} {c}: {d}" for r, c, d in RULES]
    + [
        "",
        "## Limits",
        "",
        "- Proxy-visible traffic only: connections that bypass the proxy environment variables are not recorded.",
        "- No pinned-certificate hosts were seen (no TLS-failure rows in any run).",
        "- The session is 4 prompts.",
        "- With the local 7B model, the captured replies are mostly tool-call JSON printed as text, so tool-execution traffic (file edits, test runs) was not exercised; the counts cover startup, model calls and metadata fetches.",
        "- The model server is local Ollama, so no vendor model endpoints were exercised.",
        "- Request counts depend on cache state at the start of a run; compare host sets as well as counts.",
        "",
    ]
)
open(
    os.path.join(here, "results", "WEEK1_TABLE.md"), "w", encoding="utf-8", newline="\n"
).write("\n".join(w))
print("TABLE.md and WEEK1_TABLE.md written")
notes = os.path.join(here, "results", "NOTES.md")
if os.path.exists(notes):
    with open(
        os.path.join(here, "results", "WEEK1_TABLE.md"),
        "a",
        encoding="utf-8",
        newline="\n",
    ) as fh:
        fh.write("\n" + open(notes, encoding="utf-8").read())
