"""`tools/paint.py`: colour on a terminal, and the exact same bytes anywhere else.

Written here rather than ported -- `paint.py` is this repository's, and the tools
it dresses came from `martinus/woswoar` printing in one colour.

**Most of this file is about the half that prints nothing.** A mutation sweep is
launched detached with its output redirected to a file, and `tools/watch.py
--match 'caught|SURVIVED'` counts rows by grepping that file. An escape sequence
inside the word `caught` makes that pattern match nothing, and a watcher counting
zero rows on a healthy run says `STALLED: the process is alive and not working`
-- a wrong answer that reads exactly like a real finding, which is CLAUDE.md §8's
whole subject. So the tests below are mostly negative: what must *not* appear,
and where.

Both halves are here on purpose. "Never colour a pipe" is trivially satisfied by
a `paint` that never colours anything, and that version would pass every
assertion about a log file in this suite. `TestATerminalGetsTheColour` is what
makes the rest of them mean something.

Imports the standard library and the tool, and nothing else -- `tests/support.py`
builds a sandbox for tupferl, and this is about a stream.
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO
from unittest import mock

import pytest

from tools import paint

#: Every SGR sequence, for the round-trip that says painting adds and removes
#: nothing else. Deliberately broader than what this module emits: the claim is
#: "strip the escapes and you have the text back", and a stripper that only knew
#: the four codes used here could not tell a stray one from the text.
ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

#: One of each kind, so a test over "every colour" is over the roles rather than
#: over a list that will be one short after the next one is added. Keyed by role
#: because `parametrize` over the values alone names each case after the escape
#: sequence -- `test_it[\x1b[32m]`, which is unreadable in a failure line. It
#: does round-trip as a selection, checked rather than assumed, so the reason
#: here is legibility and not the nodeid hazard it looks like.
CODES = {
    "GOOD": paint.GOOD,
    "BAD": paint.BAD,
    "ODD": paint.ODD,
    "HEAD": paint.HEAD,
    "QUIET": paint.QUIET,
}

#: `CODES` plus the reset, for the one test that is about the *shape* of a
#: constant rather than about a role.
EVERY_CODE = {**CODES, "OFF": paint.OFF}


@pytest.fixture(autouse=True)
def _no_color() -> Iterator[None]:
    """An environment with no `NO_COLOR` in it, for every test in this module.

    Autouse rather than a `usefixtures` mark, which is the one place this file
    departs from the rule CLAUDE.md states for a sandbox. The reason the two
    differ: a sandbox is a property of *some* classes, so a class without the
    mark has to be a class the property is false of. Here it is false of
    nothing -- the variable is honoured by the code under test, so a developer
    who has it set would otherwise see this whole file pass by agreeing with
    them, every positive assertion failing and every negative one holding for
    the wrong reason. A mark that must go on every class is a default written
    out five times, and the sixth class is the one that forgets it.
    """
    cleared = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
    with mock.patch.dict(os.environ, cleared, clear=True):
        yield


@pytest.fixture
def terminal() -> Iterator[TextIO]:
    """A real one. `openpty` rather than an object claiming `isatty`, because
    the claim under test is about what a terminal is, and this file is the
    one place in the suite that can afford to ask the kernel.

    Nothing is ever written to it -- `coloured` only asks -- so the pty
    buffer that hangs a `macos` leg elsewhere in this suite cannot fill.
    """
    master, slave = os.openpty()
    stream = os.fdopen(slave, "w")
    try:
        yield stream
    finally:
        stream.close()
        os.close(master)


@pytest.fixture
def pipe() -> Iterator[TextIO]:
    """What a shell's `|` and `subprocess.PIPE` both hand a program."""
    reader, writer = os.pipe()
    stream = os.fdopen(writer, "w")
    try:
        yield stream
    finally:
        stream.close()
        os.close(reader)


@pytest.fixture
def redirected() -> Iterator[TextIO]:
    """`> sweep.log`, which is how every long run in this repository is
    started. The one that has to stay plain."""
    with tempfile.TemporaryDirectory() as box, (Path(box) / "sweep.log").open("w") as stream:
        yield stream


