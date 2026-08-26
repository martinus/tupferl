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
import unittest
from pathlib import Path
from typing import TextIO
from unittest import mock

from tools import paint

#: Every SGR sequence, for the round-trip that says painting adds and removes
#: nothing else. Deliberately broader than what this module emits: the claim is
#: "strip the escapes and you have the text back", and a stripper that only knew
#: the four codes used here could not tell a stray one from the text.
ESCAPES = re.compile(r"\x1b\[[0-9;]*m")

#: One of each kind, so a test over "every colour" is over the roles rather than
#: over a list that will be one short after the next one is added.
CODES = (paint.GOOD, paint.BAD, paint.ODD, paint.HEAD, paint.QUIET)


class Fixture(unittest.TestCase):
    """Streams of the three kinds, and an environment with no `NO_COLOR` in it."""

    def setUp(self) -> None:
        # Cleared for every test here, not only the ones about it. The variable
        # is honoured by the code under test, so a developer who has it set
        # would otherwise see this file pass by agreeing with them -- every
        # `assertIn(code, ...)` below would fail, and every `assertNotIn` would
        # hold for the wrong reason.
        cleared = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
        patched = mock.patch.dict(os.environ, cleared, clear=True)
        patched.start()
        self.addCleanup(patched.stop)

    def terminal(self) -> TextIO:
        """A real one. `openpty` rather than an object claiming `isatty`, because
        the claim under test is about what a terminal is, and this file is the
        one place in the suite that can afford to ask the kernel.

        Nothing is ever written to it -- `coloured` only asks -- so the pty
        buffer that hangs a `macos` leg elsewhere in this suite cannot fill.
        """
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        stream = os.fdopen(slave, "w")
        self.addCleanup(stream.close)
        return stream

    def pipe(self) -> TextIO:
        """What a shell's `|` and `subprocess.PIPE` both hand a program."""
        reader, writer = os.pipe()
        self.addCleanup(os.close, reader)
        stream = os.fdopen(writer, "w")
        self.addCleanup(stream.close)
        return stream

    def redirected(self) -> TextIO:
        """`> sweep.log`, which is how every long run in this repository is
        started. The one that has to stay plain."""
        box = tempfile.TemporaryDirectory()
        self.addCleanup(box.cleanup)
        stream = (Path(box.name) / "sweep.log").open("w")
        self.addCleanup(stream.close)
        return stream


class TestATerminalGetsTheColour(Fixture):
    """The positive half, without which every negative one below is vacuous."""

    def test_a_pty_is_coloured(self) -> None:
        self.assertTrue(paint.coloured(self.terminal()))

    def test_the_code_is_actually_emitted(self) -> None:
        painted = paint.paint("caught", paint.GOOD, self.terminal())
        self.assertEqual(f"{paint.GOOD}caught{paint.OFF}", painted)

    def test_stdout_is_what_it_asks_when_nobody_says(self) -> None:
        """The default, and the reason it is resolved at the call rather than at
        import: `sys.stdout` is replaced by everything that captures output, and
        a constant computed at import would answer about the terminal the
        process started with."""
        with mock.patch.object(sys, "stdout", self.terminal()):
            self.assertTrue(paint.coloured())
            self.assertIn(paint.BAD, paint.paint("SURVIVED", paint.BAD))


class TestEverythingElseGetsTheBytes(Fixture):
    """`tools/watch.py` greps the log. This is the guarantee it rests on."""

    def test_a_redirected_run_is_not_coloured(self) -> None:
        self.assertFalse(paint.coloured(self.redirected()))

    def test_a_pipe_is_not_coloured(self) -> None:
        self.assertFalse(paint.coloured(self.pipe()))

    def test_the_text_comes_back_unchanged_byte_for_byte(self) -> None:
        """Not "contains the word" -- identical. A log line that gained so much
        as a reset sequence is a line some other tool has to know to strip."""
        for code in CODES:
            with self.subTest(code=code.encode()):
                self.assertEqual(
                    "  caught   x.py:1", paint.paint("  caught   x.py:1", code, self.redirected())
                )

    def test_no_color_beats_a_terminal(self) -> None:
        """Honoured because a user who set it meant it -- and because a test
        that asserts on a tool's text is about the text."""
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            self.assertFalse(paint.coloured(self.terminal()))
            self.assertEqual("caught", paint.paint("caught", paint.GOOD, self.terminal()))

    def test_an_empty_no_color_is_not_set(self) -> None:
        """`NO_COLOR=` is how a shell *unsets* a variable it inherited, and
        `os.environ.get` returns `""` for it. Reading that as "the user asked for
        no colour" would make the variable impossible to turn back off."""
        with mock.patch.dict(os.environ, {"NO_COLOR": ""}):
            self.assertTrue(paint.coloured(self.terminal()))

    def test_no_code_means_no_escape_at_all(self) -> None:
        """An empty code is a real answer, not a caller's mistake:
        `tools/watch.py`'s `SHOUT.get(word, "")` says "no colour" that way for a
        message it does not recognise. Wrapped naively it welds a bare reset onto
        the end of a line that was meant to be left alone -- which is this
        module's one job, done to the one line it was told not to touch."""
        self.assertEqual("running: 3 rows", paint.paint("running: 3 rows", "", self.terminal()))

    def test_a_closed_stream_is_not_an_error(self) -> None:
        """`isatty` on a closed file raises `ValueError`. Printing is not the
        thing a tool is for, and a colour decision must never be what ends a run
        -- the answer is simply no."""
        stream = self.pipe()
        stream.close()
        self.assertFalse(paint.coloured(stream))

    def test_something_that_is_not_a_stream_at_all(self) -> None:
        """A stand-in for stdout that does not implement `isatty`. Same
        argument: an answer, not an exception."""
        self.assertFalse(paint.coloured(mock.Mock(spec=[])))


