#!/usr/bin/env bash
# End-to-end walk-through of every devguard command in a throwaway directory.
# It builds a git origin with worktrees, starts disposable processes that look like
# `next dev`, and lets devguard find, stop and clean up after them.
# Safety: watch.roots limits devguard to this temp directory, so no other dev
# server on the machine is ever considered, and sweep only sees the temp repo.
# DEVGUARD_BIN runs the same walk-through against an installed devguard or a zipapp.
# Needs: bash, git, python3 (3.11+).
set -uo pipefail

src="$(cd "$(dirname "$0")/.." && pwd)/src"
work=$(mktemp -d "${TMPDIR:-/tmp}/devguard-demo-XXXXXX")
work=$(cd "$work" && pwd -P)
pids=()
cleanup() {
	for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null; done
	wait 2>/dev/null
	rm -rf "$work"
}
trap cleanup EXIT

export GIT_CONFIG_GLOBAL="$work/gitconfig" GIT_CONFIG_NOSYSTEM=1
printf '[user]\n\tname = Demo\n\temail = demo@example.invalid\n[init]\n\tdefaultBranch = main\n' >"$GIT_CONFIG_GLOBAL"
export DEVGUARD_CONFIG="$work/devguard.toml"
cat >"$DEVGUARD_CONFIG" <<EOF
state_dir = "$work/state"
[watch]
roots = ["$work"]
hard_gb = 0.03
soft_gb = 0.02
floor_gb = 0
grace_seconds = 3
fetch_interval_minutes = 0
metrics = true
[slot]
dir = "$work/slots"
[sweep]
min_idle_hours = 0
EOF

dg() { if [ -n "${DEVGUARD_BIN:-}" ]; then "$DEVGUARD_BIN" "$@"; else PYTHONPATH="$src" python3 -m devguard "$@"; fi; }
step() { printf '\n== %s\n' "$1"; }
failures=0
expect() { # expect <description> <actual> <expected>
	if [ "$2" = "$3" ]; then echo "  ok: $1"; else echo "  FAILED: $1 (got: $2, expected: $3)"; failures=$((failures + 1)); fi
}
contains() { # contains <description> <haystack> <needle>
	case "$2" in *"$3"*) echo "  ok: $1" ;; *) echo "  FAILED: $1 (missing: $3)"; failures=$((failures + 1)) ;; esac
}
alive() { kill -0 "$1" 2>/dev/null && [ "$(ps -o stat= -p "$1" 2>/dev/null | cut -c1)" != "Z" ] && echo yes || echo no; }
q() { git -C "$1" "${@:2}" >/dev/null 2>&1; }

step "set up an origin, a primary checkout and three worktrees"
git init -q --bare "$work/origin.git"
git clone -q "$work/origin.git" "$work/app" 2>/dev/null
printf 'node_modules\n.next/\n' >"$work/app/.gitignore"
echo hi >"$work/app/README.md"
q "$work/app" add . && q "$work/app" commit -m init && q "$work/app" push -u origin main
q "$work/app" remote set-head origin main
for name in done active dirty; do
	q "$work/app" worktree add -b "$name" "$work/wt-$name" origin/main
	echo "$name" >"$work/wt-$name/$name.txt"
	q "$work/wt-$name" add . && q "$work/wt-$name" commit -m "$name" && q "$work/wt-$name" push -u origin "$name"
	mkdir -p "$work/wt-$name/node_modules/pkg" "$work/wt-$name/apps/web/.next"
	head -c 300000 /dev/zero >"$work/wt-$name/node_modules/pkg/index.js"
	head -c 200000 /dev/zero >"$work/wt-$name/apps/web/.next/cache.bin"
done
q "$work/app" fetch origin && q "$work/app" merge --no-ff -m "merge done" origin/done && q "$work/app" push origin main
q "$work/app" fetch --prune origin
echo "notes" >"$work/wt-dirty/scratch.txt"
echo "  origin, app (main), wt-done (merged), wt-active (open), wt-dirty (open, uncommitted file)"

step "worktrees: one table for every worktree"
out=$(dg worktrees "$work/app")
echo "$out"
contains "wt-done is merged" "$(grep wt-done <<<"$out")" "merged"
contains "wt-active is open" "$(grep wt-active <<<"$out")" "open"
contains "wt-dirty has 1 dirty file" "$(grep wt-dirty <<<"$out" | awk '{print $3}')" "1"

step "port: a stable port per worktree"
p1=$(dg port -q "$work/wt-active")
p2=$(dg port -q "$work/wt-active")
echo "  wt-active -> $p1"
expect "same port every time" "$p1" "$p2"
expect "inside 4000-4999" "$([ "$p1" -ge 4000 ] && [ "$p1" -le 4999 ] && echo yes)" yes
expect "differs from wt-done" "$([ "$p1" != "$(dg port -q "$work/wt-done")" ] && echo yes)" yes
dg port --list "$work/app" | tail -1

step "slot: three jobs, one slot, so they run one after another"
for i in 1 2 3; do
	dg slot -n 1 -- sh -c "echo start >>'$work/slot.log'; sleep 0.4; echo end >>'$work/slot.log'" 2>/dev/null &
done
wait
expect "never overlapped" "$(tr '\n' ' ' <"$work/slot.log")" "start end start end start end "
dg slot -- sh -c 'exit 7'
expect "exit code passes through" "$?" 7

