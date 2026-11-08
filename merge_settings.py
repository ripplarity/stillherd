import json
import os
import sys

p = sys.argv[1]
s = json.load(open(p)) if os.path.exists(p) else {}
D = '"$CLAUDE_PROJECT_DIR"/.claude/hooks/'


def add(ev, matcher, cmd):
    lst = s.setdefault("hooks", {}).setdefault(ev, [])
    if any(h.get("command") == cmd for e in lst for h in e.get("hooks", [])):
        return
    e = {"hooks": [{"type": "command", "command": cmd}]}
    if matcher:
        e["matcher"] = matcher
    lst.append(e)


add("PreToolUse", "Bash", "bash " + D + "git_guard.sh")
add("PreToolUse", "Bash", "bash " + D + "stash_guard.sh")
add(
    "SubagentStop",
    None,
    (sys.argv[2] if len(sys.argv) > 2 else "python3") + " " + D + "claim_capture.py",
)
s["stillherd"] = {"version": "0.1.0"}
s.pop("corral", None)  # key used by releases before the rename
json.dump(s, open(p, "w"), indent=2)
