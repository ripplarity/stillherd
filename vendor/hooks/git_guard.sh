#!/usr/bin/env bash
# PreToolUse(Bash) guard. Reads hook JSON on stdin. Fails OPEN on any error (prints nothing, exit 0 = allow).
# Only acts on git push / gh pr create / git commit.
#   - Real credential leak in the to-be-pushed diff   -> permissionDecision: deny
#   - Committing/pushing on a protected branch (main)  -> permissionDecision: ask
# No node needed: the hook JSON is parsed with python3 (or sed if python3 is missing); decisions are made in bash.
set +e
in=$(cat 2>/dev/null)
# fast reject: no git/gh push/commit anywhere in the raw payload -> allow
printf '%s' "$in" | grep -qE 'git +push|gh +pr +create|git +commit' || exit 0

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

is_push=0; is_pr=0; is_commit=0
printf '%s' "$cmd" | grep -qE '(^|[;&| ])git +push'      && is_push=1
printf '%s' "$cmd" | grep -qE '(^|[;&| ])gh +pr +create' && is_pr=1
printf '%s' "$cmd" | grep -qE '(^|[;&| ])git +commit'    && is_commit=1
[ $is_push -eq 0 ] && [ $is_pr -eq 0 ] && [ $is_commit -eq 0 ] && exit 0

repo=$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null) || exit 0
[ -z "$repo" ] && exit 0

# JSON-string escape in pure sed/awk
esc() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/	/\\t/g' | awk 'BEGIN{ORS="\\n"} {print}' | sed 's/\\n$//'; }
deny() { printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"%s"}}' "$(esc "$1")"; exit 0; }
ask()  { printf '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"ask","permissionDecisionReason":"%s"}}'  "$(esc "$1")"; exit 0; }

# ---- secret scan (push / pr create) ----
if [ $is_push -eq 1 ] || [ $is_pr -eq 1 ]; then
  up=$(git -C "$repo" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)
  if [ -n "$up" ]; then
    diff=$(git -C "$repo" diff "$up"...HEAD --no-color 2>/dev/null)
  else
    diff=$(git -C "$repo" log --branches --not --remotes -p --no-color 2>/dev/null)
  fi
  if [ -n "$diff" ]; then
    added=$(printf '%s\n' "$diff" | grep -E '^\+' 2>/dev/null)
    hit=$(printf '%s\n' "$added" | grep -EnI 'BEGIN [A-Z ]*PRIVATE KEY|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|xox[baprs]-[0-9A-Za-z-]{10,}|sk-ant-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}' 2>/dev/null | head -3)
    envf=$(printf '%s\n' "$diff" | grep -E '^\+\+\+ b/.*\.env([^.a-zA-Z]|$)' 2>/dev/null | grep -vi 'env.example' | head -3)
    if [ -n "$hit" ] || [ -n "$envf" ]; then
      msg="BLOCKED: the diff about to be pushed appears to contain a secret."
      [ -n "$hit" ]  && msg="$msg"$'\n'"Credential pattern(s):"$'\n'"$hit"
      [ -n "$envf" ] && msg="$msg"$'\n'"Env file(s) staged:"$'\n'"$envf"
      msg="$msg"$'\n'"If this is a false positive, push from a terminal outside the agent or disable the git_guard hook."
      deny "$msg"
    fi
  fi
fi

# ---- protected-branch confirmation (commit / push) ----
if [ $is_commit -eq 1 ] || [ $is_push -eq 1 ]; then
  br=$(git -C "$repo" symbolic-ref --short HEAD 2>/dev/null)
  case "$br" in
    main|master) ask "You're on protected branch '$br'. Branch first before committing/pushing, and confirm only if you really intend to change '$br' directly." ;;
  esac
fi
exit 0
