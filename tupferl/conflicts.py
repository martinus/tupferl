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

The two sides are **what this computer has** and **what the repository has**, and
that is deliberately not "the file in `$HOME`". `sync.settle` builds a `Sides`
from three files and `sync.reconcile` builds one from three *commits*, where
neither side is a working-tree file at all. The wording holds for both, which is
why one type serves both -- and why the fields below are not named `home` and
`stored` after the places `settle` happens to read them from.

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

import os
import shlex
import subprocess
import sys
import tempfile
import termios
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple, TextIO

from tupferl import gitrepo, merge
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

#: The three the per-file review ends on. It is the conflict prompt without
#: `[b]` and `[e]`, which have nothing to offer where only one side moved:
#: there is no second version to keep and nothing to merge. `[d]` behaves the
#: same -- it shows and asks again.
REVIEWS = (LOCAL, REMOTE, SKIP)

#: How many lines of a one-sided diff the review prints before it stops and
#: says how many are left. Same argument as `SHOWN_HUNKS`: the question has to
#: stay on the screen. `[d]` prints the whole thing for the file that needs it.
SHOWN_DIFF = 24

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
    #: The last state both sides agreed on, or `None` when there is none -- the
    #: file was managed on two machines independently, or the snapshot was lost.
    #: For `sync.settle` that is this machine's snapshot; for `sync.reconcile` it
    #: is git's merge base. See `merge.three_way`.
    base: Blob | None
    #: What this computer has -- its `$HOME` file, or its committed version.
    #: Not optional: see the module docstring.
    home: Blob
    #: What the repository has: its stored copy, or the version on the branch
    #: being merged in.
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


def bare(line: bytes) -> bytes:
    """`line` without the carriage return a CRLF file leaves on the end of it.

    `git merge-file` writes **CRLF markers into a CRLF file** -- measured, git
    2.43 -- and `split(b"\n")` leaves the `\r` attached, so a marker line
    arrives as `b"<<<<<<< .bashrc (this computer)\r"` and matched nothing.

    That was not a display bug. It made `leftover` inert for the whole class of
    CRLF dotfiles: an `[e]` where the user quit without resolving was accepted,
    and the markers reached `$HOME`, the repository *and* the snapshot on both
    machines, with `sync` exiting 0. The CRLF cases in
    `tests/test_conflicts.py` are what see it -- every fixture here was LF until
    they were written, which is why the suite was green with the bug in place.
    """
    return line[:-1] if line.endswith(b"\r") else line


def hunks(sides: Sides) -> list[Hunk]:
    """The conflicting regions of `sides.marked`, for display only.

    The two labelled markers are matched against exactly what `merge` wrote. A
    dotfiles repository is one of the few places a file legitimately *contains*
    conflict markers -- a gitattributes example, a merge driver's documentation,
    somebody's own test fixture -- and matching a bare `<<<<<<<` would split a
    file at a line nobody was arguing about.

    The separator cannot be matched that carefully, because git writes it with
    no label: a line of the file that is exactly seven equals signs cannot be
    told apart from it, and the *first* such line inside a region ends this side
    whether or not it was meant to. That is why `describe` checks the parse
    against the two real files rather than trusting it -- a mis-split shows one
    side as empty, and a user who believes it presses the other key and loses
    their own edit.

    `split(b"\n")` rather than `splitlines()`: the latter splits on eight more
    characters than git does, so a file containing one of them would shift every
    line number after it. Nothing here recovers from a malformed run of markers;
    a region that never closes is simply not reported.
    """
    if sides.marked is None:
        return []
    opens, closes = merge.markers_for(str(sides.name))
    middle = b"======="

    found: list[Hunk] = []
    start: int | None = None
    mine: list[bytes] = []
    theirs: list[bytes] | None = None
    for number, raw in enumerate(sides.marked.split(b"\n"), start=1):
        text = bare(raw)
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
            theirs.append(raw)
        else:
            mine.append(raw)
    return found


