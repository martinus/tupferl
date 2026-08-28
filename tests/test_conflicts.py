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
from typing import ClassVar
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


def blank_before(case: unittest.TestCase, text: str, marker: str) -> None:
    """Assert the line carrying `marker` has an empty line above it.

    Module-level rather than a method, because the two classes that need it are
    on different bases -- and the copy written inline on the second one omitted
    the `at > 0` guard, which makes it pass vacuously when the marker lands on
    the first line: `lines[-1]` is then the last line of the render, which is
    `""` for anything ending in a newline.
    """
    lines = text.split("\n")
    at = next(n for n, line in enumerate(lines) if marker in line)
    case.assertGreater(at, 0, f"{marker!r} is the first line, so nothing precedes it")
    case.assertEqual("", lines[at - 1], f"no blank line before {marker!r}:\n{text}")


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
        # Bounded: `ask` prints into this, and a mutant that loops fills it. See
        # `support.Spill` -- an unbounded one is charged to a mutation lane's
        # memory share and kills the session before any test can report.
        self.out = support.Spill()
        self.config = Config()

    def ask(self, sides: conflicts.Sides, keys: str) -> conflicts.Answer:
        """Type `keys` at the prompt and return what it settled on.

        `support.FALLBACK` is typed after them; see there for why.
        """
        self.terminal.type(keys + support.FALLBACK)
        with support.deadline(support.PATIENCE, f"the prompt never settled on {keys!r}"):
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

    def test_each_part_of_the_prompt_is_separated_by_a_blank_line(self) -> None:
        """Three separators, and none of them is decoration.

        This is the display the user reads before pressing a key that discards
        one side for good, and the sections it runs together are "this computer"
        and "the repository". `=======` already has no label to match on, so the
        prompt's only structure is where the blank lines are -- and dropping
        them turns the header, both sides of every hunk and the "more" line into
        one wall of text that reads as if the last section owned the lines above
        it. Every `assertIn` in this class passes against that wall, which is
        why none of them could see it.
        """
        count = conflicts.SHOWN_HUNKS + 2
        text = conflicts.describe(sides_for(*many(count)), colour=False)
        for marker in ("1 of", "2 of", "2 more"):
            blank_before(self, text, marker)

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

    def key(self) -> str:
        """One keypress, under a deadline.

        Every assertion here is about a read *returning*, so the failure these
        tests must produce is a red test rather than a hang -- see
        `support.deadline`. Without it, `ICANON` left set makes the read wait for
        a newline that never comes, and the harness files that as `BROKE`, which
        is never `caught`.
        """
        with support.deadline(support.PATIENCE, "one_key never returned"):
            return conflicts.one_key(self.terminal.source)

    def test_a_key_is_read_without_waiting_for_enter(self) -> None:
        """Plan §3.4: every choice is one keypress. Nothing but `l` is written,
        so a read that waited for a newline would run past the deadline and fail
        -- which is the assertion, and it cannot be made any other way."""
        self.terminal.type("l")
        self.assertEqual("l", self.key())

    def editing(self) -> int:
        """The two flags whose loss the user would feel, as the driver has them.

        **Not the whole `termios` structure.** Comparing all of it passed on
        Linux and failed on macOS, and the whole-structure claim is the wrong one
        to make: `VMIN` and `VTIME` carry no meaning once `ICANON` is back on, so
        a driver is free to normalise them on the way, and one of them does. The
        exact field is not established here -- macOS cannot be reproduced in this
        container, and inventing the reason would be worse than saying so.

        What the restore actually owes the user is these two bits: `ICANON` back
        on so their shell reads lines again, and `ECHO` back on so they can see
        what they type. A prompt that left either cleared looks like a hung
        terminal, which is the whole reason the `finally` exists.
        """
        return int(termios.tcgetattr(self.terminal.source.fileno())[3]) & (
            termios.ICANON | termios.ECHO
        )

    def test_the_terminal_is_left_as_it_was_found(self) -> None:
        before = self.editing()
        self.terminal.type("l")
        self.key()
        self.assertEqual(before, self.editing())

    def test_the_precondition_holds_that_one_key_really_clears_them(self) -> None:
        """Both restore tests are vacuous unless the flags are cleared in
        between: "unchanged before and after" is trivially true of a function
        that changes nothing. This asserts the middle of the sandwich, by
        reading the flags from inside the read itself."""
        seen: list[int] = []
        self.terminal.type("l")
        real = os.read

        def peek(fd: int, size: int) -> bytes:
            seen.append(self.editing())
            return real(fd, size)

        with mock.patch("os.read", peek):
            self.key()
        self.assertEqual([0], seen, "one_key did not clear ICANON and ECHO while reading")

    def blocking(self) -> tuple[int, int]:
        """`VMIN` and `VTIME`, which are what "one keypress" means to the driver.

        `VMIN = 1, VTIME = 0` is "return as soon as one byte has arrived, and
        wait for ever until it does". Every other pair is a different promise:
        `VMIN = 0` makes the read return empty when nothing has been typed yet,
        and a non-zero `VTIME` puts a deadline on it. Either turns a prompt that
        waits for the user into one that answers for them.
        """
        mode = termios.tcgetattr(self.terminal.source.fileno())
        # `tcgetattr` hands the control-character array back as `bytes` even for
        # the two entries that are counts rather than characters, though
        # `tcsetattr` takes an `int` there -- which is why `one_key` assigns one
        # and this reads the other way round.
        return tuple(  # type: ignore[return-value]
            value[0] if isinstance(value, bytes) else int(value)
            for value in (mode[6][termios.VMIN], mode[6][termios.VTIME])
        )

    def test_the_read_asks_for_one_byte_and_waits_for_it(self) -> None:
        """**The pty is set to the opposite pair first**, and that is the test.

        A fresh pty already comes up at `(1, 0)`, so asserting that from the
        outside is satisfied by a `one_key` that sets nothing at all -- the
        fixture-too-weak-to-tell-the-answers-apart shape, and invisible in the
        assertion's own text. Starting at `(0, 5)` means only an assignment can
        produce `(1, 0)`, and the read is where it has to have happened.
        """
        fd = self.terminal.source.fileno()
        mode = termios.tcgetattr(fd)
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 0, 5
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        self.assertEqual((0, 5), self.blocking(), "the fixture did not take")

        seen: list[tuple[int, int]] = []
        self.terminal.type("l")
        real = os.read

        def peek(fd: int, size: int) -> bytes:
            seen.append(self.blocking())
            return real(fd, size)

        with mock.patch("os.read", peek):
            self.key()
        self.assertEqual([(1, 0)], seen)

    def test_reading_the_rest_of_an_escape_stops_waiting_after_a_tenth_of_a_second(self) -> None:
        """The pair `rest_of_escape` needs, which is the opposite of `one_key`'s.

        `VMIN = 0, VTIME = 1` is "give me whatever has arrived, and wait up to a
        tenth of a second for it" -- the only thing that lets a *lone* Escape
        come back at all. `VMIN = 1` there would block for ever on the byte that
        is never coming, and `VTIME = 0` would make the read return before the
        terminal had delivered the `[B` of a Down arrow, splitting one keypress
        into three answers, the last of which is `b` -- *keep both*.

        The fixture types the whole sequence at once, so both bytes are already
        buffered and every one of those pairs produces the same answer from the
        outside. Reading the driver from inside the second read is the only
        place the difference exists.
        """
        seen: list[tuple[int, int]] = []
        self.terminal.type("\x1b[B")
        real = os.read

        def peek(fd: int, size: int) -> bytes:
            seen.append(self.blocking())
            return real(fd, size)

        with mock.patch("os.read", peek):
            self.key()
        # The count first. Without it `seen[1:]` and `[(0, 1)] * 0` are both
        # empty, so a `one_key` that never reached `rest_of_escape` at all would
        # satisfy the assertion below -- which is the half of this the escape
        # sequence exists to reach.
        self.assertEqual(
            3, len(seen), f"the whole sequence was not read one byte at a time: {seen}"
        )
        self.assertEqual((1, 0), seen[0], "the first byte is one blocking read")
        self.assertEqual([(0, 1), (0, 1)], seen[1:], "the rest is a timed read")

    def test_it_is_restored_even_when_the_read_raises(self) -> None:
        """The case the `finally` exists for, and the one the test above cannot
        see: an interrupt at the prompt must not leave the user's shell with
        `ECHO` off, which looks like a hung terminal.

        Without this, moving `tcsetattr` out of the `finally` into ordinary
        sequence leaves the whole suite green -- a precondition never
        established, which CLAUDE.md §2 lists by name.
        """
        before = self.editing()
        patched = mock.patch("os.read", side_effect=KeyboardInterrupt)
        with patched, self.assertRaises(KeyboardInterrupt):
            conflicts.one_key(self.terminal.source)
        self.assertEqual(before, self.editing())

    def test_an_arrow_key_is_one_keypress_and_not_three(self) -> None:
        """A single press of Down sends `\x1b[B`. Read a byte at a time, that is
        `\x1b`, `[` and `B` to three successive calls -- and `b` is *keep both*,
        so one arrow key, or one notch of a mouse wheel, silently wrote a union
        merge to `$HOME`, the repository and the snapshot with `sync` exiting 0.
        """
        self.terminal.type("\x1b[B")
        self.assertEqual("\x1b[b", self.key())

    def test_nothing_of_the_sequence_is_left_for_the_next_read(self) -> None:
        """The half the test above cannot show: the whole press was consumed, so
        the *next* key is the next key the user pressed. Asserted by typing an
        arrow and then an `l`, which is what a user who scrolled and then
        answered does."""
        self.terminal.type("\x1b[Al")
        self.key()
        self.assertEqual("l", self.key())

    def test_it_is_lower_cased(self) -> None:
        self.terminal.type("L")
        self.assertEqual("l", self.key())

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
        """The one answer that cannot lose something the user meant to keep.

        Under a deadline, because this drives `ask` directly rather than through
        `Prompted.ask` and so gets none of that helper's bounds. Removing the
        guard being tested here makes `ask` loop for ever on an exhausted stream:
        without the deadline the row came back `BROKE` -- the harness's 30s
        per-test alarm, which is not a verdict -- so the very line this test
        exists for was guarded by nothing a sweep could see.
        """
        with support.deadline(support.PATIENCE, "ask never settled at end of input"):
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
        # The whole sentence, and the key in it. "no lines" alone also appears in
        # `describe`'s "there are no lines to take from each side" three lines
        # above, so the old assertion held with this message deleted -- a marker
        # asserted that is also produced by something else, which is the shape
        # CLAUDE.md §2 lists by name. The sweep is what found it.
        self.assertIn("'b' is not one of the keys for a file with no lines", self.out.getvalue())

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
        terminal.type("r" + support.FALLBACK)
        spill = support.Spill()
        patched = mock.patch("sys.stdin", terminal.source), mock.patch("sys.stdout", spill)
        with patched[0], patched[1], support.deadline(support.PATIENCE, "the prompt never settled"):
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

    def test_the_refusal_is_separated_from_the_heading(self) -> None:
        """This branch returns early, so the separator test over the hunk loop
        cannot reach this `append("")`. Run together, the refusal reads as a
        continuation of the "N conflicts to settle" line rather than as the
        reason nothing follows it."""
        blank_before(self, conflicts.describe(self.sides(), colour=False), "cannot show")

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


