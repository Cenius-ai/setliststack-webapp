#!/usr/bin/env bash
# SetlistStack end-to-end demo.
#
# Non-interactive and safe to run as often as you like: it works in a throwaway
# directory so your own ./setliststack_state.json is never touched.
#
#   bash demo.sh
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
TOOL="$PWD/setliststack.py"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/setliststack-demo.XXXXXX")"
cleanup() { rm -rf "$WORK"; }
trap cleanup EXIT INT TERM
cd "$WORK"

# Run one command, echoing it the way a host would type it. Failures are shown
# with their exit code instead of aborting the demo: the refusal messages are
# part of the product.
sls() {
  local rendered="" arg
  for arg in "$@"; do
    case "$arg" in
      *" "*) rendered="$rendered \"$arg\"" ;;
      *) rendered="$rendered $arg" ;;
    esac
  done
  printf '\n$ setliststack%s\n' "$rendered"
  set +e
  "$PY" "$TOOL" "$@"
  local code=$?
  set -e
  if [ "$code" -ne 0 ]; then
    printf '[exit %s]\n' "$code"
  fi
}

# Run the countdown for N seconds, then send Ctrl-C exactly like a host would.
run_timer_for() {
  local seconds="$1"
  shift
  "$PY" -c '
import signal, subprocess, sys, time
seconds = float(sys.argv[1])
proc = subprocess.Popen(sys.argv[2:])
time.sleep(seconds)
proc.send_signal(signal.SIGINT)
sys.exit(proc.wait())
' "$seconds" "$PY" "$TOOL" timer "$@"
}

section() { printf '\n\n===== %s =====\n' "$1"; }

printf 'SetlistStack %s - open-mic rotation demo\n' "$("$PY" "$TOOL" --version | awk '{print $2}')"
printf 'Working in a temporary directory: %s\n' "$WORK"

section "1. Set the night's limit and build the sign-up list"
sls limit 4:30
sls add "Maya" --song "Blackbird" --key G --capo 2
sls add "Dev" --song "Wagon Wheel" --key A
sls add "Ana" --song "Landslide" --key Em --capo 3
sls add "Tomas" --song "Fast Car" --key C#m --capo 1
sls add "  maya "
sls roster

section "2. Call the first performer - the up-next card and one open slot"
sls next
sls next

section "3. Run the slot clock - 0:10 for this set, then fall into overtime"
printf '\n$ setliststack timer --limit 0:10     # 13 seconds, then Ctrl-C\n'
run_timer_for 13 --limit 0:10

section "4. What the night looks like so far"
sls log

section "5. Tags are snapshotted - changing a song later cannot rewrite history"
sls song "Maya" --song "Blackbird (Delta version)" --key D --capo 0
sls log

section "6. Call the next performer, then count down with piped output"
printf '\n(no terminal on this stream, so the countdown falls back to one plain line per second)\n'
sls next
printf '\n$ setliststack timer | cat     # 5 seconds, then Ctrl-C\n'
run_timer_for 5 | cat
printf '\n'
sls log --last 2

section "7. Someone drops out, and a new round starts"
sls remove "Ana"
sls reset --played
sls roster

section "8. Recovering a damaged state file"
printf 'not json\n' > setliststack_state.json
sls roster
sls reset --all
sls roster

section "9. Every command documents itself"
printf '\n$ setliststack --help\n'
"$PY" "$TOOL" --help

printf '\n\nYour own state file was never touched: the demo ran inside a throwaway\n'
printf 'directory that is deleted when this script exits.\n'

printf '\nReady. Start your own night with: python3 setliststack.py add "Name"\n'
