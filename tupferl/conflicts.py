"""What a conflict is, and the six ways a person settles one.

Plan §3.4 calls the conflict prompt "the key feature" and the main improvement
over chezmoi: one command that asks the user only when it must, and asks in a
form they can answer with one keypress. This module is that prompt, and the
vocabulary the rest of the program uses to talk about a file no rule could
settle.

It sits *below* `sync`, not beside it. Five things decide the shape.

**A conflict carries its own three versions.** `Sides` is built by
`sync.resolve` and travels on the outcome, so "a conflict knows what the two
computers said" is a fact of the type rather than a narrowing every caller has
to perform: `home` is a `Blob` here and not `Blob | None`, because the branch
that produces a conflict is below the one that handles a missing file.

**The prompt returns an answer, not a decision about disk.** `ask` says which of
`[l] [r] [b] [e] [s]` the user chose and, for the two that produce new bytes,
what those bytes are. `sync` alone decides what that means for the repository,
`$HOME` and the snapshot. That is what keeps this module out of an import cycle
with `sync`, and it is also why `--ours`, `--theirs` and `--no-input` can be
*settlers that answer without asking*: a flag and the keypress it stands for go
through one mapping in `sync.settled`, so they cannot resolve differently.

**`[b]` and `[e]` come from git and from the user, never from a parser here.**
"Keep both" is `git merge-file --union`; the tempting alternative -- strip the
markers out of the merged text -- means re-deriving hunk boundaries git has
already computed, and getting that wrong silently deletes a line somebody wrote.
`hunks` *does* parse the markers, but only to show them: a mis-parse there costs
a confusing display, not a lost line.

**The keypress sets the terminal flags itself.** `tty.setcbreak` was the obvious
call and is wrong across the versions this project supports: Python 3.12 changed
it to stop clearing `ECHO`, so the same code echoes the key on 3.12 and swallows
it on 3.10. Clearing `ICANON` and `ECHO` here, and echoing the key back
deliberately, is the same behaviour on every supported interpreter.

**A `$HOME` with nobody at it is `--no-input`.** When stdin is not a terminal
there is no one to press a key, and a prompt would either block a cron job for
ever or read EOF and pick something. Both are worse than reporting the conflict
and leaving both copies alone, which is what `[s]` does anyway.
"""

from __future__ import annotations

import difflib
import os
import shlex
import subprocess
import sys
import tempfile
import termios
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import NamedTuple, TextIO

from tupferl import merge
from tupferl.config import Config
from tupferl.copies import Blob
from tupferl.errors import TupferlError

#: The six keys of plan §3.4's prompt. Single characters because they are what
#: the user types; the constants exist so that no branch below compares against
#: a bare letter, where a typo is a key that silently does nothing.
LOCAL = "l"
REMOTE = "r"
BOTH = "b"
EDIT = "e"
DIFF = "d"
SKIP = "s"

#: The five that end the prompt. `DIFF` is not among them: showing the diff is
#: something the prompt *does*, after which it asks again.
ANSWERS = (LOCAL, REMOTE, BOTH, EDIT, SKIP)

#: How many conflicting hunks are shown before the display stops and says how
#: many are left. A file with forty of them would otherwise scroll the question
#: off the screen, which is the one line the user has to be able to see.
SHOWN_HUNKS = 3

#: How many lines of one side of one hunk are shown. Same argument.
SHOWN_LINES = 12

#: Everything written to a terminal, and nothing written to a pipe. `NO_COLOR`
#: is honoured because a user who set it meant it, and because the test sandbox
#: sets it -- so an assertion about the prompt's text is about the text.
BOLD = "\033[1m"
MINE = "\033[36m"
THEIRS = "\033[33m"
DIM = "\033[2m"
OFF = "\033[0m"


class Sides(NamedTuple):
    """The three versions of one file, and what git made of them."""

    name: PurePosixPath
    #: The last state both computers agreed on, or `None` when there is none --
    #: the file was managed on two machines independently, or the snapshot was
    #: lost. See `merge.three_way`.
    base: Blob | None
    #: What this computer has. Not optional: see the module docstring.
    home: Blob
    #: What the repository has.
    stored: Blob
    #: The merge with standard conflict markers in it -- what the prompt shows
    #: and what `[e]` opens. `None` for a binary file, where there are no lines
    #: to mark and `[b]`, `[e]` and `[d]` have nothing to offer.
    marked: bytes | None
    #: How many hunks git could not decide; `merge.WHOLE_FILE` for a binary file.
    conflicts: int

    @property
    def binary(self) -> bool:
        """Whether this is a file with no lines to take from either side."""
        return self.marked is None


