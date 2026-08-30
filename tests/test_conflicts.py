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
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import ClassVar, TextIO
from unittest import mock

import pytest

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


def blank_before(text: str, marker: str) -> None:
    """Assert the line carrying `marker` has an empty line above it.

    Module-level because two classes need it, and because the copy once written
    inline on the second of them omitted the `at > 0` guard -- which makes it
    pass vacuously when the marker lands on the first line: `lines[-1]` is then
    the last line of the render, which is `""` for anything ending in a newline.
    (It used to say "the two classes are on different bases"; neither has a base
    at all now, so that is no longer the reason it is not a method.)

    It used to take the `TestCase` as its first argument, so that it could call
    `assertGreater` on it. Plain `assert` needs no such thing, which is the
    clearest small win in this cluster: the parameter existed only to reach the
    framework.
    """
    lines = text.split("\n")
    at = next(n for n, line in enumerate(lines) if marker in line)
    assert at > 0, f"{marker!r} is the first line, so nothing precedes it"
    assert lines[at - 1] == "", f"no blank line before {marker!r}:\n{text}"


def one_conflict() -> conflicts.Sides:
    return sides_for(BASE, MINE, THEIRS)


def binary() -> conflicts.Sides:
    """A file with a NUL in it that both computers changed -- no lines to take."""
    return sides_for(b"\x00base", b"\x00mine", b"\x00theirs")


class Prompt:
    """A terminal to type into and a buffer to read the prompt out of."""

    def __init__(self, stack: contextlib.ExitStack) -> None:
        self.stack = stack
        self.terminal = stack.enter_context(contextlib.closing(support.Terminal()))
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
            return conflicts.ask(sides, self.terminal.source, self.out)

    def scratch(self) -> Path:
        """A throwaway directory that lives as long as the test.

        `support.tempdir` and never pytest's `tmp_path`: `tmp_path` keeps three
        numbered roots per user under `/tmp/pytest-of-<user>`, and a sweep races
        thousands of probe processes over that numbering.

        The `ExitStack` is what the fixture below yields; it used to be built in
        `setUp` because `TestCase.enterContext` is 3.11 and this project
        supports 3.10. A yield-fixture makes that choice moot.
        """
        return self.stack.enter_context(support.tempdir())

    def editor_that(self, body: str) -> None:
        """Install a real `$EDITOR`: a shell script whose body is `body`.

        **Every one of `paths.ENV_KEYS`'s editor variables is cleared first**,
        not just set. `conflicts.editor` reads `GIT_EDITOR` before `$EDITOR` --
        git's order, and the point of reading git's config at all -- and this
        container happens to export `GIT_EDITOR=true`. Left in place it wins,
        so five tests here ran `true`, which exits 0 and edits nothing, and the
        suite reported the prompt accepting a file nobody had touched. Setting
        one variable is not the same as controlling the answer.
        """
        where = support.fake_editor(self.scratch() / "fake-editor", body)
        self.stack.enter_context(
            mock.patch.dict(
                os.environ,
                {"EDITOR": str(where), "GIT_EDITOR": "", "VISUAL": ""},
            )
        )


@pytest.fixture
def written(terminal: support.Terminal) -> Iterator[TextIO]:
    """A writable stream that really is a terminal -- `isatty()` true, which is
    the whole subject of the class that asks for it."""
    with os.fdopen(os.dup(terminal.master), "w") as stream:
        yield stream


@pytest.fixture
def terminal() -> Iterator[support.Terminal]:
    """A pty, closed on the way out. For the classes that want a terminal and
    none of `Prompt`'s other machinery."""
    with contextlib.closing(support.Terminal()) as made:
        yield made


@pytest.fixture
def prompt() -> Iterator[Prompt]:
    with contextlib.ExitStack() as stack:
        yield Prompt(stack)


class TestParsingTheMarkers:
    """`hunks` reads back what `merge` wrote, and only for display."""

    def test_it_finds_the_two_sides_and_where_they_are(self) -> None:
        found = conflicts.hunks(one_conflict())
        assert len(found) == 1
        hunk = found[0]
        assert hunk.mine == [b"MINE-IS-HERE"]
        assert hunk.theirs == [b"THEIRS-IS-HERE"]
        # Line 3 of the merged file is the `<<<<<<<`, because two lines of
        # agreement come first. Asserted as numbers rather than as "greater than
        # zero": an off-by-one is exactly what this can catch and a range check
        # is exactly what would not.
        assert hunk.start == 3
        assert hunk.end == 7

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
        assert lines[hunk.start - 1].startswith(b"<<<<<<< ")
        assert lines[hunk.end - 1].startswith(b">>>>>>> ")

    def test_a_file_that_merely_contains_markers_is_not_split(self) -> None:
        """A dotfiles repository is one of the few places a file legitimately
        holds conflict markers. Matching the bare `<<<<<<<` would report a
        conflict inside a file nobody disagreed about."""
        decoy = b"<<<<<<< HEAD\n=======\n>>>>>>> other\n"
        sides = sides_for(decoy + BASE, decoy + MINE, decoy + THEIRS)
        assert len(conflicts.hunks(sides)) == 1

    def test_a_binary_file_has_no_hunks(self) -> None:
        assert conflicts.hunks(binary()) == []

    def test_three_conflicts_are_three_hunks(self) -> None:
        """One is not enough: a parser that reset nothing between regions would
        return one hunk holding everything, and pass the test above."""
        sides = sides_for(*many(3))
        assert sides.conflicts == 3, "the fixture did not produce three hunks"
        found = conflicts.hunks(sides)
        assert [hunk.mine for hunk in found] == [[b"mine0"], [b"mine1"], [b"mine2"]]
        assert [h.theirs for h in found] == [[b"theirs0"], [b"theirs1"], [b"theirs2"]]