class TestWhatIsInsideTheCodes(Fixture):
    """The two ways painting can be wrong without ever being visible."""

    def test_the_word_survives_intact(self) -> None:
        """`caught` stays a contiguous `caught`, so `grep` and
        `tools/watch.py --match` find it even in a coloured log. This is why the
        codes go around whole words and never inside one."""
        for code in CODES:
            with self.subTest(code=code.encode()):
                self.assertIn("SURVIVED", paint.paint("SURVIVED", code, self.terminal()))

    def test_stripping_the_codes_gives_the_text_back(self) -> None:
        for text in ("caught", "  caught   x.py:1 `a` -> `b`", "\n2 survived.", "a\nb", "  "):
            for code in CODES:
                with self.subTest(text=text, code=code.encode()):
                    painted = paint.paint(text, code, self.terminal())
                    self.assertEqual(text, ESCAPES.sub("", painted))

    def test_painting_does_not_change_the_visible_width(self) -> None:
        """Pad first, paint second -- and this is the assertion that says the
        order was right. `f"{painted:9}"` counts the escape *bytes* as columns,
        so a nine-wide field becomes four and every coloured row in a table sits
        five characters left of every plain one.
        """
        padded = paint.paint(f"{'caught':9}", paint.GOOD, self.terminal())
        self.assertEqual(9, len(ESCAPES.sub("", padded)))
        self.assertNotEqual(9, len(padded), "the fixture is not painting at all")

    def test_a_leading_newline_stays_a_blank_line(self) -> None:
        """Half the headings are spelled `f"\\n{n} survived..."`. Wrapped
        naively, the escape lands at the end of the *previous* line -- which
        renders the same and greps differently."""
        painted = paint.paint("\n2 survived.", paint.BAD, self.terminal())
        self.assertTrue(painted.startswith("\n"), painted.encode())
        self.assertEqual(f"\n{paint.BAD}2 survived.{paint.OFF}", painted)

    def test_a_trailing_newline_stays_outside_too(self) -> None:
        painted = paint.paint("done\n", paint.GOOD, self.terminal())
        self.assertTrue(painted.endswith("\n"), painted.encode())
        self.assertEqual(f"{paint.GOOD}done{paint.OFF}\n", painted)

    def test_nothing_but_newlines_is_left_alone(self) -> None:
        """An escape sequence around nothing is a sequence a reader has to strip
        before finding out it says nothing."""
        self.assertEqual("\n\n", paint.paint("\n\n", paint.GOOD, self.terminal()))

    def test_whitespace_that_is_not_a_newline_is_still_painted(self) -> None:
        """The hoist is newlines only. A padded field is trailing *spaces*, and
        stripping those would be the width bug above, arriving from inside."""
        self.assertEqual(
            f"{paint.GOOD}  {paint.OFF}", paint.paint("  ", paint.GOOD, self.terminal())
        )


class TestTheRolesAreDistinguishable(Fixture):
    """Three kinds of news, and a reader who tells them apart by colour before
    reading a word. Two roles sharing a code would make that channel say less
    than it appears to, which nothing else here would notice."""

    def test_good_bad_and_odd_are_three_different_colours(self) -> None:
        self.assertEqual(3, len({paint.GOOD, paint.BAD, paint.ODD}))

    def test_every_code_is_a_complete_sequence(self) -> None:
        """A constant missing its `m` is not a colour, it is four characters of
        rubbish printed into the middle of a line."""
        for code in (*CODES, paint.OFF):
            with self.subTest(code=code.encode()):
                self.assertRegex(code, r"^\x1b\[[0-9;]+m$")


class TestASpillIsNotATerminal(Fixture):
    """`io.StringIO` answers `isatty()` False, which is what makes every existing
    assertion in this suite about a tool's printed text keep working unchanged.
    Asserted rather than assumed: it is the reason no other test file had to
    move."""

    def test_a_stringio_is_not_coloured(self) -> None:
        self.assertFalse(paint.coloured(io.StringIO()))
