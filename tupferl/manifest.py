"""What the repository holds, and what it is allowed to take.

Two halves of one question. `managed` reads the repository and says what is in
it; `check` reads a `$HOME` path and says whether it may go in. They are here
together because the answer has to be the same shape from both directions -- a
file admitted under one name and then listed under another is a file the user
cannot remove.

**The admission rules are where this module earns its place.** `tupferl add`
copies a file into a git repository that will be pushed to a remote, so a rule
that lets the wrong file through is not an inconvenience. Five of the seven exist
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

- **A name that says it holds a credential is refused** (#35), unless
  `add --anyway`. Plan §2 puts encryption out of scope, so what this program
  stores it stores in plaintext and pushes -- and the danger was never that
  decision, it was that nothing said so at the moment it mattered. See `SECRETS`,
  including for why it is a short list of famous filenames and not a scanner.

The other two -- the size limit and the ignore list -- are ordinary settings
(plan §5), and they are checked last so that a file refused for a *safety*
reason says so rather than blaming its size.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator, Sequence
from contextlib import suppress
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
    # milestone 3's sync would want, and adding it then would have been a field
    # with no reader. The reader arrived, and what it wanted was `location`
    # below: a function of `(repo, host, on_host)` that `add` can also call
    # without having a `Managed` at all. Waiting is what got the shape right.


def location(repo: Path, host: str, on_host: bool) -> Path:
    """The tree a file belongs in: this host's overlay, or the shared one.

    Plan §3.3's rule, resolved. `add --host` passes its flag, `sync` passes the
    `Managed.host` it got from `managed`, and milestone 6's `status` and `diff`
    will pass the same -- so the ternary is written once instead of at every
    command that has to find a file. The comment on `Managed` predicted this
    reader and declined to add a `path` field for it; this is the shape that
    turned out to be wanted, which is the argument for having waited.
    """
    tree, overlay = roots(repo, host)
    return overlay if on_host else tree


def roots(repo: Path, host: str) -> tuple[Path, Path]:
    """The two trees a managed file can live in: shared, then this host's overlay.

    Both, for the callers that need both: `remove` clears them and `managed`
    merges them with the second winning. A caller that wants *one* of them wants
    `location` above, which is the same rule with the choice already made.
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
    # survivor: branch -- equivalent, measured: `Path.cwd() / Path('/etc/x')` is `/etc/x` --
    #   pathlib's `/` returns the right operand when it is absolute, so taking the branch for an
    #   already-absolute path is the identity.
    if not expanded.is_absolute():
        expanded = Path.cwd() / expanded
    return Path(os.path.normpath(expanded))


def mergeable(name: PurePosixPath, repo: Path, host: str) -> bool:
    """May `sync` settle a conflict over this *repository* path, on this host?

    Asked of a path that arrives from git's index rather than from a walk of the
    working tree, which is why it exists at all: `manifest.under` skips
    `paths.META` deliberately, and nothing reapplied that rule to a name coming
    out of `gitrepo.conflicted`. So a conflicting `.tupferl/state/<host>/<file>`
    was offered at the dotfile prompt, and settling it wrote a **merge of two
    snapshots** -- a state neither machine was ever in (#15).

    That is worse than an odd-looking prompt. `sync`'s interruption guarantee
    rests on the snapshot being exactly the last state both sides agreed on: a
    merge of two of them is neither side's history, and every later comparison
    is against a version that never existed.

    Three admissions, and the last two are the reason this is not simply "skip
    everything under `.tupferl/`":

    - **an ordinary dotfile**, anywhere outside `paths.META`;
    - **`config.toml`**, which really is a file two machines can disagree about,
      and refusing it would send the user to `git pull` for something the tool is
      otherwise happy to manage;
    - **this host's own overlay**, which is a dotfile that happens to live under
      `paths.META`. Refusing it would be a regression: an overlay conflict is
      exactly the disagreement about lines the prompt is for.

    Everything else -- any snapshot, and another host's overlay -- is tupferl's
    own state or somebody else's, and neither is this machine's to merge.

    **Repository-relative, and that is the whole subtlety.** The obvious
    implementation is "is it in `manifest.managed`?", and it is wrong: `managed`
    names a file relative to *its own root*, so this host's overlay copy of
    `.vimrc` is `.vimrc` there while git reports it as
    `.tupferl/hosts/<host>/.vimrc`. Comparing the two directly rejects every
    legitimate overlay conflict. Measured, before it was written that way.

    Every path it compares against comes from `paths`, rather than `"hosts"`,
    `"state"` and `"config.toml"` spelled again here. The layout has one owner,
    and a second copy of it in the one function that decides what may be merged
    is the copy that would be missed when it moves.
    """
    where = repo / name
    if not where.is_relative_to(repo / paths.META):
        return True
    # No case for the settings file any more. It used to live at
    # `.tupferl/config.toml` -- inside `META`, which is otherwise tupferl's own
    # and not mergeable -- so it needed an exception to be syncable at all. It
    # is a dotfile in `$HOME` now, so it arrives here like `.bashrc` does, on
    # the first branch above, and `META` holds only machinery again.
    return where.is_relative_to(paths.host_overlay(repo, host))