class TestWhatTheUserSees:
    def test_it_names_the_file_and_shows_both_sides(self) -> None:
        text = conflicts.describe(one_conflict(), colour=False)
        assert ".bashrc" in text
        assert "1 conflict to settle" in text
        assert "MINE-IS-HERE" in text
        assert "THEIRS-IS-HERE" in text
        assert "this computer" in text
        assert "the repository" in text

    def test_the_line_numbers_say_which_file_they_are_of(self) -> None:
        """They are the merged file's, which is the one `[e]` opens. Saying so is
        the difference between a useful number and a wrong one: the numbers of
        the two original files are not recoverable from the markers."""
        assert "of the merged file" in conflicts.describe(one_conflict(), colour=False)

    def test_a_long_conflict_is_cut_and_says_so(self) -> None:
        lines = [f"line-{index}".encode() for index in range(conflicts.SHOWN_LINES + 5)]
        sides = sides_for(b"base\n", b"\n".join(lines) + b"\n", b"theirs\n")
        text = conflicts.describe(sides, colour=False)
        assert f"line-{conflicts.SHOWN_LINES - 1}" in text
        assert f"line-{conflicts.SHOWN_LINES}" not in text
        assert "5 more lines" in text

    def test_many_conflicts_are_cut_and_point_at_the_diff(self) -> None:
        count = conflicts.SHOWN_HUNKS + 2
        sides = sides_for(*many(count))
        assert sides.conflicts == count, "the fixture did not produce five hunks"
        text = conflicts.describe(sides, colour=False)
        assert f"{conflicts.SHOWN_HUNKS} of {count}" in text
        assert "2 more" in text
        assert f"mine{count - 1}" not in text

    def test_each_part_of_the_prompt_is_separated_by_a_blank_line(self) -> None:
        """Three separators, and none of them is decoration.

        This is the display the user reads before pressing a key that discards
        one side for good, and the sections it runs together are "this computer"
        and "the repository". `=======` already has no label to match on, so the
        prompt's only structure is where the blank lines are -- and dropping
        them turns the header, both sides of every hunk and the "more" line into
        one wall of text that reads as if the last section owned the lines above
        it. Every *substring* assertion in this class passes against that wall,
        which is why none of them could see it -- the text is all there, in the
        wrong shape.
        """
        count = conflicts.SHOWN_HUNKS + 2
        text = conflicts.describe(sides_for(*many(count)), colour=False)
        for marker in ("1 of", "2 of", "2 more"):
            blank_before(text, marker)

    def test_a_binary_file_says_there_are_no_lines(self) -> None:
        text = conflicts.describe(binary(), colour=False)
        assert "not a text file" in text
        assert "whole file" in text

    def test_a_binary_file_is_not_offered_the_keys_that_need_lines(self) -> None:
        """`[b]`, `[e]` and `[d]` all mean "work with the lines". Offering them
        and refusing afterwards tells the user after they have decided."""
        keys = conflicts.choices(binary(), colour=False)
        assert "[l]" in keys
        assert "[r]" in keys
        assert "[s]" in keys
        for absent in ("[b]", "[e]", "[d]"):
            assert absent not in keys

    def test_a_text_file_is_offered_all_six(self) -> None:
        keys = conflicts.choices(one_conflict(), colour=False)
        for key in ("[l]", "[r]", "[b]", "[e]", "[d]", "[s]"):
            assert key in keys

    def test_colour_is_added_only_when_it_is_asked_for(self) -> None:
        """Both halves, because the sandbox sets `NO_COLOR` -- so every other
        assertion in this file runs against the uncoloured branch, and a
        `paint` that ignored its argument would pass all of them."""
        assert "\033[" in conflicts.describe(one_conflict(), colour=True)
        assert "\033[" not in conflicts.describe(one_conflict(), colour=False)

    def test_a_file_that_is_not_utf8_still_prints(self) -> None:
        """A managed file is bytes. Raising here would be raising on exactly the
        file the user most needs to look at."""
        sides = sides_for(b"base\n", b"\xff\xfe mine\n", b"\xfe\xff theirs\n")
        assert "�" in conflicts.describe(sides, colour=False)


class TestTheFullDiff:
    def test_it_compares_the_two_computers(self) -> None:
        text = conflicts.unified(one_conflict())
        assert "-MINE-IS-HERE" in text
        assert "+THEIRS-IS-HERE" in text

    def test_it_names_the_two_sides_the_way_the_prompt_does(self) -> None:
        """The same labels as the conflict markers, so the diff and the prompt
        cannot describe the same two sides differently."""
        text = conflicts.unified(one_conflict())
        mine_at, _, theirs_at = merge.labels_for(str(NAME))
        assert f"--- {mine_at}" in text
        assert f"+++ {theirs_at}" in text

    def test_it_is_not_a_diff_against_the_merge_base(self) -> None:
        """The question at the prompt is which of the two computers to keep, and
        a diff against a third version answers a different one. `alpha` is only
        in the base, so its absence is what tells the two apart."""
        assert "alpha" not in conflicts.unified(one_conflict())


