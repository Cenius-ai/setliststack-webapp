# Using SetlistStack

Every example below is a real, copy-pasteable command with the output it
produces. `python3 setliststack.py` is abbreviated as `setliststack` where the
lines get long; run whichever form you like.

## Set the night's slot length

```
$ setliststack limit
Default limit: 5:00 (built-in)

$ setliststack limit 4:30
Default limit set to 4:30.

$ setliststack limit
Default limit: 4:30 (set)
```

The default is stamped onto every slot that `next` opens. `timer --limit MM:SS`
overrides it for one set only and rewrites that slot's limit.

## Build the sign-up list

```
$ setliststack add "Maya" --song "Blackbird" --key G --capo 2
Added Maya (position 1).
  Maya: Blackbird — key G, capo 2

$ setliststack add "Dev"
Added Dev (position 2).
```

Names are trimmed, must be 1–40 characters, and are unique regardless of case:

```
$ setliststack add "  maya "
Maya is already in the rotation. Next: setliststack roster
```

## See the rotation

```
$ setliststack roster
──────────────────────────────────────────────
PENDING
  up next: Maya
  Dev

PLAYED
  — nobody has played yet
──────────────────────────────────────────────
2 performers — 2 pending, 0 played
```

Once everyone has played the pending section says so and names the command that
starts a new round.

## Tag a song's key and capo

```
$ setliststack song "Dev" --song "Wagon Wheel" --key A --capo 0
Dev: Wagon Wheel — key A, capo 0

$ setliststack song "Dev"
Dev: Wagon Wheel — key A, capo 0

$ setliststack song "Dev" --key H
Unknown key "H". Valid keys: A A# B C C# D D# E F F# G G# or with m (Am). Next: setliststack song "Name" --key G
```

With no flags `song` prints what is already set; running it again with flags
overwrites the tags it is given.

## Call the next performer

```
$ setliststack next
──────────────────────────────────────────────
UP NEXT: Maya
Song: Blackbird
Key: G
Capo: 2
Limit: 4:30
──────────────────────────────────────────────
Slot open — run setliststack timer
```

Only one slot may be open at a time, so a second `next` is refused:

```
$ setliststack next
Maya still has an open slot. Finish it with setliststack timer or wipe it with setliststack reset --all.
```

The song title, key and capo are copied onto the slot, so editing a song later
never rewrites what you already announced or logged.

## Run the slot clock

```
$ setliststack timer
remaining 4:30  ·  Maya  ·  limit 4:30
remaining 4:29  ·  Maya  ·  limit 4:30
...
remaining 0:01  ·  Maya  ·  limit 4:30
remaining 0:00  ·  Maya  ·  limit 4:30
OVERTIME +0:01  ·  Maya  ·  limit 4:30
OVERTIME +0:02  ·  Maya  ·  limit 4:30
Slot closed: Maya — 4:32 of 4:30, over by 0:02.
```

On a terminal the countdown redraws one line in place. When the output is piped,
`TERM` is `dumb`, `NO_COLOR` is set or you pass `--no-color`, it prints one plain
line per second and emits no escape bytes. Either way, Ctrl-C (or the natural end
of the set) closes the slot and records `ended_at`, `elapsed_seconds`,
`overtime_seconds` and `status: done`. Repeated Ctrl-C never loses the record.

## Read the log

```
$ setliststack log
─────────────────────────────────────────────────────────────────
PERFORMER        SONG                    LIMIT  ELAPSED  OVERTIME
─────────────────────────────────────────────────────────────────
Maya             Blackbird                4:30     4:32     +0:02
Dev              Wagon Wheel              4:30     3:40         —
─────────────────────────────────────────────────────────────────
2 finished slots — 1 ran over the limit.

$ setliststack log --last 1
```

Long song titles are truncated with an ellipsis so the table stays inside 80
columns. With no finished slots the log tells you how to create the first one.

## Remove someone who dropped out

```
$ setliststack remove "Ana"
Removed Ana from the rotation.
```

Finished slots are history and stay in the file, even for a performer you have
removed.

## Start a new round, or recover

```
$ setliststack reset --played
Round reset. 4 performers pending, 2 finished slots kept.

$ setliststack reset
Nothing to reset. Use setliststack reset --played or setliststack reset --all.
```

If the state file is damaged, every command says the same thing:

```
$ setliststack roster
State file is not valid JSON. Run setliststack reset --all to start fresh.

$ setliststack reset --all
State reset. All performers and history cleared.
```

`reset --all` does not read the old file at all, so it works even when the file
holds garbage.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success (including an empty log and a closed slot) |
| `1` | State-file problem: corrupt, unreadable, wrong version, or not writable |
| `2` | A host mistake: bad name, unknown key, bad capo, bad limit, nothing to do |
| `130` | Ctrl-C outside the countdown |

Success output goes to stdout, every failure is exactly one line on stderr
ending in the command that fixes it.
