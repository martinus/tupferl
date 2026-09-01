"""Colour for a terminal, and nothing at all for anything else.

Every escape sequence this package writes is in this file. They were in
`conflicts`, which until now held the only coloured output there was -- and the
moment `status --diff` wanted the same treatment, leaving them there would have
meant two palettes. That is worse than it sounds: the two places most likely to
be read one after the other are the conflict prompt's `[d]` and `status --diff`,
which show *the same diff of the same file*, so a drift between them is visible
to the one user who is comparing them.

**Colour is decided per stream, never per process.** `coloured` asks the stream
being written to, so `tupferl status --diff | delta` and every test that
captures output get exactly the bytes they got before this existed. `NO_COLOR`
turns it off because a user who set it meant it -- and because `tests/support.py`
sets it in the sandbox, which is what keeps every assertion about this program's
wording an assertion about the wording.

**A pager still counts as a terminal.** `inspection.show` decides to page on
`out.isatty()` and paints on the same answer, so the colour reaches `less`
rather than being suppressed by the pipe to it. That is git's behaviour and the
reason `show` exports `LESS=FRX`: `R` is what passes the escapes through.

Not `tools/paint.py`, which is six of the same lines. `tools/` is ported between
projects and must not depend on the package it is meant to be able to break --
its own docstring says so from the other side. The two agree on `OFF`, on the
`paint(text, code, ...)` shape and on painting whole words only; they differ in
the last argument, because everything here computes `coloured(out)` once at the
top of a prompt and threads the answer down, where a tool asks per line.

**Named `colours` and not `paint` for two mechanical reasons, neither of them
taste.** `tools/mutants.targets_for` maps a source stem to `test_<stem>*`, so a
second `paint.py` would send both modules' mutations at `tests/test_paint.py` --
harmless, since the selection is an ordering rather than a gate, and still a
test file whose docstring names one subject being handed another. And every
caller here computes a local `colour`, so a module bound as `colour` would be
shadowed by it at exactly the call sites that need both. The plural is what
makes `colours.paint(text, BOLD, colour)` legal and readable at once.
"""

from __future__ import annotations

import os
from typing import TextIO

#: Bold: structure. A file's name, the keys on offer, a diff's `---`/`+++`
#: header -- the lines a reader uses to find their place rather than to read.
BOLD = "\033[1m"
#: Cyan: this computer. `MINE` everywhere it names a side, so the colour means
#: one thing across the prompt, the review and the diff.
MINE = "\033[36m"
#: Yellow: the repository.
THEIRS = "\033[33m"
#: Dim: provenance. Text that must be *there* -- a hunk's line numbers, a count
#: of what was elided -- but which is not what the reader is scanning for.
DIM = "\033[2m"
#: Green: a line the diff adds.
ADDED = "\033[32m"
#: Red: a line the diff removes. Red for *removal*, not for an error -- which is
#: why the two sides of a diff are green and red while the two sides of a
#: conflict are cyan and yellow: a diff has a direction and a conflict does not.
REMOVED = "\033[31m"
#: Magenta: a hunk header, `@@ -1,4 +1,4 @@`. git's colour for the same line.
HUNK = "\033[35m"
OFF = "\033[0m"

#: The two header lines of a unified diff. Checked *before* the `+`/`-` bodies,
#: because both start with one -- `---` painted as a removed line and `+++` as an
#: added one is the first way `diff` below gets this wrong, and it reads almost
#: right, since the colours land on the sides a reader would guess.
#:
#: **And a prefix alone is not enough, which is the second way.** A removed line
#: is its content with a `-` in front, so a dotfile line reading `-- keymaps` --
#: an ordinary Lua comment, and `.config/nvim/` is full of them -- arrives as
#: `--- keymaps` and matches this exactly. `diff` therefore asks *where* as well
#: as *what*: a header only exists before the first `@@` of a file, and nothing
#: can be inside a hunk and at a header at once.
HEADERS = ("--- ", "+++ ")


def paint(text: str, code: str, colour: bool) -> str:
    """`text` in `code`, or `text`, depending.

    The `colour` argument is a `bool` rather than a stream: every caller here
    asks `coloured` once, at the top of a prompt or a rendering, and threads the
    answer down. Asking per line would be one `isatty` per line and, worse, one
    more place for a caller to pass the wrong stream.

    **Around whole words, never inside them.** Nothing in this package greps its
    own output the way `tools/watch.py` greps a sweep's, so the cost here is
    lower than it is there -- but a user's `| grep alias` over `status --diff` is
    the same shape, and the rule costs nothing to keep.
    """
    return f"{code}{text}{OFF}" if colour else text


def coloured(out: TextIO) -> bool:
    """Whether to colour what goes to `out`.

    A terminal and no `NO_COLOR`. Asked of the stream rather than of
    `sys.stdout` so that a caller writing somewhere else gets the right answer,
    and so the tests can drive both halves -- which they must, since the sandbox
    sets `NO_COLOR` and every other assertion about this program's text depends
    on that.
    """
    return out.isatty() and not os.environ.get("NO_COLOR")


def diff(text: str, colour: bool) -> str:
    """A unified diff with its lines coloured the way git colours them.

    `merge.unified` produces the text and this decides what it looks like, which
    is the same split `merge`'s docstring already draws for the labels: bytes in
    and bytes out there, display here. So `status --diff`, the conflict prompt's
    `[d]` and the per-file review all get one answer to "what does a diff look
    like" instead of three.

    **A line's prefix is not enough to say what it is**, and both ways that
    bites are in `HEADERS`' comment: the two headers start with the characters
    the two bodies do, and a *removed* line whose content begins `--` is spelled
    exactly like a `---` header. So this tracks where in the file it is.
    `inside` is "past the first `@@`", where a header cannot occur; an empty
    line ends a file's diff, which is `inspection.difference`'s separator
    between two of them and is the one line a unified diff never contains --
    a blank *context* line is a space, and a blank *removed* line is a `-`.

    Anything that is not a diff line is returned untouched, which is what lets
    `inspection.difference` hand this a string that also carries `\\n`-joined
    sentences about skipped and binary files. Colouring by structure rather than
    by knowing which lines are which is what keeps that true as those sentences
    change.
    """
    # survivor: branch -- `if not colour:` is never taken -- equivalent, and by
    #   construction rather than by luck: `paint` with `colour=False` returns its
    #   argument, and `"\n".join(text.split("\n"))` is the identity. So walking the
    #   text with colour off produces the same string this arm returns without
    #   walking it. The arm is a fast path for the case every captured stream and
    #   every test in the suite takes, and it cannot be observed from the outside.
    if not colour:
        # Before the split, not after: a diff of a large file is the one thing
        # here that is worth not walking line by line for nothing, and every
        # captured stream and every test takes this arm.
        return text
    out: list[str] = []
    inside = False
    for line in text.split("\n"):
        if not line:
            inside = False
            out.append(line)
        elif line.startswith("@@"):
            inside = True
            out.append(paint(line, HUNK, colour))
        elif not inside and line.startswith(HEADERS):
            out.append(paint(line, BOLD, colour))
        elif line.startswith("+"):
            out.append(paint(line, ADDED, colour))
        elif line.startswith("-"):
            out.append(paint(line, REMOVED, colour))
        else:
            out.append(line)
    return "\n".join(out)
