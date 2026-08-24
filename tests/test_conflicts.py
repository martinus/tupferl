"""The conflict prompt, driven through a real terminal.

Plan §3.4's requirement is "every choice is one keypress", and that is a claim
about a *terminal*: the code path that makes it true sets `ICANON` off with
`termios`, and it is skipped entirely when stdin is a pipe. A test that fed the
prompt a pipe would exercise the other branch and prove nothing about the
requirement -- so every test of a keypress here opens a pty with `pty.openpty`
and types into it, which is what `tests/support.py` does for git and what plan
§7.1 asks for generally.

The editor is a real program too: a two-line shell script in the sandbox, run
through the same `shlex.split` and `subprocess.run` a user's `$EDITOR` goes
through. The three that matter -- one that writes, one that exits non-zero, and
one that leaves the file alone -- differ only in their body, and the third is
what produces the "you left the markers in" path without a fixture that has to
know what a marker looks like.
"""

from __future__ import annotations

import contextlib
import functools
import io
import os
import termios
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from tests import support
from tupferl import conflicts, merge
from tupferl.config import Config
from tupferl.copies import Blob
from tupferl.errors import TupferlError

NAME = PurePosixPath(".bashrc")

#: Three versions whose first line differs on both sides, so the merge conflicts
#: in exactly one place and neither side is a prefix of the other. The tail is
#: five distinct lines so that a hunk's line numbers are not 1, which is the one
#: number an off-by-one in `hunks` would still produce.
BASE = b"keep-one\nkeep-two\nalpha\nkeep-three\nkeep-four\n"
MINE = b"keep-one\nkeep-two\nMINE-IS-HERE\nkeep-three\nkeep-four\n"
THEIRS = b"keep-one\nkeep-two\nTHEIRS-IS-HERE\nkeep-three\nkeep-four\n"


#: How many unchanged lines separate one generated conflict from the next.
#: More than twice git's three lines of diff context, because at three the two
#: changes share a hunk and `many(5)` produces *one* conflict -- measured, at
#: three, four, five, six and eight. A fixture that quietly collapsed to one
#: hunk would pass every "it found the hunks" assertion below.
APART = 8


def many(count: int) -> tuple[bytes, bytes, bytes]:
    """Three versions that disagree in `count` separate places.

    Each side's marker lines carry its own name, so a hunk cannot be attributed
    to the wrong side and no two hunks are textually identical -- the same rule
    `tests/test_sync_properties.py` states for its regions, for the same reason.
    """

    def build(marker: str) -> bytes:
        rows: list[str] = []
        for index in range(count):
            rows.append(f"{marker}{index}")
            rows += [f"pad-{index}-{step}" for step in range(APART)]
        return ("\n".join(rows) + "\n").encode()

    return build("base"), build("mine"), build("theirs")


@functools.cache
def sides_for(base: bytes | None, mine: bytes, theirs: bytes) -> conflicts.Sides:
    """A `Sides` built by the real merge, the way `sync.resolve` builds one.

    Cached, because every call spawns `git merge-file` and this file asked for
    the same three byte strings 42 times per run -- 0.105s of its 0.130s. Safe
    to share: a `Sides` is an immutable `NamedTuple` of bytes, and no test here
    mutates one. Still the real git, just not 42 times for the same answer.
    """
    merged = merge.three_way(str(NAME), base, mine, theirs)
    return conflicts.Sides(
        NAME,
        None if base is None else Blob(base, False),
        Blob(mine, False),
        Blob(theirs, False),
        merged.data,
        merged.conflicts,
    )


def one_conflict() -> conflicts.Sides:
    return sides_for(BASE, MINE, THEIRS)


def binary() -> conflicts.Sides:
    """A file with a NUL in it that both computers changed -- no lines to take."""
    return sides_for(b"\x00base", b"\x00mine", b"\x00theirs")