class TestFindingARunOfLines(unittest.TestCase):
    """`conflicts.somewhere_in`, which `trustworthy` is built on.

    Tested directly because through `trustworthy` only one of its answers is
    ever observable: a whole sweep of it survived every mutation, including
    "return `False` instead of `True`" and an off-by-one in the range.
    """

    WHOLE: ClassVar[list[bytes]] = [b"a", b"b", b"c", b"d"]

    def test_an_empty_run_is_always_there(self) -> None:
        """One side of a conflict legitimately has no lines -- the other added
        them -- so this is the ordinary case, not a degenerate one."""
        self.assertTrue(conflicts.somewhere_in([], self.WHOLE))
        self.assertTrue(conflicts.somewhere_in([], []))

    def test_a_run_at_the_start_middle_and_end(self) -> None:
        """All three, because the range's bounds are what an off-by-one moves:
        `len(whole) - len(run) + 1` dropping the `+ 1` still finds the first two.
        """
        for run in ([b"a", b"b"], [b"b", b"c"], [b"c", b"d"], [b"d"]):
            with self.subTest(run=run):
                self.assertTrue(conflicts.somewhere_in(run, self.WHOLE))

    def test_lines_that_are_present_but_not_consecutive(self) -> None:
        """The property is a *block*. Both lines are in `WHOLE`, which is what a
        check written with `all(line in whole ...)` would accept."""
        self.assertFalse(conflicts.somewhere_in([b"a", b"c"], self.WHOLE))

    def test_a_run_that_is_not_there_at_all(self) -> None:
        self.assertFalse(conflicts.somewhere_in([b"z"], self.WHOLE))

    def test_a_run_longer_than_the_whole(self) -> None:
        """The range is empty here, and a `+ 1` in the wrong place makes it
        index past the end instead."""
        self.assertFalse(conflicts.somewhere_in([*self.WHOLE, b"e"], self.WHOLE))

    def test_the_whole_thing_is_a_run_of_itself(self) -> None:
        self.assertTrue(conflicts.somewhere_in(self.WHOLE, self.WHOLE))