def relative(wanted: str | Path, home: Path) -> PurePosixPath:
    """Turn what the user typed into the name a managed file has, or say why not.

    The name *is* the mapping (plan §3.2), so this is the one translation from a
    command line to a key in the repository. `remove` and `diff` both do it, and
    a second spelling would be a second answer to "is `~/../etc/passwd` under
    `$HOME`?" -- which is exactly the question that must not have two.

    **Two readings of a relative argument, in this order** (#27). `tupferl list`
    prints `.bashrc`, and `tupferl diff .bashrc` used to answer "`/somewhere
    -else/.bashrc` is outside your home directory" -- the tool printing an
    identifier it would not take back, and blaming the user for naming a file
    they had not named.

    - **The working directory first.** Someone standing in `~/.config` typing
      `tupferl diff nvim/init.lua` means the file under their feet, and that is
      how paths are typed at a shell.
    - **Then `$HOME`**, but only when the first reading landed *outside* it, and
      only for an argument that was relative to begin with. `/etc/hostname` keeps
      its own error rather than becoming `$HOME/etc/hostname`, and so does
      `~/../etc/passwd`, which `named` has already made absolute.

    One case stays ambiguous and takes the first reading: standing in
    `~/.config`, `tupferl diff .bashrc` means `~/.config/.bashrc`, which is
    probably not what was meant. Resolving it would mean asking the manifest what
    is managed, and this function deliberately does not know -- see below. The
    answer is at least *a* file under `$HOME`, so the caller's "not managed"
    names something real.

    Says nothing about whether the file is managed, or exists, or is a file. It
    answers where it *would* live, which is what the caller then looks for --
    `remove` and `diff` each check afterwards, and differently, and a fallback
    that consulted the manifest would collapse the two answers into one.
    """
    path = named(wanted)
    try:
        return PurePosixPath(path.relative_to(home).as_posix())
    except ValueError:
        pass

    typed = str(wanted)
    if not Path(os.path.expanduser(typed)).is_absolute():
        # A name rather than a path: what `list` prints. `named` again, so `..`
        # inside it is collapsed the same way and cannot climb back out.
        inside = named(home / typed)
        with suppress(ValueError):
            return PurePosixPath(inside.relative_to(home).as_posix())

    raise TupferlError(
        f"{path} is outside {home}, so it was never managed; name a file under it, "
        f"or a name `tupferl status --all` prints."
    )


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


#: Names refused unless `add --anyway` says otherwise (#35).
#:
#: **This is a list of famous filenames, not a secret scanner**, and the
#: distinction is the whole of why it is short. Plan §2 puts encryption out of
#: scope -- what tupferl stores, it stores in plaintext and pushes to a remote --
#: and the danger is not that decision but that nothing said so at the moment it
#: mattered: `tupferl add ~/.ssh/id_ed25519` succeeded, silently, and the key was
#: then in a git history nothing here can rewrite.
#:
#: Every entry earns its place by being a file whose *only* purpose is to hold a
#: credential. Anything vaguer belongs to the user's `ignore` setting, and
#: anything cleverer -- entropy, `AKIA[0-9A-Z]{16}` -- would put this program in
#: the business of reading the bytes of the files it is meant to be copying, and
#: would be wrong in both directions at once.
#:
#: The two deliberate *absences* matter as much as the entries. `.ssh/config`
#: and `.ssh/known_hosts` are ordinary dotfiles people want synced; they live in
#: the directory this rule is most about, and refusing the whole of `~/.ssh`
#: would be wrong far more often than right. `*.pub` likewise: a public key is
#: public.
#:
#: **Half of these are anchored at `$HOME` and half are not**, which is a
#: consequence of `fnmatch`'s `*` matching `/` as well, and is worth knowing
#: rather than discovering. `.ssh/id_*`, `.aws/credentials` and `.gnupg/*` only
#: match at the top of the tree, so a key under `~/projects/thing/.ssh/` is
#: **not** refused; `*.pem` and `*.key` match at any depth. Left that way on
#: purpose: matching `id_*` anywhere would refuse `~/pictures/id_photo.png`, and
#: a rule that fires on holiday snaps is one people learn to pass `--anyway` to
#: without reading.
SECRETS = (
    ".ssh/id_*",
    ".aws/credentials",
    ".netrc",
    ".pgpass",
    ".gnupg/*",
    "*.pem",
    "*.key",
)

