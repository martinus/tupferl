"""`tupferl sync` -- milestone 3: snapshots, detection, and the resolutions that
need nobody.

Plan §3.4 is the design and this is it, minus the prompt: pull, work out what
changed on each side, resolve everything that can be resolved without asking,
commit, push. A file both sides changed in the same place is *reported and left
alone* -- milestone 4 adds the prompt that settles it. Nothing here ever picks a
side.

Four things decide the shape of this module.

**A stored copy is `tupferl/copies.py`'s idea, not this module's.** `Blob`,
`read` and `write` come from there, and so does `add`'s -- so the merge base
written when a file is first managed and the one written by every sync
afterwards are the same bytes decided by the same rule.

**Three versions, and a snapshot is what makes them three.** `.tupferl/state/
<hostname>/<name>` holds each file as it was after this machine's last
successful sync, so a change in `$HOME` and a change in the repository can be
told apart from each other -- which two versions alone cannot do. The snapshots
are committed, so a machine that is restored from the remote still knows its own
merge base.

**The snapshot is written last, and that ordering is the whole interruption
story** (plan §7.4 asks for it by name). A sync killed part-way leaves the
snapshot *older* than the two copies, and an older base makes the next run treat
both sides as changed -- so it merges, conservatively, and loses nothing. Written
first, the same interruption would leave a snapshot claiming `$HOME` had already
been updated when it had not, and the next run would copy the stale `$HOME` file
over the new one. That is silent data loss, and it is one line of ordering away.

**A managed file missing from `$HOME` is restored, never unmanaged.** Plan §4
gives `remove` for "stop managing this", so a missing file is far more likely to
be a mistake -- an `rm`, a reinstall, a fresh machine -- than an instruction.
Reading it as an instruction would let one bad `rm` delete a dotfile from every
machine the user owns, which is the failure a dotfiles manager exists to prevent.

**The remote is integrated before anything local is written.** git's own merge of
the two histories has the real merge base and is better than anything this module
could do; running it first means the repository side of every three-way
comparison below is already up to date. It is also why a *git-level* conflict is
reported rather than resolved here: it means two committed versions disagree, and
that is the prompt's job in milestone 4.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from tupferl import conflicts, gitrepo, manage, manifest, merge, paths
from tupferl.copies import Blob, read, write
from tupferl.errors import TupferlError

#: How many times a push may be re-tried after the remote turned out to have
#: moved. Plan §3.4 step 5 asks for the retry and does not bound it; a bound
#: exists so that a remote somebody else is pushing to in a loop ends in a
#: sentence rather than in a `sync` that never returns.
ATTEMPTS = 3

#: How many sync backups are kept, as plan §5 asks. Old ones are removed by age,
#: which is what the directory name sorts by.
BACKUPS_KEPT = 5

#: The backup directory's name. Microseconds are in it so that two syncs in the
#: same second get separate directories -- otherwise "the last 5 syncs" quietly
#: becomes "the last 5 seconds that had a sync in them", and the tests, which
#: sync several times in a row, would all share one.
STAMP = "%Y%m%dT%H%M%S.%f"

#: What happened to one file. Strings rather than an enum because they are also
#: the word the report prints for the four ordinary ones.
UNCHANGED = "unchanged"
TO_REPO = "stored"
TO_HOME = "updated"
RESTORED = "restored"
MERGED = "merged"
CONFLICT = "conflict"
REFUSED = "refused"

#: The four answers to plan §3.4's prompt that write something. The fifth,
#: `[s] skip`, is `CONFLICT`: skipping is the conflict standing, so it needs no
#: action of its own -- and a second constant with the same rule and the same
#: report line is one that can drift from it.
KEPT_LOCAL = "kept local"
KEPT_REMOTE = "kept remote"
KEPT_BOTH = "kept both"
EDITED = "edited"


class Rule(NamedTuple):
    """What an action *means*, for the three functions that ask."""

    #: Write the repository's copy.
    to_repo: bool
    #: Write the `$HOME` copy -- which is also exactly when a backup is taken,
    #: since the backup is of the file about to be replaced.
    to_home: bool
    #: Something is left for a person, so `sync` exits 1. Not derivable from the
    #: two above: an unchanged file writes nothing either, and is fine.
    needs_user: bool


#: One row per action, because the alternative was five tuple-membership tests
#: spread over `apply`, `report` and `main`, each re-deriving one property of one
#: action from its name. Milestone 4 adds at least *kept local*, *kept remote*,
#: *kept both*, *edited* and *skipped*; with the tuples, each had to be entered
#: correctly in five places, and a missed entry in `apply` writes nothing while
#: the report says it did -- silent, and only a per-action test would see it.
#: Here it is one row, and a row that is missing is a `KeyError`.
#:
#: "Did anything change?" is deliberately *not* a column: it is exactly
#: `to_repo or to_home`, and a stored copy of a derivable fact is one that can go
#: out of step with what it was derived from.
RULES: dict[str, Rule] = {
    UNCHANGED: Rule(to_repo=False, to_home=False, needs_user=False),
    TO_REPO: Rule(to_repo=True, to_home=False, needs_user=False),
    TO_HOME: Rule(to_repo=False, to_home=True, needs_user=False),
    RESTORED: Rule(to_repo=False, to_home=True, needs_user=False),
    MERGED: Rule(to_repo=True, to_home=True, needs_user=False),
    CONFLICT: Rule(to_repo=False, to_home=False, needs_user=True),
    REFUSED: Rule(to_repo=False, to_home=False, needs_user=True),
    # `[l]` writes only the repository and `[r]` only `$HOME`, because in each
    # case the other side already holds exactly those bytes. It is not an
    # optimisation: `to_home` is also what takes the backup, and backing up a
    # file that is about to be rewritten with its own contents would push a real
    # backup out of plan §5's window of five.
    KEPT_LOCAL: Rule(to_repo=True, to_home=False, needs_user=False),
    KEPT_REMOTE: Rule(to_repo=False, to_home=True, needs_user=False),
    KEPT_BOTH: Rule(to_repo=True, to_home=True, needs_user=False),
    EDITED: Rule(to_repo=True, to_home=True, needs_user=False),
}

#: Which action each of the prompt's five answers is. A table for `RULES`'
#: reason: the alternative is a chain of comparisons in `settled`, where a
#: missing arm falls through to whatever the last one was rather than failing.
MEANS: dict[str, str] = {
    conflicts.LOCAL: KEPT_LOCAL,
    conflicts.REMOTE: KEPT_REMOTE,
    conflicts.BOTH: KEPT_BOTH,
    conflicts.EDIT: EDITED,
    conflicts.SKIP: CONFLICT,
}


def changed(outcome: Outcome) -> bool:
    """Whether this outcome put new bytes anywhere the user can see."""
    rule = RULES[outcome.action]
    return rule.to_repo or rule.to_home


class Outcome(NamedTuple):
    """What one managed file needs, and what it needs written."""

    name: PurePosixPath
    action: str
    #: What to store on both sides, or `None` when nothing is to be written --
    #: a conflict, or a path tupferl will not touch.
    blob: Blob | None
    #: The three versions, for `CONFLICT` and for nothing else. Present exactly
    #: when the action is `CONFLICT`, which is what lets `report` and `settle`
    #: test *this* rather than the action string: the test is then also the
    #: narrowing, so there is no second place to keep the two in step.
    sides: conflicts.Sides | None = None
    #: Why, for REFUSED. Empty otherwise.
    why: str = ""


def executable_after(base: Blob | None, ours: Blob, theirs: Blob) -> bool:
    """The executable bit a merged file gets.

    A three-way merge of one bit, and it needs no conflict case: a bit has two
    values, so if both sides changed it they changed it to the *same* value and
    there is nothing to disagree about.

    With no base there is no such argument, and nothing in the data says which
    side is right. It resolves towards executable because the two mistakes are
    not equal: a script restored without the bit fails the moment the user runs
    it, where a spurious bit on a config file is invisible and harmless.
    """
    if base is None:
        return ours.executable or theirs.executable
    return theirs.executable if ours.executable == base.executable else ours.executable


def resolve(name: PurePosixPath, base: Blob | None, home: Blob | None, stored: Blob) -> Outcome:
    """Plan §3.4 step 3, as one function over three versions of one file.

    Pure: it reads no files and writes none, so every row of plan §7.4's table --
    (local changed / repo changed / both / neither) crossed with (overlapping
    edits / not) -- is a test that needs no repository.

    The order of the checks is the argument:

    1. `$HOME` has no file, so restore the one the repository holds. Checked
       first because it is the second machine's first sync, and because the
       comparisons below have nothing to compare.
    2. The two sides already agree, so there is nothing to write -- whatever the
       snapshot says. The snapshot still moves, which is how a machine catches up
       after somebody edited both copies to the same thing.
    3. One side matches the snapshot, so only the *other* changed: copy, do not
       merge. Cheaper, and it cannot introduce a conflict marker into a file
       nobody disagreed about.
    4. Both changed. Merge them over the snapshot, and if git cannot decide,
       leave both copies exactly as they are.
    """
    if home is None:
        return Outcome(name, RESTORED, stored)
    if home == stored:
        return Outcome(name, UNCHANGED, home)
    if base is not None:
        if home == base:
            return Outcome(name, TO_HOME, stored)
        if stored == base:
            return Outcome(name, TO_REPO, home)

    merged = merge.three_way(str(name), None if base is None else base.data, home.data, stored.data)
    if merged.data is None or merged.conflicts:
        # The marked bytes travel with the conflict rather than being recomputed
        # by whoever settles it: `[e]` opens exactly the file the prompt showed,
        # and a second `three_way` call could not be relied on to produce the
        # same one -- git's merge is deterministic, but that is a property of a
        # version rather than a promise, and this is the file the user edits.
        return Outcome(
            name,
            CONFLICT,
            None,
            conflicts.Sides(name, base, home, stored, merged.data, merged.conflicts),
        )
    return Outcome(name, MERGED, Blob(merged.data, executable_after(base, home, stored)))


def settled(sides: conflicts.Sides, answer: conflicts.Answer) -> Outcome:
    """What one answer to the prompt means on disk.

    The single mapping from a choice to an action, which is why `--ours` and
    `--theirs` are settlers that return an `Answer` rather than shortcuts around
    the prompt: a flag reaches disk through this function and so cannot resolve
    a conflict differently from the keypress it stands for.

    Keyed on whether the answer *carried* bytes rather than on which choice it
    was. `[b]` and `[e]` are the two that produce a file that is neither side,
    and both hand it back here; the other three name a side this function
    already has, and copying those bytes through the answer would be a second
    place for them to differ from the ones that were compared.
    """
    action = MEANS[answer.choice]
    if answer.data is not None:
        bit = executable_after(sides.base, sides.home, sides.stored)
        return Outcome(sides.name, action, Blob(answer.data, bit))
    if action == KEPT_LOCAL:
        return Outcome(sides.name, action, sides.home)
    if action == KEPT_REMOTE:
        return Outcome(sides.name, action, sides.stored)
    return Outcome(sides.name, CONFLICT, None, sides)


class Backups:
    """Plan §5's backup directory, created only if something needs backing up.

    Lazily, and that is not thrift: a run that changes nothing must leave the
    disk exactly as it found it, or `tupferl sync` on a quiet machine would
    produce a new empty directory every time it was run -- and five of those
    would push the last real backup out of the window that is supposed to keep
    it.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.where: Path | None = None

    def take(self, name: PurePosixPath, blob: Blob) -> Path:
        """Save `blob` under `name`, and return where it went."""
        if self.where is None:
            self.where = self.root / datetime.now().strftime(STAMP)
            self.where.mkdir(parents=True, exist_ok=True)
            self.forget_old()
        target = self.where / name
        write(target, blob)
        return target

    def forget_old(self) -> None:
        """Keep the newest `BACKUPS_KEPT` backup directories and delete the rest.

        By name, which sorts by time because `STAMP` puts the units in that
        order. Directories only: something a user dropped in here by hand is
        theirs, and this deletes trees.
        """
        made = sorted(found for found in self.root.iterdir() if found.is_dir())
        for old in made[: max(0, len(made) - BACKUPS_KEPT)]:
            shutil.rmtree(old)