def somewhere_in(run: list[bytes], whole: list[bytes]) -> bool:
    """Whether `run` appears as a block of consecutive lines of `whole`."""
    # survivor: branch -- tupferl/conflicts.py:231 in somewhere_in() -- the `if` is never taken --
    #   equivalent, measured: with the guard skipped, an empty `run` still returns `True` from the
    #   comprehension -- `whole[0:0] == []` holds and the range is never empty. The guard says the
    #   answer plainly rather than deriving it.
    if not run:
        return True
    # survivor: arith -- tupferl/conflicts.py:233 in somewhere_in() -- `-` becomes `+` --
    #   equivalent: the extra iterations slice past the end of `whole` and produce lists shorter
    #   than `run`, which cannot equal it. Measured: a run that is absent stays absent with the
    #   wider range.
    return any(whole[at : at + len(run)] == run for at in range(len(whole) - len(run) + 1))


def trustworthy(sides: Sides, regions: list[Hunk]) -> bool:
    """Whether the parsed sides really are what the two computers hold.

    A conflict region's lines come verbatim from one file or the other, so each
    hunk's `mine` must be a block of `$HOME`'s lines and each `theirs` a block
    of the repository's. When a line inside a region is itself `=======` the
    split lands in the wrong place and this fails -- which is the point.
    `describe` then shows no sides at all rather than showing them swapped, and
    points at `[d]`, which reads the two files directly and cannot be fooled.

    Measured case: `$HOME` holding `=======` immediately above its own change
    made the prompt print "this computer" with nothing under it and attribute
    that change to the repository. A user who trusts that display presses `[r]`
    and destroys the line they wrote.
    """
    mine = sides.home.data.split(b"\n")
    theirs = sides.stored.data.split(b"\n")
    return all(
        somewhere_in(hunk.mine, mine) and somewhere_in(hunk.theirs, theirs) for hunk in regions
    )


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
    # git's count, not `len(regions)`. They agree on everything git produces;
    # where they cannot -- a file whose own content looks like a marker -- git's
    # is the one that decided there was a conflict at all, and the parse below
    # is the one that is refused rather than shown.
    lines = [
        f"{paint(str(sides.name), BOLD, colour)}: "
        f"{sides.conflicts} conflict{'s' if sides.conflicts > 1 else ''} to settle."
    ]
    if not trustworthy(sides, regions):
        lines.append("")
        lines.append(
            "  tupferl cannot show the two sides of this one: a line in it looks "
            "like a\n  conflict marker, so where one side ends is not decidable. "
            "Press [d] for the\n  whole difference, which is read from the two "
            "files and cannot be fooled."
        )
        return "\n".join(lines)

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

    The two sides against each other, and not either against the merge base: the
    question at the prompt is which of *those two* to keep, and a diff against a
    third version is a different question.

    For a `Sides` built by `sync.reconcile` neither side is the user's `$HOME`
    file -- both are committed blobs -- so this says "this computer" and "the
    repository", which is true of both kinds of conflict.

    `merge.unified` does the diffing, because milestone 6's `tupferl diff` shows
    the same two sides of the same two files and a second renderer here would be
    a second answer to "which one is `---` and which is `+++`".
    """
    return merge.unified(str(sides.name), sides.home.data, sides.stored.data)


def editor(repo: Path | None = None) -> str:
    """The command `[e]` runs: git's answer, in git's order.

    `GIT_EDITOR`, then `core.editor`, then `$VISUAL`, then `$EDITOR` -- because
    someone who configured an editor for git configured how they edit text, not
    how they edit a commit message.

    **There was an `editor` in `.tupferl/config.toml` above all of these**, and
    plan §5 asked for it. It went when git's sources arrived: that file is
    committed and shared, so an editor set there is one machine's choice landing
    on every other, and the four sources below it are all per-machine. Someone
    who wants a different editor for a three-way conflict than for a commit
    message says so per run -- `GIT_EDITOR=meld tupferl sync` -- which is a
    sentence rather than a setting.

    `repo` is optional so the resolution is answerable without one: the tests
    that only ask about precedence have no repository, and inventing one for
    them would make the question harder to ask than it is.

    No fallback to `vi`. Guessing an editor that may not be installed turns "you
    have not told me which editor to use" into whatever `vi` does on a machine
    without it, and plan §5 asks every error to say what the user can do next.
    """
    said = (
        os.environ.get("GIT_EDITOR")
        or (gitrepo.configured_editor(repo) if repo is not None else "")
        or os.environ.get("VISUAL")
        or os.environ.get("EDITOR")
    )
    if not said:
        raise TupferlError(
            "no editor is set, so there is nothing to open the merged file with; "
            "set $EDITOR, or git's `core.editor`."
        )
    return said


def leftover(sides: Sides, data: bytes) -> bool:
    """Whether tupferl's own conflict markers are still in `data`.

    Its own, matched in full against what `merge` wrote -- the same rule as
    `hunks`, from the same `merge.markers_for`, so the two cannot be half
    changed. A user whose dotfile legitimately contains `<<<<<<<` is not told
    they left the merge half-finished; a user who really did leave it
    half-finished is told, before those markers reach both computers.

    `bare` on every line, because a CRLF file's markers carry a `\r` and
    without it this returned `False` for every one of them -- see `bare`.
    """
    marks = set(merge.markers_for(str(sides.name)))
    return any(bare(line) in marks for line in data.split(b"\n"))


@contextmanager
def workspace(name: PurePosixPath) -> Iterator[Path]:
    """A throwaway file named after `name`, so the editor picks the right mode.

    The basename only, in a temporary directory of its own: an editor chooses
    its syntax highlighting and its indent rules from the file name, and a user
    editing `init.lua` in a file called `tmp9k2j` gets neither. The basename and
    not the whole path, because `PurePosixPath.name` cannot contain a separator
    and so cannot reach outside the directory.
    """
    with tempfile.TemporaryDirectory(prefix="tupferl-edit-") as box:
        yield Path(box) / name.name


def edit(sides: Sides, buffer: bytes, out: TextIO, repo: Path | None = None) -> bytes | None:
    """Open `buffer` in the user's editor and return what came back.

    `None` means the editor never gave an answer -- it exited non-zero, or it
    removed the file, or it left something that is not a readable file where the
    file was. `ask` re-asks in that case rather than aborting, and it keeps the
    buffer it had: there is nothing here to keep.

    A file that came back *is* returned, markers and all. Whether it is finished
    is `ask`'s question, not this one, and the split matters: a save that still
    has markers in it is text the user wrote, and handing it back is what lets
    the next `[e]` reopen their half-finished work instead of the pristine
    merge.

    **Every failure here is recoverable and none of them ends the run.** The
    user is standing at the prompt and can choose another key. That includes
    having no `$EDITOR`: `editor` raises, and this catches it, because an
    aborted `sync` would leave the conflicts it had already settled written to
    disk and uncommitted -- a much worse answer to a mistyped `e` than a line
    saying what to set.

    The buffer is a real file in a throwaway directory rather than `$HOME`'s
    copy. An editor opened directly on the managed file would leave the markers
    in `$HOME` if the user quit, and a sync interrupted after that is one where
    `$HOME` holds a file no program can read.

    `shlex.split`, because `EDITOR="code --wait"` and `EDITOR="emacs -nw"` are
    both ordinary. The child inherits this process's terminal -- it must, since
    it is a full-screen program the user is about to type into -- so nothing
    here captures its output.
    """
    try:
        command = editor(repo)
    except TupferlError as unset:
        print(f"{unset}", file=out)
        return None
    with workspace(sides.name) as scratch:
        scratch.write_bytes(buffer)
        done = subprocess.run([*shlex.split(command), str(scratch)], check=False)
        if done.returncode != 0:
            print(f"{command} exited with {done.returncode}; nothing was changed.", file=out)
            return None
        try:
            return scratch.read_bytes()
        except OSError as gone:
            # An editor that removed the file, or left a directory in its place.
            # Not a traceback and not an aborted run: `sync` exits 1 for "there
            # are conflicts left", and a crash that also exits 1 is one a script
            # reads as a normal result.
            print(f"{command} left nothing to read ({gone.strerror}).", file=out)
            return None


#: The most bytes one keypress can be. Six covers a modified arrow
#: (`\x1b[1;5B`); eight leaves room without letting a runaway read swallow a
#: line of a paste.
KEYPRESS = 8

#: How long to wait for the rest of an escape sequence, in tenths of a second
#: (`VTIME`'s unit). A terminal sends the whole sequence in one burst, so this
#: only has to outlast the write; a lone Escape costs exactly this much before
#: the prompt calls it "not a key", which is the same trade every editor makes.
ESCAPE_WAIT = 1


def rest_of_escape(fd: int, mode: list[Any]) -> bytes:
    """Everything after an `\x1b` that belongs to the same keypress.

    One byte at a time, stopping at the byte that ends the sequence rather than
    reading a fixed number: `os.read(fd, 8)` returns *everything the terminal
    has*, which is the escape sequence **and the key pressed after it**. That is
    not hypothetical -- it swallowed the answer in three tests here, on the first
    attempt at this function.

    `ESC [` and `ESC O` introduce a sequence; anything else after `ESC` is a lone
    Escape followed by an ordinary key. A CSI sequence ends at its first byte in
    `@`..`~`, which is what the loop watches for.

    `VMIN=0` with `VTIME` is what makes a lone Escape return rather than block:
    with nothing more to read, the call comes back empty after `ESCAPE_WAIT`.
    """
    mode[6][termios.VMIN] = 0
    mode[6][termios.VTIME] = ESCAPE_WAIT
    termios.tcsetattr(fd, termios.TCSANOW, mode)
    rest = b""
    while len(rest) < KEYPRESS:
        more = os.read(fd, 1)
        if not more:
            break
        rest += more
        if len(rest) == 1 and more not in (b"[", b"O"):
            break
        if len(rest) > 1 and b"@" <= more <= b"~":
            break
    return rest


def one_key(source: TextIO) -> str:
    """One keypress from `source`, lower-cased. Empty string at end of input.

    Two paths, and the second is not a test affordance: `sync` with a pipe on
    stdin takes it whenever something has already decided the prompt is worth
    showing.

    **A whole escape sequence is one keypress, not three.** `source.read(1)`
    returns one character and leaves the rest in the stream's buffer, so a single
    press of the Down arrow arrived as `\x1b`, `[` and `B` at three successive
    calls -- and `b` is *keep both*. One arrow key, or one notch of a mouse wheel
    in a terminal without the alternate screen, silently wrote a union merge into
    `$HOME`, the repository and the snapshot, and `sync` exited 0. Anything that
    is not exactly one character is not a key, and `ask` says so rather than
    acting on its last byte.

    The read is of the raw descriptor for that reason: a buffered stream holds
    the tail of the sequence where no drain can reach it. Nothing else in this
    module reads `source`, so mixing a raw read with a buffered one cannot arise.

    The terminal flags are set here rather than through `tty.setcbreak`, which
    changed behaviour in Python 3.12: it stopped clearing `ECHO`, so the same
    call echoes the key on 3.12 and does not on 3.10. `ICANON` is what makes the
    read return without waiting for Enter -- plan §3.4's "every choice is one
    keypress" -- and `ECHO` is cleared so the key can be echoed back with a
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
        # `TCSANOW`, never `TCSADRAIN`. Every flag set here is an *input* flag,
        # so there is no output to wait for -- and `TCSADRAIN` waits for the
        # terminal's pending output to drain, which can block for ever when
        # nobody is reading the other end. A pty starts with `ECHO` on, so
        # anything typed at it before this call is sitting in that output queue;
        # if it fills, the drain never completes and the prompt hangs before it
        # has read a byte. Linux's buffer is large enough to hide it and macOS's
        # is not, which is the shape CLAUDE.md warns about: green on three legs
        # and hung on the fourth.
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        first = os.read(fd, 1)
        if first == b"\x1b":
            first += rest_of_escape(fd, mode)
        return first.decode("utf-8", "replace").lower()
    finally:
        # In a `finally` because an interrupt at the prompt would otherwise leave
        # the user's shell with echo off, which looks like a hung terminal. And
        # `TCSANOW` for the reason above, doubly so here: a restore that blocks
        # leaves the terminal in exactly the state this line exists to undo.
        termios.tcsetattr(fd, termios.TCSANOW, before)