step "ps: start three fake dev servers"
for name in done active dirty; do
	mkdir -p "$work/wt-$name/node_modules/.bin"
	cat >"$work/wt-$name/node_modules/.bin/next" <<'PY'
import os, time
blob = bytearray(int(os.environ.get("ALLOC_MB", "1")) * 1024 * 1024)
while True:
    time.sleep(1)
PY
done
# A symlink named node to python3 gives the fake servers a real-looking command line.
ln -s "$(python3 -c 'import os, sys; print(os.path.realpath(sys.executable))')" "$work/node"
start() { # start <worktree> <port> <MB>: a process whose command line is `node .../next dev -p PORT`
	(cd "$work/wt-$1" && ALLOC_MB=$3 exec "$work/node" "$work/wt-$1/node_modules/.bin/next" dev -p "$2") &
	pids+=($!)
}
start done 4101 1
done_pid=${pids[-1]}
start active 4102 64
big_pid=${pids[-1]}
start dirty 4103 1
small_pid=${pids[-1]}
sleep 1.5
out=$(dg ps)
echo "$out"
contains "the merged worktree's server shows as merged" "$(grep 4101 <<<"$out")" "merged"
contains "the active server shows as open" "$(grep 4102 <<<"$out")" "open"
expect "only this demo's servers are listed (watch.roots)" "$(grep -c ' next dev ' <<<"$out")" 3

step "watch --once: dry run logs what it would stop, stops nothing"
dg watch --once
expect "merged server still running" "$(alive "$done_pid")" yes
expect "big server still running" "$(alive "$big_pid")" yes

step "watch --once --apply: stops the merged one and the one over the 0.03 GiB limit"
dg watch --once --apply
sleep 0.5
expect "merged server stopped" "$(alive "$done_pid")" no
expect "big server stopped" "$(alive "$big_pid")" no
expect "small open server untouched" "$(alive "$small_pid")" yes
contains "watch log has the stops" "$(dg watch --log)" "STOP port 4101"

step "report: what the watcher did, from metrics.jsonl and an imported dev-watch log"
expect "metrics.jsonl has one sample per pass" "$(wc -l <"$work/state/metrics.jsonl" | tr -d ' ')" 2
expect "metrics.jsonl is private" "$(stat -c %a "$work/state/metrics.jsonl" 2>/dev/null || stat -f %Lp "$work/state/metrics.jsonl")" 600
cat >"$work/dev-watch.log" <<'LOG'
2026-10-26 10:00:00 WARN port 4101 pid 11 3.2 GiB /srv/old/web: over 3 GiB, restart it soon
2026-10-26 10:05:00 STOP port 4101 pid 11 5.2 GiB /srv/old/web: over the 5 GiB limit
2026-10-26 10:08:00 STOP port 4101 pid 12 5.1 GiB /srv/old/web: over the 5 GiB limit
2026-10-26 10:12:00 STOP port 4101 pid 13 5.3 GiB /srv/old/web: over the 5 GiB limit
LOG
out=$(dg report --since 3650d --import-dev-watch "$work/dev-watch.log")
echo "$out"
contains "passes are counted" "$out" "Watch passes recorded: 2 (1 apply, 1 dry)"
contains "the merged stop is reported" "$out" "stopped: branch merged"
contains "the hard-limit stop is reported" "$out" "stopped: over hard_gb"
contains "imported events are marked" "$out" "imported from dev-watch log"
contains "the stop-restart loop is found" "$out" "3 stops in"
contains "a suggestion is made" "$out" "/srv/old/web hit the hard limit 3 times"
expect "--json is valid JSON" "$(dg report --json | python3 -c 'import json, sys; print(json.load(sys.stdin)["passes"])')" 2
kill "$small_pid" 2>/dev/null

step "sweep: dry run shows what would be deleted"
out=$(dg sweep "$work/app")
echo "$out"
contains "wt-done is swept" "$out" "sweep  $work/wt-done"
contains "wt-active is kept (not merged)" "$out" "wt-active [active]: branch active is not merged"
contains "wt-dirty is kept" "$(grep wt-dirty <<<"$out")" "keep"
expect "nothing deleted yet" "$(test -d "$work/wt-done/node_modules" && echo yes)" yes

step "sweep --apply: deletes build output in the finished worktree only"
dg sweep --apply "$work/app"
expect "wt-done node_modules gone" "$(test -e "$work/wt-done/node_modules" && echo present || echo gone)" gone
expect "wt-done .next gone" "$(test -e "$work/wt-done/apps/web/.next" && echo present || echo gone)" gone
expect "wt-done source kept" "$(cat "$work/wt-done/done.txt")" done
expect "wt-done still clean" "$(git -C "$work/wt-done" status --porcelain | wc -l | tr -d ' ')" 0
expect "wt-active untouched" "$(test -f "$work/wt-active/node_modules/pkg/index.js" && echo yes)" yes
expect "wt-dirty untouched" "$(test -f "$work/wt-dirty/node_modules/pkg/index.js" && echo yes)" yes
expect "primary untouched" "$(test -d "$work/app/.git" && echo yes)" yes
expect "receipt written" "$(ls "$work/state/sweep/"*.json 2>/dev/null | wc -l | tr -d ' ')" 1

echo
if [ "$failures" -eq 0 ]; then echo "All checks passed."; else echo "$failures check(s) FAILED."; fi
exit "$failures"
