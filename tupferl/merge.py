"""The 3-way merge, delegated to `git merge-file`.

Plan §9 left the implementation open and recommended this one; the README
records the decision. The argument is not that a hand-written 3-way merge is
hard -- it is that it would be the most defect-dense file in the project, in the
one place where a defect silently loses a line the user wrote. git is already a
hard requirement, and its merge has been read by more people than this program
will ever have users.

Three things about the wrapper are load-bearing:

**The conflict count comes from git's exit status**, not from searching the
output for `<<<<<<<`. A dotfile may legitimately contain that string -- a
gitattributes example, a merge driver's own documentation, a test fixture in
somebody's dotfiles repository -- and a merge that reported a conflict for it
would refuse to sync a file with nothing wrong with it.

**Bytes throughout.** These files come off disk and go back to disk, and
`tupferl add` admits any regular file under the size limit. Decoding to `str`
would mean choosing an encoding on the user's behalf, and getting it wrong turns
a sync into corruption. git treats its inputs as bytes with newlines in them,
which is exactly the model here.

**A file with a NUL byte in it cannot be merged at all.** `git merge-file`
refuses one -- "Cannot merge binary files" -- and that is not a failure to
report, it is the honest answer: there are no lines to take from each side.
`is_text` asks the question here rather than reading git's English back out of
stderr, using git's own rule (`buffer_is_binary`: a NUL in the first 8000
bytes), measured against git 2.43 at the byte either side of the boundary. The
property test below found this on its first run, with `edit=['\x00']`.

**A missing snapshot is an empty base, not a skipped merge.** No snapshot means
no common ancestor -- the file was added on two machines independently, or the
state directory was lost -- and with an empty base every difference between the
two sides is a genuine disagreement that git reports as a conflict. That is the
true answer: nothing in the data says which side is newer.
"""

from __future__ import annotations

import difflib
import tempfile
from pathlib import Path
from typing import NamedTuple

from tupferl import gitrepo
from tupferl.errors import TupferlError

#: How much of a file git looks at before deciding it is binary, and therefore
#: how much `is_text` looks at. The same number as git's `FIRST_FEW_BYTES`: a
#: probe that read *more* would call a file binary that git would merge, and one
#: that read less would hand git a file it refuses.
PROBE = 8000

#: What a binary file that both sides changed counts as: one conflict, covering
#: the whole file. There are no hunks to count -- the two versions disagree and
#: nothing in either says which lines correspond.
WHOLE_FILE = 1


class Merged(NamedTuple):
    """The merged bytes, and how many hunks git could not decide."""

    #: The merged file, or `None` when there is no such thing -- a binary file
    #: that both sides changed. `None` exactly when `conflicts` is `WHOLE_FILE`
    #: for that reason, so a caller that reads `data` only when `conflicts == 0`
    #: never sees it. Milestone 4's prompt is the one place that will: `[e]` edit
    #: and `[b]` keep both have nothing to offer for a binary file, where `[l]`
    #: and `[r]` still do.
    data: bytes | None
    #: 0 for a clean merge. Above 0, `data` carries standard conflict markers and
    #: is what milestone 4's `[e]` hands to the user's editor.
    conflicts: int


def is_text(data: bytes) -> bool:
    """Whether git will merge this as lines rather than refusing it as binary.

    git's own rule, not a guess at it: a NUL byte within the first `PROBE`
    bytes. Measured against git 2.43 at 7999, 8000 and 8001 -- the first is
    refused and the other two merge -- because a probe that disagreed with git
    by one byte would produce, rarely, the failure this exists to prevent.
    """
    return b"\0" not in data[:PROBE]


def labels_for(name: str) -> tuple[str, str, str]:
    """What the conflict markers say, for the three sides.

    The file's own name in each, because a marker reading `<<<<<<< ours` in an
    editor opened on `.bashrc` is telling the reader something they already know.
    The wording matches the prompt in plan §3.4 -- "this computer" against "the
    repository" -- so the marker and the prompt cannot describe the same two
    sides differently.
    """
    return (f"{name} (this computer)", f"{name} (last sync)", f"{name} (the repository)")


def unified(name: str, mine: bytes, theirs: bytes, reverse: bool = False) -> str:
    """A unified diff of the two sides, labelled the way the markers are.

    `reverse` puts the repository on the `-` side and `$HOME` on the `+` side.
    **It swaps the bytes and the labels in one expression each**, so the two
    cannot come apart -- a diff whose header says "this computer" over the
    repository's lines is worse than no diff, because it is read rather than
    doubted. Passing `theirs` and `mine` the other way round at a call site
    would do exactly that, which is why this is a flag here rather than an
    argument order there.

    Why it exists: `tupferl status --diff` shows what the *next sync* will do,
    and sync's direction is per file. For a file whose local edit is about to be
    pushed, the repository is the side being replaced, so it belongs on `-`.
    Without this the diff read as "here is what discarding your edit would do" --
    the exact opposite of what was about to happen. The conflict prompt's `[d]`
    keeps the default, where there is no direction to show: both sides changed.

    Here rather than in `conflicts`, for `markers_for`'s reason: the prompt's
    `[d]`, the conflict markers and `tupferl diff` all name the same two sides,
    and three spellings of "this computer" against "the repository" is two
    chances for them to disagree about which is which. `labels_for` is one line
    above; this is its third reader.

    `difflib.diff_bytes` rather than decoding first, so a file that is not UTF-8
    still produces a diff of the right lines. Only the finished text is decoded,
    with `errors="replace"`, because this is display and a `UnicodeDecodeError`
    here would refuse to show the user the one file they asked about.

    `split(b"\n")` and not `splitlines()`. On **bytes** the extra separator is
    `\r` -- not the long list `str.splitlines()` adds, which CLAUDE.md records
    for the `str` case and which was written into this docstring first and was
    wrong: `b"a\x0bb".splitlines()` is one line. `\r` is enough on its own here.
    A CRLF dotfile is every line ending in one, so `splitlines()` would show a
    diff of half-lines with the carriage returns silently eaten, and every hunk
    header after the first would be a line number the file does not have. git
    splits on `\n` alone, and this diff sits beside git's.

    Empty when the two are identical, which is the caller's test for "there is
    nothing to show" -- rather than a second byte comparison that could differ
    from what was actually diffed.
    """
    mine_at, _, theirs_at = labels_for(name)
    before, after = (theirs, mine) if reverse else (mine, theirs)
    before_at, after_at = (theirs_at, mine_at) if reverse else (mine_at, theirs_at)
    rows = difflib.diff_bytes(
        difflib.unified_diff,
        before.split(b"\n"),
        after.split(b"\n"),
        before_at.encode(),
        after_at.encode(),
        lineterm=b"",
    )
    return "\n".join(row.decode("utf-8", "replace") for row in rows)


