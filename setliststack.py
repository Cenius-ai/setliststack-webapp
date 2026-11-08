#!/usr/bin/env python3
"""SetlistStack - open-mic set order rotation, key/capo tags, slot countdown.

One file, standard library only. Start with:

    python3 setliststack.py --help

The THEME block below is this tool's design source of truth: a single accent
(oklch(0.58 0.12 55) == #b0652a), monospace aligned columns and box-drawing
separators. Colour is never the only signal - every painted status is also
spelled out in words, and colour is dropped whenever output is not a terminal,
TERM is dumb, or NO_COLOR is set.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

# --------------------------------------------------------------------------- #
# THEME - the committed design direction (accent #b0652a / oklch(0.58 0.12 55))
# --------------------------------------------------------------------------- #

TOOL = "setliststack"
VERSION = "1.0"

ACCENT = "38;2;176;101;42"  # #b0652a as a 24-bit SGR foreground
BOLD = "1"
DIM = "2"

CARD_WIDTH = 46
LOG_WIDTH = 65

# --------------------------------------------------------------------------- #
# Domain constants
# --------------------------------------------------------------------------- #

STATE_VERSION = 1
DEFAULT_STATE_PATH = "./setliststack_state.json"

DEFAULT_LIMIT_SECONDS = 300  # 5:00 built-in default
MIN_LIMIT_SECONDS = 10  # 0:10
MAX_LIMIT_SECONDS = 3600  # 60:00

NAME_MAX_LENGTH = 40
TICK_SECONDS = 0.05  # countdown poll granularity; the display redraws each second

NATURAL_KEYS = ("A", "A#", "B", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#")
VALID_KEYS = NATURAL_KEYS + tuple(key + "m" for key in NATURAL_KEYS)
KEY_LIST = " ".join(NATURAL_KEYS)

EXAMPLES = """examples:
  setliststack add "Maya" --song "Blackbird" --key G --capo 2
  setliststack add "Dev" --song "Wagon Wheel" --key A
  setliststack roster
  setliststack next
  setliststack timer
  setliststack log --last 5
  setliststack reset --played

The state file lives at ./setliststack_state.json; pass --state PATH to use
another one. If that file is ever damaged, recover with:

  setliststack reset --all