def stale(snapshots: Path, keep: set[PurePosixPath]) -> list[PurePosixPath]:
    """Names of snapshots for files nothing manages any more.

    They appear when another machine runs `tupferl remove`: the file leaves the
    repository, and this machine's merge base for it would otherwise sit in the
    tree for ever, committed, and be the base for a file that came back later
    under the same name.

    `manifest.under` does the walking, rather than an `rglob` of this module's
    own. This list feeds an `unlink`, and a delete whose walk rule is maintained
    apart from the walk that decides what *is* managed is a delete that can
    disagree with it.

    Names rather than paths, because that is what the caller needs twice over --
    to build the path to remove, and to name the file in the commit message.

    Not sorted. It was, until the mutation sweep pointed out that nothing could
    tell: these names go into `touched`, and `message` sorts that. Sorting twice
    reads as though one of them mattered -- the same note `manifest.under` already
    carries for the same reason.
    """
    return [name for name in manifest.under(snapshots, snapshots) if name not in keep]


def integrate(repo: Path, remote: str, branch: str) -> bool:
    """Fetch, and merge the remote branch if it holds anything new. Did it?

    The answer is what tells `sync` whether a rejected push is worth re-trying:
    if nothing came in, the remote did not move and pushing again would fail the
    same way. It is a fact about commits rather than a reading of git's English
    -- see `gitrepo.is_ancestor`.
    """
    fetched = gitrepo.fetch(repo, remote)
    if not fetched.ok:
        raise TupferlError(
            f"could not fetch from {remote}: {gitrepo.reason(fetched)}; "
            f"run `tupferl doctor` to check the remote."
        )
    there = f"{remote}/{branch}"
    if not gitrepo.has_ref(repo, there) or gitrepo.is_ancestor(repo, there, "HEAD"):
        # No such branch yet (an empty remote, until the first push), or nothing
        # on it this machine does not already have.
        return False

    done = gitrepo.merge(repo, there)
    if done.ok:
        return True
    stuck = gitrepo.unmerged(repo)
    # Abort before raising, so the repository is left in the state it was found
    # in. A half-merged tree would make the *next* run refuse to start, turning
    # one conflict into a machine that cannot sync at all.
    gitrepo.abort_merge(repo)
    if stuck:
        # Not the conflict prompt's case, though it looks like one. The prompt
        # settles `$HOME` against the repository over this machine's snapshot;
        # this is two *commits* that git could not merge, which needs the three
        # index stages rather than the three files. Reachable when a machine
        # commits, fails to push, and the other machine pushes a change to the
        # same lines in the window -- so it is a real path, and it has an issue.
        raise TupferlError(
            f"{there} and this machine have both committed changes to "
            f"{', '.join(stuck)}; tupferl cannot settle a conflict between two "
            f"commits yet, so resolve it with `git -C {repo} pull` and try again."
        )
    raise TupferlError(f"could not merge {there}: {gitrepo.reason(done)}")