class Prompted(unittest.TestCase):
    """A terminal to type into and a buffer to read the prompt out of."""

    def setUp(self) -> None:
        self.terminal = support.Terminal()
        self.addCleanup(self.terminal.close)
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.out = io.StringIO()
        self.config = Config()

    def ask(self, sides: conflicts.Sides, keys: str) -> conflicts.Answer:
        """Type `keys` at the prompt and return what it settled on.

        `support.FALLBACK` is typed after them; see there for why.
        """
        self.terminal.type(keys + support.FALLBACK)
        return conflicts.ask(sides, self.config, self.terminal.source, self.out)

    def scratch(self) -> Path:
        """A throwaway directory that lives as long as the test.

        `contextlib.ExitStack` rather than `TestCase.enterContext`, which is
        3.11 and this project supports 3.10 -- the version the `tomllib`
        fallback exists for.
        """
        return self.stack.enter_context(support.tempdir())

    def editor_that(self, body: str) -> None:
        """Install a real `$EDITOR`: a shell script whose body is `body`."""
        where = support.fake_editor(self.scratch() / "fake-editor", body)
        self.stack.enter_context(mock.patch.dict(os.environ, {"EDITOR": str(where)}))


class TestParsingTheMarkers(unittest.TestCase):
    """`hunks` reads back what `merge` wrote, and only for display."""

    def test_it_finds_the_two_sides_and_where_they_are(self) -> None:
        found = conflicts.hunks(one_conflict())
        self.assertEqual(1, len(found))
        hunk = found[0]
        self.assertEqual([b"MINE-IS-HERE"], hunk.mine)
        self.assertEqual([b"THEIRS-IS-HERE"], hunk.theirs)
        # Line 3 of the merged file is the `<<<<<<<`, because two lines of
        # agreement come first. Asserted as numbers rather than as "greater than
        # zero": an off-by-one is exactly what this can catch and a range check
        # is exactly what would not.
        self.assertEqual(3, hunk.start)
        self.assertEqual(7, hunk.end)

    def test_the_numbers_really_point_at_the_markers(self) -> None:
        """The independent check, because the numbers above were read off a run.

        A number computed by the code under test and pasted into the test is the
        shape CLAUDE.md §2 calls a copy of the code. This one indexes the merged
        file the prompt shows and insists the marker is there.
        """
        sides = one_conflict()
        assert sides.marked is not None
        lines = sides.marked.split(b"\n")
        hunk = conflicts.hunks(sides)[0]
        self.assertTrue(lines[hunk.start - 1].startswith(b"<<<<<<< "))
        self.assertTrue(lines[hunk.end - 1].startswith(b">>>>>>> "))

    def test_a_file_that_merely_contains_markers_is_not_split(self) -> None:
        """A dotfiles repository is one of the few places a file legitimately
        holds conflict markers. Matching the bare `<<<<<<<` would report a
        conflict inside a file nobody disagreed about."""
        decoy = b"<<<<<<< HEAD\n=======\n>>>>>>> other\n"
        sides = sides_for(decoy + BASE, decoy + MINE, decoy + THEIRS)
        self.assertEqual(1, len(conflicts.hunks(sides)))

    def test_a_binary_file_has_no_hunks(self) -> None:
        self.assertEqual([], conflicts.hunks(binary()))

    def test_three_conflicts_are_three_hunks(self) -> None:
        """One is not enough: a parser that reset nothing between regions would
        return one hunk holding everything, and pass the test above."""
        sides = sides_for(*many(3))
        self.assertEqual(3, sides.conflicts, "the fixture did not produce three hunks")
        found = conflicts.hunks(sides)
        self.assertEqual([[b"mine0"], [b"mine1"], [b"mine2"]], [hunk.mine for hunk in found])
        self.assertEqual([[b"theirs0"], [b"theirs1"], [b"theirs2"]], [h.theirs for h in found])