#: What `SECRETS` lets through despite matching. One entry, and it is the half of
#: an ssh key pair that is meant to be shared.
NOT_SECRET = ("*.pub",)


def secret(name: PurePosixPath) -> str | None:
    """The `SECRETS` pattern `name` matches, or `None`.

    The pattern rather than a `bool`, so the refusal can name it -- a user told
    only "this looks like a secret" cannot tell a rule they disagree with from a
    rule they misunderstand.

    `fnmatchcase` and the parent walk are `ignored`'s, and for its reasons: case
    folding on macOS would make two machines disagree about the same repository,
    and `.gnupg/*` has to mean the subtree rather than one directory entry.
    """
    if any(fnmatchcase(name.name, allowed) for allowed in NOT_SECRET):
        return None
    candidates = [name, *name.parents]
    for pattern in SECRETS:
        for candidate in candidates:
            if str(candidate) != "." and fnmatchcase(str(candidate), pattern):
                return pattern
    return None


def ignored(name: PurePosixPath, patterns: Sequence[str]) -> bool:
    """Whether `name` or any directory above it matches an ignore pattern.

    The parent check is what makes `ignore = [".cache"]` mean the whole subtree
    rather than one directory entry nobody stores anyway. Without it a user would
    have to write `.cache/**`, and would find out they had not when the contents
    turned up in a push.

    `fnmatchcase` rather than `fnmatch`: the latter folds case on macOS, so the
    same repository would ignore different files on two machines -- and the
    repository is the thing both machines share.
    """
    # survivor: branch -- equivalent: the guard is an early-out for the common case, and `any([])`
    #   is `False` -- the same answer the body computes from an empty pattern list.
    if not patterns:
        return False  # the default, and the branch most runs take
    candidates = [name, *name.parents]
    return any(
        fnmatchcase(str(candidate), pattern)
        for candidate in candidates
        if str(candidate) != "."
        for pattern in patterns
    )


def check(
    path: Path, home: Path, repo: Path, config: Config, anyway: bool = False
) -> PurePosixPath:
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
        # Before the two settings below and after the four safety rules above,
        # which is where it belongs: this is a safety rule, and the module
        # docstring's reason for putting settings last is that a file refused
        # for safety should say so rather than blaming its size.
        if not anyway and (pattern := secret(relative)) is not None:
            raise TupferlError(
                f"{path} matches {pattern}, and tupferl stores what it manages in "
                f"plaintext and pushes it to your remote; add it with `--anyway` if "
                f"that is what you want, or leave it unmanaged."
            )
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
    where: Path, home: Path, repo: Path, config: Config, anyway: bool = False
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
            admitted.append(check(found, home, repo, config, anyway))
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
    shared = {name: Managed(name, host=False) for name in under(tree, tree)}
    for name in under(overlay, overlay):
        shared[name] = Managed(name, host=True)
    return [shared[name] for name in sorted(shared)]


def under(where: Path, root: Path) -> Iterator[PurePosixPath]:
    """Every file under `where`, named relative to `root`, skipping git and us.

    Public because `sync.stale` walks the snapshot directory looking for merge
    bases nothing manages any more -- and that list feeds an `unlink`. A delete
    whose walk rule is maintained apart from the walk that decides what *is*
    managed is a delete that can disagree with it.

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
            yield from under(child, root)
        elif child.is_file():
            yield PurePosixPath(child.relative_to(root).as_posix())
