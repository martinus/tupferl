"""`init`, `add`, `remove`, `list` -- milestone 2 of the plan.

Four commands that share one shape: work out what the repository should hold,
change it, commit, and say what happened. The parts worth explaining:

**Everything commits immediately.** `tupferl add` could leave the copy staged
for `sync` to commit later, and that would be one fewer git call. It does not,
because `doctor`'s "uncommitted changes" check is a real signal that a previous
run was interrupted, and a design where the normal state of the repository is
"dirty" throws that signal away. It also means `git log` in the repository is a
record of what the user asked for, which is the thing they will read when a file
turns out to be missing.

**Only the executable bit is preserved** (plan §5). The copy is written 0o644,
or 0o755 when the source was executable by anyone -- not `copy2`, which carries
the whole mode across. That is not laziness: git records exactly this one bit,
so any other bit stored in the working tree would be lost on the first clone and
the two machines would disagree about a file neither of them changed.

**`remove` does not require the file to exist.** Plan §4 says it keeps the file
in `$HOME`, which is the usual case -- but the reason someone reaches for it is
often that they deleted the file already and want the repository to stop pushing
it to their other machine. Requiring existence would refuse exactly then.
"""

from __future__ import annotations

import filecmp
import shutil
import stat
from pathlib import Path, PurePosixPath

from tupferl import gitrepo, manifest, paths
from tupferl.config import Config, load
from tupferl.errors import TupferlError

#: What `init` writes when it has cloned an empty remote. Comments only, so it
#: parses to the defaults -- its job is to exist, giving the repository a first
#: commit and a branch. A clone with no commits is on an unborn branch, which is
#: the one state where `HEAD` does not resolve and half of git answers oddly;
#: normalising it once here is cheaper than every later command asking.
TEMPLATE = """\
# tupferl settings. Every key is optional; these are the defaults.
#
# hostname = "this-machine"     # overridden by TUPFERL_HOSTNAME, which is what a
#                               # second machine must use -- this file is shared.
# editor = "vim"                # for the conflict prompt
# ignore = ["*.log", ".cache"]  # a pattern also hides everything under it
# max_file_size = 1048576       # bytes
"""

#: Executable by anyone. The one mode bit git records, and therefore the only
#: one worth reading off a file -- see `mode_for`. Named here rather than in
#: `sync`, which reads the same bit to decide whether a `chmod +x` on one machine
#: is a change to carry to the other: two spellings of "executable" would let the
#: two commands disagree about a file neither of them edited.
EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH

#: The two modes a stored copy can have. Everything else about the source's mode
#: is dropped on purpose (plan §5): git records exactly this one bit, so any
#: other bit kept in the working tree is lost on the first clone and the two
#: machines then disagree about a file neither of them changed.
EXECUTABLE = 0o755
PLAIN = 0o644

#: How many names a generated commit message lists before it summarises. Long
#: enough that an ordinary `add` names everything it did, short enough that
#: `git log --oneline` stays readable after adding a directory of two hundred.
NAMED_IN_MESSAGE = 5


def open_repo() -> tuple[Path, Config]:
    """The repository and its settings, or the error that says to run `init`.

    Every command except `init` starts here, so the "you have not set this up"
    message is written once rather than four times in four wordings.
    """
    repo = paths.repo_dir()
    if not repo.exists():
        raise TupferlError(f"no repository at {repo}; run `tupferl init <git-url>` first.")
    if not gitrepo.is_repository(repo):
        raise TupferlError(
            f"{repo} exists but is not a git repository; move it aside, then run "
            f"`tupferl init <git-url>`."
        )
    return repo, load(paths.config_file(repo))


def describe(what: str, names: list[PurePosixPath], host: str) -> str:
    """A commit message in the plan's shape: `<what> from <host>: a, b, c`."""
    shown = ", ".join(str(name) for name in names[:NAMED_IN_MESSAGE])
    if len(names) > NAMED_IN_MESSAGE:
        shown += f", and {len(names) - NAMED_IN_MESSAGE} more"
    return f"{what} from {host}: {shown}"