class TestATerminalGetsTheColour:
    """The positive half, without which every negative one below is vacuous."""

    def test_a_pty_is_coloured(self, terminal: TextIO) -> None:
        assert paint.coloured(terminal)

    def test_the_code_is_actually_emitted(self, terminal: TextIO) -> None:
        painted = paint.paint("caught", paint.GOOD, terminal)
        assert painted == f"{paint.GOOD}caught{paint.OFF}"

    def test_stdout_is_what_it_asks_when_nobody_says(self, terminal: TextIO) -> None:
        """The default, and the reason it is resolved at the call rather than at
        import: `sys.stdout` is replaced by everything that captures output, and
        a constant computed at import would answer about the terminal the
        process started with."""
        with mock.patch.object(sys, "stdout", terminal):
            assert paint.coloured()
            assert paint.BAD in paint.paint("SURVIVED", paint.BAD)


class TestEverythingElseGetsTheBytes:
    """`tools/watch.py` greps the log. This is the guarantee it rests on."""

    def test_a_redirected_run_is_not_coloured(self, redirected: TextIO) -> None:
        assert not paint.coloured(redirected)

    def test_a_pipe_is_not_coloured(self, pipe: TextIO) -> None:
        assert not paint.coloured(pipe)

    @pytest.mark.parametrize("code", CODES.values(), ids=list(CODES))
    def test_the_text_comes_back_unchanged_byte_for_byte(
        self, code: str, redirected: TextIO
    ) -> None:
        """Not "contains the word" -- identical. A log line that gained so much
        as a reset sequence is a line some other tool has to know to strip."""
        assert paint.paint("  caught   x.py:1", code, redirected) == "  caught   x.py:1"

    def test_no_color_beats_a_terminal(self, terminal: TextIO) -> None:
        """Honoured because a user who set it meant it -- and because a test
        that asserts on a tool's text is about the text."""
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            assert not paint.coloured(terminal)
            assert paint.paint("caught", paint.GOOD, terminal) == "caught"

    def test_an_empty_no_color_is_not_set(self, terminal: TextIO) -> None:
        """`NO_COLOR=` is how a shell *unsets* a variable it inherited, and
        `os.environ.get` returns `""` for it. Reading that as "the user asked for
        no colour" would make the variable impossible to turn back off."""
        with mock.patch.dict(os.environ, {"NO_COLOR": ""}):
            assert paint.coloured(terminal)

    def test_no_code_means_no_escape_at_all(self, terminal: TextIO) -> None:
        """An empty code is a real answer, not a caller's mistake:
        `tools/watch.py`'s `SHOUT.get(word, "")` says "no colour" that way for a
        message it does not recognise. Wrapped naively it welds a bare reset onto
        the end of a line that was meant to be left alone -- which is this
        module's one job, done to the one line it was told not to touch."""
        assert paint.paint("running: 3 rows", "", terminal) == "running: 3 rows"

    def test_a_closed_stream_is_not_an_error(self, pipe: TextIO) -> None:
        """`isatty` on a closed file raises `ValueError`. Printing is not the
        thing a tool is for, and a colour decision must never be what ends a run
        -- the answer is simply no."""
        stream = pipe
        stream.close()
        assert not paint.coloured(stream)

    def test_something_that_is_not_a_stream_at_all(self) -> None:
        """A stand-in for stdout that does not implement `isatty`. Same
        argument: an answer, not an exception."""
        assert not paint.coloured(mock.Mock(spec=[]))


