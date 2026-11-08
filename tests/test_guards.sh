#!/usr/bin/env bash
# Pipe 6 fixture hook payloads through both vendored guards and assert deny/allow.
# Usage: bash tests/test_guards.sh   (needs bash and git; python3 optional, node not used)
HERE="$(cd "$(dirname "$0")/.." && pwd)"
G="$HERE/vendor/hooks"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
cd "$T" && git init -q . && git config user.email t@example.invalid && git config user.name t && git checkout -q -b feature
echo base > a.txt && git add a.txt && git commit -qm base
# a commit holding a fake credential (built at runtime so no secret-shaped text sits in the repo)
printf 'key=%s%s\n' "AKIA" "IOSFODNN7EXAMPLE" > leak.txt && git add leak.txt && git commit -qm "add leak"
run() {  # name, command, expected(deny|allow)
  local json out dec
  json=$(printf '{"tool_name":"Bash","tool_input":{"command":"%s"}}' "$2")
  out="$(printf '%s' "$json" | bash "$G/stash_guard.sh" 2>/dev/null)$(printf '%s' "$json" | bash "$G/git_guard.sh" 2>/dev/null)"
  case "$out" in *'"permissionDecision":"deny"'*) dec=deny ;; *) dec=allow ;; esac
  if [ "$dec" = "$3" ]; then pass=$((pass+1)); echo "ok   $1 -> $dec"; else echo "FAIL $1 -> $dec (expected $3)"; fi
}
pass=0
run "stash push"  "git stash push -m wip"            deny
run "stash pop"   "git stash pop"                     deny
run "stash list"  "git stash list"                    allow
run "plain commit" "git commit -m ok"                 allow
run "push w/ key" "git push origin feature"           deny
run "harmless ls" "ls -la"                            allow
echo "$pass/6 passed"
[ "$pass" -eq 6 ]
