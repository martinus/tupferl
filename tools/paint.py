"""Colour for a terminal, and nothing at all for anything else.

Written for this repository rather than ported: the tools it serves came from
`martinus/woswoar`, which prints in one colour.

The interesting half is the *off* half. A sweep is launched detached with its
output redirected to a file -- that is the whole reason `tools/watch.py` exists
-- and `watch.py --match 'caught|SURVIVED'` then counts rows by grepping that
file. An escape sequence *inside* the word `caught` would make that pattern
match nothing, and a watcher counting zero rows on a healthy run reports a job
that is alive and not working. It would be wrong in the direction CLAUDE.md §8
is entirely about: a check that reports something plausible while measuring
nothing. So two rules, and both are tested:

- **Never when the stream is not a terminal.** `coloured` asks the stream, not
  the process, so a caller writing somewhere else gets the right answer.
- **Around whole words, never inside them.** `paint("caught", GOOD)` leaves
  `caught` intact between the codes, so even a reader that strips nothing can
  still find the word.

**Pad before painting.** `f"{paint(word, GOOD):9}"` counts the escape bytes as
columns, so a nine-wide field becomes a four-wide one and every table built that
way is ragged -- painted rows short, plain rows right. Pad the bare text and
paint the padded result: `paint(f"{word:9}", GOOD)`.

Not `tupferl.conflicts.paint`, which is the same six lines. `tools/` is ported
between projects and imports nothing from the package under test (`mutants.py`
is the single exception, and it only manipulates `sys.path`). Sharing this would
make the tooling depend on the thing it is meant to be able to break.

The codes are named for what they *mean*, not for what they look like. A call
site says which kind of news it is printing and this file decides the colour, so
"survivors are red" is one line here rather than an agreement between eleven
`print`s in three modules.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

#: Bright green: a row that did what it was supposed to do.
GOOD = "\033[32m"
#: Red: the finding. A survivor, a red baseline, a failing test -- the lines a
#: reader is here for, and the ones that must be visible in a screen of `caught`.
BAD = "\033[31m"
#: Yellow: a row that asked nothing, a cap that dropped work, a skip. Neither
#: good news nor bad, and the mistake this tooling exists to prevent is reading
#: one as the other.
ODD = "\033[33m"
#: Bold: structure. Counts, headings, the one line of a paragraph that is the
#: point of it.
HEAD = "\033[1m"
#: Dim: progress and provenance. Text that must be *there* -- a path, a row
#: count, a lane budget -- but which is not what the reader is scanning for.
QUIET = "\033[2m"
OFF = "\033[0m"


def coloured(out: TextIO | None = None) -> bool:
    """Whether to colour what goes to `out`, defaulting to stdout.

    Resolved at the call rather than at import, because `sys.stdout` is replaced
    after import by anything that captures output -- `contextlib.redirect_stdout`,
    a test, a shell redirect that arrives before the process does not, but the
    other two do. A module-level constant here would answer about the terminal
    the process started with.

    `NO_COLOR` is honoured because a user who set it meant it, and because the
    test sandbox sets it (`tests/support.py`) -- so an assertion about a tool's
    text is about the text.
    """
    stream = sys.stdout if out is None else out
    try:
        terminal = stream.isatty()
    except (AttributeError, ValueError):
        # A closed stream raises `ValueError`, and something standing in for one
        # need not have `isatty` at all. Neither is a reason to fail: the answer
        # to "should this be coloured" is then no, and the caller still gets its
        # text.
        return False
    return terminal and not os.environ.get("NO_COLOR")


def paint(text: str, code: str, out: TextIO | None = None) -> str:
    """`text` wrapped in `code`, or `text`, depending on where it is going.

    Newlines are hoisted out of the wrapping, so a leading `\n` stays a blank
    line rather than becoming a blank line with a colour on it. Half of the
    headings here are spelled `f"\n{count} survived..."` and the naive wrapping
    puts the escape at the end of the *previous* line -- which renders the same
    and greps differently, and "renders the same" is not a thing this repository
    accepts as an answer about output another tool reads.
    """
    if not code or not coloured(out):
        # `not code` is not defensive: `tools/watch.py`'s `SHOUT.get(word, "")`
        # deliberately answers "no colour" for a message it does not recognise,
        # and without this that answer arrived as a bare `\x1b[0m` welded to the
        # end of the line -- an escape sequence appended to text that was
        # supposed to have been left alone. Found by the test for that fallback.
        return text
    lead = len(text) - len(text.lstrip("\n"))
    tail = len(text) - len(text.rstrip("\n"))
    if lead + tail >= len(text):
        # Nothing but newlines, and `""` between two codes is an escape sequence
        # a reader has to strip to find out it says nothing.
        return text
    return f"{text[:lead]}{code}{text[lead : len(text) - tail]}{OFF}{text[len(text) - tail :]}"