def markers_for(name: str) -> tuple[bytes, bytes]:
    """The two marker lines `three_way` writes for `name`, as bytes.

    Here rather than in `conflicts`, beside the labels they are built from.
    Both the prompt's display and its "did you finish?" check match these
    exactly, and a spelling that lives in two places is one that can be half
    changed -- in the one module whose whole job is to agree with what git
    wrote.

    The separator, `=======`, is deliberately not here: it carries no label, so
    it cannot be told apart from a line of a file that happens to be seven
    equals signs. `describe` handles that by checking its own parse rather than
    by matching harder.
    """
    mine_at, _, theirs_at = labels_for(name)
    return f"<<<<<<< {mine_at}".encode(), f">>>>>>> {theirs_at}".encode()


def keep_both(name: str, base: bytes | None, ours: bytes, theirs: bytes) -> bytes:
    """The conflict prompt's `[b]`: every hunk keeps both versions, in turn.

    git's `--union`, not a marker-stripper written here. Stripping the markers
    out of `three_way`'s output means re-deriving the hunk boundaries git has
    already computed, and getting that wrong silently deletes a line somebody
    wrote.

    Returns bytes rather than a `Merged`, because a union merge has no conflict
    count to report and always produces a file: both of `Merged`'s fields would
    be dead for every caller, and the caller would have to assert them so. Only
    valid for a file `is_text` admits -- `[b]` is not offered for one that is
    not, since there are no lines to take from each side.
    """
    merged = three_way(name, base, ours, theirs, union=True)
    # survivor: connector -- tupferl/merge.py:171 in keep_both() -- `or` becomes `and` -- named in
    #   the code: `keep_both`'s own comment says this branch is not reachable through the prompt,
    #   which offers `[b]` only for a text file, and reports rather than asserts the two ways it
    #   could happen.
    if merged.data is None or merged.conflicts:
        # Not reachable through the prompt, which offers `[b]` only for a text
        # file. Reported rather than asserted because the two ways it could
        # happen -- a binary side, or a git whose `--union` conflicts -- are
        # both things about the machine rather than about this program.
        raise TupferlError(
            f"could not keep both versions of {name}: git returned "
            f"{merged.conflicts} conflicts from a union merge, which should have "
            f"none; run `tupferl doctor` to check your git installation."
        )
    return merged.data


def three_way(
    name: str, base: bytes | None, ours: bytes, theirs: bytes, union: bool = False
) -> Merged:
    """Merge `ours` and `theirs` over their common ancestor `base`.

    `name` is the managed file's name -- it reaches the conflict markers and the
    error message, and is not a path, so nothing here reads it as one.

    A side that is not text makes the whole file one conflict, with no merged
    bytes to return. See `is_text`.

    `union` is git's `--union`, and `keep_both` above is the one caller: it
    returns the bytes rather than this three-field answer, because a union
    merge cannot conflict and cannot fail to produce a file.

    The three sides are written to a throwaway directory rather than merged from
    memory: `git merge-file` takes file names, and the alternative is feeding it
    `/dev/stdin` three times, which is not a thing. The directory is removed even
    if the merge raises.
    """
    if not all(is_text(side) for side in (base or b"", ours, theirs)):
        return Merged(None, WHOLE_FILE)

    with tempfile.TemporaryDirectory(prefix="tupferl-merge-") as box:
        where = Path(box)
        # `merge_file` rewrites its first argument in place, so `ours` is a copy
        # and the caller's bytes are untouched.
        mine = where / "ours"
        mine.write_bytes(ours)
        common = where / "base"
        common.write_bytes(b"" if base is None else base)
        yours = where / "theirs"
        yours.write_bytes(theirs)

        done = gitrepo.merge_file(mine, common, yours, labels_for(name), union=union)
        if done.code is None or not 0 <= done.code <= gitrepo.MOST_CONFLICTS:
            # Not a conflict: git could not run, or refused the inputs. Reported
            # rather than folded into "conflicted", because the two need
            # different actions from the user and a merge that never happened
            # must not look like one that happened badly.
            raise TupferlError(
                f"could not merge {name}: {gitrepo.reason(done)}; "
                f"run `tupferl doctor` to check your git installation."
            )
        return Merged(mine.read_bytes(), done.code)