def record(repo: Path, paths_: list[Path], message: str, doing: str) -> bool:
    """Stage `paths_` and commit them; `False` if there was nothing to commit.

    Written three times before this existed -- in `init`, `add` and `remove` --
    and the three copies had already drifted in the two ways duplication
    predicts. `init` threw `stage`'s result away entirely, so a staging failure
    there was silent while the other two raised. And `add` and `remove`
    interpolated raw `staged.err` / `made.err`, which is a multi-line blob ending
    in generic advice, in the same file that used `gitrepo.reason` correctly
    forty lines earlier -- `reason` having been extracted in that very change for
    exactly this.

    `doing` names what was being written, so the message stays as specific as the
    three hand-written ones were.
    """
    staged = gitrepo.stage(repo, paths_)
    if not staged.ok:
        raise TupferlError(f"could not stage {doing} in {repo}: {gitrepo.reason(staged)}")
    if not gitrepo.staged(repo):
        # Asked rather than assumed, for `sync`: it stages every file it looked
        # at, including the ones it decided nothing about, so that a copy left
        # behind by an interrupted run is committed by the next one. `git commit`
        # with nothing staged fails, and reporting that as "could not commit"
        # would turn the ordinary "nothing changed" run into an error.
        #
        # `add`, `remove` and `init` cannot reach this: each works out what
        # changed before it calls, and calls only when something did.
        return False
    made = gitrepo.commit(repo, message)
    if not made.ok:
        raise TupferlError(f"could not commit {doing} in {repo}: {gitrepo.reason(made)}")
    return True


def mode_for(source: Path) -> int:
    """The mode a stored copy gets: `EXECUTABLE` if the source is, else `PLAIN`.

    Executable by *anyone*, not by the owner alone. A script that arrived from a
    tarball as 0o711 is a script, and storing it as non-executable would put it
    back on the other machine unrunnable -- a failure the user would blame on the
    program that reads it.
    """
    return EXECUTABLE if source.stat().st_mode & EXEC_BITS else PLAIN


