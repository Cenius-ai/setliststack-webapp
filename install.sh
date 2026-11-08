#!/usr/bin/env bash
# SetlistStack install step.
#
# Runs as your normal user: it checks the interpreter and the file, installs no
# system packages, creates no state and never starts anything long-running.
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (Python 3.9 or newer). Install it, then re-run this script." >&2
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "Python 3.9 or newer is required; found $(python3 --version 2>&1)." >&2
  exit 1
fi

# Standard library only: requirements.txt intentionally lists no packages, so
# there is nothing to download. The import check is what proves the single file
# is complete.
python3 -c 'import setliststack'

echo "SetlistStack is ready (Python $(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])'))."
echo "Next: python3 setliststack.py --help"
echo "Then: python3 setliststack.py add \"Name\" --song \"Title\" --key G --capo 0"