class TestWhenOnlyOneHunkIsUntrustworthy(unittest.TestCase):
    """One bad hunk out of two must condemn the display.

    `all` becoming `any` survived the first sweep: with a single hunk the two
    are the same answer, so a fixture with one conflict cannot tell them apart.
    """

    def sides(self) -> conflicts.Sides:
        pad = "\n".join(f"pad-{step}" for step in range(APART))
        base = f"one\n{pad}\ntwo\n".encode()
        mine = f"MINE\n{pad}\n=======\nMY SECOND\n".encode()
        theirs = f"THEIRS\n{pad}\nTHEIR SECOND\n".encode()
        return sides_for(base, mine, theirs)

    def test_the_fixture_has_two_hunks_and_only_one_is_bad(self) -> None:
        """The precondition. With one hunk, or with both bad, the assertion
        below holds against `any` as well and guards nothing."""
        sides = self.sides()
        regions = conflicts.hunks(sides)
        self.assertEqual(2, len(regions), "the fixture is not two hunks")
        good, bad = regions
        self.assertTrue(conflicts.trustworthy(sides, [good]), "the first hunk is not clean")
        self.assertFalse(conflicts.trustworthy(sides, [bad]), "the second hunk is not broken")

    def test_one_bad_hunk_condemns_the_display(self) -> None:
        self.assertFalse(conflicts.trustworthy(self.sides(), conflicts.hunks(self.sides())))
        self.assertIn("cannot show the two sides", conflicts.describe(self.sides(), colour=False))