def ask(sides: Sides, source: TextIO, out: TextIO, repo: Path | None = None) -> Answer:
    """Plan §3.4 step 4: show the conflict and settle it with one keypress.

    `[d]` and a key that is not on offer both re-ask, which is why this is a
    loop. So does `[e]` when the editor gave no answer -- and when it gave one
    that still has the markers in it, the text the user wrote becomes the buffer
    the next `[e]` opens, so a half-finished resolution is not lost by pressing
    the wrong key next.

    End of input is `[s]`. That is the same answer `--no-input` gives, and it is
    the only one that cannot lose something the user meant to keep.

    The question and the keys are built once. They depend on nothing the loop
    changes, and `describe` re-parses the whole marked file -- so re-*printing*
    is intended and re-*computing* was not.
    """
    colour = coloured(out)
    question = describe(sides, colour)
    keys = choices(sides, colour)
    buffer = sides.marked
    while True:
        print(f"\n{question}\n", file=out)
        print(keys, file=out)
        out.flush()

        key = one_key(source)
        if key == "":
            print(f"{SKIP}   (end of input)", file=out)
            return Answer(SKIP)
        if len(key) != 1:
            # An escape sequence, or a keypress this terminal sends as several
            # bytes. Echoed as a repr rather than as itself: the raw bytes would
            # move the cursor or clear the screen on their way out.
            print(f"{key!r} is not a key.", file=out)
            continue
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
            return Answer(
                BOTH,
                merge.keep_both(
                    str(sides.name),
                    None if sides.base is None else sides.base.data,
                    sides.home.data,
                    sides.stored.data,
                ),
            )
        if key == EDIT:
            # `buffer` is bytes here: `sides.binary` was checked above, so
            # `marked` is not `None`, and every reassignment below is bytes.
            assert buffer is not None
            typed = edit(sides, buffer, out, repo)
            if typed is None:
                continue
            buffer = typed
            if leftover(sides, typed):
                print(
                    "the merged file still has tupferl's conflict markers in it, so it "
                    "was not saved; remove them, or choose another key. What you wrote "
                    "is what [e] will open next.",
                    file=out,
                )
                continue
            return Answer(EDIT, typed)
        print(f"{key!r} is not one of the keys.", file=out)