class TestOneKeypress:
    def key(self, terminal: support.Terminal) -> str:
        """One keypress, under a deadline.

        Every assertion here is about a read *returning*, so the failure these
        tests must produce is a red test rather than a hang -- see
        `support.deadline`. Without it, `ICANON` left set makes the read wait for
        a newline that never comes, and the harness files that as `BROKE`, which
        is never `caught`.
        """
        with support.deadline(support.PATIENCE, "one_key never returned"):
            return conflicts.one_key(terminal.source)

    def test_a_key_is_read_without_waiting_for_enter(self, terminal: support.Terminal) -> None:
        """Plan §3.4: every choice is one keypress. Nothing but `l` is written,
        so a read that waited for a newline would run past the deadline and fail
        -- which is the assertion, and it cannot be made any other way."""
        terminal.type("l")
        assert self.key(terminal) == "l"

    def editing(self, terminal: support.Terminal) -> int:
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
        return int(termios.tcgetattr(terminal.source.fileno())[3]) & (termios.ICANON | termios.ECHO)

    def test_the_terminal_is_left_as_it_was_found(self, terminal: support.Terminal) -> None:
        before = self.editing(terminal)
        terminal.type("l")
        self.key(terminal)
        assert self.editing(terminal) == before

    def test_the_precondition_holds_that_one_key_really_clears_them(
        self, terminal: support.Terminal
    ) -> None:
        """Both restore tests are vacuous unless the flags are cleared in
        between: "unchanged before and after" is trivially true of a function
        that changes nothing. This asserts the middle of the sandwich, by
        reading the flags from inside the read itself."""
        seen: list[int] = []
        terminal.type("l")
        real = os.read

        def peek(fd: int, size: int) -> bytes:
            seen.append(self.editing(terminal))
            return real(fd, size)

        with mock.patch("os.read", peek):
            self.key(terminal)
        assert seen == [0], "one_key did not clear ICANON and ECHO while reading"

    def blocking(self, terminal: support.Terminal) -> tuple[int, int]:
        """`VMIN` and `VTIME`, which are what "one keypress" means to the driver.

        `VMIN = 1, VTIME = 0` is "return as soon as one byte has arrived, and
        wait for ever until it does". Every other pair is a different promise:
        `VMIN = 0` makes the read return empty when nothing has been typed yet,
        and a non-zero `VTIME` puts a deadline on it. Either turns a prompt that
        waits for the user into one that answers for them.
        """
        mode = termios.tcgetattr(terminal.source.fileno())
        # `tcgetattr` hands the control-character array back as `bytes` even for
        # the two entries that are counts rather than characters, though
        # `tcsetattr` takes an `int` there -- which is why `one_key` assigns one
        # and this reads the other way round.
        return tuple(  # type: ignore[return-value]
            value[0] if isinstance(value, bytes) else int(value)
            for value in (mode[6][termios.VMIN], mode[6][termios.VTIME])
        )

    def test_the_read_asks_for_one_byte_and_waits_for_it(self, terminal: support.Terminal) -> None:
        """**The pty is set to the opposite pair first**, and that is the test.

        A fresh pty already comes up at `(1, 0)`, so asserting that from the
        outside is satisfied by a `one_key` that sets nothing at all -- the
        fixture-too-weak-to-tell-the-answers-apart shape, and invisible in the
        assertion's own text. Starting at `(0, 5)` means only an assignment can
        produce `(1, 0)`, and the read is where it has to have happened.
        """
        fd = terminal.source.fileno()
        mode = termios.tcgetattr(fd)
        mode[6][termios.VMIN], mode[6][termios.VTIME] = 0, 5
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        assert self.blocking(terminal) == (0, 5), "the fixture did not take"

        seen: list[tuple[int, int]] = []
        terminal.type("l")
        real = os.read

        def peek(fd: int, size: int) -> bytes:
            seen.append(self.blocking(terminal))
            return real(fd, size)

        with mock.patch("os.read", peek):
            self.key(terminal)
        assert seen == [(1, 0)]

    def test_reading_the_rest_of_an_escape_stops_waiting_after_a_tenth_of_a_second(
        self, terminal: support.Terminal
    ) -> None:
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
        terminal.type("\x1b[B")
        real = os.read

        def peek(fd: int, size: int) -> bytes:
            seen.append(self.blocking(terminal))
            return real(fd, size)

        with mock.patch("os.read", peek):
            self.key(terminal)
        # The count first. Without it `seen[1:]` and `[(0, 1)] * 0` are both
        # empty, so a `one_key` that never reached `rest_of_escape` at all would
        # satisfy the assertion below -- which is the half of this the escape
        # sequence exists to reach.
        assert len(seen) == 3, f"the whole sequence was not read one byte at a time: {seen}"
        assert seen[0] == (1, 0), "the first byte is one blocking read"
        assert seen[1:] == [(0, 1), (0, 1)], "the rest is a timed read"

    def test_it_is_restored_even_when_the_read_raises(self, terminal: support.Terminal) -> None:
        """The case the `finally` exists for, and the one the test above cannot
        see: an interrupt at the prompt must not leave the user's shell with
        `ECHO` off, which looks like a hung terminal.

        Without this, moving `tcsetattr` out of the `finally` into ordinary
        sequence leaves the whole suite green -- a precondition never
        established, which CLAUDE.md §2 lists by name.
        """
        before = self.editing(terminal)
        patched = mock.patch("os.read", side_effect=KeyboardInterrupt)
        with patched, pytest.raises(KeyboardInterrupt):
            conflicts.one_key(terminal.source)
        assert self.editing(terminal) == before

    def test_an_arrow_key_is_one_keypress_and_not_three(self, terminal: support.Terminal) -> None:
        """A single press of Down sends `\x1b[B`. Read a byte at a time, that is
        `\x1b`, `[` and `B` to three successive calls -- and `b` is *keep both*,
        so one arrow key, or one notch of a mouse wheel, silently wrote a union
        merge to `$HOME`, the repository and the snapshot with `sync` exiting 0.
        """
        terminal.type("\x1b[B")
        assert self.key(terminal) == "\x1b[b"

    def test_nothing_of_the_sequence_is_left_for_the_next_read(
        self, terminal: support.Terminal
    ) -> None:
        """The half the test above cannot show: the whole press was consumed, so
        the *next* key is the next key the user pressed. Asserted by typing an
        arrow and then an `l`, which is what a user who scrolled and then
        answered does."""
        terminal.type("\x1b[Al")
        self.key(terminal)
        assert self.key(terminal) == "l"

    def test_it_is_lower_cased(self, terminal: support.Terminal) -> None:
        terminal.type("L")
        assert self.key(terminal) == "l"

    def test_a_pipe_is_read_a_line_at_a_time(self) -> None:
        """The other branch, and it is not a test affordance: `sync` with its
        input redirected takes it."""
        assert conflicts.one_key(io.StringIO("r\n")) == "r"

    def test_end_of_input_is_the_empty_string(self) -> None:
        assert conflicts.one_key(io.StringIO("")) == ""


class TestAKeypressThatIsNotAKey:
    def test_an_arrow_key_does_not_answer_the_prompt(self, prompt: Prompt) -> None:
        """It re-asks, and it does not act on the last byte of the sequence."""
        got = prompt.ask(one_conflict(), "\x1b[B")
        assert got.choice == conflicts.SKIP
        assert "is not a key" in prompt.out.getvalue()

    def test_the_escape_is_not_echoed_raw(self, prompt: Prompt) -> None:
        """Printing the bytes back would move the cursor or clear the screen on
        their way out, which is what a `repr` avoids."""
        prompt.ask(one_conflict(), "\x1b[B")
        assert "\x1b[B" not in prompt.out.getvalue()


class TestTheKeys:
    def test_l_keeps_this_computer(self, prompt: Prompt) -> None:
        assert prompt.ask(one_conflict(), "l") == conflicts.Answer(conflicts.LOCAL)

    def test_r_keeps_the_repository(self, prompt: Prompt) -> None:
        assert prompt.ask(one_conflict(), "r") == conflicts.Answer(conflicts.REMOTE)

    def test_s_skips(self, prompt: Prompt) -> None:
        assert prompt.ask(one_conflict(), "s") == conflicts.Answer(conflicts.SKIP)

    def test_end_of_input_skips(self, prompt: Prompt) -> None:
        """The one answer that cannot lose something the user meant to keep.

        Under a deadline, because this drives `ask` directly rather than through
        `Prompted.ask` and so gets none of that helper's bounds. Removing the
        guard being tested here makes `ask` loop for ever on an exhausted stream:
        without the deadline the row came back `BROKE` -- the harness's 30s
        per-test alarm, which is not a verdict -- so the very line this test
        exists for was guarded by nothing a sweep could see.
        """
        with support.deadline(support.PATIENCE, "ask never settled at end of input"):
            got = conflicts.ask(one_conflict(), io.StringIO(""), prompt.out)
        assert got == conflicts.Answer(conflicts.SKIP)

    def test_b_keeps_both_sides_in_turn(self, prompt: Prompt) -> None:
        got = prompt.ask(one_conflict(), "b")
        assert got.choice == conflicts.BOTH
        assert got.data is not None
        assert got.data == (
            b"keep-one\nkeep-two\nMINE-IS-HERE\nTHEIRS-IS-HERE\nkeep-three\nkeep-four\n"
        )

    def test_b_leaves_no_markers_behind(self, prompt: Prompt) -> None:
        """Stated separately from the bytes above, because it is the property
        that matters: a union merge that kept a marker would put `<<<<<<<` into
        the user's `.bashrc` on both computers."""
        got = prompt.ask(one_conflict(), "b")
        assert got.data is not None
        assert b"<<<<<<<" not in got.data
        assert b">>>>>>>" not in got.data

    def test_d_shows_the_diff_and_asks_again(self, prompt: Prompt) -> None:
        got = prompt.ask(one_conflict(), "ds")
        assert got.choice == conflicts.SKIP
        assert "-MINE-IS-HERE" in prompt.out.getvalue()
        # Asked twice, which is what "and asks again" means.
        assert prompt.out.getvalue().count("1 conflict to settle") == 2

    def test_a_key_that_is_not_on_offer_asks_again(self, prompt: Prompt) -> None:
        got = prompt.ask(one_conflict(), "zs")
        assert got.choice == conflicts.SKIP
        assert "not one of the keys" in prompt.out.getvalue()

    def test_a_binary_file_refuses_the_keys_it_did_not_offer(self, prompt: Prompt) -> None:
        got = prompt.ask(binary(), "bs")
        assert got.choice == conflicts.SKIP
        # The whole sentence, and the key in it. "no lines" alone also appears in
        # `describe`'s "there are no lines to take from each side" three lines
        # above, so the old assertion held with this message deleted -- a marker
        # asserted that is also produced by something else, which is the shape
        # CLAUDE.md §2 lists by name. The sweep is what found it.
        assert "'b' is not one of the keys for a file with no lines" in prompt.out.getvalue()

    def test_a_binary_file_still_takes_a_side(self, prompt: Prompt) -> None:
        assert prompt.ask(binary(), "l").choice == conflicts.LOCAL


