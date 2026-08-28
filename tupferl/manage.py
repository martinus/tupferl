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

**What a stored copy *is* lives in `tupferl/copies.py`**, not here: the bytes,
the one mode bit that travels, and the single rule for "the target is already
this file". `sync` needs all three too, and the two modules had briefly grown
their own answers to the third.

**`remove` does not require the file to exist.** Plan §4 says it keeps the file
in `$HOME`, which is the usual case -- but the reason someone reaches for it is
often that they deleted the file already and want the repository to stop pushing
it to their other machine. Requiring existence would refuse exactly then.

**`remove --host` is the only way back out of a host overlay**, and it is a
different operation from `remove` rather than a narrower one. Plain `remove`
stops managing the file *everywhere*, which for someone who only wanted to stop
overriding it on this machine deletes the shared copy every other machine is
using. Plan §7.4.3 asks for "add/remove with `--host`" by name; plan §4's table
mentions the flag only on `add`, so this is the testing section read as the
authority. See `remove` for what the two do.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from tupferl import copies, gitrepo, manifest, paths
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


def a_few(names: list[PurePosixPath]) -> str:
    """`a, b, c, and 97 more` -- `NAMED_IN_MESSAGE` of them, then a count.

    Extracted from `describe` when `add`'s printed output wanted the same shape
    (#28). One rule, so a commit message and the line the user reads name the
    same number of files -- two thresholds would be two answers to "is this list
    too long to read?".
    """
    shown = ", ".join(str(name) for name in names[:NAMED_IN_MESSAGE])
    if len(names) > NAMED_IN_MESSAGE:
        shown += f", and {len(names) - NAMED_IN_MESSAGE} more"
    return shown


def describe(what: str, names: list[PurePosixPath], host: str) -> str:
    """A commit message in the plan's shape: `<what> from <host>: a, b, c`."""
    return f"{what} from {host}: {a_few(names)}"


def added(touched: list[PurePosixPath], admitted: int, host: str) -> str:
    """What an `add` commit says it did.

    Naming files is only true when files were *stored*. `add` also commits when
    every copy was already byte-for-byte identical and only a merge base was
    missing -- a repository whose `.tupferl/state` was deleted, or an earlier run
    that died between the copy and the commit -- and "add from laptop: .bashrc"
    then describes something that did not happen. `sync.message` has the same
    split for the same reason.
    """
    if touched:
        return describe("add", touched, host)
    return f"add from {host}: record the merge base for {count(admitted)}"


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
        raise TupferlError(
            f"could not stage {doing} in {repo}: {gitrepo.reason(staged)}; nothing was "
            f"committed, so run `tupferl doctor` and try again."
        )
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
        raise TupferlError(
            f"could not commit {doing} in {repo}: {gitrepo.reason(made)}; the files are "
            f"staged, so fix that and run `tupferl sync`, which commits what it finds."
        )
    return True


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


