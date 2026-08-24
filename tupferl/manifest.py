"""What the repository holds, and what it is allowed to take.

Two halves of one question. `managed` reads the repository and says what is in
it; `check` reads a `$HOME` path and says whether it may go in. They are here
together because the answer has to be the same shape from both directions -- a
file admitted under one name and then listed under another is a file the user
cannot remove.

**The admission rules are where this module earns its place.** `tupferl add`
copies a file into a git repository that will be pushed to a remote, so a rule
that lets the wrong file through is not an inconvenience. Four of the six exist
for that reason rather than for tidiness:

- **Symlinks are refused** (plan §5). A copy cannot represent a link, so the
  alternative is copying what it *points at* -- and `~/.aws/credentials` as a
  symlink to somewhere unreadable-looking is exactly how a secret ends up
  committed under a name nobody would search for.
- **A path that goes through a symlink is refused too**, for the same reason one
  step up. Only the components *between* `$HOME` and the file are checked, never
  `$HOME` itself: on macOS `/tmp` is a symlink to `/private/tmp`, so a check that
  walked all the way up would refuse everything on a machine whose home is under
  it -- including every test.
- **The path must be under `$HOME` once `..` is collapsed.** Collapsing is
  lexical, and that is only sound because the link check above rules out the case
  where it would differ from the truth -- `check` refuses rather than returning a
  name when it does. Resolving links first instead would answer about a file the
  user never named.
- **The repository is not addable to itself.** It lives under `$HOME` by
  default (`~/.local/share/tupferl/repo`), so `tupferl add ~/.local` would
  otherwise walk into it and manage tupferl's own copies of everything.

The other two -- the size limit and the ignore list -- are ordinary settings
(plan §5), and they are checked last so that a file refused for a *safety*
reason says so rather than blaming its size.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from tupferl import paths
from tupferl.config import Config
from tupferl.errors import TupferlError


class Managed(NamedTuple):
    """One file the repository holds, from the repository's point of view."""

    #: Where it lives relative to `$HOME`, which is also where it lives in the
    #: repository (plan §3.2: the path *is* the mapping).
    name: PurePosixPath
    #: True when it came from `.tupferl/hosts/<hostname>/` rather than from the
    #: shared tree. Plan §4 asks `list` to mark these.
    host: bool

    # No `path` field, and no `marker`. Both were tried while writing this.
    #
    # `marker` -- what `list` prints beside a name -- is that command's
    # formatting, and `status` (plan §6) will want a different one from the same
    # data; presentation on the model makes the second one a change to the first.
    #
    # `path` -- the file on disk this host would actually use -- is what
    # milestone 3's sync will want, and adding it now would have been a field
    # with no reader. `roots` below is the part that earns its place today,
    # because three callers were each spelling its rule out.


def roots(repo: Path, host: str) -> tuple[Path, Path]:
    """The two trees a managed file can live in: shared, then this host's overlay.

    Plan §3.3's rule in one place. `add --host` writes into the second, `remove`
    clears both, `managed` merges them with the second winning, and milestone 3's
    sync will read them the same way rather than spelling it a fifth time.
    """
    return repo, paths.host_overlay(repo, host)


class Refused(NamedTuple):
    """One file that may not be managed, and the sentence saying why."""

    path: Path
    why: str


def named(path: str | Path) -> Path:
    """Expand `~`, make it absolute, and collapse `..` -- lexically.

    `os.path.normpath` rather than `Path.resolve`, and the difference is the
    whole point: `resolve` follows symlinks, so it would answer about a file the
    user did not name.

    The collapsed path is a *candidate*. It is only true if nothing between
    `$HOME` and the file is a link, which `check` establishes before it returns a
    name -- so a lexical answer never escapes this module unverified.
    """
    expanded = Path(os.path.expanduser(str(path)))
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.normpath(expanded))


def links_between(where: Path, home: Path) -> Path | None:
    """The first symlink on the way down from `home` to `where`, if any.

    Starts *below* `home` deliberately. `$HOME` itself may well be reached
    through a link -- it is on any macOS machine whose home is under `/tmp`, and
    in every test this project has -- and that says nothing about the file being
    added.
    """
    walked = home
    for part in where.relative_to(home).parts:
        walked = walked / part
        if walked.is_symlink():
            return walked
    return None


def ignored(name: PurePosixPath, patterns: list[str]) -> bool:
    """Whether `name` or any directory above it matches an ignore pattern.

    The parent check is what makes `ignore = [".cache"]` mean the whole subtree
    rather than one directory entry nobody stores anyway. Without it a user would
    have to write `.cache/**`, and would find out they had not when the contents
    turned up in a push.

    `fnmatchcase` rather than `fnmatch`: the latter folds case on macOS, so the
    same repository would ignore different files on two machines -- and the
    repository is the thing both machines share.
    """
    if not patterns:
        return False  # the default, and the branch most runs take
    candidates = [name, *name.parents]
    return any(
        fnmatchcase(str(candidate), pattern)
        for candidate in candidates
        if str(candidate) != "."
        for pattern in patterns
    )


