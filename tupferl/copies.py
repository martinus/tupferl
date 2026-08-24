"""A stored copy: its bytes, the one mode bit that travels, and one rule for
deciding whether what is already there is that.

Plan §3.1 is the storage model -- the repository holds a *copy* of each managed
file, never a link -- so "read a file", "write a file" and "is the file already
exactly this" are the vocabulary every command shares. This module is below both
`manage` (which copies files in) and `sync` (which merges them), because it was
briefly in neither and that had already gone wrong:

**Two definitions of "already exactly that" were writing the same file.** Since
`add` learned to seed the merge base, `add` wrote `.tupferl/state/<host>/<name>`
by comparing `filecmp` plus the whole permission mask, and `sync` wrote it by
comparing bytes plus the executable bit. The two agree today -- a stored copy is
always chmod'd to `EXECUTABLE` or `PLAIN`, so the mask cannot differ -- which is
exactly why nobody would notice if that stopped being true. The symptom would be
a *spurious conflict*: two machines whose merge bases differ by a byte nobody
wrote. One predicate, used by both, removes the class.

**Only the executable bit is preserved** (plan §5). Not `copy2`, which carries
the whole mode across: git records exactly this one bit, so any other bit kept
in the working tree is lost on the first clone and the two machines then
disagree about a file neither of them changed.

**Bytes, not text.** These files go to disk and come back; `manifest` admits any
regular file under the size limit. Decoding would mean choosing an encoding on
the user's behalf, and getting it wrong turns a sync into corruption.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import NamedTuple

#: Executable by *anyone*, not by the owner alone. A script that arrived from a
#: tarball as 0o711 is a script, and storing it as non-executable would put it
#: back on the other machine unrunnable -- a failure the user would blame on the
#: program that reads it.
EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

#: The two modes a stored copy can have, and there is no third.
EXECUTABLE = 0o755
PLAIN = 0o644

#: The two modes git records for a regular file in a tree or an index. Anything
#: else is a symlink (`0o120000`) or a submodule (`0o160000`), and neither is a
#: copy of a file's bytes.
#:
#: Load-bearing where a path comes from git rather than from `manifest`, which
#: refuses both at `add` time. `sync.reconcile` settles conflicts from the index,
#: and `copies.write` follows a symlink -- so without this check a settled
#: conflict over a committed symlink writes **through** it, to a file outside the
#: repository entirely. Reproduced: two branches committing `link` pointing at
#: `../victim/target`, settled with `--ours`, destroyed `victim/target`.
REGULAR = (0o100644, 0o100755)


def executable(mode: int) -> bool:
    """Whether a git file mode carries the bit that travels.

    One rule, in the module that owns what a stored copy is. `read` asks the
    same question of a `stat` result and `mode_for` answers it for a path; a
    third spelling beside them is the drift this module exists to prevent.
    """
    return bool(mode & EXEC_BITS)


class Blob(NamedTuple):
    """A file as tupferl cares about it, and the unit of comparison.

    The executable bit is part of the *value* rather than metadata beside it,
    because it is something the user changes and expects to travel: `chmod +x
    ~/.local/bin/x` with no edit is a real change, and a comparison that ignored
    it would leave two machines permanently disagreeing about a file neither had
    edited.
    """

    data: bytes
    executable: bool


def mode_for(source: Path) -> int:
    """The mode a stored copy of `source` gets: `EXECUTABLE`, or `PLAIN`."""
    return EXECUTABLE if source.stat().st_mode & EXEC_BITS else PLAIN


def read(path: Path) -> Blob | None:
    """The file at `path`, or `None` if there is no *regular* file there.

    `lstat` rather than `stat`: a symlink where a managed file should be is not
    the file, and following it would read -- and later overwrite -- something the
    user never asked tupferl to manage. `manifest` refuses symlinks at `add`
    time; this is the same rule wherever a copy is read afterwards, for a path
    that has become one since.

    `None` for missing and for not-a-file alike. The caller separates them, and
    only where the difference matters.
    """
    try:
        found = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(found.st_mode):
        return None
    return Blob(path.read_bytes(), bool(found.st_mode & EXEC_BITS))


def write(path: Path, blob: Blob) -> bool:
    """Put `blob` at `path`; `False` if it was already exactly that.

    The comparison is the one rule this module exists for. It is not an
    optimisation -- files are a megabyte at most by default -- it is what makes a
    second `sync` with no edits touch nothing at all (plan §7.2's idempotence
    property), and what lets `add` say "added", "updated" or nothing at all
    rather than printing "added" and then that the repository did not change.
    """
    if read(path) == blob:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(blob.data)
    path.chmod(EXECUTABLE if blob.executable else PLAIN)
    return True


def store(source: Path, target: Path) -> str | None:
    """Copy `source` to `target`; say what happened, or `None` if nothing did.

    Through `read` and `write` rather than `shutil.copyfile` plus `filecmp`, so
    that the question "is the target already this file?" has one answer in this
    program. See the module docstring for what the two answers cost.
    """
    blob = read(source)
    if blob is None:
        # A regular file was there when `manifest.check` looked. Something else
        # is there now -- so this is a race, not a rule the caller broke, and
        # `None` would report it as "nothing to do".
        raise OSError(f"{source} is no longer a regular file")
    existed = target.is_file()
    if not write(target, blob):
        return None
    return "updated" if existed else "added"
