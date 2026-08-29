"""`tupferl sync`: plan §3.4, whole.

Pull, work out what changed on each side, resolve everything that can be
resolved without asking, ask about the rest, commit, push.

**Two different conflicts reach the same prompt.** `settle` compares three
*files* -- `$HOME`, the repository's copy, and this machine's snapshot as the
base -- and `reconcile` compares three *commits*, as git's three index stages,
for the merge that happens when both machines have committed to the same lines.
Both build a `conflicts.Sides` and hand it to the run's settler, so a keypress
and a flag mean the same thing whichever one asked.

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
comparison below is already up to date. When that merge conflicts, `reconcile`
settles it in place and the merge is concluded -- and whatever it cannot settle
undoes the merge entirely, because a half-merged tree makes the *next* run refuse
to start.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import NamedTuple, NoReturn

from tupferl import conflicts, gitrepo, manage, manifest, merge, paths
from tupferl.copies import REGULAR, Blob, executable, read, write
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
#: The two answers the per-file review adds, for a change that is *not* a
#: conflict. `[r]` on a file this computer edited means "put the repository's
#: copy back", which is the undo tupferl had no command for -- and `[s]` leaves
#: a one-sided change where it is.
REVERTED = "reverted"
LEFT = "left alone"

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
    # `needs_user` on `LEFT` for `CONFLICT`'s reason rather than by analogy with
    # it: the run ends with something the user was asked about still undone, and
    # an exit status of 0 there would tell a script everything is synced. It is
    # *not* counted as a conflict by the report, which counts `sides`.
    REVERTED: Rule(to_repo=False, to_home=True, needs_user=False),
    LEFT: Rule(to_repo=False, to_home=False, needs_user=True),
    KEPT_LOCAL: Rule(to_repo=True, to_home=False, needs_user=False),
    KEPT_REMOTE: Rule(to_repo=False, to_home=True, needs_user=False),
    KEPT_BOTH: Rule(to_repo=True, to_home=True, needs_user=False),
    EDITED: Rule(to_repo=True, to_home=True, needs_user=False),
}


class Means(NamedTuple):
    """What one answer to the prompt is."""

    #: The action it becomes, whose `RULES` row decides what gets written.
    action: str
    #: Which of the two sides it keeps, for the two answers that name one.
    #: `None` for the three that do not: `[b]` and `[e]` carry a file that is
    #: neither side, and `[s]` writes nothing at all.
    keeps: Callable[[conflicts.Sides], Blob] | None


#: What each of the prompt's five answers means. A table for `RULES`' reason,
#: and it holds the side as well as the action for the same one: with the action
#: alone, `settled` had to re-derive "which side is that?" from the action
#: string, so a sixth answer needed entering here, in `RULES`, *and* in a chain
#: of comparisons -- three places, which is the thing the table exists to stop.
MEANS: dict[str, Means] = {
    conflicts.LOCAL: Means(KEPT_LOCAL, lambda sides: sides.home),
    conflicts.REMOTE: Means(KEPT_REMOTE, lambda sides: sides.stored),
    conflicts.BOTH: Means(KEPT_BOTH, None),
    conflicts.EDIT: Means(EDITED, None),
    conflicts.SKIP: Means(CONFLICT, None),
}


def changed(outcome: Outcome) -> bool:
    """Whether this outcome put new bytes anywhere the user can see."""
    rule = RULES[outcome.action]
    return rule.to_repo or rule.to_home


def pushes(action: str) -> bool:
    """Whether this action replaces the repository's copy and not `$HOME`'s.

    One definition, because two readers ask it and would otherwise each spell it
    out: `inspection.rendered` puts the replaced side on a diff's `-`, and the
    per-file review says which way the file is about to travel. A diff and a
    prompt disagreeing about the direction of the same file is the worst of the
    three possible bugs here, because each on its own looks right.

    `and not to_repo`'s absence is the point: a clean merge writes *both* sides,
    so `to_repo` alone is true for it and would call a merge a push.
    """
    rule = RULES[action]
    return rule.to_repo and not rule.to_home


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
    # survivor: branch -- tupferl/sync.py:233 in resolve() -- the `if` is always taken --
    #   equivalent: with `base` `None` the two comparisons inside are `Blob == None`, which is
    #   `False` (measured), so the forced branch falls through to exactly the merge the unforced
    #   code reaches.
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

    `[l]` and `[r]` name a side this function already has, and take it from the
    table rather than through the answer -- a copy of those bytes riding along
    would be a second place for them to differ from the ones that were compared.
    `[b]` and `[e]` produce a file that is neither side and hand it back. `[s]`
    does neither, which is what the last arm is: the conflict stands.
    """
    means = MEANS[answer.choice]
    if means.keeps is not None:
        return Outcome(sides.name, means.action, means.keeps(sides))
    if answer.data is not None:
        bit = executable_after(sides.base, sides.home, sides.stored)
        return Outcome(sides.name, means.action, Blob(answer.data, bit))
    # `[s]`, and the one way this can be wrong: an answer of `[b]` or `[e]`
    # built by hand with no bytes on it reads as a skip. Nothing constructs one
    # -- `ask` always attaches the file and the three flags only ever answer
    # `[l]`, `[r]` or `[s]` -- so this is named rather than guarded, since a
    # guard here could only be reached by a test written to reach it.
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
            # survivor: drop-kwarg -- tupferl/sync.py:301 in Backups.take() -- `exist_ok=True` is
            #   dropped -- unreachable within one run: `self.where` is set once per `Backups`, so
            #   the directory is created once. Two runs in the same second would collide, and no
            #   test can produce that without controlling the clock.
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