class Answer(NamedTuple):
    """What the user chose, and the bytes the choice produced."""

    #: One of `ANSWERS`.
    choice: str
    #: The settled file, for `BOTH` and `EDIT`. `None` for the other three,
    #: where the bytes to write are already one of the two sides and `sync` has
    #: them -- passing a copy back would be a second place for them to differ.
    data: bytes | None = None


#: Something that settles one conflict. The prompt is one; so is each of the
#: three flags, which answer the same question without asking it.
Settler = Callable[[Sides], Answer]


class Hunk(NamedTuple):
    """One region of the marked file that git could not decide.

    Line numbers are of the *merged* file -- the one `[e]` opens -- and are said
    so wherever they are printed. The numbers of the original two files are not
    recoverable from the markers, and printing a number from the merged file
    while implying it was `$HOME`'s would be a wrong "why" of the kind CLAUDE.md
    §7 rules out.
    """

    #: 1-based line of the `<<<<<<<` marker in the merged file.
    start: int
    #: 1-based line of the `>>>>>>>` marker.
    end: int
    mine: list[bytes]
    theirs: list[bytes]


def hunks(sides: Sides) -> list[Hunk]:
    """The conflicting regions of `sides.marked`, for display only.

    The markers are matched against the exact labels `merge.labels_for` wrote,
    not against a bare `<<<<<<<`. A dotfiles repository is one of the few places
    a file legitimately *contains* conflict markers -- a gitattributes example, a
    merge driver's documentation, somebody's own test fixture -- and matching the
    bare form would split a file at a line nobody was arguing about.

    `split(b"\\n")` rather than `splitlines()`: the latter splits on eight more
    characters than git does, so a file containing one of them would shift every
    line number after it. Nothing here recovers from a malformed run of markers;
    a region that never closes is simply not reported, because this feeds a
    display and the merged file itself is what `[e]` hands to the user.
    """
    if sides.marked is None:
        return []
    mine_at, _, theirs_at = merge.labels_for(str(sides.name))
    opens = f"<<<<<<< {mine_at}".encode()
    closes = f">>>>>>> {theirs_at}".encode()
    middle = b"======="

    found: list[Hunk] = []
    start: int | None = None
    mine: list[bytes] = []
    theirs: list[bytes] | None = None
    for number, text in enumerate(sides.marked.split(b"\n"), start=1):
        if text == opens:
            start, mine, theirs = number, [], None
        elif start is None:
            continue
        elif text == middle and theirs is None:
            theirs = []
        elif text == closes and theirs is not None:
            found.append(Hunk(start, number, mine, theirs))
            start, theirs = None, None
        elif theirs is not None:
            theirs.append(text)
        else:
            mine.append(text)
    return found


def paint(text: str, code: str, colour: bool) -> str:
    """`text` in `code`, or `text`, depending."""
    return f"{code}{text}{OFF}" if colour else text


def coloured(out: TextIO) -> bool:
    """Whether to colour what goes to `out`.

    A terminal and no `NO_COLOR`. Asked of the stream rather than of `sys.stdout`
    so that a caller writing somewhere else gets the right answer, and so the
    tests can drive both halves -- which they must, since the sandbox sets
    `NO_COLOR` and every other assertion about this module's text depends on
    that.
    """
    return out.isatty() and not os.environ.get("NO_COLOR")


def excerpt(lines: list[bytes], colour: bool) -> list[str]:
    """At most `SHOWN_LINES` of one side, decoded for a terminal.

    `errors="replace"` because a managed file is bytes and need not be UTF-8;
    the alternative is a prompt that raises on the one file the user most needs
    to look at. What is *written* is never this text -- `[l]`, `[r]`, `[b]` and
    `[e]` all carry bytes -- so a replacement character here cannot reach disk.
    """
    shown = [f"  | {line.decode('utf-8', 'replace')}" for line in lines[:SHOWN_LINES]]
    left = len(lines) - SHOWN_LINES
    if left > 0:
        shown.append(paint(f"  | ... {left} more line{'s' if left > 1 else ''}", DIM, colour))
    return shown


def describe(sides: Sides, colour: bool) -> str:
    """The conflict itself: what disagrees, and what each side says."""
    if sides.binary:
        return (
            f"{paint(str(sides.name), BOLD, colour)} is not a text file, and both computers "
            f"changed it.\nThere are no lines to take from each side, so it is one choice "
            f"for the whole file."
        )

    regions = hunks(sides)
    lines = [
        f"{paint(str(sides.name), BOLD, colour)}: "
        f"{sides.conflicts} conflict{'s' if sides.conflicts > 1 else ''} to settle."
    ]
    for index, hunk in enumerate(regions[:SHOWN_HUNKS], start=1):
        lines.append("")
        lines.append(
            paint(
                f"  {index} of {len(regions)}, lines {hunk.start}-{hunk.end} of the merged file",
                DIM,
                colour,
            )
        )
        lines.append(paint("  this computer", MINE, colour))
        lines.extend(excerpt(hunk.mine, colour))
        lines.append(paint("  the repository", THEIRS, colour))
        lines.extend(excerpt(hunk.theirs, colour))
    left = len(regions) - SHOWN_HUNKS
    if left > 0:
        lines.append("")
        lines.append(paint(f"  ... and {left} more; press [d] to see the whole file", DIM, colour))
    return "\n".join(lines)


