#!/usr/bin/env bash
# PreToolUse(Bash|PowerShell) guard: `git stash` is BANNED (allowed read-only forms: `git stash list`, `git stash show`).
# WHY: agents share checkouts and one stash stack, so `stash pop` can take a peer's entry and destroy their work.
# No node needed: the hook JSON is parsed with python3 (or sed if python3 is missing); the decision is made in bash.
# Fails OPEN on any parse error (prints nothing, exit 0 = allow).
set +e
in=$(cat 2>/dev/null)
# fast reject: no `git ... stash` anywhere in the raw payload
printf '%s' "$in" | grep -qE 'git[ 	]+(-[^ 	]+([ 	]+[^- 	][^ 	]*)?[ 	]+)*stash' || exit 0

get_cmd() {
  local c="" p
  for p in python3 python; do
    command -v "$p" >/dev/null 2>&1 || continue
    c=$(printf '%s' "$in" | "$p" -c 'import json,sys;print(json.load(sys.stdin).get("tool_input",{}).get("command",""))' 2>/dev/null)
    [ -n "$c" ] && break
  done
  if [ -z "$c" ]; then
    c=$(printf '%s' "$in" | sed -n 's/.*"command"[[:space:]]*:[[:space:]]*"\(\([^"\\]\|\\.\)*\)".*/\1/p' | head -1 | sed -e 's/\\"/"/g' -e 's/\\\\/\\/g')
  fi
  printf '%s' "$c"
}
cmd=$(get_cmd)
[ -z "$cmd" ] && exit 0
# drop quoted strings so `echo "git stash"` is not mistaken for an invocation
bare=$(printf '%s' "$cmd" | sed -E -e "s/\"[^\"]*\"//g" -e "s/'[^']*'//g")
[ -z "$bare" ] && bare="$cmd"
# any `git stash` invocation that is not `list`/`show`
printf '%s' "$bare" | grep -qE '(^|[;&|(]|&&|\|\||[ 	])git[ 	]+(-[^ 	]+([ 	]+[^- 	][^ 	]*)?[ 	]+)*stash([ 	]+(list|show)([ 	]|$)|[ 	]*$|[ 	]+(-|push|pop|apply|drop|clear|save|branch|create|store))' || exit 0
printf '%s' "$bare" | grep -qE 'git[ 	]+(-[^ 	]+([ 	]+[^- 	][^ 	]*)?[ 	]+)*stash[ 	]+(list|show)([ 	]|$)' && ! printf '%s' "$bare" | grep -qE 'stash[ 	]+(pop|apply|drop|clear|push|save|branch|create|store|-)' && exit 0
reason="git stash is BANNED: agents share one stash stack and a pop can take a peer's work. Use 'git show <rev>:<path> > file' to read another revision, 'git worktree add' for an isolated checkout, or 'git diff <rev> -- <path>'. Only 'git stash list' and 'git stash show' are allowed."
printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}' "$reason"
exit 0