def always(choice: str) -> Settler:
    """The settler a flag is: this answer, for every conflict, without asking."""
    return lambda sides: Answer(choice)


#: What one file's review is asked about, and what it answers with. `sync`
#: builds the diff and names the direction, because it is what knows both; this
#: module knows how to ask. The return is one of `REVIEWS`.
Reviewer = Callable[["Change"], str]


class Change(NamedTuple):
    """One file the next sync would move, and which way it would move it.

    Built by `sync`, which has the outcome, and handed here so that the prompt
    depends on the *question* rather than on the sync engine -- the same
    boundary `Sides` draws, and what keeps this module out of an import cycle
    with `sync`.
    """

    name: PurePosixPath
    #: The diff, oriented by `sync.pushes` before it gets here: the repository's
    #: copy on `-`, this computer's on `+`. Same orientation `status --diff`
    #: gives the same file, from the same rule, so the preview and the prompt
    #: cannot describe one file differently.
    diff: str


def happening(change: Change, colour: bool) -> str:
    """The sentence above the keys: what the sync is about to do to this file.

    Said in words rather than left to the diff's `---`/`+++`, because the whole
    complaint that produced this prompt was that a diff's direction is not
    obvious enough to bet a dotfile on.
    """
    said = f"{change.name}: you changed this here; the repository has the older copy."
    return paint(said, BOLD, colour)


