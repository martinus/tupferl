"""`tupferl/colours.py`: the palette, the stream question, and painting a diff.

**Every other assertion in this suite runs under a sandbox that sets
`NO_COLOR`, against a `StringIO` that is not a terminal.** So the coloured half
of this program is invisible from everywhere else, and each of `coloured`'s
mutations survived until the tests moved here from `tests/test_conflicts.py`
carried it. That is why the fixtures below are about being a terminal rather
than about a diff: the diff is the easy half.

Not `tests/test_paint.py`, which is `tools/paint.py`'s and stays that way --
see `tupferl/colours.py`'s docstring for why the two modules are deliberately
separate and why this one is not called `paint`.
"""

from __future__ import annotations

import contextlib
import io
import os
import re
from collections.abc import Iterator
from typing import TextIO
from unittest import mock

import pytest

from tests import support
from tupferl import colours, merge

#: Every SGR sequence, for the round-trip that says painting adds and removes
#: nothing else. A second copy of `tests/test_paint.py`'s, and deliberately: that
#: file's docstring commits it to importing the standard library and the tool it
#: covers, so the shared home a reader would reach for -- `tests/support.py` --
#: is the one place it may not take this from.
ESCAPES = re.compile(r"\x1b\[[0-9;]*m")


@pytest.fixture
def terminal() -> Iterator[support.Terminal]:
    """A pty, closed on the way out."""
    with contextlib.closing(support.Terminal()) as made:
        yield made


@pytest.fixture
def written(terminal: support.Terminal) -> Iterator[TextIO]:
    """A writable stream that really is a terminal -- `isatty()` true, which is
    the whole subject of the class that asks for it."""
    with os.fdopen(os.dup(terminal.master), "w") as stream:
        yield stream


#: A real diff, from the function that produces every diff this program shows.
#: Built rather than typed out, so that a change to how `merge.unified` labels
#: its headers reaches these tests instead of leaving them asserting against a
#: shape nothing produces any more.
DIFF = merge.unified(".bashrc", b"one\ntwo\nthree\n", b"one\nTWO\nthree\n")


def test_the_fixture_is_a_real_diff() -> None:
    """The precondition every test below rests on. A `DIFF` that came back
    empty -- two identical sides, a renamed argument -- would satisfy "no
    escape appears in a context line" and every other negative here."""
    assert DIFF.startswith("--- .bashrc")
    assert "+++ .bashrc" in DIFF
    assert "@@" in DIFF
    assert "-two" in DIFF
    assert "+TWO" in DIFF


class TestWhenColourIsUsed:
    """`colours.coloured`, both halves.

    Moved here with the function from `tests/test_conflicts.py`. A real pty is
    the only way to make `isatty()` true.
    """

    def test_a_terminal_with_no_no_colour_is_coloured(self, written: TextIO) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert colours.coloured(written)

    def test_no_colour_turns_it_off_even_on_a_terminal(self, written: TextIO) -> None:
        """A user who set it meant it."""
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}):
            assert not colours.coloured(written)

    def test_a_pipe_is_never_coloured_even_without_no_colour(self) -> None:
        """The other half of the `and`, and the reason it is not an `or`:
        escape codes in a file someone redirected the run into are noise."""
        with mock.patch.dict(os.environ, {}, clear=True):
            assert not colours.coloured(io.StringIO())


class TestPaintingOneString:
    def test_it_wraps_the_text_and_gives_it_back_intact(self) -> None:
        """Around whole words, never inside them: `grep alias` over a coloured
        `status --diff` still finds `alias`."""
        got = colours.paint("alias ll", colours.ADDED, colour=True)
        assert got == f"{colours.ADDED}alias ll{colours.OFF}"
        assert "alias ll" in got

    def test_without_colour_it_is_the_same_string_object_back(self) -> None:
        assert colours.paint("alias ll", colours.ADDED, colour=False) == "alias ll"