def choices(sides: Sides, colour: bool) -> str:
    """The keys on offer, which are not the same set for every file."""
    keep = f"[{LOCAL}] keep local   [{REMOTE}] keep remote"
    if sides.binary:
        # `[b]`, `[e]` and `[d]` all mean "work with the lines", and there are
        # none. Offering them and then refusing is worse than not offering them:
        # the user has already decided by the time they are told.
        return paint(f"  {keep}   [{SKIP}] skip", BOLD, colour)
    return paint(
        f"  {keep}   [{BOTH}] keep both\n"
        f"  [{EDIT}] edit merged file   [{DIFF}] show full diff   [{SKIP}] skip",
        BOLD,
        colour,
    )


def unified(sides: Sides) -> str:
    """`[d]`: the whole difference between the two computers, as a diff.

    `$HOME` against the repository, and not either against the merge base: the
    question at the prompt is which of *those two* to keep, and a diff against a
    third version is a different question.

    `difflib.diff_bytes` rather than decoding first, so a file that is not UTF-8
    still produces a diff of the right lines; only the finished text is decoded,
    with `errors="replace"`, for the same reason as `excerpt`.
    """
    mine_at, _, theirs_at = merge.labels_for(str(sides.name))
    rows = difflib.diff_bytes(
        difflib.unified_diff,
        sides.home.data.split(b"\n"),
        sides.stored.data.split(b"\n"),
        mine_at.encode(),
        theirs_at.encode(),
        lineterm=b"",
    )
    return "\n".join(row.decode("utf-8", "replace") for row in rows)


def editor(config: Config) -> str:
    """The command `[e]` runs, from the config or the environment.

    The config first, because plan §5 gives `.tupferl/config.toml` an `editor`
    setting and a setting that loses to an environment variable is one the user
    cannot make stick. Then `$VISUAL`, then `$EDITOR`, which is the order every
    other tool uses.

    No fallback to `vi`. Guessing an editor that may not be installed turns "you
    have not told me which editor to use" into whatever `vi` does on a machine
    without it, and plan §5 asks every error to say what the user can do next.
    """
    said = config.editor or os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not said:
        raise TupferlError(
            "no editor is set, so there is nothing to open the merged file with; "
            "set $EDITOR, or `editor` in .tupferl/config.toml."
        )
    return said


def leftover(sides: Sides, data: bytes) -> bool:
    """Whether tupferl's own conflict markers are still in `data`.

    Its own, matched in full against `merge.labels_for` -- the same rule as
    `hunks`, and for the same reason. A user whose dotfile legitimately contains
    `<<<<<<<` is not told they left the merge half-finished; a user who really
    did leave it half-finished is, before those markers reach both computers.
    """
    mine_at, _, theirs_at = merge.labels_for(str(sides.name))
    marks = {f"<<<<<<< {mine_at}".encode(), f">>>>>>> {theirs_at}".encode()}
    return any(line in marks for line in data.split(b"\n"))


@contextmanager
def workspace(name: PurePosixPath) -> Iterator[Path]:
    """A throwaway file named after `name`, so the editor picks the right mode.

    The *basename* only, in a temporary directory of its own: an editor chooses
    its syntax highlighting and its indent rules from the file name, and a user
    editing `init.lua` in a file called `tmp9k2j` gets neither. The basename and
    not the whole path, because `PurePosixPath.name` cannot contain a separator
    and so cannot reach outside the directory.
    """
    with tempfile.TemporaryDirectory(prefix="tupferl-edit-") as box:
        yield Path(box) / name.name


def edit(sides: Sides, buffer: bytes, config: Config, out: TextIO) -> bytes | None:
    """Open `buffer` in the user's editor; return what came back, or `None`.

    `None` means "ask again", and it has two causes worth telling apart in the
    message: the editor exited non-zero, or the file still carries the markers.
    Neither is an error to abort the sync with -- the user is standing right
    there and can choose something else.

    The buffer is a real file in a throwaway directory rather than `$HOME`'s
    copy. An editor opened directly on the managed file would leave the markers
    in `$HOME` if the user quit, and a sync interrupted after that is one where
    `$HOME` holds a file no program can read.

    `shlex.split`, because `EDITOR="code --wait"` and `EDITOR="emacs -nw"` are
    both ordinary. The child inherits this process's terminal -- it must, since
    it is a full-screen program the user is about to type into -- so nothing
    here captures its output.
    """
    command = editor(config)
    with workspace(sides.name) as scratch:
        scratch.write_bytes(buffer)
        done = subprocess.run([*shlex.split(command), str(scratch)], check=False)
        if done.returncode != 0:
            print(f"{command} exited with {done.returncode}; nothing was changed.", file=out)
            return None
        settled = scratch.read_bytes()
    if leftover(sides, settled):
        print(
            "the merged file still has tupferl's conflict markers in it, so it was "
            "not saved; remove them, or choose another key.",
            file=out,
        )
        return None
    return settled