class TestWhatTheUserSees(unittest.TestCase):
    def test_it_names_the_file_and_shows_both_sides(self) -> None:
        text = conflicts.describe(one_conflict(), colour=False)
        self.assertIn(".bashrc", text)
        self.assertIn("1 conflict to settle", text)
        self.assertIn("MINE-IS-HERE", text)
        self.assertIn("THEIRS-IS-HERE", text)
        self.assertIn("this computer", text)
        self.assertIn("the repository", text)

    def test_the_line_numbers_say_which_file_they_are_of(self) -> None:
        """They are the merged file's, which is the one `[e]` opens. Saying so is
        the difference between a useful number and a wrong one: the numbers of
        the two original files are not recoverable from the markers."""
        self.assertIn("of the merged file", conflicts.describe(one_conflict(), colour=False))

    def test_a_long_conflict_is_cut_and_says_so(self) -> None:
        lines = [f"line-{index}".encode() for index in range(conflicts.SHOWN_LINES + 5)]
        sides = sides_for(b"base\n", b"\n".join(lines) + b"\n", b"theirs\n")
        text = conflicts.describe(sides, colour=False)
        self.assertIn(f"line-{conflicts.SHOWN_LINES - 1}", text)
        self.assertNotIn(f"line-{conflicts.SHOWN_LINES}", text)
        self.assertIn("5 more lines", text)

    def test_many_conflicts_are_cut_and_point_at_the_diff(self) -> None:
        count = conflicts.SHOWN_HUNKS + 2
        sides = sides_for(*many(count))
        self.assertEqual(count, sides.conflicts, "the fixture did not produce five hunks")
        text = conflicts.describe(sides, colour=False)
        self.assertIn(f"{conflicts.SHOWN_HUNKS} of {count}", text)
        self.assertIn("2 more", text)
        self.assertNotIn(f"mine{count - 1}", text)

    def test_a_binary_file_says_there_are_no_lines(self) -> None:
        text = conflicts.describe(binary(), colour=False)
        self.assertIn("not a text file", text)
        self.assertIn("whole file", text)

    def test_a_binary_file_is_not_offered_the_keys_that_need_lines(self) -> None:
        """`[b]`, `[e]` and `[d]` all mean "work with the lines". Offering them
        and refusing afterwards tells the user after they have decided."""
        keys = conflicts.choices(binary(), colour=False)
        self.assertIn("[l]", keys)
        self.assertIn("[r]", keys)
        self.assertIn("[s]", keys)
        for absent in ("[b]", "[e]", "[d]"):
            self.assertNotIn(absent, keys)

    def test_a_text_file_is_offered_all_six(self) -> None:
        keys = conflicts.choices(one_conflict(), colour=False)
        for key in ("[l]", "[r]", "[b]", "[e]", "[d]", "[s]"):
            self.assertIn(key, keys)

    def test_colour_is_added_only_when_it_is_asked_for(self) -> None:
        """Both halves, because the sandbox sets `NO_COLOR` -- so every other
        assertion in this file runs against the uncoloured branch, and a
        `paint` that ignored its argument would pass all of them."""
        self.assertIn("\033[", conflicts.describe(one_conflict(), colour=True))
        self.assertNotIn("\033[", conflicts.describe(one_conflict(), colour=False))

    def test_a_file_that_is_not_utf8_still_prints(self) -> None:
        """A managed file is bytes. Raising here would be raising on exactly the
        file the user most needs to look at."""
        sides = sides_for(b"base\n", b"\xff\xfe mine\n", b"\xfe\xff theirs\n")
        self.assertIn("�", conflicts.describe(sides, colour=False))