def offers(change: Change, colour: bool) -> str:
    """The keys, worded for the direction.

    The same two letters mean the same two *sides* in both directions -- `[l]`
    is always this computer, `[r]` is always the repository, exactly as in the
    conflict prompt -- and only the consequence differs. Spelling the
    consequence out is what stops `[r]` reading as "reject": on an outbound file
    it throws away the edit you just made, which is the one keypress here that
    cannot be undone from inside tupferl.
    """
    keep = f"[{LOCAL}] store your version   [{REMOTE}] discard it, take the repository's"
    return paint(f"  {keep}\n  [{DIFF}] show the whole diff   [{SKIP}] skip", BOLD, colour)


def shown(change: Change) -> str:
    """The diff, capped, with a line saying what was left out.

    Capped rather than paged: a pager here would take over the terminal the
    prompt is about to read a keypress from. `[d]` is the way to see all of it,
    which is the same answer the conflict prompt gives for the same reason.
    """
    lines = change.diff.split("\n")
    if len(lines) <= SHOWN_DIFF:
        return change.diff
    left = len(lines) - SHOWN_DIFF
    return "\n".join([*lines[:SHOWN_DIFF], f"... and {left} more line(s); [{DIFF}] shows them"])


def review(change: Change, source: TextIO, out: TextIO) -> str:
    """Show one file's change and settle it with one keypress.

    `ask`'s shape, and deliberately so: the two prompts are the same loop with
    a different key set, and a user who has learned one has learned the other.
    Anything not on offer re-asks; `[d]` prints the whole diff and re-asks; end
    of input is `[s]`, the only answer that cannot lose something.
    """
    colour = coloured(out)
    question = happening(change, colour)
    keys = offers(change, colour)
    while True:
        print(f"\n{question}\n", file=out)
        print(shown(change), file=out)
        print(keys, file=out)
        out.flush()

        key = one_key(source)
        if key == "":
            print(f"{SKIP}   (end of input)", file=out)
            return SKIP
        if len(key) != 1:
            print(f"{key!r} is not a key.", file=out)
            continue
        print(key, file=out)
        if key in REVIEWS:
            return key
        if key == DIFF:
            print(change.diff, file=out)
            continue
        print(f"{key!r} is not one of the keys.", file=out)


def reviewing(auto: bool, ours: bool, theirs: bool, no_input: bool) -> Reviewer | None:
    """The reviewer this run uses, or `None` for a run that asks about nothing.

    **`None` rather than a reviewer that answers automatically**, so `settle`
    keeps the outcome `resolve` produced instead of routing it through a table
    that would have to name the same action back. A one-sided change already
    has a right answer; the prompt exists to let a person disagree with it, and
    a run with nobody there should not be re-deciding it through a second path.

    Every flag that already means "do not ask me" turns it off, and so does a
    stdin that is not a terminal -- `answering`'s reasoning, for the same
    reason: `init` runs a sync, so does CI, and neither has anyone to press a
    key. `--ours` and `--theirs` are included because a run that has answered
    every conflict in advance has said what it wants; stopping it on a
    one-sided change would make those flags mean less than they say.
    """
    if auto or ours or theirs or no_input or not sys.stdin.isatty():
        return None
    return lambda change: review(change, sys.stdin, sys.stdout)


def answering(no_input: bool, ours: bool, theirs: bool, repo: Path | None = None) -> Settler:
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
    return lambda sides: ask(sides, sys.stdin, sys.stdout, repo)