def one_key(source: TextIO) -> str:
    """One keypress from `source`, lower-cased. Empty string at end of input.

    Two paths, and the second is not a fallback for tests -- it is what happens
    when `sync` runs with a pipe on stdin and something has already decided the
    prompt is worth showing anyway.

    The terminal flags are set here rather than through `tty.setcbreak`, which
    changed behaviour in Python 3.12: it stopped clearing `ECHO`, so the same
    call echoes the key on 3.12 and does not on 3.10. `ICANON` is what makes the
    read return without waiting for Enter -- plan §3.4's "every choice is one
    keypress" -- and `ECHO` is cleared so that the key can be echoed back with a
    newline after it, which a terminal in this mode will not add.
    """
    if not source.isatty():
        typed = source.readline()
        return typed.strip()[:1].lower()

    fd = source.fileno()
    before = termios.tcgetattr(fd)
    mode = termios.tcgetattr(fd)
    mode[3] &= ~(termios.ICANON | termios.ECHO)
    mode[6][termios.VMIN] = 1
    mode[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, mode)
        return source.read(1).lower()
    finally:
        # In a `finally` because an interrupt at the prompt would otherwise leave
        # the user's shell with echo off, which looks like a hung terminal.
        termios.tcsetattr(fd, termios.TCSADRAIN, before)


def ask(sides: Sides, config: Config, source: TextIO, out: TextIO) -> Answer:
    """Plan §3.4 step 4: show the conflict and settle it with one keypress.

    `[d]` and a key that is not on offer both re-ask, which is why this is a
    loop. So does `[e]` when the editor said no -- and the edited text is kept as
    the buffer the next `[e]` opens, so a user who saved half a resolution does
    not lose it by pressing the wrong key next.

    End of input is `[s]`. That is the same answer `--no-input` gives, and it is
    the only one that cannot lose something the user meant to keep.
    """
    colour = coloured(out)
    buffer = sides.marked
    while True:
        print(f"\n{describe(sides, colour)}\n", file=out)
        print(choices(sides, colour), file=out)
        out.flush()

        key = one_key(source)
        if key == "":
            print(f"{SKIP}   (end of input)", file=out)
            return Answer(SKIP)
        print(key, file=out)

        if key in (LOCAL, REMOTE, SKIP):
            return Answer(key)
        if sides.binary:
            print(f"{key!r} is not one of the keys for a file with no lines.", file=out)
            continue
        if key == DIFF:
            print(unified(sides), file=out)
            continue
        if key == BOTH:
            both = merge.three_way(
                str(sides.name),
                None if sides.base is None else sides.base.data,
                sides.home.data,
                sides.stored.data,
                keep_both=True,
            )
            # `keep_both` never conflicts and never returns `None` for a file
            # that got this far -- `merge.three_way` raises rather than return
            # either -- so this is the value, not a case to handle.
            assert both.data is not None
            return Answer(BOTH, both.data)
        if key == EDIT:
            assert buffer is not None  # not binary, so `marked` is bytes
            settled = edit(sides, buffer, config, out)
            if settled is None:
                continue
            buffer = settled
            return Answer(EDIT, settled)
        print(f"{key!r} is not one of the keys.", file=out)


def always(choice: str) -> Settler:
    """The settler a flag is: this answer, for every conflict, without asking."""
    return lambda sides: Answer(choice)


def answering(config: Config, no_input: bool, ours: bool, theirs: bool) -> Settler:
    """Which settler this run uses, from plan §3.4's flag set.

    `--ours` and `--theirs` are `[l]` and `[r]` given once for every file, and
    they go through the same `Answer` the keypress produces -- so a flag cannot
    resolve a conflict differently from the key it stands for.

    A stdin that is not a terminal is `--no-input` whether or not the flag was
    passed. `sync` in a cron job, in a CI step, or with its input redirected has
    nobody to press a key, and the alternatives are blocking for ever or reading
    EOF and calling that a decision.
    """
    if ours:
        return always(LOCAL)
    if theirs:
        return always(REMOTE)
    if no_input or not sys.stdin.isatty():
        return always(SKIP)
    return lambda sides: ask(sides, config, sys.stdin, sys.stdout)