class TestTheEditorHandoff:
    def test_e_returns_what_the_editor_saved(self, prompt: Prompt) -> None:
        prompt.editor_that('printf "settled by hand\\n" > "$1"')
        got = prompt.ask(one_conflict(), "e")
        assert got == conflicts.Answer(conflicts.EDIT, b"settled by hand\n")

    def test_the_editor_opens_the_merged_file_with_its_markers(self, prompt: Prompt) -> None:
        """What `[e]` is for. The editor here copies its argument out so the
        test can look at what was handed over."""
        landed = prompt.scratch() / "seen"
        # Copies what it was given out for the test to look at, *and* settles
        # the file -- an editor that saved the markers back is
        # `test_markers_left_in_place_are_refused`, which is a different test.
        prompt.editor_that(f'cat "$1" > {landed}\nprintf "done\\n" > "$1"')
        assert prompt.ask(one_conflict(), "e").choice == conflicts.EDIT
        text = landed.read_bytes()
        assert b"<<<<<<< .bashrc (this computer)" in text
        assert b"MINE-IS-HERE" in text
        assert b"THEIRS-IS-HERE" in text

    def test_the_file_is_named_after_the_managed_one(self, prompt: Prompt) -> None:
        """An editor chooses its syntax mode from the file name, and a user
        editing `init.lua` in a file called `tmp9k2j` gets none."""
        landed = prompt.scratch() / "seen"
        prompt.editor_that(f'basename "$1" > {landed}\nprintf "done\\n" > "$1"')
        assert prompt.ask(one_conflict(), "e").choice == conflicts.EDIT
        assert landed.read_text(encoding="utf-8").strip() == ".bashrc"

    def test_an_editor_that_fails_asks_again(self, prompt: Prompt) -> None:
        prompt.editor_that("exit 3")
        got = prompt.ask(one_conflict(), "es")
        assert got.choice == conflicts.SKIP
        assert "exited with 3" in prompt.out.getvalue()

    def test_markers_left_in_place_are_refused(self, prompt: Prompt) -> None:
        """An editor that changes nothing leaves tupferl's own markers behind.
        Accepting that would put `<<<<<<<` into the file on both computers."""
        prompt.editor_that("exit 0")
        got = prompt.ask(one_conflict(), "es")
        assert got.choice == conflicts.SKIP
        assert "still has tupferl's conflict markers" in prompt.out.getvalue()

    def test_a_half_finished_resolution_is_what_the_next_edit_opens(self, prompt: Prompt) -> None:
        """The user resolved one hunk of two, saved, and was told so. Pressing
        `[e]` again must reopen *their* file, not the pristine merge -- otherwise
        being told "you are not finished" costs them the work they did.

        The editor appends a line and leaves everything else alone, so the
        second run sees the first run's output exactly when the buffer carried
        over. Two hunks, because with one there is nothing to half-finish.
        """
        landed = prompt.scratch() / "seen"
        prompt.editor_that(f'cat "$1" > {landed}\nprintf "mine\\n" >> "$1"')
        sides = sides_for(*many(2))
        got = prompt.ask(sides, "ee")
        # Both edits were refused -- the markers survive `cat` -- so it skipped.
        assert got.choice == conflicts.SKIP
        assert b"mine" in landed.read_bytes(), "the second [e] did not reopen the first's work"

    def test_an_editor_that_deletes_the_file_asks_again(self, prompt: Prompt) -> None:
        """Rather than a traceback. `sync` exits 1 for "conflicts were left", so
        a crash that also exits 1 is one a script reads as a normal result."""
        prompt.editor_that('rm -f "$1"')
        got = prompt.ask(one_conflict(), "es")
        assert got.choice == conflicts.SKIP
        assert "left nothing to read" in prompt.out.getvalue()

    def test_a_file_that_merely_mentions_a_marker_is_accepted(self, prompt: Prompt) -> None:
        """The other half, and the one a bare `<<<<<<<` check would fail: the
        markers looked for carry the file's name and the side's description, so
        a dotfile documenting merge markers still saves."""
        prompt.editor_that('printf "<<<<<<< HEAD\\nan example\\n>>>>>>> other\\n" > "$1"')
        got = prompt.ask(one_conflict(), "e")
        assert got.choice == conflicts.EDIT

    def test_with_no_editor_set_it_says_what_to_set_and_asks_again(self, prompt: Prompt) -> None:
        """It must not end the run. A `sync` aborted here has already written
        the conflicts it settled earlier and committed none of them, which is a
        far worse answer to a mistyped `e` than a line saying what to set."""
        with mock.patch.dict(os.environ, {}, clear=True):
            got = prompt.ask(one_conflict(), "es")
        assert got.choice == conflicts.SKIP
        assert "$EDITOR" in prompt.out.getvalue()


class TestWhichEditor:
    """The order, which is now the config, then git's, then the shell's.

    **`clear=True` on every one of these**, not just the two that had it. A
    precedence test has to control *every* source or it is only testing the ones
    it happens to name: this container exports `GIT_EDITOR=true`, and the two
    tests written without it went on passing until `GIT_EDITOR` became a source
    -- at which point they asserted the ambient value, not the fixture's.
    """

    def only(self, **environment: str) -> dict[str, str]:
        return environment

    def test_git_editor_beats_the_shells_variables(self) -> None:
        """git's own first source, and it outranks `$VISUAL` for the reason the
        whole chain exists: an editor configured for git is an editor."""
        with mock.patch.dict(
            os.environ, self.only(GIT_EDITOR="git", VISUAL="vis", EDITOR="ed"), clear=True
        ):
            assert conflicts.editor() == "git"

    def test_visual_beats_editor(self) -> None:
        with mock.patch.dict(os.environ, self.only(VISUAL="vis", EDITOR="ed"), clear=True):
            assert conflicts.editor() == "vis"

    def test_editor_is_the_last_answer(self) -> None:
        with mock.patch.dict(os.environ, self.only(EDITOR="ed"), clear=True):
            assert conflicts.editor() == "ed"

    def test_nothing_set_is_an_error_that_names_both_ways_in(self) -> None:
        """Two ways now, not three: `.tupferl/config.toml` had an `editor` and
        it was removed, because that file is shared and an editor is not. The
        message must not go on offering a setting that no longer exists."""
        with mock.patch.dict(os.environ, {}, clear=True), pytest.raises(TupferlError) as raised:
            conflicts.editor()
        said = str(raised.value)
        assert "$EDITOR" in said
        assert "core.editor" in said
        assert "config.toml" not in said


