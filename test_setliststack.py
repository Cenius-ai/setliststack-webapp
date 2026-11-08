#!/usr/bin/env python3
"""SetlistStack test suite - standard library only.

Run it either way:

    python3 test_setliststack.py
    python3 -m pytest test_setliststack.py -q

The suite drives the real CLI in a throwaway directory, so every assertion is
about what a host actually sees: stdout, stderr, exit codes and the state file.
"""

import contextlib
import io
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "setliststack.py")
sys.path.insert(0, HERE)

import setliststack  # noqa: E402  (import after sys.path fix-up)

BASE_ENV = {
    "PATH": "/usr/bin:/bin:/usr/local/bin",
    "HOME": os.environ.get("HOME", "/root"),
    "TERM": "dumb",
    "PYTHONIOENCODING": "utf-8",
}

EM = "\u2014"  # the em dash used for "no value" and section suffixes


def make_performer(name, song=None, key=None, capo=0, played=False):
    return {
        "name": name,
        "song_title": song,
        "song_key": key,
        "capo": capo,
        "played": played,
        "added_at": "2026-01-01T00:00:00+00:00",
    }


def make_slot(
    slot_id,
    name,
    song=None,
    key=None,
    capo=0,
    limit=300,
    status="done",
    elapsed=None,
    overtime=None,
):
    return {
        "id": slot_id,
        "performer_name": name,
        "song_title": song,
        "song_key": key,
        "capo": capo,
        "limit_seconds": limit,
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:05:00+00:00" if status == "done" else None,
        "elapsed_seconds": elapsed,
        "overtime_seconds": overtime,
        "status": status,
    }