class TestWhereTheDisplayStopsCutting(Prompted):
    """The boundaries of `SHOWN_LINES` and `SHOWN_HUNKS`.

    Exactly at the limit, not over it: `left > 0` against `left >= 0`, and `0`
    against `1`, are the same answer for every fixture that overshoots -- which
    is what the first sweep's survivors on those lines were.
    """

    def test_a_side_of_exactly_the_limit_says_nothing_about_more(self) -> None:
        lines = [f"line-{index}".encode() for index in range(conflicts.SHOWN_LINES)]
        sides = sides_for(b"base\n", b"\n".join(lines) + b"\n", b"theirs\n")
        text = conflicts.describe(sides, colour=False)
        self.assertIn(f"line-{conflicts.SHOWN_LINES - 1}", text)
        self.assertNotIn("more line", text)

    def test_one_line_over_the_limit_says_one_more(self) -> None:
        """The singular, which is the other side of the `'s' if left > 1`."""
        lines = [f"line-{index}".encode() for index in range(conflicts.SHOWN_LINES + 1)]
        sides = sides_for(b"base\n", b"\n".join(lines) + b"\n", b"theirs\n")
        self.assertIn("1 more line", conflicts.describe(sides, colour=False))

    def test_exactly_the_shown_hunks_says_nothing_about_more(self) -> None:
        sides = sides_for(*many(conflicts.SHOWN_HUNKS))
        self.assertEqual(conflicts.SHOWN_HUNKS, sides.conflicts, "the fixture is the wrong size")
        text = conflicts.describe(sides, colour=False)
        self.assertIn(f"mine{conflicts.SHOWN_HUNKS - 1}", text)
        self.assertNotIn("and 0 more", text)
        self.assertNotIn("more; press", text)

    def test_one_hunk_over_says_one_more(self) -> None:
        sides = sides_for(*many(conflicts.SHOWN_HUNKS + 1))
        self.assertIn("and 1 more; press [d]", conflicts.describe(sides, colour=False))

    def test_one_conflict_is_said_in_the_singular(self) -> None:
        self.assertIn("1 conflict to settle", conflicts.describe(one_conflict(), colour=False))

    def test_two_conflicts_are_said_in_the_plural(self) -> None:
        self.assertIn(
            "2 conflicts to settle", conflicts.describe(sides_for(*many(2)), colour=False)
        )