@dataclass(frozen=True)
class Configured(support.Sandbox):
    """A repository whose `core.editor` can be set."""

    repo: Path

    def set(self, command: str) -> None:
        support.git(["config", "core.editor", command], self.repo, self.env)


@pytest.fixture
def configured(sandbox: support.Sandbox) -> Configured:
    return Configured(**vars(sandbox), repo=support.make_repo(sandbox.home / "r", sandbox.env))


@pytest.mark.usefixtures("configured")
class TestReadingGitsConfiguredEditor:
    """`core.editor`, which needs a real repository to be set in.

    Separate from `TestWhichEditor` above, which is about the *order* and needs
    no repository -- and that is why `editor` takes the repository as an
    optional argument. Driven through a real `git config`, per plan §7.1: the
    value has to come back through the same reader a user's `~/.gitconfig` goes
    through, or this asserts a parser nobody uses.
    """

    def test_core_editor_is_used_when_nothing_more_specific_is_set(
        self, configured: Configured
    ) -> None:
        configured.set("nvim -f")
        with mock.patch.dict(os.environ, {}, clear=True):
            assert conflicts.editor(configured.repo) == "nvim -f"

    def test_git_editor_still_wins_over_the_file(self, configured: Configured) -> None:
        """git's own order among its own sources."""
        configured.set("nvim")
        with mock.patch.dict(os.environ, {"GIT_EDITOR": "git"}, clear=True):
            assert conflicts.editor(configured.repo) == "git"

    def test_core_editor_beats_the_shells_variables(self, configured: Configured) -> None:
        """The other half of the placement: without it, a machine that set
        `core.editor` and has an ancient `$EDITOR` exported by its shell profile
        would get the ancient one, which is the surprise this whole change
        exists to remove."""
        configured.set("nvim")
        with mock.patch.dict(os.environ, {"VISUAL": "vis", "EDITOR": "ed"}, clear=True):
            assert conflicts.editor(configured.repo) == "nvim"

    def test_without_a_repository_git_is_not_asked_at_all(self, configured: Configured) -> None:
        """`repo=None` must not mean "ask git about wherever we are standing".

        The fixture is the whole test: the process is *inside* a repository that
        sets `core.editor`, and the call passes no repository. Without the
        guard, `git config` runs with `cwd=None` -- the current directory -- and
        answers from a repository the caller never named, so where the user
        happened to run tupferl from would decide their editor.

        A bare `repo=None` call in a directory with no git config passes either
        way, which is what the first version of this test did.
        """
        configured.set("from-the-cwd")
        here = Path.cwd()
        os.chdir(configured.repo)
        try:
            with mock.patch.dict(os.environ, {"EDITOR": "ed"}, clear=True):
                assert conflicts.editor() == "ed"
        finally:
            os.chdir(here)


class TestWhichSettler:
    """Plan §3.4's flag set, and the rule for a terminal that is not there."""

    def test_ours_answers_keep_local_without_asking(self) -> None:
        settler = conflicts.answering(no_input=False, ours=True, theirs=False)
        assert settler(one_conflict()) == conflicts.Answer(conflicts.LOCAL)

    def test_theirs_answers_keep_remote_without_asking(self) -> None:
        settler = conflicts.answering(no_input=False, ours=False, theirs=True)
        assert settler(one_conflict()) == conflicts.Answer(conflicts.REMOTE)

    def test_no_input_answers_skip(self) -> None:
        settler = conflicts.answering(no_input=True, ours=False, theirs=False)
        assert settler(one_conflict()) == conflicts.Answer(conflicts.SKIP)

    def test_a_stdin_that_is_not_a_terminal_is_no_input(self) -> None:
        """Nobody is there to press a key. Blocking for ever and reading EOF as
        a decision are both worse than reporting the conflict."""
        with mock.patch("sys.stdin", io.StringIO("l\n")):
            settler = conflicts.answering(no_input=False, ours=False, theirs=False)
            assert settler(one_conflict()) == conflicts.Answer(conflicts.SKIP)

    def test_with_a_terminal_it_is_the_prompt(self, terminal: support.Terminal) -> None:
        """The half the test above cannot show: with a real terminal the same
        arguments produce a settler that asks, and takes the key typed at it."""
        terminal.type("r" + support.FALLBACK)
        spill = support.Spill()
        patched = mock.patch("sys.stdin", terminal.source), mock.patch("sys.stdout", spill)
        with patched[0], patched[1], support.deadline(support.PATIENCE, "the prompt never settled"):
            settler = conflicts.answering(no_input=False, ours=False, theirs=False)
            assert settler(one_conflict()) == conflicts.Answer(conflicts.REMOTE)
        assert "1 conflict to settle" in spill.getvalue()


#: The same conflict as `one_conflict`, with Windows line endings. git writes
#: **CRLF markers into a CRLF file** -- so every fixture above, being LF, is
#: blind to a whole class of file that a dotfiles repository certainly holds.
CRLF_BASE = BASE.replace(b"\n", b"\r\n")
CRLF_MINE = MINE.replace(b"\n", b"\r\n")
CRLF_THEIRS = THEIRS.replace(b"\n", b"\r\n")


def crlf() -> conflicts.Sides:
    return sides_for(CRLF_BASE, CRLF_MINE, CRLF_THEIRS)


class TestAFileWithWindowsLineEndings:
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
        assert sides.conflicts == 1
        assert sides.marked is not None
        assert b"(this computer)\r\n" in sides.marked

    def test_the_two_sides_are_still_found(self) -> None:
        found = conflicts.hunks(crlf())
        assert len(found) == 1
        assert found[0].mine == [b"MINE-IS-HERE\r"]
        assert found[0].theirs == [b"THEIRS-IS-HERE\r"]

    def test_the_prompt_shows_both_sides(self) -> None:
        text = conflicts.describe(crlf(), colour=False)
        assert "MINE-IS-HERE" in text
        assert "THEIRS-IS-HERE" in text

    def test_markers_left_in_place_are_still_refused(self, prompt: Prompt) -> None:
        """The one that matters. Without `bare`, this passes `leftover` and the
        markers are written to both computers."""
        prompt.editor_that("exit 0")
        got = prompt.ask(crlf(), "es")
        assert got.choice == conflicts.SKIP
        assert "still has tupferl's conflict markers" in prompt.out.getvalue()

    def test_a_finished_edit_is_still_accepted(self, prompt: Prompt) -> None:
        """The other half: `bare` must not make every CRLF save look unfinished."""
        prompt.editor_that('printf "done\\r\\n" > "$1"')
        assert prompt.ask(crlf(), "e").choice == conflicts.EDIT