class TestTheFullDiff(unittest.TestCase):
    def test_it_compares_the_two_computers(self) -> None:
        text = conflicts.unified(one_conflict())
        self.assertIn("-MINE-IS-HERE", text)
        self.assertIn("+THEIRS-IS-HERE", text)

    def test_it_names_the_two_sides_the_way_the_prompt_does(self) -> None:
        """The same labels as the conflict markers, so the diff and the prompt
        cannot describe the same two sides differently."""
        text = conflicts.unified(one_conflict())
        mine_at, _, theirs_at = merge.labels_for(str(NAME))
        self.assertIn(f"--- {mine_at}", text)
        self.assertIn(f"+++ {theirs_at}", text)

    def test_it_is_not_a_diff_against_the_merge_base(self) -> None:
        """The question at the prompt is which of the two computers to keep, and
        a diff against a third version answers a different one. `alpha` is only
        in the base, so its absence is what tells the two apart."""
        self.assertNotIn("alpha", conflicts.unified(one_conflict()))


class TestOneKeypress(unittest.TestCase):
    def setUp(self) -> None:
        self.terminal = support.Terminal()
        self.addCleanup(self.terminal.close)

    def test_a_key_is_read_without_waiting_for_enter(self) -> None:
        """Plan §3.4: every choice is one keypress. Nothing but `l` is written,
        so a read that waited for a newline would block until the test timed
        out -- which is the assertion, and it cannot be made any other way."""
        self.terminal.type("l")
        self.assertEqual("l", conflicts.one_key(self.terminal.source))

    def test_the_terminal_is_left_as_it_was_found(self) -> None:
        before = termios.tcgetattr(self.terminal.source.fileno())
        self.terminal.type("l")
        conflicts.one_key(self.terminal.source)
        self.assertEqual(before, termios.tcgetattr(self.terminal.source.fileno()))

    def test_it_is_restored_even_when_the_read_raises(self) -> None:
        """The case the `finally` exists for, and the one the test above cannot
        see: an interrupt at the prompt must not leave the user's shell with
        `ECHO` off, which looks like a hung terminal.

        Without this, moving `tcsetattr` out of the `finally` into ordinary
        sequence leaves the whole suite green -- a precondition never
        established, which CLAUDE.md §2 lists by name.
        """
        before = termios.tcgetattr(self.terminal.source.fileno())
        patched = mock.patch("os.read", side_effect=KeyboardInterrupt)
        with patched, self.assertRaises(KeyboardInterrupt):
            conflicts.one_key(self.terminal.source)
        self.assertEqual(before, termios.tcgetattr(self.terminal.source.fileno()))

    def test_an_arrow_key_is_one_keypress_and_not_three(self) -> None:
        """A single press of Down sends `\x1b[B`. Read a byte at a time, that is
        `\x1b`, `[` and `B` to three successive calls -- and `b` is *keep both*,
        so one arrow key, or one notch of a mouse wheel, silently wrote a union
        merge to `$HOME`, the repository and the snapshot with `sync` exiting 0.
        """
        self.terminal.type("\x1b[B")
        self.assertEqual("\x1b[b", conflicts.one_key(self.terminal.source))

    def test_nothing_of_the_sequence_is_left_for_the_next_read(self) -> None:
        """The half the test above cannot show: the whole press was consumed, so
        the *next* key is the next key the user pressed. Asserted by typing an
        arrow and then an `l`, which is what a user who scrolled and then
        answered does."""
        self.terminal.type("\x1b[Al")
        conflicts.one_key(self.terminal.source)
        self.assertEqual("l", conflicts.one_key(self.terminal.source))

    def test_it_is_lower_cased(self) -> None:
        self.terminal.type("L")
        self.assertEqual("l", conflicts.one_key(self.terminal.source))

    def test_a_pipe_is_read_a_line_at_a_time(self) -> None:
        """The other branch, and it is not a test affordance: `sync` with its
        input redirected takes it."""
        self.assertEqual("r", conflicts.one_key(io.StringIO("r\n")))

    def test_end_of_input_is_the_empty_string(self) -> None:
        self.assertEqual("", conflicts.one_key(io.StringIO("")))