def settle(repo: Path, home: Path, host: str, settler: conflicts.Settler) -> list[Outcome]:
    """Resolve every managed file, write what was decided, and commit it.

    `settler` answers the files no rule could decide -- the prompt, or one of
    plan §3.4's flags answering for it. It is called between `resolve` and
    `apply`, so a choice reaches disk through exactly the code every other
    outcome does.

    Returns one `Outcome` per managed file, in the order `manifest.managed`
    gives them, which is sorted -- so the report and the commit message list
    files in the same order on every machine.
    """
    snapshots = paths.snapshot_dir(repo, host)
    backups = Backups(paths.backup_dir())

    items = manifest.managed(repo, host)
    outcomes: list[Outcome] = []
    touched: list[PurePosixPath] = []

    for item in items:
        where = manifest.location(repo, host, item.host) / item.name
        target = home / item.name
        snapshot = snapshots / item.name
        stored = read(where)
        if stored is None:
            outcomes.append(Outcome(item.name, REFUSED, None, why=f"{where} is not a regular file"))
            continue
        found = read(target)
        if found is None and os.path.lexists(target):
            outcomes.append(
                Outcome(item.name, REFUSED, None, why=f"{target} is not a regular file")
            )
            continue

        outcome = resolve(item.name, read(snapshot), found, stored)
        if outcome.sides is not None:
            outcome = settled(outcome.sides, settler(outcome.sides))
        try:
            wrote = apply(outcome, target, where, snapshot, found, backups)
        except OSError as unwritable:
            # One unwritable path does not stop the sync, for `manifest.collect`'s
            # reason: forty files of which one now sits under a directory that has
            # become a file should leave thirty-nine synced and say what it
            # skipped. It also keeps an `OSError` from reaching the user as a
            # traceback, which plan §5 rules out for anything they can act on.
            outcome = Outcome(
                item.name, REFUSED, None, why=f"could not write it ({unwritable.strerror})"
            )
            wrote = False
        outcomes.append(outcome)
        if wrote:
            touched.append(item.name)

    for name in stale(snapshots, {item.name for item in items}):
        touched.append(name)
        gone = snapshots / name
        gone.unlink()
        manage.prune(gone.parent, repo)

    # The whole repository, not a list of the paths this run wrote. Two reasons,
    # and the second is the one that decided it: `doctor` already tells the user
    # that "`tupferl sync` will commit them" about uncommitted changes it finds,
    # and a copy left behind by an interrupted run is exactly that. A list also
    # fails outright on any name that is neither on disk nor tracked, which the
    # failure paths above can produce. `record` asks git whether anything was
    # staged, and does not commit when nothing was.
    manage.record(repo, [repo], message(touched, host), "the sync")
    return outcomes


