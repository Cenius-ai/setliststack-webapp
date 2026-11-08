# Installing SetlistStack

SetlistStack is a single Python file that uses only the standard library, so
"installing" it means checking the interpreter and putting the file where you
want it. There is no package to fetch, no database to create and no service to
start.

## 1. Prerequisites

| Requirement | Why | Check |
| --- | --- | --- |
| Python 3.9 or newer | The tool is plain Python | `python3 --version` |

Nothing else is needed: no pip packages, no compiler, no system libraries, no
network access.

## 2. Install

From the unpacked folder:

```bash
bash install.sh
```

The script:

1. confirms `python3` exists and is at least 3.9,
2. confirms `setliststack.py` imports (a complete, self-contained file),
3. prints the next command to run and exits `0`.

It runs as your normal user, installs no system packages, creates no files, and
never starts anything long-running. Re-running it is harmless.

The dependency manifest is `requirements.txt`, which intentionally lists no
third-party packages.

## 3. Where the state file goes

The tool writes `./setliststack_state.json` in whatever directory you run it
from — a fresh one is created on first use, with `0600` permissions. Use the
global flag to keep a file per gig:

```bash
python3 setliststack.py --state ~/gigs/thursday.json roster
```

## 4. Run it

```bash
python3 setliststack.py --help
python3 setliststack.py add "Maya" --song "Blackbird" --key G --capo 2
python3 setliststack.py roster
python3 setliststack.py next
python3 setliststack.py timer
```

Nothing needs to be configured before the first command: there are no
environment variables, no secrets and no defaults to accept.

## 5. Verify the install

```bash
python3 test_setliststack.py     # prints OK (N tests)
bash demo.sh                     # runs a whole night in a temp directory
```

## 6. Uninstall

Delete the folder and, if you want a clean slate, the state file you were using
(`setliststack_state.json` or whatever path you passed to `--state`).