class TestAKeypressThatIsNotAKey(Prompted):
    def test_an_arrow_key_does_not_answer_the_prompt(self) -> None:
        """It re-asks, and it does not act on the last byte of the sequence."""
        got = self.ask(one_conflict(), "\x1b[B")
        self.assertEqual(conflicts.SKIP, got.choice)
        self.assertIn("is not a key", self.out.getvalue())

    def test_the_escape_is_not_echoed_raw(self) -> None:
        """Printing the bytes back would move the cursor or clear the screen on
        their way out, which is what a `repr` avoids."""
        self.ask(one_conflict(), "\x1b[B")
        self.assertNotIn("\x1b[B", self.out.getvalue())


class TestTheKeys(Prompted):
    def test_l_keeps_this_computer(self) -> None:
        self.assertEqual(conflicts.Answer(conflicts.LOCAL), self.ask(one_conflict(), "l"))

    def test_r_keeps_the_repository(self) -> None:
        self.assertEqual(conflicts.Answer(conflicts.REMOTE), self.ask(one_conflict(), "r"))

    def test_s_skips(self) -> None:
        self.assertEqual(conflicts.Answer(conflicts.SKIP), self.ask(one_conflict(), "s"))

    def test_end_of_input_skips(self) -> None:
        """The one answer that cannot lose something the user meant to keep."""
        got = conflicts.ask(one_conflict(), self.config, io.StringIO(""), self.out)
        self.assertEqual(conflicts.Answer(conflicts.SKIP), got)

    def test_b_keeps_both_sides_in_turn(self) -> None:
        got = self.ask(one_conflict(), "b")
        self.assertEqual(conflicts.BOTH, got.choice)
        assert got.data is not None
        self.assertEqual(
            b"keep-one\nkeep-two\nMINE-IS-HERE\nTHEIRS-IS-HERE\nkeep-three\nkeep-four\n",
            got.data,
        )

    def test_b_leaves_no_markers_behind(self) -> None:
        """Stated separately from the bytes above, because it is the property
        that matters: a union merge that kept a marker would put `<<<<<<<` into
        the user's `.bashrc` on both computers."""
        got = self.ask(one_conflict(), "b")
        assert got.data is not None
        self.assertNotIn(b"<<<<<<<", got.data)
        self.assertNotIn(b">>>>>>>", got.data)

    def test_d_shows_the_diff_and_asks_again(self) -> None:
        got = self.ask(one_conflict(), "ds")
        self.assertEqual(conflicts.SKIP, got.choice)
        self.assertIn("-MINE-IS-HERE", self.out.getvalue())
        # Asked twice, which is what "and asks again" means.
        self.assertEqual(2, self.out.getvalue().count("1 conflict to settle"))

    def test_a_key_that_is_not_on_offer_asks_again(self) -> None:
        got = self.ask(one_conflict(), "zs")
        self.assertEqual(conflicts.SKIP, got.choice)
        self.assertIn("not one of the keys", self.out.getvalue())

    def test_a_binary_file_refuses_the_keys_it_did_not_offer(self) -> None:
        got = self.ask(binary(), "bs")
        self.assertEqual(conflicts.SKIP, got.choice)
        self.assertIn("no lines", self.out.getvalue())

    def test_a_binary_file_still_takes_a_side(self) -> None:
        self.assertEqual(conflicts.LOCAL, self.ask(binary(), "l").choice)