def message(touched: list[PurePosixPath], host: str) -> str:
    """Plan §3.5's commit message, and the one for the case it does not cover.

    Nothing touched and something staged means an earlier run wrote a copy and
    died before committing it, or the user put a file in the repository by hand.
    Naming files would be a lie -- this run decided nothing about them -- so the
    message says what actually happened.
    """
    if not touched:
        return f"sync from {host}: commit copies an earlier run left uncommitted"
    return manage.describe("sync", sorted(touched), host)


def apply(
    outcome: Outcome,
    target: Path,
    where: Path,
    snapshot: Path,
    found: Blob | None,
    backups: Backups,
) -> bool:
    """Write what `outcome` decided. `True` if anything on disk changed.

    The order is repository, then `$HOME`, then the snapshot, and the last of
    those three is the one that matters -- see the module docstring. The first
    two are in that order because the repository copy is the one another machine
    will see, and `$HOME` has a backup where the repository has git.
    """
    if outcome.blob is None:
        # A conflict, or a path tupferl will not touch. Nothing is written --
        # including the snapshot, which must stay at the last state both sides
        # agreed on or the next run would merge against something neither of
        # them ever had.
        return False
    rule = RULES[outcome.action]
    if rule.to_home and found is not None:
        # `found is not None` and not a fourth action: RESTORED is the case where
        # `$HOME` had no file, so there is nothing to back up, and `resolve` is
        # what guarantees that. Plan §5 wants a copy of what is replaced, not of
        # what was not there.
        backups.take(outcome.name, found)

    wrote = False
    if rule.to_repo:
        wrote |= write(where, outcome.blob)
    if rule.to_home:
        wrote |= write(target, outcome.blob)
    wrote |= write(snapshot, outcome.blob)
    return wrote