class TestWhenColourIsUsed(unittest.TestCase):
    """`conflicts.coloured`, both halves.

    Every other assertion in this file runs against a `StringIO`, which is not a
    terminal, *and* under a sandbox that sets `NO_COLOR` -- so the whole function
    is unobservable there and each of its three mutations survived. A real pty
    is the only way to make `isatty()` true.
    """

    def setUp(self) -> None:
        self.terminal = support.Terminal()
        self.addCleanup(self.terminal.close)
        self.out = os.fdopen(os.dup(self.terminal.master), "w")
        self.addCleanup(self.out.close)

    def test_a_terminal_with_no_no_colour_is_coloured(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(conflicts.coloured(self.out))

    def test_no_colour_turns_it_off_even_on_a_terminal(self) -> None:
        """A user who set it meant it."""
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(conflicts.coloured(self.out))

    def test_a_pipe_is_never_coloured_even_without_no_colour(self) -> None:
        """The other half of the `and`, and the reason it is not an `or`:
        escape codes in a file someone redirected the run into are noise."""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(conflicts.coloured(io.StringIO()))


class TestReadingAnEscapeSequence(unittest.TestCase):
    """`rest_of_escape`'s arms, each of which decides where a keypress ends.

    Every one of them survived the first sweep: `TestOneKeypress` typed a plain
    arrow and nothing else, and one shape of sequence cannot distinguish "stop at
    the final byte" from "stop after two" from "read to `KEYPRESS`".
    """

    def setUp(self) -> None:
        self.terminal = support.Terminal()
        self.addCleanup(self.terminal.close)

    def read(self, typed: str) -> str:
        """One keypress, and never a hang.

        Every test in this class drives code whose whole job is to *stop*
        reading -- the `VMIN`/`VTIME` pair and the loop's two exits. Mutate any
        of them and the read blocks, which the harness reports as `BROKE` rather
        than as caught, so the line ends up guarded by nothing. The deadline is
        what turns each of those into a red test.
        """
        self.terminal.type(typed)
        with support.deadline(support.PATIENCE, f"one_key never returned for {typed!r}"):
            return conflicts.one_key(self.terminal.source)

    def test_a_plain_arrow(self) -> None:
        self.assertEqual("\x1b[a", self.read("\x1b[A"))

    def test_an_application_mode_arrow(self) -> None:
        """`ESC O B` is what a terminal in application cursor mode sends, and it
        is the reason `O` introduces a sequence as well as `[`."""
        self.assertEqual("\x1bob", self.read("\x1bOB"))

    def test_a_modified_arrow_with_parameters(self) -> None:
        """Ctrl-Down is `ESC [ 1 ; 5 B`: four bytes that are not final before the
        one that is. A reader that stopped at the second byte would leave `1;5B`
        for the next four prompts."""
        self.assertEqual("\x1b[1;5b", self.read("\x1b[1;5B"))

    def test_the_key_after_a_modified_arrow_is_still_read(self) -> None:
        self.terminal.type("\x1b[1;5Br")
        conflicts.one_key(self.terminal.source)
        self.assertEqual("r", conflicts.one_key(self.terminal.source))

    def test_escape_followed_by_an_ordinary_key(self) -> None:
        """Neither `[` nor `O`, so the sequence is over at one byte. Alt-l sends
        exactly this, and it must not be read as `[l] keep local`."""
        self.assertEqual("\x1bl", self.read("\x1bl"))

    def test_a_lone_escape_does_not_wait_for_ever(self) -> None:
        """`VMIN=0` with `VTIME` is what makes the read come back empty rather
        than block. With `VMIN` left at 1 this test hangs, which is why the
        assertion is reached at all."""
        self.assertEqual("\x1b", self.read("\x1b"))

    def test_a_sequence_longer_than_a_keypress_stops(self) -> None:
        """`KEYPRESS` is the backstop for a sequence with no final byte -- a
        terminal that goes quiet mid-escape, or a paste. Without the bound the
        loop reads until the `VTIME` timeout on every byte."""
        self.assertEqual(conflicts.KEYPRESS + 1, len(self.read("\x1b[" + "1" * 12)))


class TestWhatTheWeakFixturesMissed(Prompted):
    """Each of these kills a mutant that survived a sweep of this file.

    They are grouped because they share a shape: the original assertion was
    true of *more* than the behaviour it meant to pin, so a mutation that
    changed the behaviour left the assertion holding. CLAUDE.md §2 calls that
    suspecting the fixture before the code, and every one of these was a
    fixture.
    """

    def test_the_first_hunk_is_numbered_one(self) -> None:
        """`enumerate(..., start=1)`. Asserting only "3 of 5" appears is true of
        `start=2` as well, which numbers the shown hunks 2, 3, 4 -- so the old
        assertion held while the user was told the first conflict was the
        second."""
        text = conflicts.describe(sides_for(*many(conflicts.SHOWN_HUNKS + 2)), colour=False)
        self.assertIn(f"1 of {conflicts.SHOWN_HUNKS + 2}", text)

    def test_an_escape_then_a_key_leaves_the_key_behind(self) -> None:
        """`ESC` followed by neither `[` nor `O` ends the sequence at one byte.

        Typing just `\\x1bl` cannot show it: with the check removed the loop reads
        `l`, finds nothing more, and returns the same `\\x1bl`. It takes a *third*
        keypress to tell the two apart -- without the check that one is swallowed
        too, and the prompt then answers with a key the user pressed for the
        question after it.
        """
        self.terminal.type("\x1blr")
        with support.deadline(support.PATIENCE, "one_key never returned"):
            self.assertEqual("\x1bl", conflicts.one_key(self.terminal.source))
            self.assertEqual("r", conflicts.one_key(self.terminal.source))

    def test_a_pipe_gives_up_everything_but_the_first_character(self) -> None:
        """`[:1]`. A line of exactly one character is true of `[:2]` and of no
        slice at all, so the fixture has to type more than one."""
        self.assertEqual("l", conflicts.one_key(io.StringIO("ls\n")))

    def test_a_pipe_line_of_two_keys_is_not_two_keys(self) -> None:
        """The other half: what comes back is one character, so `ask` reads it as
        a key rather than as "not a key"."""
        self.assertEqual(1, len(conflicts.one_key(io.StringIO("ls\n"))))

    def test_the_key_is_echoed_back(self) -> None:
        """`ECHO` is cleared, so the terminal will not do it. Without this line
        the user presses `l` and sees nothing at all happen."""
        self.ask(one_conflict(), "l")
        self.assertIn("\nl\n", self.out.getvalue())

    def test_end_of_input_says_so(self) -> None:
        """Not just that it skips -- that it tells the user why. A sync that
        exits 1 having silently skipped every conflict is one nobody can debug."""
        with support.deadline(support.PATIENCE, "ask never settled at end of input"):
            got = conflicts.ask(one_conflict(), self.config, io.StringIO(""), self.out)
        self.assertEqual(conflicts.SKIP, got.choice)
        self.assertIn("end of input", self.out.getvalue())