def integrate(repo: Path, remote: str, branch: str, host: str, settler: conflicts.Settler) -> int:
    """Fetch, and merge the remote branch if it holds anything new. How much?

    Zero is the old `False` and means exactly what it did, which is why this
    returns a count rather than a `bool`: `deliver` still reads it as "is a
    rejected push worth re-trying?", and `main` now also has a number to report
    (#26). It is a fact about commits rather than a reading of git's English --
    see `gitrepo.is_ancestor`.

    The count costs nothing over the wire. `gitrepo.distance` is a local
    `rev-list`, asked *after* the fetch above and *before* the merge below --
    the one window in which the answer is the number of commits about to arrive.
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
        return 0

    apart = gitrepo.distance(repo, "HEAD", there)
    # `1` when git would not compare, never `0`: zero is this function's word for
    # "nothing came in", and something is about to be merged. A count that is low
    # by one beats a report that denies the merge happened at all.
    coming = apart[1] if apart is not None else 1

    done = gitrepo.merge(repo, there)
    if done.ok:
        return coming

    # **Conflicted files first.** A merge can fail with nothing unmerged at all
    # -- a hook that refuses the commit, a tree that cannot be written -- and
    # there is no conflict to settle in that case and nothing to conclude.
    # Reconciling unconditionally made this branch commit a merge that had not
    # happened and report the commit's failure instead of the merge's;
    # `TestAGitLevelConflict`'s second fixture is what said so.
    if not gitrepo.unmerged(repo):
        undone(repo, f"could not merge {there}: {gitrepo.reason(done)}")

    # **Everything below runs inside an unfinished merge**, and the `finally` is
    # what makes that safe. `reconcile` prompts, runs the user's `$EDITOR`, and
    # writes files -- so it can raise a `TupferlError`, an `OSError`, or a
    # `KeyboardInterrupt` from someone pressing Ctrl-C at the prompt, and it can
    # do so with some files already settled and staged. Without the `finally`
    # each of those leaves `MERGE_HEAD` behind, and `sync.main`'s own
    # `gitrepo.unfinished` check then refuses *every* subsequent run until the
    # user does git surgery -- one interrupted prompt turning into a machine that
    # cannot sync at all, which is the failure the abort has always been for.
    #
    # `BaseException`, so Ctrl-C is covered; `settled` guards the success path so
    # a concluded merge is not undone by its own `finally`.
    concluded = False
    try:
        left = reconcile(repo, host, settler)
        # `left` alone. `gitrepo.unmerged` is `sorted(conflicted(repo))` now, and
        # `reconcile` returns exactly the names it did not stage, so a second
        # opinion from the same `ls-files` cannot disagree -- it was a redundant
        # git call and two mutations of it that no test could tell apart.
        if not left:
            finished = gitrepo.commit(repo, f"sync: settle the merge of {there}")
            if not finished.ok:
                undone(
                    repo,
                    f"settled every file of the merge of {there} and then could not commit "
                    f"it: {gitrepo.reason(finished)}",
                )
            concluded = True
            return coming
    finally:
        # survivor: branch -- tupferl/sync.py:417 in integrate() -- the `if` is always taken --
        #   equivalent: after a concluded merge git has cleared `MERGE_HEAD`, so the extra `git
        #   merge --abort` fails and its `Result` is discarded. The guard stops a *successful* merge
        #   being undone by its own `finally`, which is a claim about a git that behaved
        #   differently.
        if not concluded:
            gitrepo.abort_merge(repo)

    # What is left is what the prompt has no answer for: a file one side deleted
    # and the other edited, one that is not a regular file on both sides, or one
    # the user skipped. Each is a person's decision, and none is a choice between
    # lines. `left` and not `gitrepo.unmerged`, because the two spelled paths
    # differently until `unmerged` was rewritten and the user was told to go and
    # resolve a name that did not exist.
    raise TupferlError(
        f"{there} and this machine disagree about {', '.join(left)} in a way the prompt "
        f"cannot settle -- one side changed the file and the other removed or replaced "
        f"it, or it is not a dotfile this machine merges, or you skipped it; the merge "
        f"was undone, so resolve it with `git -C {repo} pull` and sync again."
    )


def undone(repo: Path, why: str) -> NoReturn:
    """Undo the merge in progress, then raise `why`.

    One function because the guarantee is one sentence and it was written at only
    one of three abort sites: a half-merged tree makes the *next* run refuse to
    start, so nothing may raise out of a merge without first putting the
    repository back where it was found. A fourth failure added later cannot
    forget it here.

    The message says so, because every one of these reaches the user and "the
    merge was undone" is what tells them they may simply sync again.
    """
    gitrepo.abort_merge(repo)
    raise TupferlError(
        f"{why}; the merge was undone, so nothing is half-done -- run `tupferl doctor`, "
        f"then sync again."
    )


def held(repo: Path, number: int, name: str, modes: dict[int, int]) -> Blob | None:
    """One stage of a conflicted file as a `Blob`, or `None` when that side has none.

    **`modes` decides whether the side exists; `version` only fetches it.** They
    are different questions and conflating them was wrong in two directions: a
    `cat-file` that failed for a transient reason read as "that side deleted the
    file", which `integrate` reports to the user as a delete-against-edit that
    never happened -- and on stage 1 it silently produced `base = None`, so `[b]`,
    `[e]`, the hunk display and `executable_after`'s tie-break were all computed
    against an ancestor that is not the ancestor. The user is then shown, and
    asked to settle, lines only one side ever changed.

    The mode comes from the index rather than from disk: during a conflict the
    working tree holds git's marked-up merge, whose bits say nothing about what
    either side recorded. Plan §5 asks for the executable bit to travel, and this
    is where it is.
    """
    if number not in modes:
        return None
    data = gitrepo.version(repo, number, name)
    # survivor: branch -- tupferl/sync.py:473 in held() -- the `if` is never taken -- unreachable
    #   without breaking git mid-call: the branch fires when git lists a stage and then will not
    #   produce it. `held` is called with modes git has just reported, so producing the state means
    #   killing git between two of its own calls.
    if data is None:
        raise TupferlError(
            f"git has a stage {number} for {name} in {repo} but would not produce it; "
            f"run `tupferl doctor` to check your git installation."
        )
    return Blob(data, executable(modes[number]))


def reconcile(repo: Path, host: str, settler: conflicts.Settler) -> list[str]:
    """Settle every file git could not merge. Returns the names still unsettled.

    This is plan §3.4's prompt over the *index* rather than over three files: a
    conflict between two commits, which happens whenever a machine has committed
    without pushing -- `tupferl add` does exactly that -- and the other machine
    has pushed to the same lines meanwhile.

    The three versions are the index's three stages, and which is which is a fact
    about git that `tests/test_gitrepo.py` asserts rather than assumes: stage 2
    is the branch being merged into, so it is this computer's, and stage 3 is the
    branch being merged in, so it is the repository's. Backwards, `--ours` keeps
    the side the user asked to discard.

    Four shapes are refused rather than settled, and each comes back for the
    caller to report:

    - **A path that is not this machine's to merge**, which `manifest.mergeable`
      decides: a sync snapshot, or another host's overlay. This loop walks git's
      index rather than `manifest.managed`, so nothing else reapplies the rule
      that `manifest.under` applies to a walk of the tree -- and without it a
      conflicting `.tupferl/state/<host>/<file>` reached the dotfile prompt, and
      settling it wrote a merge of two snapshots (#15).
    - **A file only one side still has.** A delete against an edit is not a
      disagreement about lines, and the prompt has no key that means "keep it" or
      "let it go" -- offering `[l]` and `[r]` for it would be inventing an answer
      to a question nobody asked.
    - **Anything that is not a regular file on both sides.** `copies.write`
      follows a symlink, so settling a conflict over a committed symlink writes
      *through* it -- to a file outside the repository. `manifest` refuses
      symlinks at `add` time; a path out of the index has had no such check.
    - **Whatever the settler skips.**

    The whole loop runs *inside* an unfinished merge, so every way out of it has
    to leave the repository somewhere the next run can start from. That is
    `integrate`'s `finally`, not this function's: see `undone`.
    """
    left: list[str] = []
    # survivor: order -- tupferl/sync.py:519 in reconcile() -- `sorted` becomes `list` -- equivalent
    #   for the names git can produce, same argument as `gitrepo.py:474`: `conflicted`'s dict is
    #   built in git's own byte order. The user-facing ordering that this feeds is asserted by
    #   `TestWhenSeveralFilesCannotBeSettled`.
    for name, modes in sorted(gitrepo.conflicted(repo).items()):
        if not manifest.mergeable(PurePosixPath(name), repo, host):
            # To `left`, never silently skipped. `integrate` concludes the merge
            # when `left` is empty, so a path dropped here would leave the index
            # unmerged while this function reported success -- and `git commit`
            # would then refuse, reaching the user as "could not commit", which
            # says nothing about the file that caused it.
            left.append(name)
            continue
        # survivor: order -- tupferl/sync.py:528 in reconcile() -- `any` becomes `all` -- measured
        #   and named in the suite: `TestWhenOneSideReplacedTheFileWithASymlink`'s docstring records
        #   that git splits a type change into two paths of one stage each (`{'.config/thing': {3:
        #   0o120000}, '.config/thing~HEAD': {2: 0o100644}}`), so a file with *mixed* stage kinds
        #   cannot be built and the two spellings cannot be told apart.
        if any(mode not in REGULAR for mode in modes.values()):
            left.append(name)
            continue
        ours = held(repo, gitrepo.OURS, name, modes)
        theirs = held(repo, gitrepo.THEIRS, name, modes)
        if ours is None or theirs is None:
            left.append(name)
            continue

        # `resolve`, not a second copy of it. The decision is the same one --
        # three versions in, settled bytes out -- and a `MEANS` row added to one
        # of two copies would apply to a `$HOME` conflict and not to a commit
        # conflict. Only `.blob` is read: `resolve`'s action names are
        # `$HOME`-flavoured (`RESTORED`, `TO_HOME`) and mean nothing here, and
        # its one-sided arms are unreachable anyway, since git would not have
        # conflicted if only one side had changed.
        outcome = resolve(PurePosixPath(name), held(repo, gitrepo.BASE, name, modes), ours, theirs)
        if outcome.sides is not None:
            outcome = settled(outcome.sides, settler(outcome.sides))
        if outcome.blob is None:
            left.append(name)
            continue
        write(repo / name, outcome.blob)
    if left:
        return left
    staged = gitrepo.stage(repo, [repo / name for name in gitrepo.conflicted(repo)])
    if not staged.ok:
        # "the merge was undone" is true because `integrate` is this function's
        # only caller and its `finally` aborts whatever raises out of here. Said
        # rather than done: aborting here as well would leave two `merge --abort`
        # calls for one merge, the second of which fails for no reason anyone
        # reading the code could see.
        raise TupferlError(
            f"could not stage the settled files: {gitrepo.reason(staged)}; the merge was "
            f"undone, so run `tupferl doctor` and sync again."
        )
    # survivor: return-value -- tupferl/sync.py:564 in reconcile() -- returns `None` instead of `[]`
    #   -- equivalent through every caller: `integrate` truth-tests the result (`if not left`) and
    #   puts it in a message only when it is non-empty, so `None` and `[]` are the same answer. A
    #   type violation with no reader -- worth a note, not a fixture.
    return []


class Traffic(NamedTuple):
    """What crossed the wire, for the one line `sync` says about the remote (#26).

    `sync` reported only what it wrote in `$HOME` and said nothing about the
    push -- the thing the command exists for. Worse, the *no remote* case had a
    sentence of its own, so the tool spoke up in the harmless case and was
    silent in the one that matters: a first `tupferl sync` answered "is it on
    the remote now?" with a blank line.

    **A count for what came in, and a plain yes for what went out.** The
    incoming count is certain and free -- `gitrepo.distance` is a local
    `rev-list`, asked after `integrate`'s own fetch and before its merge, so no
    second round trip pays for it. There is no equally certain number for the
    outgoing side: a push's count would have to be measured before the push and
    believed afterwards, and "pushed to origin/main" answers the user's question
    without inventing one.
    """

    #: Commits taken in from `<remote>/<branch>`.
    pulled: int
    #: Whether anything was sent. `False` covers both "already up to date" and
    #: "there was nothing to send", which read the same to a user and are the
    #: same fact about the remote.
    pushed: bool


class Reading(NamedTuple):
    """One managed file: where its three copies live, and what they decide.

    Everything `settle` needs to *write* the decision, and everything `status`
    needs to *describe* it. One walk rather than two, and that is the point:
    `status` promises the user a preview of the next `sync`, and a preview
    computed by a second copy of this loop is one that agrees until somebody
    edits one of them -- at which point the command whose whole job is to be
    trusted before a write is the one that is quietly wrong.
    """

    name: PurePosixPath
    #: The repository's copy: this host's overlay, or the shared tree.
    where: Path
    #: The file in `$HOME`.
    target: Path
    #: This host's merge base.
    snapshot: Path
    #: `$HOME`'s bytes, or `None` when there is no regular file there.
    found: Blob | None
    #: The repository's bytes, or `None` when what is there is not a regular
    #: file -- which is the `REFUSED` outcome below, and the one case where
    #: neither side can be shown.
    stored: Blob | None
    #: What the three versions come to. Never settled -- a `CONFLICT` here still
    #: carries its `sides`, because whether to ask a person is the caller's
    #: business and `status` must not.
    outcome: Outcome


def refused(name: PurePosixPath, why: str) -> Outcome:
    """A file tupferl will not touch, and the sentence saying why.

    A function rather than the constructor spelled out three times: `REFUSED` is
    the one action whose `blob` must be `None` -- `apply` writes whatever a blob
    holds -- and three spellings of that pairing is three chances to write one
    with bytes on it.
    """
    return Outcome(name, REFUSED, None, why=why)


def examine(repo: Path, home: Path, host: str) -> Iterator[Reading]:
    """Every managed file, resolved against its snapshot. Writes nothing.

    In `manifest.managed`'s order, which is sorted -- so the report, the commit
    message and `status` list files the same way on every machine.

    **Nothing here touches disk except to read it**, which is what lets `status`
    borrow the whole loop. `resolve` does run `git merge-file` for a file both
    sides changed, in a temporary directory of its own: that is how `status`
    can say "and they merge cleanly" rather than leaving the user to find out
    during the sync, and it is the same merge `sync` would perform.

    A generator because `settle` wants to act on each file as it arrives and
    `status` wants to measure the widest name before printing any of them; a
    list built here would decide that for both.
    """
    snapshots = paths.snapshot_dir(repo, host)
    for item in manifest.managed(repo, host):
        where = manifest.location(repo, host, item.host) / item.name
        target = home / item.name
        snapshot = snapshots / item.name
        stored = read(where)
        if stored is None:
            why = f"{where} is not a regular file"
            yield Reading(item.name, where, target, snapshot, None, None, refused(item.name, why))
            continue
        found = read(target)
        if found is None and os.path.lexists(target):
            why = f"{target} is not a regular file"
            yield Reading(item.name, where, target, snapshot, None, stored, refused(item.name, why))
            continue
        yield Reading(
            item.name,
            where,
            target,
            snapshot,
            found,
            stored,
            resolve(item.name, read(snapshot), found, stored),
        )


def looked_at(reading: Reading, reviewer: conflicts.Reviewer) -> Outcome:
    """One file's outcome after the person was shown it and asked.

    Only ever called for a change *this computer* made -- see the guard in
    `settle` for why incoming ones are left out. So `reverse=True` is not a
    decision taken here twice: it is what `pushes` already said, and the diff
    the prompt shows is the one `status --diff` would have shown.

    A table, for `RULES`' reason: three answers, one action each, and a row
    that is missing is a `KeyError` rather than a silent fall-through.

    `[r]` is `REVERTED`: the repository's copy goes back into `$HOME` and the
    edit is gone. It is the one answer here that destroys something -- the undo
    tupferl otherwise has no command for -- which is why `offers` spells it out
    rather than calling it "keep remote".
    """
    stored, found = reading.stored, reading.found
    # survivor: branch, connector -- tupferl/sync.py:722 in looked_at() -- the `if` is never taken.
    #   Equivalent: the guard is unreachable. `settle` calls this only when `pushes(action)`, which
    #   is true for `TO_REPO` alone, and `resolve` produces that only with both sides present -- so
    #   `stored is None or found is None` cannot fire. It is there because returning the outcome
    #   unchanged is what stays right if a later rule makes it reachable, where raising would turn a
    #   display decision into a failed sync.
    if stored is None or found is None:
        # Neither can be `None` for an outbound change -- `pushes` is only true
        # for `TO_REPO`, which needs both sides -- so this is unreachable rather
        # than a case being skipped. Returning the outcome unchanged is what
        # stays right if a later rule makes it reachable, where raising would
        # turn a display decision into a failed sync.
        # survivor: return-value -- tupferl/sync.py:728 in looked_at() -- returns `None` instead of
        #   `reading.outcome`. Equivalent: the line is the body of the unreachable guard above.
        #   Nothing reaches it, so what it returns cannot be observed.
        return reading.outcome
    diff = merge.unified(str(reading.name), found.data, stored.data, reverse=True)
    # **The bytes as well as the action.** `resolve` set `blob` to what `TO_REPO`
    # would write, which is `$HOME`'s copy -- so replacing the action alone made
    # `[r]` write `$HOME` back over itself and report `reverted`. A no-op that
    # says it undid something, found by pressing the key rather than by reading
    # the code: the report was right about what it meant to do and wrong about
    # what it did, which is the pair no unit test of the mapping would separate.
    became, blob = {
        conflicts.LOCAL: (TO_REPO, found),
        conflicts.REMOTE: (REVERTED, stored),
        conflicts.SKIP: (LEFT, None),
    }[reviewer(conflicts.Change(reading.name, diff))]
    return reading.outcome._replace(action=became, blob=blob)


def settle(
    repo: Path,
    home: Path,
    host: str,
    settler: conflicts.Settler,
    reviewer: conflicts.Reviewer | None = None,
) -> list[Outcome]:
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

    outcomes: list[Outcome] = []
    touched: list[PurePosixPath] = []
    # Consumed as it arrives, rather than through a list built first. Not
    # thrift: `settled` can run the user's `$EDITOR`, so a run that read every
    # file up front would answer a later file from bytes taken before that
    # editor opened. Which of the two is *better* is arguable -- the point is
    # that it was not this change's to decide, and lazily is what `settle` did
    # before `examine` was lifted out of it.
    managing: set[PurePosixPath] = set()

    for reading in examine(repo, home, host):
        managing.add(reading.name)
        outcome = reading.outcome
        if outcome.sides is not None:
            outcome = settled(outcome.sides, settler(outcome.sides))
        elif reviewer is not None and pushes(outcome.action):
            # **Only what this computer changed.** An unchanged file is most
            # files on most runs and has nothing to ask about; an *incoming*
            # one is deliberately left out, and not for want of trying. A
            # commit-level conflict the user has just settled at the `[l]/[r]`
            # prompt arrives here as an ordinary inbound change on the second
            # pass, so reviewing those asked twice about one file in one run --
            # and telling the two apart means mapping git's paths in
            # `reconcile` back to managed names, which is a lot of machinery
            # for a question already answered. `status --diff` shows what is
            # coming, correctly oriented, before the sync runs.
            outcome = looked_at(reading, reviewer)
        try:
            wrote = apply(
                outcome, reading.target, reading.where, reading.snapshot, reading.found, backups
            )
        except OSError as unwritable:
            # One unwritable path does not stop the sync, for `manifest.collect`'s
            # reason: forty files of which one now sits under a directory that has
            # become a file should leave thirty-nine synced and say what it
            # skipped. It also keeps an `OSError` from reaching the user as a
            # traceback, which plan §5 rules out for anything they can act on.
            outcome = refused(reading.name, f"could not write it ({unwritable.strerror})")
            wrote = False
        outcomes.append(outcome)
        if wrote:
            touched.append(reading.name)

    for name in stale(snapshots, managing):
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
    if rule.to_home and found is not None and found != outcome.blob:
        # Three conditions, and each rules out a backup of nothing:
        #
        # - `to_home`, because only a write to `$HOME` replaces the user's file;
        # - `found is not None`, because RESTORED is the case where `$HOME` had
        #   no file at all and `resolve` is what guarantees it;
        # - `found != outcome.blob`, because the write below would then change
        #   nothing. `[e]` reaches it easily -- editing the merged file down to
        #   exactly what `$HOME` already holds is how a user says "keep mine,
        #   but let me look first" -- and MERGED reaches it whenever the merge
        #   lands back on this side's bytes.
        #
        # The third is not thrift. A backup directory is created per run that
        # takes one, and `forget_old` keeps five: a backup of a file nothing
        # replaced evicts a real one, which is the copy plan §5 exists to keep.
        backups.take(outcome.name, found)

    wrote = False
    # survivor: branch -- tupferl/sync.py:792 in apply() -- the `if` is always taken -- equivalent,
    #   and the reason is an invariant worth stating: every rule carries the blob to the side that
    #   does *not* already hold it -- `TO_REPO` carries `home`, `TO_HOME` carries `stored` -- so
    #   writing it to the other side is a no-op and `write` reports no change. The backup is gated
    #   on `found != outcome.blob`, which is false in exactly those cases.
    if rule.to_repo:
        wrote |= write(where, outcome.blob)
    # survivor: branch -- tupferl/sync.py:794 in apply() -- the `if` is always taken -- equivalent,
    #   same invariant as `sync.py:792`: the blob is always the side the other one is missing, so
    #   the extra write changes no bytes and takes no backup.
    if rule.to_home:
        wrote |= write(target, outcome.blob)
    wrote |= write(snapshot, outcome.blob)
    return wrote


def crossed(there: str, traffic: Traffic) -> str:
    """The one line `sync` says about the remote (#26).

    Printed *before* `report`, because it is the answer to a different question
    -- "did my dotfiles get there?" -- and burying it under a per-file list is
    what made the old silence easy to miss.

    **One line, and short when nothing happened.** `report` is deliberately
    silent about unchanged files so a machine that syncs on a timer prints almost
    nothing; a paragraph here would undo that. "already up to date" is the whole
    sentence on a quiet run, and it earns its place by being the run where the
    user most wants to know the remote was actually reached.
    """
    if traffic.pulled and traffic.pushed:
        return f"{there}: took in {manage.count(traffic.pulled, 'commit')}, and pushed"
    if traffic.pulled:
        return f"{there}: took in {manage.count(traffic.pulled, 'commit')}"
    if traffic.pushed:
        return f"{there}: pushed"
    return f"{there}: already up to date"


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
    summary = f"{manage.count(len(outcomes))} managed, {moved} changed, {unsettled} in conflict"
    # **Only when there are any**, so a run nobody skipped anything on reads
    # exactly as it did before. A skipped file is neither changed nor in
    # conflict, so without this the summary said "0 changed, 0 in conflict"
    # over an exit status of 1 -- a line that reads as "nothing outstanding"
    # under a status meaning the opposite, and the per-file line above is easy
    # to scroll past on a run with forty files in it.
    left = sum(1 for outcome in outcomes if outcome.action == LEFT)
    if left:
        summary += f", {left} left alone"
    lines.append(f"\n{summary}")
    return "\n".join(lines)


def main(
    no_input: bool = False, ours: bool = False, theirs: bool = False, auto: bool = False
) -> int:
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
    repo, _ = manage.open_repo()
    host = paths.hostname()
    home = paths.home()

    marker = gitrepo.unfinished(repo)
    if marker is not None:
        raise TupferlError(
            f"{repo} has an unfinished git operation ({marker} is present); finish or "
            f"abort it with git, then sync again."
        )

    settler = conflicts.answering(no_input, ours, theirs, repo)
    # `None` on a run that has nobody to ask or has already said what it wants,
    # and then `settle` keeps what `resolve` decided -- which is what `sync` did
    # for every one-sided change before this existed.
    reviewer = conflicts.reviewing(auto, ours, theirs, no_input)
    remote = gitrepo.first_remote(repo)
    branch = gitrepo.branch(repo)
    if remote is None:
        outcomes = settle(repo, home, host, settler, reviewer)
        print(f"no remote configured, so nothing was pushed; {repo} is a git repository")
    else:
        if branch is None:
            raise TupferlError(
                f"{repo} has no branch checked out, so there is nothing to push; "
                f"run `git -C {repo} checkout main`."
            )
        came = integrate(repo, remote, branch, host, settler)
        outcomes, moved = deliver(repo, home, host, remote, branch, settler, reviewer)
        print(crossed(f"{remote}/{branch}", Traffic(came + moved.pulled, moved.pushed)))

    print(report(outcomes))
    return 1 if any(RULES[outcome.action].needs_user for outcome in outcomes) else 0


def deliver(
    repo: Path,
    home: Path,
    host: str,
    remote: str,
    branch: str,
    settler: conflicts.Settler,
    reviewer: conflicts.Reviewer | None = None,
) -> tuple[list[Outcome], Traffic]:
    """Push, and plan §3.4 step 5: if the remote moved, pull, redo, push again.

    Returns what it did to the remote as well as to the files, because nothing
    else can: `main` cannot measure the push from outside, since `settle` commits
    in the middle of this function and a count taken before it would be of the
    wrong tree (#26). A retry pulls again, so the incoming count accumulates
    here rather than being the first `integrate`'s alone.

    Redoing the whole of `settle` rather than just the push, because what came in
    may need merging into `$HOME` -- and doing it again is cheap: every file that
    is already settled resolves to UNCHANGED and writes nothing.

    The same `settler`, so a redo can ask about a file the first pass never saw:
    the remote moved, and what arrived may conflict with what this machine just
    wrote. Asking again is the honest thing -- the alternative is a second pass
    that silently skips a conflict the first pass would have prompted for.

    A file the user *skipped* is asked about again too, up to `ATTEMPTS` times.
    That is intended rather than overlooked: each retry follows a real change to
    the repository's copy, so it is a different question from the one they
    declined, and a `[s]` that stuck would hide it.
    """
    outcomes = settle(repo, home, host, settler, reviewer)
    there = f"{remote}/{branch}"
    pulled = 0
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
            return outcomes, Traffic(pulled, pushed=False)
        pushed = gitrepo.push(repo, remote, branch)
        if pushed.ok:
            return outcomes, Traffic(pulled, pushed=True)
        came = integrate(repo, remote, branch, host, settler)
        pulled += came
        if not came:
            raise TupferlError(
                f"could not push to {remote}: {gitrepo.reason(pushed)}; "
                f"run `tupferl doctor` to check the remote."
            )
        outcomes = settle(repo, home, host, settler, reviewer)
    raise TupferlError(
        f"{remote} moved again on each of {ATTEMPTS} attempts, so nothing was pushed; "
        f"try again when it is quieter."
    )