class TestWhatIsInsideTheCodes:
    """The two ways painting can be wrong without ever being visible."""

    @pytest.mark.parametrize("code", CODES.values(), ids=list(CODES))
    def test_the_word_survives_intact(self, code: str, terminal: TextIO) -> None:
        """`caught` stays a contiguous `caught`, so `grep` and
        `tools/watch.py --match` find it even in a coloured log. This is why the
        codes go around whole words and never inside one."""
        assert "SURVIVED" in paint.paint("SURVIVED", code, terminal)

    @pytest.mark.parametrize(
        "text", ("caught", "  caught   x.py:1 `a` -> `b`", "\n2 survived.", "a\nb", "  ")
    )
    @pytest.mark.parametrize("code", CODES.values(), ids=list(CODES))
    def test_stripping_the_codes_gives_the_text_back(
        self, code: str, text: str, terminal: TextIO
    ) -> None:
        painted = paint.paint(text, code, terminal)
        assert ESCAPES.sub("", painted) == text

    def test_painting_does_not_change_the_visible_width(self, terminal: TextIO) -> None:
        """Pad first, paint second -- and this is the assertion that says the
        order was right. `f"{painted:9}"` counts the escape *bytes* as columns,
        so a nine-wide field becomes four and every coloured row in a table sits
        five characters left of every plain one.
        """
        padded = paint.paint(f"{'caught':9}", paint.GOOD, terminal)
        assert len(ESCAPES.sub("", padded)) == 9
        assert len(padded) != 9, "the fixture is not painting at all"

    def test_a_leading_newline_stays_a_blank_line(self, terminal: TextIO) -> None:
        """Half the headings are spelled `f"\\n{n} survived..."`. Wrapped
        naively, the escape lands at the end of the *previous* line -- which
        renders the same and greps differently."""
        painted = paint.paint("\n2 survived.", paint.BAD, terminal)
        assert painted.startswith("\n"), painted.encode()
        assert painted == f"\n{paint.BAD}2 survived.{paint.OFF}"

    def test_a_trailing_newline_stays_outside_too(self, terminal: TextIO) -> None:
        painted = paint.paint("done\n", paint.GOOD, terminal)
        assert painted.endswith("\n"), painted.encode()
        assert painted == f"{paint.GOOD}done{paint.OFF}\n"

    def test_nothing_but_newlines_is_left_alone(self, terminal: TextIO) -> None:
        """An escape sequence around nothing is a sequence a reader has to strip
        before finding out it says nothing."""
        assert paint.paint("\n\n", paint.GOOD, terminal) == "\n\n"

    def test_the_empty_string_is_left_alone_too(self, terminal: TextIO) -> None:
        """The only input that tells `>=` from `>` in that guard, and this test
        is the whole reason it is not `>`.

        Every all-newline case satisfies both: `"\n"` has `lead + tail == 2`
        against a length of 1, because the same newline is counted from each
        end. Only `""` lands exactly on the boundary at `0 >= 0`, and with `>`
        it falls through and returns a bare `code + OFF` -- an escape sequence
        wrapped around nothing at all, on a line that had nothing to say.

        Measured: `>=` becoming `>` survived the sweep of this change, because
        the test above uses `"\n\n"` and cannot see the difference.
        """
        assert paint.paint("", paint.GOOD, terminal) == ""

    def test_whitespace_that_is_not_a_newline_is_still_painted(self, terminal: TextIO) -> None:
        """The hoist is newlines only. A padded field is trailing *spaces*, and
        stripping those would be the width bug above, arriving from inside."""
        assert paint.paint("  ", paint.GOOD, terminal) == f"{paint.GOOD}  {paint.OFF}"


class TestTheRolesAreDistinguishable:
    """Three kinds of news, and a reader who tells them apart by colour before
    reading a word. Two roles sharing a code would make that channel say less
    than it appears to, which nothing else here would notice."""

    def test_good_bad_and_odd_are_three_different_colours(self) -> None:
        assert len({paint.GOOD, paint.BAD, paint.ODD}) == 3

    @pytest.mark.parametrize("code", EVERY_CODE.values(), ids=list(EVERY_CODE))
    def test_every_code_is_a_complete_sequence(self, code: str) -> None:
        """A constant missing its `m` is not a colour, it is four characters of
        rubbish printed into the middle of a line."""
        assert re.fullmatch(r"\x1b\[[0-9;]+m", code), code.encode()


class TestASpillIsNotATerminal:
    """`io.StringIO` answers `isatty()` False, which is what makes every existing
    assertion in this suite about a tool's printed text keep working unchanged.
    Asserted rather than assumed: it is the reason no other test file had to
    move."""

    def test_a_stringio_is_not_coloured(self) -> None:
        assert not paint.coloured(io.StringIO())
