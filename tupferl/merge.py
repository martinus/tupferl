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

import tempfile
from pathlib import Path
from typing import NamedTuple

from tupferl import gitrepo
from tupferl.errors import TupferlError

#: The most conflict hunks `git merge-file` will report. Above this its exit
#: status saturates, so a larger number cannot be distinguished from an error.
MOST_CONFLICTS = 127

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


def three_way(name: str, base: bytes | None, ours: bytes, theirs: bytes) -> Merged:
    """Merge `ours` and `theirs` over their common ancestor `base`.

    `name` is the managed file's name -- it reaches the conflict markers and the
    error message, and is not a path, so nothing here reads it as one.

    A side that is not text makes the whole file one conflict, with no merged
    bytes to return. See `is_text`.

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

        done = gitrepo.merge_file(mine, common, yours, labels_for(name))
        if done.code is None or not 0 <= done.code <= MOST_CONFLICTS:
            # Not a conflict: git could not run, or refused the inputs. Reported
            # rather than folded into "conflicted", because the two need
            # different actions from the user and a merge that never happened
            # must not look like one that happened badly.
            raise TupferlError(
                f"could not merge {name}: {gitrepo.reason(done)}; "
                f"run `tupferl doctor` to check your git installation."
            )
        return Merged(mine.read_bytes(), done.code)