def report(outcomes: list[Outcome]) -> str:
    """What the run did, one line per file that had something happen to it.

    Silent about UNCHANGED files, which are most of them on most runs: a sync
    that printed forty lines saying nothing happened would bury the one line
    saying something did.
    """
    lines: list[str] = []
    for outcome in outcomes:
        if outcome.action == UNCHANGED:
            continue
        if outcome.action == REFUSED:
            lines.append(f"skipped {outcome.name}: {outcome.why}")
        elif outcome.sides is not None:
            # `sides is not None` rather than `action == CONFLICT`: the two say
            # the same thing, and this one also narrows the type for the line
            # below. The count rather than "conflicted", because "3 to settle"
            # told the user whether the prompt was a keypress or an editor --
            # and this is the line they see when they skipped it.
            lines.append(
                f"conflict in {outcome.name} ({outcome.sides.conflicts} to settle); "
                f"both copies left as they are"
            )
        else:
            lines.append(f"{outcome.action} {outcome.name}")

    unsettled = sum(1 for outcome in outcomes if outcome.sides is not None)
    moved = sum(1 for outcome in outcomes if changed(outcome))
    lines.append(
        f"\n{manage.count(len(outcomes))} managed, {moved} changed, {unsettled} in conflict"
    )
    return "\n".join(lines)