def store(source: Path, target: Path) -> str | None:
    """Copy `source` to `target`; say what happened, or `None` if nothing did.

    `copyfile` then `chmod`, rather than `copy2`. See the module docstring: git
    records one mode bit, so storing more is storing something that cannot
    survive a clone.

    The comparison before writing is not an optimisation -- the files are a
    megabyte at most by default and the copy is not worth avoiding. It is so `add` can
    say "added", "updated" or nothing at all rather than printing "added" and
    then, three lines later, that the repository did not change. Mode is part of
    the comparison because a `chmod +x` with no edit is a real change that git
    will record. `filecmp` rather than reading both files whole, because
    `max_file_size` is a setting and someone will raise it.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = mode_for(source)
    existed = target.is_file()
    if (
        existed
        and target.stat().st_mode & 0o777 == mode
        and filecmp.cmp(source, target, shallow=False)
    ):
        return None
    shutil.copyfile(source, target)
    target.chmod(mode)
    return "updated" if existed else "added"


def init(url: str) -> int:
    """Clone `url` into the repository directory, or explain why not.

    Clone, never "clone and fall back to creating one". A URL that cannot be
    reached is overwhelmingly a typo, and quietly making a local repository
    pointed at it would hide that until the first sync -- by which time the user
    has added files and believes they are backed up. Creating the remote is a
    step the user does on their host, and an empty one clones fine, which is the
    first-run path.
    """
    repo = paths.repo_dir()
    if repo.exists():
        # The order matters and is asserted by tests: a repository first, then
        # not-a-directory (before `iterdir`, which raises `NotADirectoryError`
        # there -- a traceback where a sentence belongs), then non-empty.
        if gitrepo.is_repository(repo):
            raise TupferlError(
                f"{repo} is already a tupferl repository; use `tupferl sync` to update it, "
                f"or move it aside to start over."
            )
        if not repo.is_dir():
            raise TupferlError(f"{repo} exists and is not a directory; move it aside.")
        if any(repo.iterdir()):
            raise TupferlError(
                f"{repo} already exists and is not empty; move it aside before running init."
            )

    done = gitrepo.clone(url, repo)
    if not done.ok:
        raise TupferlError(
            f"could not clone {url}: {gitrepo.reason(done)}; check the URL and your access."
        )

    print(f"cloned {url} into {repo}")
    if not gitrepo.has_commits(repo):
        # An empty remote: the first-run case, and the only one where this
        # writes anything. See TEMPLATE for why it is worth a commit.
        settings = paths.config_file(repo)
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(TEMPLATE, encoding="utf-8")
        record(
            repo,
            [settings],
            f"tupferl: start a repository on {paths.hostname()}",
            "the settings file",
        )
        print(f"the remote was empty, so {settings.name} was created and committed")
    print("next: `tupferl add <path>...` to start managing files")
    return 0


def add(wanted: list[str], to_host: bool) -> int:
    """Copy files into the repository and commit them.

    A path the user *named* that cannot be managed is an error and nothing is
    committed: they asked for that file, and a run that silently skipped it would
    leave them believing it was stored. A file found by *walking* a named
    directory is reported and skipped -- see `manifest.collect`.
    """
    repo, config = open_repo()
    home = paths.home()
    host = paths.hostname(config.hostname)
    tree, overlay = manifest.roots(repo, host)
    root = overlay if to_host else tree

    # A dict used as an ordered set: keys only, because the source is always
    # `home / name` and a derived value stored beside the key it comes from is
    # one that can go out of step with it.
    #
    # Not a `set`. Python randomises string hashing per process, so a set's
    # iteration order changes between runs -- which makes "is this sorted?"
    # unobservable to a test: the mutation that drops the `sorted` below passes
    # about half the time by luck. A dict keeps insertion order, so the argument
    # order and the stored order differ deterministically and the sort has
    # something that can see it missing.
    admitted: dict[PurePosixPath, None] = {}
    refused: list[manifest.Refused] = []
    for raw in wanted:
        path = manifest.named(raw)
        # Checked here so a path the *user named* raises rather than being
        # skipped. A directory is then walked; a file is already its own answer,
        # and running the whole six-rule check over it a second time inside
        # `collect` was work with no second opinion in it.
        name = manifest.check(path, home, repo, config)
        if path.is_dir():
            names, skipped = manifest.collect(path, home, repo, config)
            refused.extend(skipped)
            admitted.update(dict.fromkeys(names))
        else:
            admitted[name] = None

    for skip in refused:
        print(f"skipped {skip.path}: {skip.why}")
    if not admitted:
        raise TupferlError("nothing to add: every path given was skipped.")

    snapshots = paths.snapshot_dir(repo, host)
    touched: list[PurePosixPath] = []
    written: list[Path] = []
    for name in sorted(admitted):
        did = store(home / name, root / name)
        # The merge base starts here. `add` has just made the two copies
        # identical, which is exactly what a snapshot records -- and without one
        # the first `sync` after an edit has no common ancestor and reports the
        # file as conflicting *with its own copy*. Found by a milestone 3 test
        # that edited a file between `add` and `sync`, which is an ordinary
        # thing to do.
        store(home / name, snapshots / name)
        written.extend([root / name, snapshots / name])
        if did is not None:
            touched.append(name)
            print(f"{did} {name}{' (host)' if to_host else ''}")

    if not record(repo, written, describe("add", touched or sorted(admitted), host), "the copies"):
        # Every file was already stored, byte for byte and bit for bit, and its
        # snapshot was already there. Not an error: `add` is how someone
        # re-stores a file they have since edited, and this is what it does when
        # they had not. git decides, rather than a second comparison of our own.
        print(f"no change: the repository already held {count(len(admitted))}")
    return 0


def count(many: int) -> str:
    """`1 file` or `7 files` -- the plural nobody notices until it is wrong."""
    return "1 file" if many == 1 else f"{many} files"


def remove(wanted: str) -> int:
    """Stop managing a file, leaving it in `$HOME`.

    Removes it from the shared tree *and* this host's overlay when both hold it.
    Removing only one would leave the file still managed by the other, which
    reads as the command having failed silently.
    """
    repo, config = open_repo()
    home = paths.home()
    host = paths.hostname(config.hostname)

    path = manifest.named(wanted)
    try:
        name = PurePosixPath(path.relative_to(home).as_posix())
    except ValueError:
        raise TupferlError(
            f"{path} is outside {home}, so it was never managed; name a file under it."
        ) from None

    gone = [where / name for where in manifest.roots(repo, host) if (where / name).is_file()]
    if not gone:
        raise TupferlError(f"{name} is not managed; `tupferl list` shows what is.")
    for where in gone:
        where.unlink()
        prune(where.parent, repo)

    record(repo, gone, describe("remove", [name], host), "the removal")
    print(f"removed {name} from the repository; the file in {home} was not touched")
    return 0


def prune(where: Path, repo: Path) -> None:
    """Delete directories left empty by a removal, up to but not including `repo`.

    git does not track directories, so an empty one left behind is invisible in
    the commit and present in every clone's working tree -- `~/.config/nvim/`
    with nothing in it, on a machine that never used nvim.

    Public because `sync` prunes the same way when it drops the snapshot of a
    file another machine stopped managing; the underscore said "one caller" and
    there are two.
    """
    # `repo in where.parents` as well as the inequality, and not only for
    # tidiness: this loop deletes directories and walks *upwards*. Its safety
    # otherwise rests on `name` being relative -- which it is, because
    # `relative_to` cannot return anything else -- but an invariant three
    # functions away is not what a delete loop should rest on. With this, a name
    # that somehow arrived absolute stops immediately instead of climbing to `/`.
    while where != repo and repo in where.parents and not any(where.iterdir()):
        where.rmdir()
        where = where.parent


def listing() -> int:
    """Print what is managed, marking the files this host overrides."""
    repo, config = open_repo()
    found = manifest.managed(repo, paths.hostname(config.hostname))
    if not found:
        print("nothing is managed yet; `tupferl add <path>...` starts.")
        return 0
    for item in found:
        print(f"{'host' if item.host else '    '}  {item.name}")
    hosts = sum(1 for item in found if item.host)
    print(f"\n{len(found)} managed, {hosts} from this host's overlay")
    return 0