class TestTheEditorHandoff(Prompted):
    def test_e_returns_what_the_editor_saved(self) -> None:
        self.editor_that('printf "settled by hand\\n" > "$1"')
        got = self.ask(one_conflict(), "e")
        self.assertEqual(conflicts.Answer(conflicts.EDIT, b"settled by hand\n"), got)

    def test_the_editor_opens_the_merged_file_with_its_markers(self) -> None:
        """What `[e]` is for. The editor here copies its argument out so the
        test can look at what was handed over."""
        landed = self.scratch() / "seen"
        # Copies what it was given out for the test to look at, *and* settles
        # the file -- an editor that saved the markers back is
        # `test_markers_left_in_place_are_refused`, which is a different test.
        self.editor_that(f'cat "$1" > {landed}\nprintf "done\\n" > "$1"')
        self.assertEqual(conflicts.EDIT, self.ask(one_conflict(), "e").choice)
        text = landed.read_bytes()
        self.assertIn(b"<<<<<<< .bashrc (this computer)", text)
        self.assertIn(b"MINE-IS-HERE", text)
        self.assertIn(b"THEIRS-IS-HERE", text)

    def test_the_file_is_named_after_the_managed_one(self) -> None:
        """An editor chooses its syntax mode from the file name, and a user
        editing `init.lua` in a file called `tmp9k2j` gets none."""
        landed = self.scratch() / "seen"
        self.editor_that(f'basename "$1" > {landed}\nprintf "done\\n" > "$1"')
        self.assertEqual(conflicts.EDIT, self.ask(one_conflict(), "e").choice)
        self.assertEqual(".bashrc", landed.read_text(encoding="utf-8").strip())

    def test_an_editor_that_fails_asks_again(self) -> None:
        self.editor_that("exit 3")
        got = self.ask(one_conflict(), "es")
        self.assertEqual(conflicts.SKIP, got.choice)
        self.assertIn("exited with 3", self.out.getvalue())

    def test_markers_left_in_place_are_refused(self) -> None:
        """An editor that changes nothing leaves tupferl's own markers behind.
        Accepting that would put `<<<<<<<` into the file on both computers."""
        self.editor_that("exit 0")
        got = self.ask(one_conflict(), "es")
        self.assertEqual(conflicts.SKIP, got.choice)
        self.assertIn("still has tupferl's conflict markers", self.out.getvalue())

    def test_a_half_finished_resolution_is_what_the_next_edit_opens(self) -> None:
        """The user resolved one hunk of two, saved, and was told so. Pressing
        `[e]` again must reopen *their* file, not the pristine merge -- otherwise
        being told "you are not finished" costs them the work they did.

        The editor appends a line and leaves everything else alone, so the
        second run sees the first run's output exactly when the buffer carried
        over. Two hunks, because with one there is nothing to half-finish.
        """
        landed = self.scratch() / "seen"
        self.editor_that(f'cat "$1" > {landed}\nprintf "mine\\n" >> "$1"')
        sides = sides_for(*many(2))
        got = self.ask(sides, "ee")
        # Both edits were refused -- the markers survive `cat` -- so it skipped.
        self.assertEqual(conflicts.SKIP, got.choice)
        self.assertIn(
            b"mine", landed.read_bytes(), "the second [e] did not reopen the first's work"
        )

    def test_an_editor_that_deletes_the_file_asks_again(self) -> None:
        """Rather than a traceback. `sync` exits 1 for "conflicts were left", so
        a crash that also exits 1 is one a script reads as a normal result."""
        self.editor_that('rm -f "$1"')
        got = self.ask(one_conflict(), "es")
        self.assertEqual(conflicts.SKIP, got.choice)
        self.assertIn("left nothing to read", self.out.getvalue())

    def test_a_file_that_merely_mentions_a_marker_is_accepted(self) -> None:
        """The other half, and the one a bare `<<<<<<<` check would fail: the
        markers looked for carry the file's name and the side's description, so
        a dotfile documenting merge markers still saves."""
        self.editor_that('printf "<<<<<<< HEAD\\nan example\\n>>>>>>> other\\n" > "$1"')
        got = self.ask(one_conflict(), "e")
        self.assertEqual(conflicts.EDIT, got.choice)

    def test_with_no_editor_set_it_says_what_to_set_and_asks_again(self) -> None:
        """It must not end the run. A `sync` aborted here has already written
        the conflicts it settled earlier and committed none of them, which is a
        far worse answer to a mistyped `e` than a line saying what to set."""
        with mock.patch.dict(os.environ, {}, clear=True):
            got = self.ask(one_conflict(), "es")
        self.assertEqual(conflicts.SKIP, got.choice)
        self.assertIn("$EDITOR", self.out.getvalue())