def check(path: Path, home: Path, repo: Path, config: Config) -> PurePosixPath:
    """The repository-relative name for `path`, or a `TupferlError` saying no.

    Order matters and is asserted by tests: the safety rules come before the
    settings, so a symlink is refused as a symlink rather than as "too large" if
    it happens to point at something big.
    """
    # One `lstat` for all four questions asked below -- link-ness, existence,
    # type and size. Three separate calls were three separate moments, which is
    # what the comment further down already objected to for two of them; and
    # `lstat` rather than `stat` because the first question is about the link
    # itself, not about what it points at.
    try:
        found = os.lstat(path)
    except OSError:
        raise TupferlError(f"{path} does not exist; check the path.") from None
    if stat.S_ISLNK(found.st_mode):
        raise TupferlError(
            f"{path} is a symlink, and tupferl stores copies rather than links; "
            f"add what it points at instead."
        )

    try:
        relative = PurePosixPath(path.relative_to(home).as_posix())
    except ValueError:
        raise TupferlError(
            f"{path} is outside {home}, and tupferl mirrors your home directory; "
            f"only files under it can be managed."
        ) from None

    if (link := links_between(path, home)) is not None:
        raise TupferlError(
            f"the path to {path} goes through the symlink {link}, so the copy "
            f"tupferl stores could not be put back the same way on another "
            f"machine; add the real path instead."
        )
    if path == repo or repo in path.parents or path in repo.parents:
        raise TupferlError(
            f"{path} is tupferl's own repository at {repo}, or contains it; "
            f"managing it would store the repository inside itself."
        )

    # The type and size questions are answered from the `lstat` above, so they
    # are about one moment rather than two. Asking twice lets a file be a socket
    # for the type check and a regular file for the size check -- not a race
    # anyone will reproduce, but one that would be read as a bug in here.
    if not stat.S_ISREG(found.st_mode) and not stat.S_ISDIR(found.st_mode):
        raise TupferlError(
            f"{path} is not a regular file or directory, and tupferl only stores "
            f"file contents; leave it where it is."
        )
    if stat.S_ISREG(found.st_mode):
        if ignored(relative, config.ignore):
            raise TupferlError(
                f"{path} matches an `ignore` pattern in this repository's settings; "
                f"remove the pattern to manage it."
            )
        if found.st_size > config.max_file_size:
            raise TupferlError(
                f"{path} is {found.st_size} bytes, over the {config.max_file_size}-byte "
                f"limit; raise `max_file_size` in the settings, or leave it unmanaged."
            )
    return relative


def collect(
    where: Path, home: Path, repo: Path, config: Config
) -> tuple[list[PurePosixPath], list[Refused]]:
    """Every file at or under `where` that may be managed, and every refusal.

    Both halves are returned rather than raising on the first problem. Adding a
    directory of forty files, three of which are sockets, should manage the
    thirty-seven and say what it skipped -- stopping at the first would make the
    command unusable on any real `~/.config`.

    A *named* file that is refused is a different matter, and `add` treats it as
    an error: the user asked for that one by name.
    """
    admitted: list[PurePosixPath] = []
    refused: list[Refused] = []
    for found in walk(where):
        try:
            admitted.append(check(found, home, repo, config))
        except TupferlError as no:
            refused.append(Refused(found, str(no)))
    return admitted, refused


def walk(where: Path) -> Iterator[Path]:
    """`where` if it is a file, else every file under it, in a stable order.

    Sorted, because the order reaches the user twice -- in what `add` prints and
    in the commit message it writes -- and an order that depends on the
    filesystem makes two machines' commits differ for no reason anyone can see.

    Symlinked *directories* are not descended into. `check` would refuse each
    file found through one anyway, but walking there first means reading a tree
    the user did not name, which for a link into `/` is a great deal of it.
    """
    if where.is_symlink() or not where.is_dir():
        yield where
        return
    for child in sorted(where.iterdir()):
        yield from walk(child)


def managed(repo: Path, host: str) -> list[Managed]:
    """Everything the repository holds for this machine: shared, then overlay.

    `.tupferl/` is skipped wherever it appears. It holds tupferl's own settings,
    per-host overlays and (from milestone 3) the sync snapshots -- none of which
    is a dotfile, and all of which would otherwise be listed as managed and be
    removable by name.

    A host overlay *replaces* the shared file (plan §3.3), so a name present in
    both appears once, marked as the overlay -- which is the file that would
    actually be written to `$HOME` on this machine.

    Sorted here and nowhere else. The order reaches a user twice -- in `list`
    and in the commit messages `add` writes -- so one that depended on the
    filesystem would make two machines disagree for no visible reason.
    """
    tree, overlay = roots(repo, host)
    shared = {name: Managed(name, host=False) for name in _under(tree, tree)}
    for name in _under(overlay, overlay):
        shared[name] = Managed(name, host=True)
    return [shared[name] for name in sorted(shared)]


def _under(where: Path, root: Path) -> Iterator[PurePosixPath]:
    """Every file under `where`, named relative to `root`, skipping git and us.

    Nothing for a path that is not a directory, which is the ordinary answer
    rather than an edge case: a host that has never run `add --host` has no
    overlay directory, and that is most runs. `managed` used to ask the same
    question again before calling -- two checks for one fact, the second of
    which nothing could reach.

    The order is *not* sorted here. It was, until the mutation sweep pointed
    out that nothing could tell: `managed` sorts what it returns, so this walk's
    order never reaches anyone. Sorting twice reads as though one of them
    mattered.
    """
    if not where.is_dir():
        return
    for child in where.iterdir():
        if child.name in (".git", paths.META):
            continue
        if child.is_dir() and not child.is_symlink():
            yield from _under(child, root)
        elif child.is_file():
            yield PurePosixPath(child.relative_to(root).as_posix())