class TestALineThatLooksLikeASeparator:
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
        assert found[0].mine == [], "the fixture no longer mis-splits"
        assert b"MY SECTION" in found[0].theirs

    def test_it_is_refused_rather_than_shown(self) -> None:
        text = conflicts.describe(self.sides(), colour=False)
        assert "cannot show the two sides" in text
        assert "[d]" in text
        assert "MY SECTION" not in text

    def test_the_refusal_is_separated_from_the_heading(self) -> None:
        """This branch returns early, so the separator test over the hunk loop
        cannot reach this `append("")`. Run together, the refusal reads as a
        continuation of the "N conflicts to settle" line rather than as the
        reason nothing follows it."""
        blank_before(conflicts.describe(self.sides(), colour=False), "cannot show")

    def test_an_ordinary_conflict_is_still_shown(self) -> None:
        """The other half: a check that refused everything would pass the test
        above and make the prompt useless."""
        assert "MINE-IS-HERE" in conflicts.describe(one_conflict(), colour=False)

    def test_the_full_diff_still_tells_the_truth(self) -> None:
        """`[d]` reads the two files rather than the markers, so it is the way
        out -- which is what the message points at."""
        text = conflicts.unified(self.sides())
        assert "-MY SECTION" in text
        assert "+THEIR LINE" in text


class TestFindingARunOfLines:
    """`conflicts.somewhere_in`, which `trustworthy` is built on.

    Tested directly because through `trustworthy` only one of its answers is
    ever observable: a whole sweep of it survived every mutation, including
    "return `False` instead of `True`" and an off-by-one in the range.
    """

    WHOLE: ClassVar[list[bytes]] = [b"a", b"b", b"c", b"d"]

    def test_an_empty_run_is_always_there(self) -> None:
        """One side of a conflict legitimately has no lines -- the other added
        them -- so this is the ordinary case, not a degenerate one."""
        assert conflicts.somewhere_in([], self.WHOLE)
        assert conflicts.somewhere_in([], [])

    @pytest.mark.parametrize("run", [[b"a", b"b"], [b"b", b"c"], [b"c", b"d"], [b"d"]])
    def test_a_run_at_the_start_middle_and_end(self, run: list[bytes]) -> None:
        """All three, because the range's bounds are what an off-by-one moves:
        `len(whole) - len(run) + 1` dropping the `+ 1` still finds the first two.
        """
        assert conflicts.somewhere_in(run, self.WHOLE)

    def test_lines_that_are_present_but_not_consecutive(self) -> None:
        """The property is a *block*. Both lines are in `WHOLE`, which is what a
        check written with `all(line in whole ...)` would accept."""
        assert not conflicts.somewhere_in([b"a", b"c"], self.WHOLE)

    def test_a_run_that_is_not_there_at_all(self) -> None:
        assert not conflicts.somewhere_in([b"z"], self.WHOLE)

    def test_a_run_longer_than_the_whole(self) -> None:
        """The range is empty here, and a `+ 1` in the wrong place makes it
        index past the end instead."""
        assert not conflicts.somewhere_in([*self.WHOLE, b"e"], self.WHOLE)

    def test_the_whole_thing_is_a_run_of_itself(self) -> None:
        assert conflicts.somewhere_in(self.WHOLE, self.WHOLE)


class TestWhenOnlyOneHunkIsUntrustworthy:
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
        assert len(regions) == 2, "the fixture is not two hunks"
        good, bad = regions
        assert conflicts.trustworthy(sides, [good]), "the first hunk is not clean"
        assert not conflicts.trustworthy(sides, [bad]), "the second hunk is not broken"

    def test_one_bad_hunk_condemns_the_display(self) -> None:
        assert not conflicts.trustworthy(self.sides(), conflicts.hunks(self.sides()))
        assert "cannot show the two sides" in conflicts.describe(self.sides(), colour=False)


class TestWhereTheDisplayStopsCutting:
    """The boundaries of `SHOWN_LINES` and `SHOWN_HUNKS`.

    Exactly at the limit, not over it: `left > 0` against `left >= 0`, and `0`
    against `1`, are the same answer for every fixture that overshoots -- which
    is what the first sweep's survivors on those lines were.
    """

    def test_a_side_of_exactly_the_limit_says_nothing_about_more(self) -> None:
        lines = [f"line-{index}".encode() for index in range(conflicts.SHOWN_LINES)]
        sides = sides_for(b"base\n", b"\n".join(lines) + b"\n", b"theirs\n")
        text = conflicts.describe(sides, colour=False)
        assert f"line-{conflicts.SHOWN_LINES - 1}" in text
        assert "more line" not in text

    def test_one_line_over_the_limit_says_one_more(self) -> None:
        """The singular, which is the other side of the `'s' if left > 1`."""
        lines = [f"line-{index}".encode() for index in range(conflicts.SHOWN_LINES + 1)]
        sides = sides_for(b"base\n", b"\n".join(lines) + b"\n", b"theirs\n")
        assert "1 more line" in conflicts.describe(sides, colour=False)

    def test_exactly_the_shown_hunks_says_nothing_about_more(self) -> None:
        sides = sides_for(*many(conflicts.SHOWN_HUNKS))
        assert sides.conflicts == conflicts.SHOWN_HUNKS, "the fixture is the wrong size"
        text = conflicts.describe(sides, colour=False)
        assert f"mine{conflicts.SHOWN_HUNKS - 1}" in text
        assert "and 0 more" not in text
        assert "more; press" not in text

    def test_one_hunk_over_says_one_more(self) -> None:
        sides = sides_for(*many(conflicts.SHOWN_HUNKS + 1))
        assert "and 1 more; press [d]" in conflicts.describe(sides, colour=False)

    def test_one_conflict_is_said_in_the_singular(self) -> None:
        assert "1 conflict to settle" in conflicts.describe(one_conflict(), colour=False)

    def test_two_conflicts_are_said_in_the_plural(self) -> None:
        assert "2 conflicts to settle" in conflicts.describe(sides_for(*many(2)), colour=False)


class TestWhenColourIsUsed:
    """`conflicts.coloured`, both halves.

    Every other assertion in this file runs against a `StringIO`, which is not a
    terminal, *and* under a sandbox that sets `NO_COLOR` -- so the whole function
    is unobservable there and each of its three mutations survived. A real pty
    is the only way to make `isatty()` true.
    """

    def test_a_terminal_with_no_no_colour_is_coloured(self, written: TextIO) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert conflicts.coloured(written)

    def test_no_colour_turns_it_off_even_on_a_terminal(self, written: TextIO) -> None:
        """A user who set it meant it."""
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            assert not conflicts.coloured(written)

    def test_a_pipe_is_never_coloured_even_without_no_colour(self) -> None:
        """The other half of the `and`, and the reason it is not an `or`:
        escape codes in a file someone redirected the run into are noise."""
        with mock.patch.dict(os.environ, {}, clear=True):
            assert not conflicts.coloured(io.StringIO())