def add(wanted: list[str], to_host: bool, anyway: bool = False) -> int:
    """Copy files into the repository and commit them.

    A path the user *named* that cannot be managed is an error and nothing is
    committed: they asked for that file, and a run that silently skipped it would
    leave them believing it was stored. A file found by *walking* a named
    directory is reported and skipped -- see `manifest.collect`.
    """
    repo, config = open_repo()
    home = paths.home()
    host = paths.hostname(config.hostname)
    root = manifest.location(repo, host, to_host)

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
        # and running the whole seven-rule check over it a second time inside
        # `collect` was work with no second opinion in it.
        #
        # Which makes `is_dir()` always taken an **equivalent mutant**, and it is
        # named rather than tested: `walk` yields a lone file as itself, so a
        # named file routed through `collect` is re-checked and admitted under
        # the same name. The branch buys one fewer `stat` storm, not a different
        # answer, and a test asserting otherwise would be asserting the cost.
        name = manifest.check(path, home, repo, config, anyway)
        if path.is_dir():
            names, skipped = manifest.collect(path, home, repo, config, anyway)
            refused.extend(skipped)
            admitted.update(dict.fromkeys(names))
        else:
            admitted[name] = None

    for skip in refused:
        print(f"skipped {skip.path}: {skip.why}")
    if not admitted:
        raise TupferlError(
            "nothing to add: every path given was skipped for the reason printed above "
            "it; name a file tupferl can store."
        )

    snapshots = paths.snapshot_dir(repo, host)
    touched: list[PurePosixPath] = []
    written: list[Path] = []
    #: What happened to each file, by the word `copies.store` used for it. Kept
    #: apart rather than counted together: see `stored`.
    done: dict[str, list[PurePosixPath]] = {}
    for name in sorted(admitted):
        did = copies.store(home / name, root / name)
        # The merge base starts here. `add` has just made the two copies
        # identical, which is exactly what a snapshot records -- and without one
        # the first `sync` after an edit has no common ancestor and reports the
        # file as conflicting *with its own copy*. Found by a milestone 3 test
        # that edited a file between `add` and `sync`, which is an ordinary
        # thing to do.
        copies.store(home / name, snapshots / name)
        written.extend([root / name, snapshots / name])
        if did is not None:
            touched.append(name)
            done.setdefault(did, []).append(name)

    for line in stored(done, to_host):
        print(line)

    if record(repo, written, added(touched, len(admitted), host), "the copies"):
        print(NOT_SHARED)
    else:
        # Every file was already stored, byte for byte and bit for bit, and its
        # snapshot was already there. Not an error: `add` is how someone
        # re-stores a file they have since edited, and this is what it does when
        # they had not. git decides, rather than a second comparison of our own.
        #
        # No `NOT_SHARED` on this arm: nothing was committed, so there is
        # nothing waiting to be sent, and saying otherwise sends the user to run
        # a sync with no work in it.
        print(f"no change: the repository already held {count(len(admitted))}")
    return 0


#: What `add` and `remove` say after committing, because neither pushes.
#:
#: Both leave the work in the local repository, so until a sync runs the change
#: exists on this machine and nowhere else -- and a command that reports success
#: is exactly what makes that easy to miss. Issue #60 asked whether they should
#: sync by themselves; they should not, because `sync` can stop at a conflict
#: prompt and run `$EDITOR`, and `tupferl add .bashrc` must not pause to ask
#: about an unrelated file. Saying so costs a line and takes nothing away.
NOT_SHARED = "not shared yet -- run `tupferl sync` to send this to your other computers"

#: What `list`, `status` and `diff` all say on a repository nothing has been
#: added to yet. One sentence rather than three, because it names the command
#: that fixes it and three copies of that name is three places to change when a
#: verb's spelling does.
NOTHING_MANAGED = "nothing is managed yet; `tupferl add <path>...` starts."


def count(many: int, thing: str = "file") -> str:
    """`1 file` or `7 files` -- the plural nobody notices until it is wrong.

    `thing` because milestone 6 wanted a second noun: `status` counts *commits*
    to pull and to push. Naive pluralisation -- append an `s` -- which is right
    for both nouns this program counts, and `test_manage.TestCounting` names
    the cases including zero.

    `conflicts.describe` spells its own plural, and deliberately: it would have
    to import a command module for a three-character difference, and that edge
    -- `conflicts` depending on `manage` -- is one the module docstring there is
    at pains not to have.
    """
    return f"1 {thing}" if many == 1 else f"{many} {thing}s"


