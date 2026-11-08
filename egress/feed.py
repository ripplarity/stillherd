#!/usr/bin/env python3
"""feed.py: build results/feed.xml (RSS 2.0) and results/feed.json from the committed results/CHANGELOG.md.
One item per dated diff section. Text carries only hosts, request counts, bytes and version numbers.
Regenerated from committed logs only; no network."""

import datetime as dt
import email.utils
import json
import os
import re
import sys
from xml.sax.saxutils import escape

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "results")
REPO = "https://github.com/ripplarity/stillherd"
CL_URL = REPO + "/blob/main/egress/results/CHANGELOG.md"
BANNED = ["telemetry", "spy", "leak", "exfiltrat", "hidden", "secret", "suspicious", "phone-home"]


def check(field, text, exempt=()):
    """Raise on banned words in generated prose. Hosts and file names are data: exempt, kept verbatim."""
    t = text
    for e in sorted(set(exempt), key=len, reverse=True):
        if e:
            t = t.replace(e, " ")
    low = t.lower()
    bad = [w for w in BANNED if w in low]
    if bad:
        raise ValueError("banned words %s in field '%s'" % (bad, field))


def slug(h, seen):
    s = re.sub(r"[^\w\- ]", "", h.lower()).replace(" ", "-")
    n = seen.get(s, 0)
    seen[s] = n + 1
    return s if n == 0 else "%s-%d" % (s, n)


def tool_ver(fn):
    """(tool, version, label) from <tool>_<version>[_optout]_<runN>.jsonl"""
    p = fn.replace(".jsonl", "").split("_")
    ver = p[1] if len(p) > 1 else "?"
    run = next((x for x in p[2:] if re.fullmatch(r"run\d+", x)), None)
    return p[0], ver, ("optout" in p[2:]), run


def parse(md):
    items, seen = [], {}
    parts = re.split(r"(?m)^## ", md)[1:]
    for sec in parts:
        lines = sec.rstrip("\n").split("\n")
        head = lines[0].strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2}): (\S+) vs (\S+)$", head)
        if not m:
            continue
        date, fa, fb = m.groups()
        body = lines[1:]
        added = removed = None
        reqs = None
        rows = []
        prose = []
        for ln in body:
            if ln.startswith(("Hosts added:", "Hosts removed:", "Total requests")):
                prose.append(ln)
            if ln.startswith("Hosts added:"):
                added = [] if "none" in ln else [x.strip() for x in ln[12:].rstrip(".").split(",")]
            elif ln.startswith("Hosts removed:"):
                removed = [] if "none" in ln else [x.strip() for x in ln[14:].rstrip(".").split(",")]
            elif ln.startswith("Total requests"):
                mm = re.match(r"Total requests (\d+) -> (\d+)", ln)
                reqs = (mm.group(1), mm.group(2))
            elif ln.startswith("|") and not ln.startswith("|---") and not ln.startswith("| host"):
                rows.append([c.strip() for c in ln.strip("|").split("|")])
        ta, va, oa, ra = tool_ver(fa)
        tb, vb, ob, rb = tool_ver(fb)
        tool = ta if ta == tb else "%s/%s" % (ta, tb)
        vers = va if va == vb else "%s -> %s" % (va, vb)
        if oa != ob:
            tag = "default vs opt-out run" if ob else "opt-out vs default run"
        elif ra and rb and ra != rb:
            tag = "%s vs %s" % (ra, rb)
        else:
            tag = ra or rb or ""
        title = "%s %s%s: hosts +%d/-%d, requests %s -> %s" % (
            tool, vers, " (%s)" % tag if tag else "", len(added or []), len(removed or []), reqs[0], reqs[1])
        desc = ["files: %s vs %s" % (fa, fb)] + prose + [
            "host | requests A | requests B | change | bytes A | bytes B | change"]
        desc += [" | ".join(r) for r in rows]
        desc = "\n".join(desc)
        hosts = list((added or []) + (removed or []) + [r[0] for r in rows]) + [fa, fb]
        check("title", title, hosts)
        check("description (section '%s')" % head, desc, hosts)
        items.append({
            "title": title,
            "description": desc,
            "link": CL_URL + "#" + slug(head, seen),
            "date": date,
            "pubDate": email.utils.format_datetime(
                dt.datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)),
        })
    return items


def build(res=RES):
    with open(os.path.join(res, "CHANGELOG.md"), encoding="utf-8") as f:
        items = parse(f.read())
    xml = ['<?xml version="1.0" encoding="UTF-8"?>', '<rss version="2.0"><channel>',
           "<title>stillherd egress changes</title>", "<link>%s</link>" % CL_URL,
           "<description>Host and request-count differences between recorded egress runs.</description>"]
    for it in items:
        xml += ["<item>", "<title>%s</title>" % escape(it["title"]),
                "<description>%s</description>" % escape(it["description"]),
                "<link>%s</link>" % escape(it["link"]),
                '<guid isPermaLink="true">%s</guid>' % escape(it["link"]),
                "<pubDate>%s</pubDate>" % it["pubDate"], "</item>"]
    xml.append("</channel></rss>")
    with open(os.path.join(res, "feed.xml"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(xml) + "\n")
    with open(os.path.join(res, "feed.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump({"items": items}, f, indent=2)
        f.write("\n")
    return items


if __name__ == "__main__":
    n = build(sys.argv[1] if len(sys.argv) > 1 else RES)
    print("feed: %d items" % len(n))