class TestPaintingADiff:
    """`colours.diff`, whose whole logic is the *order* of four tests."""

    def test_the_headers_are_structure_and_not_removed_and_added_lines(self) -> None:
        """The one mistake this function exists to not make, and it reads almost
        right: `--- ` starts with `-` and `+++ ` starts with `+`, so testing the
        bodies first paints the two headers red and green -- on the sides a
        reader would guess, which is what makes it hard to see.

        Asserted as "bold, and neither of the two body colours", because
        `startswith` order is exactly what a reordering mutation changes."""
        painted = colours.diff(DIFF, colour=True)
        minus = next(row for row in painted.split("\n") if ".bashrc" in row and "---" in row)
        plus = next(row for row in painted.split("\n") if ".bashrc" in row and "+++" in row)
        for row in (minus, plus):
            assert row.startswith(colours.BOLD), row
            assert colours.REMOVED not in row, row
            assert colours.ADDED not in row, row

    def test_a_removed_comment_line_is_not_mistaken_for_a_header(self) -> None:
        """`-- keymaps` is an ordinary Lua comment and `.config/nvim/` is full
        of them. Removed, it arrives as `--- keymaps`, which a prefix test reads
        as a file header -- so the line the user is losing is painted as
        structure rather than red, in the one file where it happens most.

        The pair is asserted: the *real* header stays bold in the same output,
        so a fix that simply stopped recognising headers fails here."""
        text = merge.unified("init.lua", b"-- keymaps\nreturn {}\n", b"return {}\n")
        painted = colours.diff(text, colour=True).split("\n")
        assert f"{colours.REMOVED}--- keymaps{colours.OFF}" in painted, painted
        assert any(row.startswith(colours.BOLD) and "init.lua" in row for row in painted), painted

    def test_an_added_line_beginning_with_two_pluses_is_not_a_header_either(self) -> None:
        """The same trap on the other side: `++ x` added is `+++ x`."""
        text = merge.unified("notes", b"stay\n", b"stay\n++ x\n")
        painted = colours.diff(text, colour=True).split("\n")
        assert f"{colours.ADDED}+++ x{colours.OFF}" in painted, painted

    def test_the_second_file_of_a_two_file_diff_still_has_headers(self) -> None:
        """What the blank line is *for*, and the only place it is observable.

        `inspection.difference` joins each file's diff with a blank line, and
        that line is what puts `inside` back to False so the next file's
        `---`/`+++` are recognised as headers again. Without it the second file
        is still inside the first's last hunk, and its two header lines are
        painted as a removed and an added line -- the exact mis-colouring the
        positional test exists to prevent, arriving one file later.

        The blank line itself is asserted to survive: dropping it loses the
        separator and gives the same wrong answer by another route.
        """
        second = merge.unified(".vimrc", b"set nu\n", b"set number\n")
        painted = colours.diff(f"{DIFF}\n\n{second}", colour=True).split("\n")
        assert "" in painted, "the separator was dropped"
        header = next(row for row in painted if ".vimrc" in row and "--- " in row)
        assert header.startswith(colours.BOLD), header
        assert colours.REMOVED not in header, header

    def test_a_removed_line_is_red_and_an_added_line_is_green(self) -> None:
        painted = colours.diff(DIFF, colour=True).split("\n")
        assert f"{colours.REMOVED}-two{colours.OFF}" in painted
        assert f"{colours.ADDED}+TWO{colours.OFF}" in painted

    def test_the_hunk_header_gets_its_own_colour(self) -> None:
        """Its own, not the body's: `@@ -1,3 +1,3 @@` contains both a `-` and a
        `+` run, and a reader scanning for where a hunk starts is not scanning
        for what it added."""
        painted = next(row for row in colours.diff(DIFF, colour=True).split("\n") if "@@" in row)
        assert painted.startswith(colours.HUNK)
        assert colours.ADDED not in painted
        assert colours.REMOVED not in painted

    def test_a_context_line_is_left_exactly_as_it_was(self) -> None:
        painted = colours.diff(DIFF, colour=True).split("\n")
        assert " one" in painted, "the unchanged line was painted or lost"

    @pytest.mark.parametrize(
        "text",
        [
            ".bashrc: skipped, it is a fifo.",
            ".bashrc: executable here, not in the repository.",
            ".vimrc: both sides changed, so this is the difference, not a direction.",
        ],
    )
    def test_a_sentence_that_is_not_a_diff_line_passes_through(self, text: str) -> None:
        """`inspection.difference` hands this one string carrying diffs *and*
        these sentences about files it could not diff. Colouring by structure
        rather than by knowing which lines are which is what keeps that working
        as the sentences change."""
        assert colours.diff(text, colour=True) == text

    def test_with_colour_off_the_text_is_returned_unchanged(self) -> None:
        """Not "no escapes appear", which a function returning `""` satisfies.
        Every captured stream and every other test in this suite takes this
        arm, so it is the one that must be byte-for-byte."""
        assert colours.diff(DIFF, colour=False) == DIFF

    def test_stripping_the_escapes_gives_the_diff_back(self) -> None:
        """Painting adds colour and nothing else -- no reflow, no lost blank
        line, no reordering. The strongest single claim available here, and the
        one that a rewrite of the loop has to keep."""
        painted = colours.diff(DIFF, colour=True)
        assert ESCAPES.sub("", painted) == DIFF


class TestTheRolesAreDistinguishable:
    def test_no_two_roles_share_a_code(self) -> None:
        """A palette where two roles are the same colour is one where a test
        asserting the right role passes for the wrong one -- and the diff's
        three roles and the conflict prompt's three are shown one after the
        other, by `[d]`, on one screen."""
        roles = {
            "BOLD": colours.BOLD,
            "MINE": colours.MINE,
            "THEIRS": colours.THEIRS,
            "DIM": colours.DIM,
            "ADDED": colours.ADDED,
            "REMOVED": colours.REMOVED,
            "HUNK": colours.HUNK,
        }
        assert len(set(roles.values())) == len(roles), roles