class TestWhichEditor(unittest.TestCase):
    def test_the_config_wins(self) -> None:
        """A setting that loses to an environment variable is one the user
        cannot make stick."""
        with mock.patch.dict(os.environ, {"VISUAL": "vis", "EDITOR": "ed"}):
            self.assertEqual("cfg", conflicts.editor(Config(editor="cfg")))

    def test_visual_beats_editor(self) -> None:
        with mock.patch.dict(os.environ, {"VISUAL": "vis", "EDITOR": "ed"}):
            self.assertEqual("vis", conflicts.editor(Config()))

    def test_editor_is_the_last_answer(self) -> None:
        with mock.patch.dict(os.environ, {"EDITOR": "ed"}, clear=True):
            self.assertEqual("ed", conflicts.editor(Config()))

    def test_nothing_set_is_an_error_that_says_what_to_do(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(TupferlError) as raised:
            conflicts.editor(Config())
        self.assertIn("config.toml", str(raised.exception))


class TestWhichSettler(unittest.TestCase):
    """Plan §3.4's flag set, and the rule for a terminal that is not there."""

    def sides(self) -> conflicts.Sides:
        return one_conflict()

    def test_ours_answers_keep_local_without_asking(self) -> None:
        settler = conflicts.answering(Config(), no_input=False, ours=True, theirs=False)
        self.assertEqual(conflicts.Answer(conflicts.LOCAL), settler(self.sides()))

    def test_theirs_answers_keep_remote_without_asking(self) -> None:
        settler = conflicts.answering(Config(), no_input=False, ours=False, theirs=True)
        self.assertEqual(conflicts.Answer(conflicts.REMOTE), settler(self.sides()))

    def test_no_input_answers_skip(self) -> None:
        settler = conflicts.answering(Config(), no_input=True, ours=False, theirs=False)
        self.assertEqual(conflicts.Answer(conflicts.SKIP), settler(self.sides()))

    def test_a_stdin_that_is_not_a_terminal_is_no_input(self) -> None:
        """Nobody is there to press a key. Blocking for ever and reading EOF as
        a decision are both worse than reporting the conflict."""
        with mock.patch("sys.stdin", io.StringIO("l\n")):
            settler = conflicts.answering(Config(), no_input=False, ours=False, theirs=False)
            self.assertEqual(conflicts.Answer(conflicts.SKIP), settler(self.sides()))

    def test_with_a_terminal_it_is_the_prompt(self) -> None:
        """The half the test above cannot show: with a real terminal the same
        arguments produce a settler that asks, and takes the key typed at it."""
        terminal = support.Terminal()
        self.addCleanup(terminal.close)
        terminal.type("r")
        spill = io.StringIO()
        with mock.patch("sys.stdin", terminal.source), mock.patch("sys.stdout", spill):
            settler = conflicts.answering(Config(), no_input=False, ours=False, theirs=False)
            self.assertEqual(conflicts.Answer(conflicts.REMOTE), settler(self.sides()))
        self.assertIn("1 conflict to settle", spill.getvalue())


#: The same conflict as `one_conflict`, with Windows line endings. git writes
#: **CRLF markers into a CRLF file** -- so every fixture above, being LF, is
#: blind to a whole class of file that a dotfiles repository certainly holds.
CRLF_BASE = BASE.replace(b"\n", b"\r\n")
CRLF_MINE = MINE.replace(b"\n", b"\r\n")
CRLF_THEIRS = THEIRS.replace(b"\n", b"\r\n")


def crlf() -> conflicts.Sides:
    return sides_for(CRLF_BASE, CRLF_MINE, CRLF_THEIRS)


class TestAFileWithWindowsLineEndings(Prompted):
    """The class of file every other fixture here is blind to.

    `split(b"\n")` leaves the `\r` on the end of each line, so a marker arrives
    as `b"<<<<<<< .bashrc (this computer)\r"`. Matching without stripping it made
    `leftover` inert for every CRLF dotfile -- an `[e]` the user quit without
    resolving was accepted, and the markers reached `$HOME`, the repository and
    the snapshot on both machines with `sync` exiting 0.
    """

    def test_the_fixture_really_is_crlf_and_really_conflicts(self) -> None:
        """The precondition, asserted rather than assumed: a fixture git merged
        cleanly, or one whose markers came back LF, would make every assertion
        below vacuous."""
        sides = crlf()
        self.assertEqual(1, sides.conflicts)
        assert sides.marked is not None
        self.assertIn(b"(this computer)\r\n", sides.marked)

    def test_the_two_sides_are_still_found(self) -> None:
        found = conflicts.hunks(crlf())
        self.assertEqual(1, len(found))
        self.assertEqual([b"MINE-IS-HERE\r"], found[0].mine)
        self.assertEqual([b"THEIRS-IS-HERE\r"], found[0].theirs)

    def test_the_prompt_shows_both_sides(self) -> None:
        text = conflicts.describe(crlf(), colour=False)
        self.assertIn("MINE-IS-HERE", text)
        self.assertIn("THEIRS-IS-HERE", text)

    def test_markers_left_in_place_are_still_refused(self) -> None:
        """The one that matters. Without `bare`, this passes `leftover` and the
        markers are written to both computers."""
        self.editor_that("exit 0")
        got = self.ask(crlf(), "es")
        self.assertEqual(conflicts.SKIP, got.choice)
        self.assertIn("still has tupferl's conflict markers", self.out.getvalue())

    def test_a_finished_edit_is_still_accepted(self) -> None:
        """The other half: `bare` must not make every CRLF save look unfinished."""
        self.editor_that('printf "done\\r\\n" > "$1"')
        self.assertEqual(conflicts.EDIT, self.ask(crlf(), "e").choice)


class TestALineThatLooksLikeASeparator(Prompted):
    """git writes `=======` with no label, so a line of the file that *is* seven
    equals signs cannot be told apart from it.

    The first such line inside a region ends this side whether it was meant to
    or not, and the display then shows one side empty and attributes that side's
    lines to the other. A user who believes it presses the other key and
    destroys their own edit -- so "display only" is not a defence, and `describe`
    refuses to show a parse it cannot corroborate.
    """

    def sides(self) -> conflicts.Sides:
        base = b"title\nold\ntail\n"
        mine = b"title\n=======\nMY SECTION\ntail\n"
        theirs = b"title\nTHEIR LINE\ntail\n"
        return sides_for(base, mine, theirs)

    def test_the_parse_really_does_go_wrong(self) -> None:
        """The precondition. If git ever stopped producing this shape the tests
        below would pass against a parse that was fine all along."""
        found = conflicts.hunks(self.sides())
        self.assertEqual([], found[0].mine, "the fixture no longer mis-splits")
        self.assertIn(b"MY SECTION", found[0].theirs)

    def test_it_is_refused_rather_than_shown(self) -> None:
        text = conflicts.describe(self.sides(), colour=False)
        self.assertIn("cannot show the two sides", text)
        self.assertIn("[d]", text)
        self.assertNotIn("MY SECTION", text)

    def test_an_ordinary_conflict_is_still_shown(self) -> None:
        """The other half: a check that refused everything would pass the test
        above and make the prompt useless."""
        self.assertIn("MINE-IS-HERE", conflicts.describe(one_conflict(), colour=False))

    def test_the_full_diff_still_tells_the_truth(self) -> None:
        """`[d]` reads the two files rather than the markers, so it is the way
        out -- which is what the message points at."""
        text = conflicts.unified(self.sides())
        self.assertIn("-MY SECTION", text)
        self.assertIn("+THEIR LINE", text)