def main(no_input: bool = False, ours: bool = False, theirs: bool = False) -> int:
    """Pull, resolve, commit, push. Plan §3.5's one command.

    The three flags are `conflicts.answering`'s subject, not this function's:
    each is an answer given once for every conflict instead of at a prompt, and
    all four routes -- three flags and the keypress -- reach disk through
    `settled`.

    Returns 1 when something was left for the user, 0 when nothing was. Not 2:
    that is "tupferl could not run", and a conflict is a result. With `--ours`
    or `--theirs` nothing is ever left, so a scripted sync that finds conflicts
    still returns 0 -- which is the point of the flags.
    """
    repo, config = manage.open_repo()
    host = paths.hostname(config.hostname)
    home = paths.home()

    marker = gitrepo.unfinished(repo)
    if marker is not None:
        raise TupferlError(
            f"{repo} has an unfinished git operation ({marker} is present); finish or "
            f"abort it with git, then sync again."
        )

    settler = conflicts.answering(config, no_input, ours, theirs)
    remote = gitrepo.first_remote(repo)
    branch = gitrepo.branch(repo)
    if remote is None:
        outcomes = settle(repo, home, host, settler)
        print(f"no remote configured, so nothing was pushed; {repo} is a git repository")
    else:
        if branch is None:
            raise TupferlError(
                f"{repo} has no branch checked out, so there is nothing to push; "
                f"run `git -C {repo} checkout main`."
            )
        integrate(repo, remote, branch)
        outcomes = deliver(
            repo, home, host, remote, branch, settler, settle(repo, home, host, settler)
        )

    print(report(outcomes))
    return 1 if any(RULES[outcome.action].needs_user for outcome in outcomes) else 0


def deliver(
    repo: Path,
    home: Path,
    host: str,
    remote: str,
    branch: str,
    settler: conflicts.Settler,
    outcomes: list[Outcome],
) -> list[Outcome]:
    """Push, and plan §3.4 step 5: if the remote moved, pull, redo, push again.

    Redoing the whole of `settle` rather than just the push, because what came in
    may need merging into `$HOME` -- and doing it again is cheap: every file that
    is already settled resolves to UNCHANGED and writes nothing.

    The same `settler`, so a redo can ask about a file the first pass never saw:
    the remote moved, and what arrived may conflict with what this machine just
    wrote. Asking again is the honest thing -- the alternative is a second pass
    that silently skips a conflict the first pass would have prompted for.
    """
    there = f"{remote}/{branch}"
    for _ in range(ATTEMPTS):
        if gitrepo.is_ancestor(repo, "HEAD", there):
            # Everything here is already on the remote, so there is nothing to
            # push. Worth one local `merge-base` to skip: a push that prints
            # "Everything up-to-date" still opens the connection and negotiates
            # refs, and a machine that syncs on a timer takes this path almost
            # every time. `integrate` fetched, so `there` is current -- and when
            # it does not exist at all (an empty remote, before the first push)
            # `merge-base` fails, which is `False`, which pushes. That is the
            # answer wanted, from the same call.
            return outcomes
        pushed = gitrepo.push(repo, remote, branch)
        if pushed.ok:
            return outcomes
        if not integrate(repo, remote, branch):
            raise TupferlError(
                f"could not push to {remote}: {gitrepo.reason(pushed)}; "
                f"run `tupferl doctor` to check the remote."
            )
        outcomes = settle(repo, home, host, settler)
    raise TupferlError(
        f"{remote} moved again on each of {ATTEMPTS} attempts, so nothing was pushed; "
        f"try again when it is quieter."
    )