class TestReadingAnEscapeSequence:
    """`rest_of_escape`'s arms, each of which decides where a keypress ends.

    Every one of them survived the first sweep: `TestOneKeypress` typed a plain
    arrow and nothing else, and one shape of sequence cannot distinguish "stop at
    the final byte" from "stop after two" from "read to `KEYPRESS`".
    """

    def read(self, terminal: support.Terminal, typed: str) -> str:
        """One keypress, and never a hang.

        Every test in this class drives code whose whole job is to *stop*
        reading -- the `VMIN`/`VTIME` pair and the loop's two exits. Mutate any
        of them and the read blocks, which the harness reports as `BROKE` rather
        than as caught, so the line ends up guarded by nothing. The deadline is
        what turns each of those into a red test.
        """
        terminal.type(typed)
        with support.deadline(support.PATIENCE, f"one_key never returned for {typed!r}"):
            return conflicts.one_key(terminal.source)

    def test_a_plain_arrow(self, terminal: support.Terminal) -> None:
        assert self.read(terminal, "\x1b[A") == "\x1b[a"

    def test_an_application_mode_arrow(self, terminal: support.Terminal) -> None:
        """`ESC O B` is what a terminal in application cursor mode sends, and it
        is the reason `O` introduces a sequence as well as `[`."""
        assert self.read(terminal, "\x1bOB") == "\x1bob"

    def test_a_modified_arrow_with_parameters(self, terminal: support.Terminal) -> None:
        """Ctrl-Down is `ESC [ 1 ; 5 B`: four bytes that are not final before the
        one that is. A reader that stopped at the second byte would leave `1;5B`
        for the next four prompts."""
        assert self.read(terminal, "\x1b[1;5B") == "\x1b[1;5b"

    def test_the_key_after_a_modified_arrow_is_still_read(self, terminal: support.Terminal) -> None:
        terminal.type("\x1b[1;5Br")
        conflicts.one_key(terminal.source)
        assert conflicts.one_key(terminal.source) == "r"

    def test_escape_followed_by_an_ordinary_key(self, terminal: support.Terminal) -> None:
        """Neither `[` nor `O`, so the sequence is over at one byte. Alt-l sends
        exactly this, and it must not be read as `[l] keep local`."""
        assert self.read(terminal, "\x1bl") == "\x1bl"

    def test_a_lone_escape_does_not_wait_for_ever(self, terminal: support.Terminal) -> None:
        """`VMIN=0` with `VTIME` is what makes the read come back empty rather
        than block. With `VMIN` left at 1 this test hangs, which is why the
        assertion is reached at all."""
        assert self.read(terminal, "\x1b") == "\x1b"

    def test_a_sequence_longer_than_a_keypress_stops(self, terminal: support.Terminal) -> None:
        """`KEYPRESS` is the backstop for a sequence with no final byte -- a
        terminal that goes quiet mid-escape, or a paste. Without the bound the
        loop reads until the `VTIME` timeout on every byte."""
        assert len(self.read(terminal, "\x1b[" + "1" * 12)) == conflicts.KEYPRESS + 1


class TestWhatTheWeakFixturesMissed:
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
        assert f"1 of {conflicts.SHOWN_HUNKS + 2}" in text

    def test_an_escape_then_a_key_leaves_the_key_behind(self, prompt: Prompt) -> None:
        """`ESC` followed by neither `[` nor `O` ends the sequence at one byte.

        Typing just `\\x1bl` cannot show it: with the check removed the loop reads
        `l`, finds nothing more, and returns the same `\\x1bl`. It takes a *third*
        keypress to tell the two apart -- without the check that one is swallowed
        too, and the prompt then answers with a key the user pressed for the
        question after it.
        """
        prompt.terminal.type("\x1blr")
        with support.deadline(support.PATIENCE, "one_key never returned"):
            assert conflicts.one_key(prompt.terminal.source) == "\x1bl"
            assert conflicts.one_key(prompt.terminal.source) == "r"

    def test_a_pipe_gives_up_everything_but_the_first_character(self) -> None:
        """`[:1]`. A line of exactly one character is true of `[:2]` and of no
        slice at all, so the fixture has to type more than one."""
        assert conflicts.one_key(io.StringIO("ls\n")) == "l"

    def test_a_pipe_line_of_two_keys_is_not_two_keys(self) -> None:
        """The other half: what comes back is one character, so `ask` reads it as
        a key rather than as "not a key"."""
        assert len(conflicts.one_key(io.StringIO("ls\n"))) == 1

    def test_the_key_is_echoed_back(self, prompt: Prompt) -> None:
        """`ECHO` is cleared, so the terminal will not do it. Without this line
        the user presses `l` and sees nothing at all happen."""
        prompt.ask(one_conflict(), "l")
        assert "\nl\n" in prompt.out.getvalue()

    def test_end_of_input_says_so(self, prompt: Prompt) -> None:
        """Not just that it skips -- that it tells the user why. A sync that
        exits 1 having silently skipped every conflict is one nobody can debug."""
        with support.deadline(support.PATIENCE, "ask never settled at end of input"):
            got = conflicts.ask(one_conflict(), io.StringIO(""), prompt.out)
        assert got.choice == conflicts.SKIP
        assert "end of input" in prompt.out.getvalue()


def one_change(diff: str = "--- a\n+++ b\n-old\n+new\n") -> conflicts.Change:
    """A file this computer changed, as `sync.looked_at` builds one."""
    return conflicts.Change(PurePosixPath(".bashrc"), diff)


class TestWhatTheReviewShows:
    """`happening` and `shown`: the two lines above the keys, as pure functions.

    Unit tests because they *are* units, and because the sweep said so: driving
    the whole prompt asserted the diff and the keys and never the sentence
    between them, so `happening` returning `None` survived -- as did every
    mutation of the cap in `shown`, which the end-to-end test could not see
    because `[d]` prints the whole diff underneath it anyway.
    """

    def test_the_sentence_names_the_file_and_which_side_is_older(self) -> None:
        said = conflicts.happening(one_change(), colour=False)
        assert ".bashrc" in said
        assert "you changed this here" in said
        assert "the repository has the older copy" in said

    def test_a_short_diff_is_shown_whole(self) -> None:
        change = one_change("\n".join(f"line {n}" for n in range(conflicts.SHOWN_DIFF)))
        assert conflicts.shown(change) == change.diff
        assert "more line(s)" not in conflicts.shown(change)

    @pytest.mark.parametrize("count", [conflicts.SHOWN_DIFF - 1, conflicts.SHOWN_DIFF])
    def test_a_diff_exactly_at_the_cap_is_still_shown_whole(self, count: int) -> None:
        """The boundary, which is where `<=` becoming `<` lives. One line either
        side of it is the only thing that tells the two spellings apart."""
        change = one_change("\n".join(f"line {n}" for n in range(count)))
        assert conflicts.shown(change) == change.diff

    def test_a_longer_diff_is_cut_and_says_how_much_was_left(self) -> None:
        """The count is asserted, not just its presence: `len(lines) -
        SHOWN_DIFF` becoming `+` still prints a number, and a test that only
        looked for "more line(s)" passed for it."""
        over = 7
        change = one_change("\n".join(f"line {n}" for n in range(conflicts.SHOWN_DIFF + over)))
        cut = conflicts.shown(change)
        assert f"and {over} more line(s)" in cut
        assert f"line {conflicts.SHOWN_DIFF - 1}" in cut, "the kept part is wrong"
        assert f"line {conflicts.SHOWN_DIFF}" not in cut, "the slice kept too much"
        assert len(cut.split("\n")) == conflicts.SHOWN_DIFF + 1, "the cut is the wrong size"