class CliTestCase(unittest.TestCase):
    """Shared helpers: a temp working directory and a way to run the CLI."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="setliststack-test-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = os.path.join(self.dir, "state.json")

    # -- process helpers --------------------------------------------------- #

    def cli_argv(self, *args, state=None):
        return [sys.executable, SCRIPT, "--state", state or self.state] + list(args)

    def cli_env(self, env=None):
        environment = dict(BASE_ENV)
        if env:
            environment.update(env)
        return environment

    def run_cli(self, *args, state=None, env=None):
        return subprocess.run(
            self.cli_argv(*args, state=state),
            cwd=self.dir,
            capture_output=True,
            text=True,
            env=self.cli_env(env),
            timeout=120,
        )

    def spawn_cli(self, *args, stdout=None, env=None):
        return subprocess.Popen(
            self.cli_argv(*args),
            cwd=self.dir,
            stdin=subprocess.DEVNULL,
            stdout=stdout if stdout is not None else subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=stdout is None,
            env=self.cli_env(env),
        )

    def call_main(self, argv):
        """Run main() in-process so save failures can be simulated."""
        out_buffer, err_buffer = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out_buffer), contextlib.redirect_stderr(
            err_buffer
        ):
            code = setliststack.main(argv)
        return code, out_buffer.getvalue(), err_buffer.getvalue()

    # -- assertions -------------------------------------------------------- #

    def assert_ok(self, result):
        self.assertEqual(
            result.returncode,
            0,
            "expected success, got {0}\nstdout:{1}\nstderr:{2}".format(
                result.returncode, result.stdout, result.stderr
            ),
        )

    def assert_fails(self, result, code, fragment):
        self.assertEqual(
            result.returncode,
            code,
            "expected exit {0}\nstdout:{1}\nstderr:{2}".format(
                code, result.stdout, result.stderr
            ),
        )
        self.assertIn(fragment, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(
            len(result.stderr.strip().splitlines()),
            1,
            "errors must be exactly one line, got: {0!r}".format(result.stderr),
        )

    # -- state helpers ----------------------------------------------------- #

    def read_state(self, path=None):
        with open(path or self.state, encoding="utf-8") as handle:
            return json.load(handle)

    def write_raw_state(self, text, path=None):
        with open(path or self.state, "w", encoding="utf-8") as handle:
            handle.write(text)

    def seed_state(self, performers=None, slots=None, default_limit=300):
        now = setliststack.utcnow_iso()
        data = {
            "version": 1,
            "performers": performers or [],
            "slots": slots or [],
            "settings": {
                "default_limit_seconds": default_limit,
                "limit_is_user_set": False,
            },
            "created_at": now,
            "updated_at": now,
        }
        self.write_raw_state(json.dumps(data, indent=2))
        return data


# --------------------------------------------------------------------------- #
# T1 - single-file CLI scaffold with versioned, atomic JSON state
# --------------------------------------------------------------------------- #


class TestScaffold(CliTestCase):
    def test_version_flag(self):
        result = self.run_cli("--version")
        self.assert_ok(result)
        self.assertEqual(result.stdout.strip(), "setliststack 1.0")

    def test_state_file_is_created_with_0600_and_version(self):
        result = self.run_cli("roster")
        self.assert_ok(result)
        self.assertTrue(os.path.exists(self.state), "state file should be created")
        mode = os.stat(self.state).st_mode & 0o777
        self.assertEqual(oct(mode), oct(0o600), "state file must be 0600")
        data = self.read_state()
        self.assertEqual(data["version"], 1)
        for key in ("performers", "slots", "settings", "created_at", "updated_at"):
            self.assertIn(key, data)

    def test_save_leaves_no_temp_files(self):
        self.assert_ok(self.run_cli("add", "Maya"))
        names = sorted(os.listdir(self.dir))
        self.assertEqual(names, ["state.json"], "no temp files may survive a save")

    def test_failed_save_keeps_original_state_and_exits_1(self):
        self.assert_ok(self.run_cli("add", "Maya"))
        with open(self.state, "rb") as handle:
            before = handle.read()

        def failing_write(_path, _text):
            raise OSError(28, "No space left on device")

        original = setliststack.write_atomic
        setliststack.write_atomic = failing_write
        try:
            code, _out, err_text = self.call_main(
                ["--state", self.state, "add", "Dev"]
            )
        finally:
            setliststack.write_atomic = original

        with open(self.state, "rb") as handle:
            self.assertEqual(handle.read(), before, "original state must be intact")
        self.assertEqual(code, 1)
        self.assertEqual(len(err_text.strip().splitlines()), 1)
        self.assertIn("Could not save the state file", err_text)
        self.assertNotIn("Traceback", err_text)

    def test_write_atomic_failure_leaves_no_temp_file(self):
        self.assert_ok(self.run_cli("add", "Maya"))
        with open(self.state, "rb") as handle:
            before = handle.read()

        def failing_replace(_src, _dst):
            raise OSError(13, "Permission denied")

        original = os.replace
        os.replace = failing_replace
        try:
            with self.assertRaises(OSError):
                setliststack.write_atomic(self.state, '{"version": 1}\n')
        finally:
            os.replace = original

        with open(self.state, "rb") as handle:
            self.assertEqual(handle.read(), before)
        leftovers = [n for n in os.listdir(self.dir) if n.startswith(".setliststack-")]
        self.assertEqual(leftovers, [], "the temp file must be removed on failure")


# --------------------------------------------------------------------------- #
# T2 - add with name validation
# --------------------------------------------------------------------------- #


class TestAdd(CliTestCase):
    def test_add_prints_position_and_lands_in_roster(self):
        result = self.run_cli("add", "Maya")
        self.assert_ok(result)
        self.assertIn("Added Maya (position 1).", result.stdout)
        roster = self.run_cli("roster")
        self.assertIn("Maya", roster.stdout)

    def test_duplicate_is_trimmed_and_case_insensitive(self):
        self.assert_ok(self.run_cli("add", "Maya"))
        result = self.run_cli("add", "  maya ")
        self.assert_fails(result, 2, "Maya is already in the rotation.")
        self.assertEqual(len(self.read_state()["performers"]), 1)

    def test_empty_name_is_rejected(self):
        result = self.run_cli("add", "")
        self.assert_fails(result, 2, "Performer name cannot be empty.")
        self.assertIn('setliststack add "Name"', result.stderr)

    def test_name_longer_than_40_characters_is_rejected(self):
        result = self.run_cli("add", "A" * 41)
        self.assert_fails(result, 2, "at most 40 characters")
        self.assertEqual(self.read_state()["performers"], [])

    def test_40_characters_is_accepted(self):
        result = self.run_cli("add", "B" * 40)
        self.assert_ok(result)
        self.assertEqual(self.read_state()["performers"][0]["name"], "B" * 40)


# --------------------------------------------------------------------------- #
# T3 - roster listing
# --------------------------------------------------------------------------- #


class TestRoster(CliTestCase):
    def test_pending_played_sections_and_up_next_marker(self):
        self.seed_state(
            performers=[
                make_performer("Maya", played=True),
                make_performer("Dev"),
                make_performer("Ana"),
            ]
        )
        result = self.run_cli("roster")
        self.assert_ok(result)
        self.assertIn("PENDING", result.stdout)
        self.assertIn("up next: Dev", result.stdout)
        self.assertIn("PLAYED", result.stdout)
        self.assertIn("3 performers " + EM + " 2 pending, 1 played", result.stdout)
        self.assertLess(result.stdout.index("Dev"), result.stdout.index("Ana"))
        self.assertGreater(
            result.stdout.index("Maya"), result.stdout.index("PLAYED")
        )

    def test_empty_roster_points_at_add(self):
        result = self.run_cli("roster")
        self.assert_ok(result)
        self.assertIn(
            'No performers yet. Add one with: setliststack add "Name"', result.stdout
        )

    def test_everyone_played_points_at_reset_played(self):
        self.seed_state(
            performers=[
                make_performer("Maya", played=True),
                make_performer("Dev", played=True),
            ]
        )
        result = self.run_cli("roster")
        self.assert_ok(result)
        self.assertIn(
            "Everyone has played " + EM + " start a new round with: "
            "setliststack reset --played",
            result.stdout,
        )


# --------------------------------------------------------------------------- #
# T4 - remove keeps finished slot history
# --------------------------------------------------------------------------- #


class TestRemove(CliTestCase):
    def test_remove_pending_performer(self):
        self.assert_ok(self.run_cli("add", "Maya"))
        self.assert_ok(self.run_cli("add", "Dev"))
        result = self.run_cli("remove", "Maya")
        self.assert_ok(result)
        self.assertIn("Removed Maya from the rotation.", result.stdout)
        roster = self.run_cli("roster")
        self.assertNotIn("Maya", roster.stdout)
        self.assertIn("Dev", roster.stdout)

    def test_remove_keeps_finished_slot(self):
        self.seed_state(
            performers=[make_performer("Maya"), make_performer("Dev")],
            slots=[make_slot(1, "Maya", "Blackbird", "G", 2, 300, elapsed=312, overtime=12)],
        )
        result = self.run_cli("remove", "Maya")
        self.assert_ok(result)
        data = self.read_state()
        self.assertEqual(len(data["slots"]), 1)
        self.assertEqual(data["slots"][0]["performer_name"], "Maya")
        self.assertEqual([p["name"] for p in data["performers"]], ["Dev"])

    def test_unknown_name(self):
        result = self.run_cli("remove", "Zed")
        self.assert_fails(
            result, 2, "No performer named Zed. Run setliststack roster"
        )

# --------------------------------------------------------------------------- #
# T5 - next: up-next card, active slot, single-active-slot guard
# --------------------------------------------------------------------------- #


class TestNext(CliTestCase):
    def seed_rotation(self):
        self.seed_state(
            performers=[
                make_performer("Maya", "Blackbird", "G", 2),
                make_performer("Dev"),
                make_performer("Ana"),
            ]
        )

    def test_card_and_slot_creation(self):
        self.seed_rotation()
        result = self.run_cli("next")
        self.assert_ok(result)
        for fragment in (
            "UP NEXT: Maya",
            "Song: Blackbird",
            "Key: G",
            "Capo: 2",
            "Limit: 5:00",
            "Slot open " + EM + " run setliststack timer",
        ):
            self.assertIn(fragment, result.stdout)

        data = self.read_state()
        active = [slot for slot in data["slots"] if slot["status"] == "active"]
        self.assertEqual(len(active), 1, "exactly one active slot")
        slot = active[0]
        self.assertEqual(slot["performer_name"], "Maya")
        self.assertEqual(slot["song_title"], "Blackbird")
        self.assertEqual(slot["song_key"], "G")
        self.assertEqual(slot["capo"], 2)
        self.assertEqual(slot["limit_seconds"], 300)
        self.assertTrue(slot["started_at"])
        maya = [p for p in data["performers"] if p["name"] == "Maya"][0]
        self.assertTrue(maya["played"], "the called performer is marked played")

    def test_second_next_is_refused(self):
        self.seed_rotation()
        self.assert_ok(self.run_cli("next"))
        result = self.run_cli("next")
        self.assert_fails(
            result,
            2,
            "Maya still has an open slot. Finish it with setliststack timer or "
            "wipe it with setliststack reset --all.",
        )
        slots = self.read_state()["slots"]
        self.assertEqual(len([s for s in slots if s["status"] == "active"]), 1)

    def test_everyone_played(self):
        self.seed_state(
            performers=[make_performer("Maya", played=True), make_performer("Dev", played=True)]
        )
        result = self.run_cli("next")
        self.assert_fails(
            result,
            2,
            "Everyone has played. Start a new round with: setliststack reset --played",
        )

    def test_empty_roster(self):
        result = self.run_cli("next")
        self.assert_fails(result, 2, "No performers yet.")
        self.assertEqual(self.read_state()["slots"], [])

    def test_rotation_order_is_insertion_order(self):
        self.seed_rotation()
        self.assertIn("UP NEXT: Maya", self.run_cli("next").stdout)
        self.seed_state(
            performers=[
                make_performer("Maya", played=True),
                make_performer("Dev"),
                make_performer("Ana"),
            ]
        )
        self.assertIn("UP NEXT: Dev", self.run_cli("next").stdout)


# --------------------------------------------------------------------------- #
# T6 - song title / key / capo tags
# --------------------------------------------------------------------------- #


class TestSongTags(CliTestCase):
    def test_set_all_three_tags(self):
        self.assert_ok(self.run_cli("add", "Maya"))
        result = self.run_cli(
            "song", "Maya", "--song", "Blackbird", "--key", "G", "--capo", "2"
        )
        self.assert_ok(result)
        self.assertIn("Maya: Blackbird " + EM + " key G, capo 2", result.stdout)
        card = self.run_cli("next")
        self.assertIn("Song: Blackbird", card.stdout)
        self.assertIn("Key: G", card.stdout)
        self.assertIn("Capo: 2", card.stdout)

    def test_no_flags_with_no_tags(self):
        self.assert_ok(self.run_cli("add", "Maya"))
        result = self.run_cli("song", "Maya")
        self.assert_ok(result)
        self.assertIn(
            'Maya has no song set yet. Add one with: setliststack song "Maya" '
            '--song "Title" --key G --capo 0',
            result.stdout,
        )

    def test_no_flags_prints_existing_tags(self):
        self.assert_ok(self.run_cli("add", "Dev", "--song", "Wagon Wheel", "--key", "A"))
        result = self.run_cli("song", "Dev")
        self.assert_ok(result)
        self.assertIn("Dev: Wagon Wheel " + EM + " key A, capo 0", result.stdout)

    def test_invalid_key_lists_the_valid_keys(self):
        self.assert_ok(self.run_cli("add", "Maya"))
        result = self.run_cli("song", "Maya", "--key", "H")
        self.assert_fails(result, 2, "Unknown key \"H\"")
        for key in ("A", "A#", "B", "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#"):
            self.assertIn(key, result.stderr)
        self.assertIn("m (Am)", result.stderr)

    def test_minor_keys_are_accepted(self):
        self.assert_ok(self.run_cli("add", "Maya"))
        result = self.run_cli("song", "Maya", "--key", "am")
        self.assert_ok(result)
        self.assertIn("key Am", result.stdout)

    def test_capo_out_of_range(self):
        self.assert_ok(self.run_cli("add", "Maya"))
        result = self.run_cli("song", "Maya", "--capo", "13")
        self.assert_fails(result, 2, "Capo must be a whole number from 0 to 12.")
        result = self.run_cli("song", "Maya", "--capo", "-1")
        self.assert_fails(result, 2, "Capo must be a whole number from 0 to 12.")

    def test_unknown_performer(self):
        result = self.run_cli("song", "Nobody", "--key", "G")
        self.assert_fails(result, 2, "No performer named Nobody.")


# --------------------------------------------------------------------------- #
# T7 - timer countdown, ASCII fallback, slot completion
# --------------------------------------------------------------------------- #


class TestTimer(CliTestCase):
    def open_slot(self, limit=300, name="Maya"):
        self.seed_state(
            performers=[make_performer(name, "Blackbird", "G", 2, played=True)],
            slots=[make_slot(1, name, "Blackbird", "G", 2, limit, status="active")],
        )

    def interrupt_after(self, seconds, *args, env=None):
        proc = self.spawn_cli("timer", *args, env=env)
        try:
            time.sleep(seconds)
            proc.send_signal(signal.SIGINT)
            std_out, std_err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        return proc.returncode, std_out, std_err

    def test_ctrl_c_closes_the_slot(self):
        self.open_slot(limit=300)
        code, std_out, _std_err = self.interrupt_after(2.6, env={"TERM": "dumb"})
        self.assertEqual(code, 0)
        self.assertIn("Slot closed: Maya", std_out)
        slot = self.read_state()["slots"][0]
        self.assertEqual(slot["status"], "done")
        self.assertTrue(slot["ended_at"])
        self.assertGreaterEqual(slot["elapsed_seconds"], 2)
        self.assertLessEqual(slot["elapsed_seconds"], 6)
        self.assertEqual(slot["overtime_seconds"], 0)

    def test_plain_mode_emits_no_escape_bytes(self):
        self.open_slot(limit=300)
        code, std_out, _std_err = self.interrupt_after(2.4, env={"TERM": "dumb"})
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", std_out, "plain mode must not emit ANSI bytes")
        remaining_lines = [
            line for line in std_out.splitlines() if line.startswith("remaining ")
        ]
        self.assertGreaterEqual(len(remaining_lines), 2)
        self.assertIn("remaining 5:00", std_out.splitlines()[0])

    def test_no_color_environment_forces_plain_mode(self):
        self.open_slot(limit=300)
        code, std_out, _std_err = self.interrupt_after(
            1.4, env={"TERM": "xterm-256color", "NO_COLOR": "1"}
        )
        self.assertEqual(code, 0)
        self.assertNotIn("\x1b", std_out)

    @unittest.skipUnless(hasattr(os, "openpty"), "pty is unavailable on this platform")
    def test_tty_mode_redraws_one_line(self):
        self.open_slot(limit=300)
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            self.cli_argv("timer"),
            cwd=self.dir,
            stdin=subprocess.DEVNULL,
            stdout=slave,
            stderr=slave,
            env=self.cli_env({"TERM": "xterm-256color"}),
        )
        os.close(slave)
        chunks = []
        try:
            time.sleep(1.6)
            proc.send_signal(signal.SIGINT)
            deadline = time.time() + 30
            while time.time() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        break
                    if not data:
                        break
                    chunks.append(data)
                elif proc.poll() is not None:
                    break
            proc.wait(timeout=30)
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        transcript = b"".join(chunks)
        self.assertEqual(proc.returncode, 0)
        self.assertIn(b"\x1b[2K", transcript, "TTY mode must erase the line it redraws")
        self.assertIn(b"\r", transcript)

    def test_overtime_counter_and_recorded_seconds(self):
        self.open_slot(limit=3)
        code, std_out, _std_err = self.interrupt_after(4.6, env={"TERM": "dumb"})
        self.assertEqual(code, 0)
        self.assertIn("OVERTIME +", std_out)
        slot = self.read_state()["slots"][0]
        self.assertEqual(slot["status"], "done")
        self.assertGreaterEqual(slot["elapsed_seconds"], 4)
        self.assertEqual(
            slot["overtime_seconds"], slot["elapsed_seconds"] - 3
        )

    def test_no_open_slot(self):
        result = self.run_cli("timer")
        self.assert_fails(
            result, 2, "No open slot. Run setliststack next to call the next performer."
        )

    def test_limit_override_rewrites_the_slot(self):
        self.open_slot(limit=300)
        code, std_out, _std_err = self.interrupt_after(
            1.4, "--limit", "1:00", env={"TERM": "dumb"}
        )
        self.assertEqual(code, 0)
        self.assertIn("remaining 1:00", std_out.splitlines()[0])
        self.assertEqual(self.read_state()["slots"][0]["limit_seconds"], 60)

    def test_bad_limit_override(self):
        self.open_slot(limit=300)
        result = self.run_cli("timer", "--limit", "99:99")
        self.assert_fails(result, 2, "Limit must be mm:ss between 0:10 and 60:00")

# --------------------------------------------------------------------------- #
# T8 - corrupt state, reset --all, reset --played
# --------------------------------------------------------------------------- #


class TestRecovery(CliTestCase):
    def test_corrupt_state_prints_one_line_and_exits_1(self):
        self.write_raw_state("not json")
        result = self.run_cli("roster")
        self.assert_fails(
            result, 1, "State file is not valid JSON. Run setliststack reset --all"
        )

    def test_wrong_version_names_reset_all(self):
        self.write_raw_state(json.dumps({"version": 99, "performers": [], "slots": []}))
        result = self.run_cli("log")
        self.assert_fails(result, 1, "reset --all")

    def test_unreadable_state_names_reset_all(self):
        directory_state = os.path.join(self.dir, "state-dir")
        os.makedirs(directory_state)
        result = self.run_cli("roster", state=directory_state)
        self.assert_fails(result, 1, "reset --all")

    def test_reset_all_recovers_without_parsing(self):
        self.write_raw_state("not json at all {{{")
        result = self.run_cli("reset", "--all")
        self.assert_ok(result)
        self.assertIn("State reset. All performers and history cleared.", result.stdout)
        data = self.read_state()
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["performers"], [])
        self.assertEqual(data["slots"], [])

    def test_reset_all_never_calls_the_loader(self):
        self.write_raw_state("not json at all {{{")

        def explode(_path):
            raise AssertionError("reset --all must not load the state file")

        original = setliststack.load_state
        setliststack.load_state = explode
        try:
            code, std_out, _err = self.call_main(["--state", self.state, "reset", "--all"])
        finally:
            setliststack.load_state = original
        self.assertEqual(code, 0)
        self.assertIn("State reset.", std_out)

    def test_reset_played_clears_flags_and_keeps_history(self):
        self.seed_state(
            performers=[
                make_performer("Maya", played=True),
                make_performer("Dev", played=True),
                make_performer("Ana", played=True),
            ],
            slots=[
                make_slot(1, "Maya", "Blackbird", "G", 2, elapsed=312, overtime=12),
                make_slot(2, "Dev", "Wagon Wheel", "A", 0, elapsed=220, overtime=0),
                make_slot(3, "Ana", "Landslide", "Em", 3, elapsed=301, overtime=1),
            ],
        )
        result = self.run_cli("reset", "--played")
        self.assert_ok(result)
        data = self.read_state()
        self.assertEqual([p["played"] for p in data["performers"]], [False, False, False])
        self.assertEqual(len(data["slots"]), 3)

    def test_reset_without_a_flag(self):
        self.seed_state(performers=[make_performer("Maya")])
        before = self.read_state()
        result = self.run_cli("reset")
        self.assert_fails(
            result,
            2,
            "Nothing to reset. Use setliststack reset --played or setliststack "
            "reset --all.",
        )
        self.assertEqual(self.read_state(), before)


# --------------------------------------------------------------------------- #
# T9 - limit default setting
# --------------------------------------------------------------------------- #


class TestLimit(CliTestCase):
    def test_built_in_default(self):
        result = self.run_cli("limit")
        self.assert_ok(result)
        self.assertIn("Default limit: 5:00 (built-in)", result.stdout)

    def test_set_default_and_next_stamps_it(self):
        self.assert_ok(self.run_cli("limit", "4:30"))
        shown = self.run_cli("limit")
        self.assertIn("Default limit: 4:30 (set)", shown.stdout)
        self.assert_ok(self.run_cli("add", "Maya", "--song", "Blackbird", "--key", "G"))
        card = self.run_cli("next")
        self.assertIn("Limit: 4:30", card.stdout)
        self.assertEqual(self.read_state()["slots"][0]["limit_seconds"], 270)

    def test_timer_uses_the_slot_limit_when_no_flag_is_given(self):
        self.seed_state(
            performers=[make_performer("Maya", played=True)],
            slots=[make_slot(1, "Maya", "Blackbird", "G", 2, 270, status="active")],
            default_limit=270,
        )
        proc = self.spawn_cli("timer", env={"TERM": "dumb"})
        try:
            time.sleep(1.4)
            proc.send_signal(signal.SIGINT)
            std_out, _std_err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("remaining 4:30", std_out.splitlines()[0])

    def test_default_limit_reaches_the_next_slot_after_override(self):
        self.assert_ok(self.run_cli("limit", "4:30"))
        self.seed_state(
            performers=[make_performer("Maya", played=True)],
            slots=[make_slot(1, "Maya", "Blackbird", "G", 2, 270, status="active")],
            default_limit=270,
        )
        proc = self.spawn_cli("timer", "--limit", "1:00", env={"TERM": "dumb"})
        try:
            time.sleep(1.3)
            proc.send_signal(signal.SIGINT)
            std_out, _std_err = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("remaining 1:00", std_out.splitlines()[0])
        self.assertEqual(self.read_state()["slots"][0]["limit_seconds"], 60)

    def test_invalid_limits(self):
        for value in ("abc", "0:05", "90:00", "4:60", "5"):
            result = self.run_cli("limit", value)
            self.assert_fails(
                result, 2, "Limit must be mm:ss between 0:10 and 60:00 (for example 4:30)."
            )


# --------------------------------------------------------------------------- #
# T10 - log of finished slots
# --------------------------------------------------------------------------- #


class TestLog(CliTestCase):
    def test_rows_mark_overtime(self):
        self.seed_state(
            performers=[make_performer("Maya", played=True), make_performer("Dev", played=True)],
            slots=[
                make_slot(1, "Maya", "Blackbird", "G", 2, 300, elapsed=312, overtime=12),
                make_slot(2, "Dev", "Wagon Wheel", "A", 0, 300, elapsed=220, overtime=0),
            ],
        )
        result = self.run_cli("log")
        self.assert_ok(result)
        self.assertIn("Maya", result.stdout)
        self.assertIn("Blackbird", result.stdout)
        self.assertIn("5:12", result.stdout)
        self.assertIn("+0:12", result.stdout)
        self.assertIn("Wagon Wheel", result.stdout)
        self.assertIn("3:40", result.stdout)
        lines = [line for line in result.stdout.splitlines() if "Maya" in line]
        self.assertEqual(len(lines), 1)
        self.assertIn("+0:12", lines[0], "the over-time row is marked")
        dev_line = [line for line in result.stdout.splitlines() if "Dev" in line][0]
        self.assertNotIn("+", dev_line)

    def test_empty_log(self):
        result = self.run_cli("log")
        self.assert_ok(result)
        self.assertIn(
            "No finished slots yet. Run setliststack next then setliststack timer "
            "to record one.",
            result.stdout,
        )

    def test_last_limits_to_the_newest_rows(self):
        slots = [
            make_slot(index, name, "Song " + str(index), "C", 0, 300, elapsed=200 + index)
            for index, name in enumerate(["Ava", "Ben", "Cara", "Dev", "Eli"], start=1)
        ]
        self.seed_state(slots=slots)
        result = self.run_cli("log", "--last", "2")
        self.assert_ok(result)
        self.assertNotIn("Ava", result.stdout)
        self.assertNotIn("Cara", result.stdout)
        self.assertIn("Dev", result.stdout)
        self.assertIn("Eli", result.stdout)
        self.assertLess(result.stdout.index("Dev"), result.stdout.index("Eli"))

    def test_only_done_slots_are_listed(self):
        self.seed_state(
            slots=[
                make_slot(1, "Maya", status="done", elapsed=250),
                make_slot(2, "Dev", status="active"),
            ]
        )
        result = self.run_cli("log")
        self.assertIn("Maya", result.stdout)
        self.assertNotIn("Dev", result.stdout)


# --------------------------------------------------------------------------- #
# T11 - README, per-subcommand help, one-line errors
# --------------------------------------------------------------------------- #


class TestDocsAndHelp(CliTestCase):
    COMMANDS = [
        "roster",
        "add",
        "remove",
        "song",
        "next",
        "timer",
        "limit",
        "log",
        "reset",
    ]

    def test_top_level_help_lists_all_nine_commands(self):
        result = self.run_cli("--help")
        self.assert_ok(result)
        for command in self.COMMANDS:
            self.assertIn(command, result.stdout)
        self.assertIn("setliststack_state.json", result.stdout)

    def test_every_subcommand_help_has_usage_and_an_example(self):
        for command in self.COMMANDS:
            result = self.run_cli(command, "--help")
            self.assert_ok(result)
            self.assertIn("usage: setliststack " + command, result.stdout)
            self.assertIn("example:", result.stdout)
            self.assertIn("setliststack " + command, result.stdout.split("example:")[1])

    def test_validation_failures_are_one_line_with_a_next_command(self):
        for argv in (
            ("add", ""),
            ("song", "Nobody", "--capo", "99"),
            ("add", "Maya", "--key", "Q"),
            ("log", "--last", "0"),
        ):
            result = self.run_cli(*argv)
            self.assertEqual(result.returncode, 2, argv)
            lines = result.stderr.strip().splitlines()
            self.assertEqual(len(lines), 1, "{0} -> {1!r}".format(argv, result.stderr))
            self.assertIn("setliststack", lines[0])
            self.assertNotIn("Traceback", result.stderr)

    def test_readme_documents_the_quickstart_and_recovery(self):
        with open(os.path.join(HERE, "README.md"), encoding="utf-8") as handle:
            readme = handle.read()
        for fragment in (
            "setliststack add",
            "setliststack roster",
            "setliststack next",
            "setliststack timer",
            "setliststack log",
            "setliststack_state.json",
            "setliststack reset --all",
            "--state",
        ):
            self.assertIn(fragment, readme)


# --------------------------------------------------------------------------- #
# T12 - parsers and the demo script
# --------------------------------------------------------------------------- #


class TestParsers(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(setliststack.parse_key("G"), "G")
        self.assertEqual(setliststack.parse_key(" a# "), "A#")
        self.assertEqual(setliststack.parse_key("am"), "Am")
        self.assertEqual(setliststack.parse_key("A#M"), "A#m")
        for bad in ("H", "Bb", "", "G##"):
            with self.assertRaises(setliststack.UsageError):
                setliststack.parse_key(bad)

    def test_capo(self):
        self.assertEqual(setliststack.parse_capo("0"), 0)
        self.assertEqual(setliststack.parse_capo("12"), 12)
        for bad in ("13", "-1", "1.5", "", "two"):
            with self.assertRaises(setliststack.UsageError):
                setliststack.parse_capo(bad)

    def test_limit(self):
        self.assertEqual(setliststack.parse_limit("0:10"), 10)
        self.assertEqual(setliststack.parse_limit("4:30"), 270)
        self.assertEqual(setliststack.parse_limit("60:00"), 3600)
        for bad in ("abc", "0:05", "90:00", "4:60", "5", ""):
            with self.assertRaises(setliststack.UsageError):
                setliststack.parse_limit(bad)

    def test_names(self):
        self.assertEqual(setliststack.require_name("  Maya  "), "Maya")
        for bad in ("", "   ", "A" * 41):
            with self.assertRaises(setliststack.UsageError):
                setliststack.require_name(bad)

    def test_duration_formatting(self):
        self.assertEqual(setliststack.fmt_mmss(300), "5:00")
        self.assertEqual(setliststack.fmt_mmss(270), "4:30")
        self.assertEqual(setliststack.fmt_mmss(3600), "60:00")
        self.assertEqual(setliststack.fmt_mmss(9), "0:09")


class TestDemoScript(CliTestCase):
    def test_demo_script_exists_and_is_non_interactive(self):
        path = os.path.join(HERE, "demo.sh")
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("setliststack.py", body)
        self.assertNotIn("read -r", body, "the demo must never wait for input")
        self.assertNotIn("input(", body)


def main():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if result.wasSuccessful():
        print("OK ({0} tests)".format(result.testsRun))
        return 0
    print(
        "FAILED ({0} failures, {1} errors)".format(
            len(result.failures), len(result.errors)
        )
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