def remove(wanted: str, from_host: bool) -> int:
    """Stop managing a file, leaving it in `$HOME`.

    Without `--host`, removes it from the shared tree *and* this host's overlay
    when both hold it. Removing only one would leave the file still managed by
    the other, which reads as the command having failed silently.

    With `--host`, removes *only* this host's overlay and leaves the shared copy
    alone -- "stop overriding this here", which plain `remove` cannot express
    because it would take the shared copy every other machine uses with it.

    **The snapshot is deliberately not touched**, and that is what makes the
    next sync do the right thing rather than ask. After an overlay is dropped,
    `$HOME` still holds what the overlay put there and the snapshot still
    records it, so `resolve` sees `$HOME` unchanged and copies the shared
    version down -- one side changed, no merge, no prompt (and `sync` backs the
    replaced file up on the way, so the overlay's content is still recoverable).
    Delete the snapshot here and the same sync has no merge base, so it merges
    the overlay against the shared file and hands the user a conflict about a
    file they just said they had no opinion on. When the shared copy does not
    exist either, the file simply stops being managed and `sync.stale` prunes
    the snapshot on its own.
    """
    repo, config = open_repo()
    home = paths.home()
    host = paths.hostname(config.hostname)

    name = manifest.relative(wanted, home)

    tree, overlay = manifest.roots(repo, host)
    # Before the unlink loop, because without `--host` that loop deletes this
    # very file. It is unobservable either way today -- `said` ignores the
    # argument in exactly that branch -- but a value that is only right because
    # nobody reads it is one that becomes wrong the day somebody does.
    shared = (tree / name).is_file()

    searched = [overlay] if from_host else [tree, overlay]
    gone = [where / name for where in searched if (where / name).is_file()]
    if not gone:
        raise TupferlError(
            f"{name} is not in {host}'s overlay; `tupferl status --all` marks the files that are."
            if from_host
            else f"{name} is not managed; `tupferl status --all` lists what is."
        )
    for where in gone:
        where.unlink()
        prune(where.parent, repo)

    what = "remove overlay" if from_host else "remove"
    record(repo, gone, describe(what, [name], host), "the removal")
    print(said(name, home, host, from_host, shared))
    print(NOT_SHARED)
    return 0


def said(name: PurePosixPath, home: Path, host: str, from_host: bool, shared: bool) -> str:
    """What `remove` printed, which is the only place the user learns the difference.

    Each of the three sentences ends with what happens to the file in `$HOME`,
    because that is the question the word "remove" raises and the one the user
    is about to check for themselves.

    **"will replace" is an approximation, named here rather than hedged in the
    message.** It is exact when `$HOME` still matches the snapshot, which is the
    case someone who has just stopped overriding a file is in. With an unsynced
    edit pending, the next sync merges the two instead -- or reports a conflict
    if the edit overlaps what the override changed. Nothing is lost in either
    case, and a sentence qualified for both would stop being read.
    """
    if not from_host:
        return f"removed {name} from the repository; the file in {home} was not touched"
    if shared:
        return (
            f"removed {name} from {host}'s overlay; the shared version will replace "
            f"the one in {home} on the next sync"
        )
    return (
        f"removed {name} from {host}'s overlay, and nothing else manages it here; "
        f"the file in {home} was not touched"
    )


#: How many files `add` names one per line before it counts them instead.
#:
#: Ten because that is about where a listing stops being read and starts being
#: scrolled -- and the `skipped <path>: <why>` lines above it, which are the part
#: someone actually needs, are what a long listing pushes off the screen. The
#: README's own example is `tupferl add ~/.config/nvim`, and a real one is
#: hundreds of files; measured, a directory of 100 printed 100 lines.
NAMED_ONE_BY_ONE = 10


def stored(done: dict[str, list[PurePosixPath]], to_host: bool) -> list[str]:
    """What `add` says it did, one line per file or one line per verb (#28).

    **The two words stay apart**, and that is the constraint that makes this
    more than a `len()`. `copies.store` answers `"added"`, `"updated"` or
    `None`, and the third is silent: a file already byte-for-byte identical was
    not added, and a summary that counted it in would tell the user they had
    added something they had not. `added` below carries the same split for the
    commit message and its docstring explains why -- this is that argument
    applied to what reaches the terminal.

    Sorted by the word so two runs that did the same things print them in the
    same order. `dict` preserves insertion order, and insertion order here is
    whichever file happened to sort first.
    """
    total = sum(len(names) for names in done.values())
    marked = " (host)" if to_host else ""
    if total <= NAMED_ONE_BY_ONE:
        return [f"{did} {name}{marked}" for did in sorted(done) for name in done[did]]
    return [
        f"{did} {count(len(names))}{marked}: {a_few(names)}" for did, names in sorted(done.items())
    ]


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