"""

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class UsageError(Exception):
    """A host mistake: printed as one stderr line, exit code 2."""


class StateError(Exception):
    """A state-file problem: printed as one stderr line, exit code 1."""


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

_USE_COLOR = False


def set_color(enabled: bool) -> None:
    global _USE_COLOR
    _USE_COLOR = bool(enabled)


def paint(text: str, *codes: str) -> str:
    """Wrap text in ANSI SGR codes, or return it untouched when colour is off."""
    if not _USE_COLOR or not codes:
        return text
    return "\x1b[" + ";".join(codes) + "m" + text + "\x1b[0m"


def supports_ansi(stream: Any) -> bool:
    """True only when cursor and colour control are safe on this stream."""
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM", "").strip().lower() in ("", "dumb"):
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def write_line(stream: Any, text: str) -> None:
    """Write one line, degrading unencodable characters instead of raising."""
    try:
        stream.write(text + "\n")
        stream.flush()
    except UnicodeEncodeError:
        stream.write(text.encode("ascii", "replace").decode("ascii") + "\n")
        stream.flush()


def out(text: str) -> None:
    write_line(sys.stdout, text)


def err(text: str) -> None:
    write_line(sys.stderr, text)


def force_utf8(stream: Any) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, TypeError, ValueError):
        # An unusual or captured stream keeps its own encoding; write_line
        # still degrades unencodable characters rather than raising.
        pass


def rule(width: int = CARD_WIDTH) -> str:
    return paint("\u2500" * width, DIM)


# --------------------------------------------------------------------------- #
# Small formatting helpers
# --------------------------------------------------------------------------- #


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def fmt_mmss(seconds: int) -> str:
    seconds = int(seconds)
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    return "{}{}:{:02d}".format(sign, seconds // 60, seconds % 60)


def truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return "\u2026"[:width]
    return text[: width - 1] + "\u2026"


def emdash(value: Optional[str], fallback: str = "\u2014") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback

# --------------------------------------------------------------------------- #
# Parsers (shared by the subcommands and exercised directly by the test suite)
# --------------------------------------------------------------------------- #


def parse_key(raw: str) -> str:
    text = str(raw).strip()
    minor = text.endswith("m") or text.endswith("M")
    root = (text[:-1] if minor else text).strip().upper()
    normalised = root + "m" if minor else root
    if root not in NATURAL_KEYS or normalised not in VALID_KEYS:
        raise UsageError(
            'Unknown key "{0}". Valid keys: {1} or with m (Am). '
            'Next: setliststack song "Name" --key G'.format(text, KEY_LIST)
        )
    return normalised


def parse_capo(raw: str) -> int:
    text = str(raw).strip()
    message = (
        "Capo must be a whole number from 0 to 12. "
        'Next: setliststack song "Name" --capo 0'
    )
    if not re.fullmatch(r"\d{1,2}", text):
        raise UsageError(message)
    value = int(text)
    if value > 12:
        raise UsageError(message)
    return value


def parse_limit(raw: str) -> int:
    text = str(raw).strip()
    invalid = UsageError(
        "Limit must be mm:ss between 0:10 and 60:00 (for example 4:30). "
        "Next: setliststack limit 5:00"
    )
    if not re.fullmatch(r"\d{1,3}:\d{2}", text):
        raise invalid
    minutes, seconds = text.split(":")
    if int(seconds) > 59:
        raise invalid
    total = int(minutes) * 60 + int(seconds)
    if total < MIN_LIMIT_SECONDS or total > MAX_LIMIT_SECONDS:
        raise invalid
    return total


def parse_last(raw: str) -> int:
    text = str(raw).strip()
    if not re.fullmatch(r"\d{1,4}", text) or int(text) < 1:
        raise UsageError(
            "--last needs a whole number of 1 or more. Next: setliststack log --last 5"
        )
    return int(text)


def require_name(raw: str) -> str:
    name = " ".join(str(raw).split())
    if not name:
        raise UsageError(
            'Performer name cannot be empty. Add a name: setliststack add "Name"'
        )
    if any(ord(char) < 32 for char in name):
        raise UsageError(
            "Performer names cannot contain control characters. "
            'Next: setliststack add "Name"'
        )
    if len(name) > NAME_MAX_LENGTH:
        raise UsageError(
            "Performer name is too long ({0} characters). Names can be at most {1} "
            'characters. Next: setliststack add "Name"'.format(
                len(name), NAME_MAX_LENGTH
            )
        )
    return name


def require_song_title(raw: str) -> str:
    title = " ".join(str(raw).split())
    if not title:
        raise UsageError(
            'Song title cannot be empty. Next: setliststack song "Name" --song "Title"'
        )
    return title


# --------------------------------------------------------------------------- #
# State file: version tag, atomic writes, corrupt-state recovery
# --------------------------------------------------------------------------- #


def fresh_state() -> dict:
    now = utcnow_iso()
    return {
        "version": STATE_VERSION,
        "performers": [],
        "slots": [],
        "settings": {
            "default_limit_seconds": DEFAULT_LIMIT_SECONDS,
            "limit_is_user_set": False,
        },
        "created_at": now,
        "updated_at": now,
    }


def write_atomic(path: str, text: str) -> None:
    """Write text to path through a same-directory temp file plus os.replace."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle_fd, temp_path = tempfile.mkstemp(
        dir=directory, prefix=".setliststack-", suffix=".tmp"
    )
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def save_state(state: dict, path: str) -> None:
    state["updated_at"] = utcnow_iso()
    payload = json.dumps(state, indent=2, ensure_ascii=False) + "\n"
    try:
        write_atomic(path, payload)
    except OSError as exc:
        directory = os.path.dirname(os.path.abspath(path)) or "."
        raise StateError(
            "Could not save the state file ({0}). Check that {1} exists and is "
            "writable, then run setliststack roster".format(
                exc.strerror or exc, directory
            )
        )