class TestReviewingOneChange:
    """`review`: the same loop as `ask`, over three answers instead of five.

    Driven through `Prompted`'s fake terminal rather than a real sync, for the
    reason its own docstring gives -- and with `support.deadline`, which is what
    turns a mutant that loops for ever into a fast failure the sweep can read.
    Without it five rows here came back `BROKE`, which is not a verdict: the
    harness's 30s alarm fired and the line each was on ended up guarded by
    nothing a sweep could see. CLAUDE.md records that trap; this is it again.
    """

    def review(self, prompt: Prompt, keys: str, diff: str | None = None) -> str:
        prompt.terminal.type(keys + support.FALLBACK)
        with support.deadline(support.PATIENCE, f"the review never settled on {keys!r}"):
            change = one_change() if diff is None else one_change(diff)
            return conflicts.review(change, prompt.terminal.source, prompt.out)

    # One method per key rather than a loop over `REVIEWS`. `Prompted` builds
    # one terminal per *test*, so a second `type()` in the same one appends to a
    # stream that still holds the first call's `FALLBACK` -- and every answer
    # after the first came back `[s]`.
    def test_l_answers_with_this_computer(self, prompt: Prompt) -> None:
        assert self.review(prompt, conflicts.LOCAL) == conflicts.LOCAL

    def test_r_answers_with_the_repository(self, prompt: Prompt) -> None:
        assert self.review(prompt, conflicts.REMOTE) == conflicts.REMOTE

    def test_s_answers_with_skip(self, prompt: Prompt) -> None:
        assert self.review(prompt, conflicts.SKIP) == conflicts.SKIP

    def test_every_key_on_offer_is_an_answer(self) -> None:
        """The three above are `REVIEWS` spelled out, so this is what notices a
        fourth being added to the tuple with nothing driving it."""
        assert conflicts.REVIEWS == (conflicts.LOCAL, conflicts.REMOTE, conflicts.SKIP)

    def test_the_question_the_diff_and_the_keys_are_all_printed(self, prompt: Prompt) -> None:
        """Three prints, three assertions. Each survived on its own before this:
        the end-to-end test asserted the diff and the keys, so dropping the
        sentence between them changed nothing it looked at."""
        self.review(prompt, "l")
        said = prompt.out.getvalue()
        assert "you changed this here" in said, "the sentence is gone"
        assert "-old" in said, "the diff is gone"
        assert "[l] store your version" in said, "the keys are gone"

    def test_the_key_is_echoed(self, prompt: Prompt) -> None:
        """A terminal in raw mode does not echo, so the prompt does it. Without
        the echo the screen shows a question that was answered by nothing
        visible -- and `[s]` and `[l]` then look identical in a transcript."""
        self.review(prompt, "s")
        assert "\ns\n" in prompt.out.getvalue(), "the keypress was not echoed"

    def test_end_of_input_is_a_skip(self, prompt: Prompt) -> None:
        """The answer that cannot lose anything, for a stream with nothing left.
        `ask` has the same rule and the same test beside it -- and the guard
        makes `review` loop for ever without it, so `deadline` is what keeps
        this a failure rather than a hang."""
        with support.deadline(support.PATIENCE, "review never settled at end of input"):
            got = conflicts.review(one_change(), io.StringIO(""), prompt.out)
        assert got == conflicts.SKIP
        assert "end of input" in prompt.out.getvalue()

    def test_the_prompt_is_flushed_before_it_waits_for_a_key(self, prompt: Prompt) -> None:
        """Watch the call, because the thing that changed is the call.

        A buffered stream can hold the question while `one_key` blocks on the
        terminal, and the user is then looking at a cursor with no prompt above
        it. Nothing a captured stream asserts can see that -- a `Spill` shows
        the text either way -- which is why dropping the flush came back
        `SURVIVED` on one sweep and `caught` on the next, on the same tree: the
        catch was a timing accident. CLAUDE.md's ARG_MAX rule is the same shape
        -- when the change is "this call happens", assert the call.
        """
        prompt.terminal.type(conflicts.LOCAL + support.FALLBACK)
        flushed: list[int] = []
        with (
            mock.patch.object(prompt.out, "flush", lambda: flushed.append(1)),
            support.deadline(support.PATIENCE, "the review never settled"),
        ):
            conflicts.review(one_change(), prompt.terminal.source, prompt.out)
        assert flushed == [1], "the prompt was not flushed before the read"

    def test_a_keypress_that_is_several_bytes_is_not_a_key(self, prompt: Prompt) -> None:
        """A press of Down is `\x1b[B`, and read as one answer it is not one.

        `ask` has the same guard and the same test; this is `review`'s. It also
        stops the *loop* being unbounded in the direction that matters: a
        mutant that rejects every key spins for ever, so `deadline` above is
        what makes this row an answer rather than a `BROKE`.

        Echoed as a repr and not as itself -- the raw bytes would move the
        cursor or clear the screen on their way out.
        """
        assert self.review(prompt, "\x1b[B" + conflicts.LOCAL) == conflicts.LOCAL
        said = prompt.out.getvalue()
        assert "is not a key" in said
        # The escape itself, spelled the way `repr` spells it. Not the whole
        # sequence: the pty hands `B` back as `b`, which is a property of the
        # terminal rather than of the prompt -- and `\\x1b` is the half that
        # says the bytes were shown rather than sent to the screen, which is
        # what the repr is for.
        assert "\\x1b[" in said, "the sequence was echoed raw"
        assert "\x1b[" not in said.replace("\\x1b[", ""), "a raw escape reached the output"

    def test_a_key_from_the_other_prompt_re_asks(self, prompt: Prompt) -> None:
        """`[b]` settles a conflict and means nothing here. One key, not a loop
        over both: the terminal is per-test, and a second `type()` reads the
        first call's `FALLBACK` back."""
        assert self.review(prompt, conflicts.BOTH + conflicts.LOCAL) == conflicts.LOCAL
        assert "is not one of the keys" in prompt.out.getvalue()

    def test_the_editor_key_is_not_one_of_these_either(self, prompt: Prompt) -> None:
        assert self.review(prompt, conflicts.EDIT + conflicts.LOCAL) == conflicts.LOCAL
        assert "is not one of the keys" in prompt.out.getvalue()

    def test_d_prints_the_whole_diff_and_asks_again(self, prompt: Prompt) -> None:
        long = "\n".join(f"line {n}" for n in range(conflicts.SHOWN_DIFF + 5))
        assert self.review(prompt, conflicts.DIFF + conflicts.SKIP, long) == conflicts.SKIP
        said = prompt.out.getvalue()
        assert f"line {conflicts.SHOWN_DIFF + 4}" in said, "[d] did not show the rest"
        assert "more line(s)" in said, "the first display was not capped"