def corrupt_state(reason: str) -> StateError:
    return StateError(
        "State file is corrupt ({0}). Run setliststack reset --all to start "
        "fresh.".format(reason)
    )


def validate_state(data: Any) -> dict:
    if not isinstance(data, dict):
        raise corrupt_state("expected a JSON object")
    if data.get("version") != STATE_VERSION:
        raise StateError(
            "State file version {0!r} is not supported (this build reads version "
            "{1}). Run setliststack reset --all to start fresh.".format(
                data.get("version"), STATE_VERSION
            )
        )
    performers = data.get("performers")
    if not isinstance(performers, list):
        raise corrupt_state("performers list is missing")
    for performer in performers:
        if not isinstance(performer, dict):
            raise corrupt_state("a performer entry is not an object")
        if not isinstance(performer.get("name"), str) or not performer["name"].strip():
            raise corrupt_state("a performer entry has no name")
        performer.setdefault("song_title", None)
        performer.setdefault("song_key", None)
        performer.setdefault("capo", 0)
        performer.setdefault("played", False)
        performer.setdefault("added_at", utcnow_iso())
    slots = data.get("slots")
    if not isinstance(slots, list):
        raise corrupt_state("slots list is missing")
    active_count = 0
    for slot in slots:
        if not isinstance(slot, dict):
            raise corrupt_state("a slot entry is not an object")
        if slot.get("status") not in ("active", "done"):
            raise corrupt_state("a slot entry has an unknown status")
        if not isinstance(slot.get("performer_name"), str):
            raise corrupt_state("a slot entry has no performer name")
        if slot["status"] == "active":
            active_count += 1
    if active_count > 1:
        raise corrupt_state("{0} slots are open at once".format(active_count))
    settings = data.get("settings")
    if not isinstance(settings, dict):
        raise corrupt_state("settings block is missing")
    if not isinstance(settings.get("default_limit_seconds"), int):
        raise corrupt_state("settings.default_limit_seconds is missing")
    settings.setdefault("limit_is_user_set", True)
    data.setdefault("created_at", utcnow_iso())
    data.setdefault("updated_at", utcnow_iso())
    return data


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        state = fresh_state()
        save_state(state, path)
        return state
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read()
    except OSError as exc:
        raise StateError(
            "State file cannot be read ({0}). Run setliststack reset --all to "
            "start fresh.".format(exc.strerror or exc)
        )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise StateError(
            "State file is not valid JSON. Run setliststack reset --all to start "
            "fresh."
        )
    return validate_state(data)

# --------------------------------------------------------------------------- #
# Domain lookups
# --------------------------------------------------------------------------- #


def find_performer(state: dict, name: str) -> Optional[dict]:
    wanted = name.strip().casefold()
    for performer in state["performers"]:
        if performer["name"].strip().casefold() == wanted:
            return performer
    return None


def active_slot(state: dict) -> Optional[dict]:
    for slot in state["slots"]:
        if slot.get("status") == "active":
            return slot
    return None


def next_slot_id(state: dict) -> int:
    ids = [
        slot["id"] for slot in state["slots"] if isinstance(slot.get("id"), int)
    ]
    return (max(ids) if ids else 0) + 1


def performer_has_tags(performer: dict) -> bool:
    return bool(
        performer.get("song_title")
        or performer.get("song_key")
        or int(performer.get("capo") or 0) != 0
    )


def format_tags(
    name: str, title: Optional[str], key: Optional[str], capo: int
) -> str:
    return "{0}: {1} \u2014 key {2}, capo {3}".format(
        name, emdash(title), emdash(key), capo
    )


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #


def render_card(slot: dict) -> str:
    """The signature output: the up-next announcement card."""
    lines = [rule()]
    lines.append(paint("UP NEXT: " + slot["performer_name"], ACCENT, BOLD))
    lines.append("Song: " + emdash(slot.get("song_title")))
    lines.append("Key: " + emdash(slot.get("song_key")))
    lines.append("Capo: " + str(int(slot.get("capo") or 0)))
    lines.append("Limit: " + fmt_mmss(int(slot.get("limit_seconds") or 0)))
    lines.append(rule())
    return "\n".join(lines)


def render_roster(state: dict) -> str:
    performers = state["performers"]
    if not performers:
        return 'No performers yet. Add one with: setliststack add "Name"'

    pending = [item for item in performers if not item.get("played")]
    played = [item for item in performers if item.get("played")]

    lines = [rule(), paint("PENDING", BOLD)]
    if pending:
        lines.append("  " + paint("up next: " + pending[0]["name"], ACCENT, BOLD))
        for performer in pending[1:]:
            lines.append("  " + performer["name"])
    else:
        lines.append(
            "  Everyone has played \u2014 start a new round with: "
            "setliststack reset --played"
        )
    lines.append("")
    lines.append(paint("PLAYED", BOLD))
    if played:
        for performer in played:
            lines.append("  " + performer["name"])
    else:
        lines.append("  \u2014 nobody has played yet")
    lines.append(rule())

    total = len(performers)
    noun = "performer" if total == 1 else "performers"
    lines.append(
        "{0} {1} \u2014 {2} pending, {3} played".format(
            total, noun, len(pending), len(played)
        )
    )
    return "\n".join(lines)


def render_log(slots: Sequence[dict]) -> str:
    header = "{0:<16} {1:<22} {2:>6} {3:>8} {4:>9}".format(
        "PERFORMER", "SONG", "LIMIT", "ELAPSED", "OVERTIME"
    )
    lines = [rule(LOG_WIDTH), paint(header, BOLD), rule(LOG_WIDTH)]
    over_count = 0
    for slot in slots:
        overtime = int(slot.get("overtime_seconds") or 0)
        if overtime > 0:
            over_count += 1
            overtime_cell = paint("{0:>9}".format("+" + fmt_mmss(overtime)), ACCENT)
        else:
            overtime_cell = "{0:>9}".format("\u2014")
        elapsed = slot.get("elapsed_seconds")
        elapsed_cell = fmt_mmss(int(elapsed)) if elapsed is not None else "\u2014"
        lines.append(
            "{0:<16} {1:<22} {2:>6} {3:>8} {4}".format(
                truncate(slot["performer_name"], 16),
                truncate(emdash(slot.get("song_title")), 22),
                fmt_mmss(int(slot.get("limit_seconds") or 0)),
                elapsed_cell,
                overtime_cell,
            )
        )
    lines.append(rule(LOG_WIDTH))
    total = len(slots)
    noun = "slot" if total == 1 else "slots"
    if over_count:
        lines.append(
            "{0} finished {1} \u2014 {2} ran over the limit.".format(
                total, noun, over_count
            )
        )
    else:
        lines.append(
            "{0} finished {1} \u2014 none ran over the limit.".format(total, noun)
        )
    return "\n".join(lines)

# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #


def cmd_roster(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    out(render_roster(state))
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    name = require_name(args.name)
    existing = find_performer(state, name)
    if existing is not None:
        raise UsageError(
            "{0} is already in the rotation. Next: setliststack roster".format(
                existing["name"]
            )
        )
    title = require_song_title(args.song) if args.song is not None else None
    key = parse_key(args.key) if args.key is not None else None
    capo = parse_capo(args.capo) if args.capo is not None else 0

    state["performers"].append(
        {
            "name": name,
            "song_title": title,
            "song_key": key,
            "capo": capo,
            "played": False,
            "added_at": utcnow_iso(),
        }
    )
    save_state(state, args.state)
    out("Added {0} (position {1}).".format(name, len(state["performers"])))
    if title is not None or key is not None:
        out("  " + format_tags(name, title, key, capo))
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    name = require_name(args.name)
    target = find_performer(state, name)
    if target is None:
        raise UsageError(
            "No performer named {0}. Run setliststack roster to see the "
            "rotation.".format(name)
        )
    removed_name = target["name"]
    done_slots = [
        slot
        for slot in state["slots"]
        if slot.get("status") == "done"
        and slot["performer_name"].casefold() == removed_name.casefold()
    ]
    state["performers"].remove(target)
    save_state(state, args.state)
    out("Removed {0} from the rotation.".format(removed_name))
    if done_slots:
        out(
            "  {0} finished {1} for {2} kept in the log.".format(
                len(done_slots),
                "slot" if len(done_slots) == 1 else "slots",
                removed_name,
            )
        )
    return 0


def cmd_song(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    name = require_name(args.name)
    target = find_performer(state, name)
    if target is None:
        raise UsageError(
            "No performer named {0}. Run setliststack roster to see the "
            "rotation.".format(name)
        )

    changed = False
    if args.song is not None:
        target["song_title"] = require_song_title(args.song)
        changed = True
    if args.key is not None:
        target["song_key"] = parse_key(args.key)
        changed = True
    if args.capo is not None:
        target["capo"] = parse_capo(args.capo)
        changed = True

    display_name = target["name"]
    if changed:
        save_state(state, args.state)
        out(
            format_tags(
                display_name,
                target.get("song_title"),
                target.get("song_key"),
                int(target.get("capo") or 0),
            )
        )
        return 0

    if performer_has_tags(target):
        out(
            format_tags(
                display_name,
                target.get("song_title"),
                target.get("song_key"),
                int(target.get("capo") or 0),
            )
        )
        return 0

    out(
        '{0} has no song set yet. Add one with: setliststack song "{0}" '
        '--song "Title" --key G --capo 0'.format(display_name)
    )
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    open_slot = active_slot(state)
    if open_slot is not None:
        raise UsageError(
            "{0} still has an open slot. Finish it with setliststack timer or "
            "wipe it with setliststack reset --all.".format(open_slot["performer_name"])
        )
    performers = state["performers"]
    if not performers:
        raise UsageError('No performers yet. Add one with: setliststack add "Name"')
    pending = [item for item in performers if not item.get("played")]
    if not pending:
        raise UsageError(
            "Everyone has played. Start a new round with: setliststack reset --played"
        )

    performer = pending[0]
    slot = {
        "id": next_slot_id(state),
        "performer_name": performer["name"],
        "song_title": performer.get("song_title"),
        "song_key": performer.get("song_key"),
        "capo": int(performer.get("capo") or 0),
        "limit_seconds": int(state["settings"]["default_limit_seconds"]),
        "started_at": utcnow_iso(),
        "ended_at": None,
        "elapsed_seconds": None,
        "overtime_seconds": None,
        "status": "active",
    }
    performer["played"] = True
    state["slots"].append(slot)
    save_state(state, args.state)

    out(render_card(slot))
    out("Slot open \u2014 run setliststack timer")
    return 0


def cmd_limit(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    settings = state["settings"]
    if args.value is None:
        origin = "built-in" if not settings.get("limit_is_user_set") else "set"
        out(
            "Default limit: {0} ({1})".format(
                fmt_mmss(int(settings["default_limit_seconds"])), origin
            )
        )
        return 0
    seconds = parse_limit(args.value)
    settings["default_limit_seconds"] = seconds
    settings["limit_is_user_set"] = True
    save_state(state, args.state)
    out("Default limit set to {0}.".format(fmt_mmss(seconds)))
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    finished = [slot for slot in state["slots"] if slot.get("status") == "done"]
    if args.last is not None:
        count = parse_last(args.last)
        finished = finished[-count:]
    if not finished:
        out(
            "No finished slots yet. Run setliststack next then setliststack timer "
            "to record one."
        )
        return 0
    out(render_log(finished))
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    if args.reset_all:
        # Recovery path: the existing bytes are never parsed or validated.
        save_state(fresh_state(), args.state)
        out("State reset. All performers and history cleared.")
        return 0
    if args.played:
        state = load_state(args.state)
        cleared = 0
        for performer in state["performers"]:
            if performer.get("played"):
                performer["played"] = False
                cleared += 1
        save_state(state, args.state)
        finished = [slot for slot in state["slots"] if slot.get("status") == "done"]
        pending_count = len(state["performers"])
        out(
            "Round reset. {0} {1} pending, {2} finished {3} kept.".format(
                pending_count,
                "performer" if pending_count == 1 else "performers",
                len(finished),
                "slot" if len(finished) == 1 else "slots",
            )
        )
        still_open = active_slot(state)
        if still_open is not None:
            out(
                "{0} still has an open slot. Finish it with setliststack "
                "timer.".format(still_open["performer_name"])
            )
        return 0
    raise UsageError(
        "Nothing to reset. Use setliststack reset --played or setliststack reset --all."
    )

# --------------------------------------------------------------------------- #
# Countdown
# --------------------------------------------------------------------------- #


def raw_write(text: str) -> None:
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except UnicodeEncodeError:
        sys.stdout.write(text.encode("ascii", "replace").decode("ascii"))
        sys.stdout.flush()


def render_tick(tick: int, limit_seconds: int, performer: str) -> str:
    """One countdown frame. No ANSI bytes are added by this function itself."""
    suffix = "  \u00b7  {0}  \u00b7  limit {1}".format(
        performer, fmt_mmss(limit_seconds)
    )
    remaining = limit_seconds - tick
    if remaining >= 0:
        return paint("remaining " + fmt_mmss(remaining), BOLD) + suffix
    return paint("OVERTIME +" + fmt_mmss(-remaining), ACCENT, BOLD) + suffix


def emit_tick(fancy: bool, line: str) -> None:
    if fancy:
        raw_write("\r\x1b[2K" + line)  # redraw the same line, cursor home + erase
    else:
        raw_write(line + "\n")  # plain line-per-tick, zero escape bytes


def finalize_slot(state: dict, path: str, slot: dict, elapsed_seconds: int) -> None:
    slot["ended_at"] = utcnow_iso()
    slot["elapsed_seconds"] = elapsed_seconds
    slot["overtime_seconds"] = max(0, elapsed_seconds - int(slot["limit_seconds"]))
    slot["status"] = "done"
    save_state(state, path)


def run_countdown(
    state: dict, path: str, slot: dict, limit_seconds: int, fancy: bool
) -> None:
    """Count the open slot down; time.monotonic keeps it free of clock drift.

    SIGINT is caught rather than raised: a host who mashes Ctrl-C while the
    record is being written must not lose the slot, so the handler only sets a
    flag and every later Ctrl-C is ignored until the save has finished.
    """
    performer = slot["performer_name"]
    stop = {"requested": False}

    def request_stop(_signum: int, _frame: Any) -> None:
        stop["requested"] = True

    previous = None
    try:
        previous = signal.signal(signal.SIGINT, request_stop)
    except (OSError, ValueError):
        previous = None

    start = time.monotonic()
    shown = -1
    try:
        try:
            while not stop["requested"]:
                tick = int(time.monotonic() - start)
                if tick != shown:
                    shown = tick
                    emit_tick(fancy, render_tick(tick, limit_seconds, performer))
                time.sleep(TICK_SECONDS)
        except KeyboardInterrupt:  # only if our handler could not be installed
            pass

        elapsed_seconds = int(time.monotonic() - start + 0.5)
        if previous is not None:
            try:
                signal.signal(signal.SIGINT, signal.SIG_IGN)
            except (OSError, ValueError):
                pass
        if fancy:
            raw_write("\n")
        finalize_slot(state, path, slot, elapsed_seconds)

        overtime = int(slot["overtime_seconds"] or 0)
        summary = "Slot closed: {0} \u2014 {1} of {2}".format(
            performer, fmt_mmss(elapsed_seconds), fmt_mmss(limit_seconds)
        )
        if overtime:
            summary += ", over by {0}.".format(fmt_mmss(overtime))
        else:
            summary += ", within the limit."
        out(summary)
    finally:
        if previous is not None:
            try:
                signal.signal(signal.SIGINT, previous)
            except (OSError, ValueError):
                pass


def cmd_timer(args: argparse.Namespace) -> int:
    state = load_state(args.state)
    slot = active_slot(state)
    if slot is None:
        raise UsageError(
            "No open slot. Run setliststack next to call the next performer."
        )
    if args.limit is not None:
        seconds = parse_limit(args.limit)
        if int(slot.get("limit_seconds") or 0) != seconds:
            slot["limit_seconds"] = seconds
            save_state(state, args.state)
    limit_seconds = int(slot["limit_seconds"])
    fancy = supports_ansi(sys.stdout) and not getattr(args, "no_color", False)
    run_countdown(state, args.state, slot, limit_seconds, fancy)
    return 0


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #


class ToolParser(argparse.ArgumentParser):
    """argparse with the house error contract: one line, exit 2, next action."""

    def error(self, message: str) -> None:  # type: ignore[override]
        self.exit(2, "{0}: {1}. Next: {0} --help\n".format(self.prog, message))


def add_command(
    sub: Any, name: str, help_text: str, description: str, example: str
) -> argparse.ArgumentParser:
    return sub.add_parser(
        name,
        help=help_text,
        description=description,
        epilog="example:\n  " + example,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


KEY_HELP = "song key: A A# B C C# D D# E F F# G G#, or Am etc."


def add_command(
    sub: Any, name: str, help_text: str, description: str, example: str
) -> argparse.ArgumentParser:
    return sub.add_parser(
        name,
        help=help_text,
        description=description,
        epilog="example:\n  " + example,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = ToolParser(
        prog=TOOL,
        description=(
            "Run an open-mic set order rotation: build the roster, call the next "
            "performer with an up-next card, tag each song's key and capo, and "
            "count each slot down against its time limit."
        ),
        epilog=EXAMPLES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%s %s" % (TOOL, VERSION),
        help="print the version and exit",
    )
    parser.add_argument(
        "--state",
        default=DEFAULT_STATE_PATH,
        metavar="PATH",
        help="state file to use (default: %(default)s)",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="never emit ANSI colour or cursor control"
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>", title="commands")

    roster = add_command(
        sub,
        "roster",
        "show who is pending, who is played and who is up next",
        "List the rotation in sign-up order, split into pending and played, with "
        "the next performer marked up next.",
        "setliststack roster",
    )
    roster.set_defaults(handler=cmd_roster)

    add = add_command(
        sub,
        "add",
        "add a performer to the end of the rotation",
        "Add a performer. The name is trimmed, must be 1-40 characters and must "
        "not already be in the rotation. Song tags can be set now or later with "
        "the song command.",
        'setliststack add "Maya" --song "Blackbird" --key G --capo 2',
    )
    add.add_argument("name", metavar="NAME", help="performer name (1-40 characters)")
    add.add_argument("--song", metavar="TITLE", help="song title")
    add.add_argument("--key", metavar="KEY", help=KEY_HELP)
    add.add_argument("--capo", metavar="N", help="capo position 0-12")
    add.set_defaults(handler=cmd_add)

    remove = add_command(
        sub,
        "remove",
        "remove a performer from the rotation",
        "Remove a performer from the rotation. Finished slot history is kept: "
        "the log still shows what they played.",
        'setliststack remove "Maya"',
    )
    remove.add_argument("name", metavar="NAME", help="performer to remove")
    remove.set_defaults(handler=cmd_remove)

    song = add_command(
        sub,
        "song",
        "set or show a performer's song title, key and capo",
        "With flags, write the song tags; with no flags, print the tags that are "
        "already set.",
        'setliststack song "Maya" --song "Blackbird" --key G --capo 2',
    )
    song.add_argument("name", metavar="NAME", help="performer name")
    song.add_argument("--song", metavar="TITLE", help="song title")
    song.add_argument("--key", metavar="KEY", help=KEY_HELP)
    song.add_argument("--capo", metavar="N", help="capo position 0-12")
    song.set_defaults(handler=cmd_song)

    nxt = add_command(
        sub,
        "next",
        "call the next performer and open their slot",
        "Call the next unplayed performer, print the up-next card and open "
        "exactly one slot for them. Only one slot may be open at a time.",
        "setliststack next",
    )
    nxt.set_defaults(handler=cmd_next)

    timer = add_command(
        sub,
        "timer",
        "count the open slot down against its time limit",
        "Run the countdown for the performer who was just called. It flips to "
        "OVERTIME once the limit passes; Ctrl-C closes the slot and records how "
        "it ran. Without --limit the slot's own limit is used.",
        "setliststack timer --limit 4:30",
    )
    timer.add_argument(
        "--limit", metavar="MM:SS", help="override this slot's limit (0:10 to 60:00)"
    )
    timer.set_defaults(handler=cmd_timer)

    limit = add_command(
        sub,
        "limit",
        "show or set the default slot limit",
        "With no argument, print the current default limit. With mm:ss, save it "
        "as the default that every newly called performer gets.",
        "setliststack limit 4:30",
    )
    limit.add_argument(
        "value", nargs="?", metavar="MM:SS", help="new default, 0:10 to 60:00"
    )
    limit.set_defaults(handler=cmd_limit)

    log = add_command(
        sub,
        "log",
        "show finished slots, newest last",
        "Print one row per finished slot: performer, song, limit, elapsed time "
        "and overtime.",
        "setliststack log --last 5",
    )
    log.add_argument("--last", metavar="N", help="only show the N most recent slots")
    log.set_defaults(handler=cmd_log)

    reset = add_command(
        sub,
        "reset",
        "start a new round, or recover a damaged state file",
        "reset --played clears every played flag and keeps the finished slots. "
        "reset --all wipes performers and history without reading the old file, "
        "which is the recovery path for a corrupt state file.",
        "setliststack reset --played",
    )
    reset_group = reset.add_mutually_exclusive_group()
    reset_group.add_argument(
        "--played", action="store_true", help="clear every played flag, keep history"
    )
    reset_group.add_argument(
        "--all",
        dest="reset_all",
        action="store_true",
        help="wipe performers and history, even from a corrupt state file",
    )
    reset.set_defaults(handler=cmd_reset)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    force_utf8(sys.stdout)
    force_utf8(sys.stderr)
    parser = build_parser()
    args = parser.parse_args(argv)
    set_color(not getattr(args, "no_color", False) and supports_ansi(sys.stdout))

    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0

    try:
        return int(handler(args))
    except UsageError as exc:
        err(str(exc))
        return 2
    except StateError as exc:
        err(str(exc))
        return 1
    except KeyboardInterrupt:
        err("Cancelled. Run setliststack roster to see where things stand.")
        return 130
    except OSError as exc:
        err(
            "Unexpected I/O problem ({0}). Next: run setliststack roster".format(
                exc.strerror or exc
            )
        )
        return 1
    except Exception as exc:  # CLI boundary: never leak a traceback to the host
        err(
            "Unexpected error ({0}: {1}). Next: run setliststack roster".format(
                exc.__class__.__name__, exc
            )
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
