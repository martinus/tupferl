"""A conflict between two *commits*, settled at the same prompt (issue #10).

Not the same shape as `tests/test_sync_conflicts.py`, and the difference is the
whole subject. There, `$HOME` and the repository's working copy disagree and the
snapshot is the base. Here two *branches* disagree: this machine has committed
without pushing -- which `tupferl add` does every time -- and the other machine
has pushed to the same lines meanwhile. git's own merge fails, and the three
versions are the index's three stages rather than three files.

**The fixture is the reproduction from the issue**, not a contrivance: `add`,
then the other machine syncs, then this one does. Nothing here reaches for
plumbing to arrange the conflict, because the point of the issue is that an
ordinary sequence produces it.

**Which stage is which side is what these tests are really about.** git's stage 2
is the branch being merged into and stage 3 the branch being merged in; that
lines up with `--ours` and `--theirs` by luck rather than by construction.
Backwards, every one of these still merges cleanly and silently keeps the side
the user asked to discard -- so each assertion names the content it expects, and
the two sides never share a line.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from tests import support
from tupferl import conflicts, gitrepo, paths, sync
from tupferl.errors import TupferlError

#: What each machine commits. Distinct, and neither a prefix of the other, so no
#: assertion below can hold against the wrong side.
FROM_A = "FROM-A\ntwo\nthree\n"
FROM_B = "FROM-B\ntwo\nthree\n"

#: What `[b]` keeps. `machine-b` is the one that answers, so its line is "ours"
#: and comes first -- the same order `git merge-file --union` uses.
BOTH_KEPT = "FROM-B\nFROM-A\ntwo\nthree\n"

MANAGED = ".vimrc"

#: A committed symlink, which `copies.write` would write **through**.
LINK = ".config/link"

#: The path one machine turns into a symlink while the other keeps it a file.
SWAPPED = ".config/thing"

#: What both machines in `TestWhatThisMachineWillNotMerge` call themselves. The
#: collision is the fixture.
TWIN = "laptop"

#: Long enough that two edits are separate hunks. `l20` is the line only
#: `machine-b` touches, and whether it survives is the whole assertion.
SHARED = "\n".join(f"l{number}" for number in range(30)) + "\n"


@dataclass(frozen=True)
class Victimised(support.TwoMachines):
    """Two machines, and a file *outside* the repository a settled link could
    have been written through."""

    victim: Path


@dataclass(frozen=True)
class TwoCommits(support.TwoMachines):
    """`machine-b` holds a commit the remote has never seen, and they disagree.

    The order matters and is the issue's: `machine-b` commits *first* (through
    `add`, which does not push), and only then does `machine-a` push. Reversed,
    `machine-b` would fetch the change before committing over it and there would
    be no conflict at all.
    """

    def diverge_by_committing(self, mine: str, theirs: str, executable: bool = False) -> None:
        self.second.write(MANAGED, mine)
        self.first.write(MANAGED, theirs)
        if executable:
            (self.second.home / MANAGED).chmod(0o755)
            (self.first.home / MANAGED).chmod(0o755)
        # `add` commits without pushing on both, and then only `machine-a` syncs.
        assert self.second.call("add", str(self.second.home / MANAGED)) == 0
        assert self.first.call("add", str(self.first.home / MANAGED)) == 0
        assert self.first.call("sync") == 0

    def settle(self, *args: str, keys: str | None = None) -> None:
        """Sync `machine-b` and insist it finished. Exit 0 is the assertion that
        something was decided: an unsettled conflict is 1 and a run that could
        not proceed is 2, so neither can reach the caller's checks."""
        assert self.second.call("sync", *args, keys=keys) == 0

    def concluded(self) -> None:
        """The merge is over: nothing unmerged, nothing dirty, no `MERGE_HEAD`.

        Asserted after every settled case, because a run that wrote the right
        bytes and left the merge half-finished makes the *next* sync refuse to
        start -- which is the failure `integrate`'s abort exists to prevent, in
        the branch that is supposed to succeed.
        """
        assert gitrepo.unfinished(self.second.repo) is None
        assert gitrepo.unmerged(self.second.repo) == []
        assert self.second.git("status", "--porcelain") == ""

    def everywhere(self, want: str) -> None:
        """`want` is `machine-b`'s file and its stored copy, and reaches the
        other machine on its next sync.

        The last is the one a weaker test would leave out: a choice that was
        written and then lost on the other computer is not a settled conflict.
        """
        assert self.second.read(MANAGED) == want
        assert self.second.stored(MANAGED).read_text(encoding="utf-8") == want
        assert self.first.call("sync") == 0
        assert self.first.read(MANAGED) == want


@pytest.fixture
def two_commits(two_machines: support.TwoMachines) -> TwoCommits:
    """A `TwoCommits` with the commit-level conflict already arranged."""
    box = TwoCommits(**vars(two_machines))
    assert box.second.call("init", str(box.remote)) == 0
    box.diverge_by_committing(FROM_B, FROM_A)
    return box


@pytest.fixture
def executable(two_machines: support.TwoMachines) -> TwoCommits:
    """`two_commits`, with both sides committing the file executable."""
    box = TwoCommits(**vars(two_machines))
    assert box.second.call("init", str(box.remote)) == 0
    box.diverge_by_committing(FROM_B, FROM_A, executable=True)
    return box


@contextlib.contextmanager
def raising(kind: type[BaseException]) -> Iterator[None]:
    """Make the settler raise, the way a broken `$EDITOR` or a Ctrl-C does."""

    def boom(sides: object) -> conflicts.Answer:
        raise kind("interrupted")

    with mock.patch.object(conflicts, "answering", lambda *a: boom):
        yield


@contextlib.contextmanager
def breaking(box: support.TwoMachines) -> Iterator[None]:
    """Make `git add` fail, but only while a merge is unfinished.

    Forced by patching `gitrepo.stage`, tupferl's own wrapper, rather than by
    arranging a git that refuses: git has no hook on `add`, and the fixtures
    that break a *commit* leave `add` working. The alternative -- an unwritable
    `.git/index` -- is skipped wherever the suite runs as root, which is most
    containers, so it would be a test that quietly does nothing on the leg most
    likely to run it.

    Only the merge's own staging is broken. `record`'s later `stage` must still
    work, or the test would be about that instead: `sync` never gets there,
    because `integrate` raises first.
    """
    real = gitrepo.stage

    def refuse(repo: object, paths: list[object]) -> gitrepo.Result:
        if gitrepo.unfinished(box.second.repo) is not None:
            return gitrepo.Result(out="", err="fatal: could not add", code=128)
        return real(repo, paths)  # type: ignore[arg-type]

    with mock.patch.object(gitrepo, "stage", refuse):
        yield


def collide(box: support.TwoMachines, name: str) -> None:
    """Make both machines commit to `name` without either seeing the other.

    `add` commits and does not push, which is the ordinary way a machine
    gets a commit the remote has never seen -- the issue's own reproduction.
    """
    box.second.write(name, "FROM-B\ntwo\nthree\n")
    assert box.second.call("add", str(box.second.home / name)) == 0
    box.first.write(name, "FROM-A\ntwo\nthree\n")
    assert box.first.call("sync") == 0


@pytest.mark.usefixtures("two_commits")
class TestTheFixtureReallyProducesACommitConflict:
    """Stated first, because every test below is vacuous without it.

    If `machine-b`'s sync merged cleanly, or if the conflict were the ordinary
    `$HOME`-against-repository kind, these would all pass against code that never
    runs `reconcile`.
    """

    def test_the_second_machine_holds_an_unpushed_commit(self, two_commits: TwoCommits) -> None:
        """What `add` leaves behind, and the reason the issue calls this easy to
        reach."""
        assert two_commits.second.git("rev-parse", "origin/main") != two_commits.second.git(
            "rev-parse", "HEAD"
        )

    def test_git_cannot_merge_the_two_branches_on_its_own(self, two_commits: TwoCommits) -> None:
        """The precondition, asserted by asking git rather than by assuming.

        `--no-commit` so the check leaves nothing behind; the sync under test
        does its own merge from a clean tree.
        """
        two_commits.second.git("fetch", "origin")
        assert support.git_merged(two_commits.second.repo, two_commits.second.env) != 0, (
            "git merged cleanly, so there is no conflict to settle"
        )
        assert MANAGED in gitrepo.unmerged(two_commits.second.repo)
        support.git_aborted(two_commits.second.repo, two_commits.second.env)


@pytest.mark.usefixtures("two_commits")
class TestTheFlags:
    def test_ours_keeps_this_machines_commit(self, two_commits: TwoCommits) -> None:
        two_commits.settle("--ours")
        two_commits.concluded()
        two_commits.everywhere(FROM_B)

    def test_theirs_keeps_the_repositorys_commit(self, two_commits: TwoCommits) -> None:
        """The other side, and the test that would fail if stage 2 and stage 3
        were read the wrong way round -- `--ours` alone cannot tell."""
        two_commits.settle("--theirs")
        two_commits.concluded()
        two_commits.everywhere(FROM_A)

    def test_no_input_leaves_the_repository_exactly_as_it_was(
        self, two_commits: TwoCommits
    ) -> None:
        """Nobody is there to answer, so nothing is decided -- and the merge is
        undone rather than left half-done, because a half-merged tree makes the
        next run refuse to start."""
        was = two_commits.second.git("rev-parse", "HEAD")
        assert two_commits.second.call("sync", "--no-input") == 2
        assert gitrepo.unfinished(two_commits.second.repo) is None
        assert two_commits.second.git("status", "--porcelain") == ""
        assert two_commits.second.git("rev-parse", "HEAD") == was

    def test_the_next_sync_still_works_after_one_was_left_unsettled(
        self, two_commits: TwoCommits
    ) -> None:
        """The point of aborting. Without it the second run finds an unfinished
        merge and refuses, turning one conflict into a machine that cannot sync
        at all."""
        assert two_commits.second.call("sync", "--no-input") == 2
        two_commits.settle("--theirs")
        two_commits.everywhere(FROM_A)


@pytest.mark.usefixtures("two_commits")
class TestTheKeys:
    """The same three answers, typed at a real terminal rather than passed."""

    def test_l_keeps_this_machines_commit(self, two_commits: TwoCommits) -> None:
        two_commits.settle(keys="l")
        two_commits.concluded()
        two_commits.everywhere(FROM_B)

    def test_r_keeps_the_repositorys_commit(self, two_commits: TwoCommits) -> None:
        two_commits.settle(keys="r")
        two_commits.concluded()
        two_commits.everywhere(FROM_A)

    def test_b_keeps_both(self, two_commits: TwoCommits) -> None:
        """`[b]` is a union merge of the two *commits*, which is the same
        operation on different inputs -- and the one answer that proves the
        settled bytes are neither side rather than a copy of one."""
        two_commits.settle(keys="b")
        two_commits.concluded()
        two_commits.everywhere(BOTH_KEPT)

    def test_s_leaves_the_repository_exactly_as_it_was(self, two_commits: TwoCommits) -> None:
        """`--porcelain` as well as the commit, because a file settled before the
        skipped one is written into the working tree, and only the abort takes it
        back out. Without this the test cannot see it left behind."""
        was = two_commits.second.git("rev-parse", "HEAD")
        assert two_commits.second.call("sync", keys="s") == 2
        assert gitrepo.unfinished(two_commits.second.repo) is None
        assert two_commits.second.git("status", "--porcelain") == ""
        assert two_commits.second.git("rev-parse", "HEAD") == was


@pytest.mark.usefixtures("executable")
class TestTheExecutableBit:
    """Plan §5 asks for the one mode bit to travel, and during a conflict it
    lives in the index rather than in the working tree -- which holds git's
    marked-up merge, whose bits say nothing about what either side recorded."""

    def test_the_fixture_really_committed_an_executable_file(self, executable: TwoCommits) -> None:
        """Otherwise the assertion below holds for a fixture that never set the
        bit, which is a negative claim with no precondition."""
        assert "100755" in executable.second.git("ls-files", "-s", MANAGED)

    def test_a_settled_file_is_still_executable(self, executable: TwoCommits) -> None:
        executable.settle("--ours")
        assert (executable.second.home / MANAGED).stat().st_mode & 0o111
        assert "100755" in executable.second.git("ls-files", "-s", MANAGED)


@pytest.fixture
def one_side_deleted(two_machines: support.TwoMachines) -> support.TwoMachines:
    """One machine edits and commits the file; the other stops managing it."""
    box = two_machines
    # **Both machines agree about the file first.** Diverging by content and
    # by existence at once is two conflicts, and the first one settles before
    # this one is ever reached -- which is what the first attempt at this
    # fixture did, and it failed in setup rather than in a test.
    box.first.write(MANAGED, FROM_A)
    assert box.first.call("add", str(box.first.home / MANAGED)) == 0
    assert box.first.call("sync") == 0
    assert box.second.call("init", str(box.remote)) == 0
    assert box.second.read(MANAGED) == FROM_A

    # Now they disagree about whether it should exist at all: `machine-b`
    # edits and commits without pushing, `machine-a` stops managing it.
    box.second.write(MANAGED, FROM_B)
    assert box.second.call("add", str(box.second.home / MANAGED)) == 0
    assert box.first.call("remove", str(box.first.home / MANAGED)) == 0
    assert box.first.call("sync") == 0
    return box


@pytest.mark.usefixtures("one_side_deleted")
class TestAFileOnlyOneSideStillHas:
    """A delete against an edit is not a disagreement about lines.

    The prompt has no key that means "keep it" or "let it go", so offering `[l]`
    and `[r]` would be inventing an answer to a question nobody asked. It is
    reported and the merge is undone.
    """

    def test_it_is_reported_rather_than_guessed_at(
        self, one_side_deleted: support.TwoMachines
    ) -> None:
        done = one_side_deleted.second.run("sync", "--ours")
        assert done.returncode == 2, done.stdout + done.stderr
        assert MANAGED in done.stderr
        assert "removed or replaced it" in done.stderr

    def test_the_repository_is_left_exactly_as_it_was(
        self, one_side_deleted: support.TwoMachines
    ) -> None:
        was = one_side_deleted.second.git("rev-parse", "HEAD")
        assert one_side_deleted.second.call("sync", "--ours") == 2
        assert gitrepo.unfinished(one_side_deleted.second.repo) is None
        assert one_side_deleted.second.git("status", "--porcelain") == ""
        assert one_side_deleted.second.git("rev-parse", "HEAD") == was


@pytest.fixture
def shared_ancestor(two_machines: support.TwoMachines) -> support.TwoMachines:
    """Both machines committed over one version of `SHARED`."""
    box = two_machines
    box.first.write(MANAGED, SHARED)
    assert box.first.call("add", str(box.first.home / MANAGED)) == 0
    assert box.first.call("sync") == 0
    assert box.second.call("init", str(box.remote)) == 0
    # Both hold the same committed version. `machine-b` changes the first
    # line *and* line 20; `machine-a` changes only the first. So they overlap
    # in one place and `machine-b` is alone in the other.
    box.second.write(MANAGED, SHARED.replace("l0\n", "B-FIRST\n").replace("l20\n", "B-ONLY\n"))
    box.first.write(MANAGED, SHARED.replace("l0\n", "A-FIRST\n"))
    assert box.second.call("add", str(box.second.home / MANAGED)) == 0
    assert box.first.call("add", str(box.first.home / MANAGED)) == 0
    assert box.first.call("sync") == 0
    return box


@pytest.mark.usefixtures("shared_ancestor")
class TestWhenTheTwoCommitsShareAnAncestor:
    """A conflict with a **stage 1**, which every fixture above lacks.

    `TwoCommits` has both machines `add` the file independently, so the two
    commits have no common version of it: git records stages 2 and 3 and no base.
    Mutating `sync.reconcile` to pass `base = None` therefore left the whole
    suite green.

    Finding the fixture that *does* see it took three attempts, and the two that
    failed are worth recording. Adjacent edits to a three-line file are one hunk,
    so git conflicts and the base changes nothing. Non-adjacent edits to a short
    file merge cleanly at the tree level, so there is no conflict to settle at
    all. What discriminates is **one overlapping edit and one that only one side
    made**: with the base, the one-sided edit merges silently; without it, every
    difference is a disagreement and it becomes a second conflict.

    Measured on this fixture -- 1 conflict against 2, and `l20` gone against
    `l20` kept -- which is what the assertions below are.
    """

    def test_the_fixture_really_has_a_merge_base(
        self, shared_ancestor: support.TwoMachines
    ) -> None:
        """The precondition, and the whole reason this class exists."""
        shared_ancestor.second.git("fetch", "origin")
        assert support.git_merged(shared_ancestor.second.repo, shared_ancestor.second.env) != 0
        stages = gitrepo.conflicted(shared_ancestor.second.repo)
        assert gitrepo.BASE in stages.get(MANAGED, {}), "no stage 1: there is no base"
        assert (
            gitrepo.version(shared_ancestor.second.repo, gitrepo.BASE, MANAGED) == SHARED.encode()
        )
        support.git_aborted(shared_ancestor.second.repo, shared_ancestor.second.env)

    def test_the_base_settles_the_edit_only_one_side_made(
        self, shared_ancestor: support.TwoMachines
    ) -> None:
        """`[b]` keeps both versions of what the two disagree about -- and the
        base is what says they do not disagree about line 20.

        Without it that line is a second conflict and the union keeps `l20`
        alongside `B-ONLY`, which is a line the user deleted coming back. That
        absence is the assertion; `B-ONLY` being present is only the precondition
        for it meaning anything.
        """
        assert shared_ancestor.second.call("sync", keys="b") == 0
        settled = shared_ancestor.second.read(MANAGED)
        assert "B-ONLY" in settled
        assert "l20" not in settled, "the base was not used: a one-sided edit came back"
        assert "A-FIRST" in settled
        assert "B-FIRST" in settled


@pytest.fixture
def asymmetric_modes(two_machines: support.TwoMachines) -> support.TwoMachines:
    """`machine-b` commits the file executable and `machine-a` commits it plain."""
    box = two_machines
    box.second.write(MANAGED, FROM_B)
    (box.second.home / MANAGED).chmod(0o755)
    box.first.write(MANAGED, FROM_A)
    (box.first.home / MANAGED).chmod(0o644)
    assert box.second.call("init", str(box.remote)) == 0
    assert box.second.call("add", str(box.second.home / MANAGED)) == 0
    assert box.first.call("add", str(box.first.home / MANAGED)) == 0
    assert box.first.call("sync") == 0
    return box


@pytest.mark.usefixtures("asymmetric_modes")
class TestTheExecutableBitComesFromTheIndex:
    """Asymmetric modes, because equal ones cannot tell the index from disk.

    `held`'s docstring claims the mode is read from the index rather than from
    the working tree, which during a conflict holds git's marked-up merge. With
    both sides `755` the working tree is `755` too, so mutating `held` to
    `os.stat` the path left the suite green. Here `machine-b` commits it
    executable and `machine-a` commits it plain: the working tree is still `755`,
    so a `--theirs` run can only come out non-executable by reading stage 3.
    """

    def test_the_two_sides_really_disagree_about_the_bit(
        self, asymmetric_modes: support.TwoMachines
    ) -> None:
        """Otherwise everything below holds for a fixture that set one mode."""
        asymmetric_modes.second.git("fetch", "origin")
        assert support.git_merged(asymmetric_modes.second.repo, asymmetric_modes.second.env) != 0
        stages = gitrepo.conflicted(asymmetric_modes.second.repo)[MANAGED]
        assert stages[gitrepo.OURS] == 0o100755
        assert stages[gitrepo.THEIRS] == 0o100644
        support.git_aborted(asymmetric_modes.second.repo, asymmetric_modes.second.env)

    def test_keeping_the_repositorys_side_takes_its_mode_too(
        self, asymmetric_modes: support.TwoMachines
    ) -> None:
        """The working tree is `755` throughout, so this can only pass by
        reading stage 3's mode out of the index."""
        assert asymmetric_modes.second.call("sync", "--theirs") == 0
        assert not (asymmetric_modes.second.home / MANAGED).stat().st_mode & 0o111

    def test_keeping_this_machines_side_keeps_its_own(
        self, asymmetric_modes: support.TwoMachines
    ) -> None:
        assert asymmetric_modes.second.call("sync", "--ours") == 0
        assert (asymmetric_modes.second.home / MANAGED).stat().st_mode & 0o111


@pytest.fixture
def committed_links(two_machines: support.TwoMachines) -> Victimised:
    """Both machines commit `LINK` by hand, pointing at different targets."""
    victim = two_machines.tmp / "victim"
    victim.write_text("SECRET-ORIGINAL\n", encoding="utf-8")
    box = Victimised(**vars(two_machines), victim=victim)
    assert box.second.call("init", str(box.remote)) == 0
    for machine, target in ((box.second, victim), (box.first, box.tmp / "elsewhere")):
        link = machine.repo / LINK
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
        machine.git("add", "-A")
        machine.git("commit", "-m", "a symlink, committed by hand")
    assert box.first.call("sync") == 0
    return box


@pytest.mark.usefixtures("committed_links")
class TestWhatIsNotAFileOnBothSides:
    """A committed symlink, which `copies.write` would write **through**.

    `manifest` refuses a symlink at `add` time, so one only reaches the
    repository by hand, by another tool, or from a hostile remote. But a path out
    of the *index* has had no such check, and `copies.write` does
    `write_bytes` + `chmod`, both of which follow a link -- so settling a
    conflict over one destroyed a file outside the repository entirely.
    Reproduced before the fix: two branches committing `link -> ../victim/target`
    and `link -> /dev/null`, settled with `--ours`, overwrote `victim/target`.

    `--ours` reaches it with no prompt at all, which is what makes it worth a
    test rather than a comment.
    """

    def test_the_fixture_really_committed_a_symlink(self, committed_links: Victimised) -> None:
        """Otherwise the refusal below is a claim about nothing."""
        assert "120000" in committed_links.second.git("ls-files", "-s", LINK)

    def test_it_is_refused_rather_than_written_through(self, committed_links: Victimised) -> None:
        done = committed_links.second.run("sync", "--ours")
        assert done.returncode == 2, done.stdout + done.stderr
        assert LINK in done.stderr

    def test_the_file_the_link_pointed_at_is_untouched(self, committed_links: Victimised) -> None:
        """The assertion that matters, and it is about a file *outside* the
        repository -- which is why "it was refused" is not enough on its own."""
        committed_links.second.call("sync", "--ours")
        assert committed_links.victim.read_text(encoding="utf-8") == "SECRET-ORIGINAL\n"


@pytest.mark.usefixtures("two_commits")
class TestWhenSettlingIsInterrupted:
    """Everything in `reconcile` runs inside an unfinished merge.

    So a `TupferlError`, an `OSError`, or a Ctrl-C at the prompt can arrive with
    some files already settled and staged. Without the `finally` that aborts,
    each leaves `MERGE_HEAD` behind -- and `sync.main`'s own `unfinished` check
    then refuses **every** subsequent run until the user does git surgery. One
    interrupted prompt, and the machine cannot sync at all.
    """

    def test_an_error_at_the_prompt_leaves_no_half_merged_tree(
        self, two_commits: TwoCommits
    ) -> None:
        was = two_commits.second.git("rev-parse", "HEAD")
        with raising(TupferlError):
            assert two_commits.second.call("sync") == 2
        assert gitrepo.unfinished(two_commits.second.repo) is None
        assert two_commits.second.git("status", "--porcelain") == ""
        assert two_commits.second.git("rev-parse", "HEAD") == was

    def test_a_keyboard_interrupt_leaves_no_half_merged_tree(self, two_commits: TwoCommits) -> None:
        """`BaseException`, not `Exception`: Ctrl-C at a prompt is the most
        ordinary way this happens and it is not an `Exception`."""
        with raising(KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
            two_commits.second.call("sync")
        assert gitrepo.unfinished(two_commits.second.repo) is None
        assert two_commits.second.git("status", "--porcelain") == ""

    def test_the_next_sync_still_works_afterwards(self, two_commits: TwoCommits) -> None:
        """The whole point: an interrupted prompt must not cost the machine its
        ability to sync."""
        with raising(TupferlError):
            assert two_commits.second.call("sync") == 2
        assert two_commits.second.call("sync", "--theirs") == 0
        assert two_commits.second.read(MANAGED) == FROM_A


@pytest.mark.usefixtures("two_commits")
class TestWhenTheSettledFilesCannotBeStaged:
    """`reconcile` settles every side and then `git add` fails.

    `breaking` above is how, and says why it is a patch rather than a git that
    refuses.
    """

    def test_it_says_so_and_names_a_next_step(self, two_commits: TwoCommits) -> None:
        with breaking(two_commits):
            status, said = two_commits.second.say("sync", "--theirs")
        assert status == 2, said
        assert "could not stage the settled files" in said
        assert "tupferl doctor" in said

    def test_the_message_is_true_about_the_merge_being_undone(
        self, two_commits: TwoCommits
    ) -> None:
        """The substantive half. That sentence is a claim about what happened to
        the repository, and it holds only because `integrate`'s `finally` aborts
        whatever raises out of `reconcile` -- so it is worth checking rather
        than trusting the comment that says so.
        """
        was = two_commits.second.git("rev-parse", "HEAD")
        with breaking(two_commits):
            status, said = two_commits.second.say("sync", "--theirs")
        assert status == 2, said
        assert "the merge was undone" in said
        assert gitrepo.unfinished(two_commits.second.repo) is None
        assert two_commits.second.git("status", "--porcelain") == ""
        assert two_commits.second.git("rev-parse", "HEAD") == was


@pytest.fixture
def twins(two_machines: support.TwoMachines) -> support.TwoMachines:
    """Two machines that both call themselves `TWIN`, synced once beforehand."""
    box = two_machines
    for machine in (box.first, box.second):
        machine.env["TUPFERL_HOSTNAME"] = TWIN
    # A sync under the shared hostname *before* anything diverges, so a
    # snapshot exists at `state/laptop/`. Without it the first collision is
    # a conflict with no merge base rather than the one the issue describes,
    # and the fixture fails in setup for a reason unrelated to it.
    assert box.first.call("sync") == 0
    assert box.second.call("init", str(box.remote)) == 0
    assert box.second.call("sync") == 0
    return box


@pytest.mark.usefixtures("twins")
class TestWhatThisMachineWillNotMerge:
    """#15: `reconcile` walks git's index, not `manifest.managed`.

    So a path tupferl keeps for *itself* reaches the dotfile prompt. The one
    that matters is a sync snapshot: settling it writes a **merge of two
    snapshots**, a state neither machine was ever in, and every later
    three-version comparison is then against a version that never existed.

    **Two machines sharing a hostname is what it takes**, because snapshots live
    under `.tupferl/state/<hostname>/`. That is not exotic -- two laptops both
    called `laptop`, or `TUPFERL_HOSTNAME` set the same in two places -- and
    `paths.check_hostname` neither does nor can prevent it.
    """

    def test_a_snapshot_is_never_offered_at_the_prompt(self, twins: support.TwoMachines) -> None:
        """`--ours` answers every conflict the prompt is given, so a sync that
        exits 2 is one where something never reached it.

        **This is also the class's precondition**, and it carries that on its own
        rather than through a separate test: the snapshot's path can only appear
        in this message by way of `left`, and it can only reach `left` because
        git reported it unmerged and `mergeable` refused it. A fixture where the
        conflict never happened produces a clean sync and exit 0.

        It says that here because the separate precondition it replaced was
        *wrong*, and only CI could see it: that test drove `gitrepo.fetch` and
        `gitrepo.merge` by hand instead of going through `sync`, and on the
        runner's git 2.55 the hand-rolled pair left nothing unmerged while the
        real path -- these three tests -- conflicted exactly as expected. The
        mechanism for that difference is not established and is not guessed at
        here; the lesson that is established is CLAUDE.md §2's "prefer driving
        the real thing", with a version of the real thing to point at.
        """
        collide(twins, ".bashrc")
        status, said = twins.second.say("sync", "--ours")
        assert status == 2, said
        assert f"{paths.META}/state/{TWIN}/.bashrc" in said
        assert "not a dotfile this machine merges" in said

    def test_the_ordinary_dotfile_beside_it_is_not_what_stopped_the_sync(
        self, twins: support.TwoMachines
    ) -> None:
        """`.bashrc` collides too, and it *is* mergeable -- so the refusal has to
        be about the snapshot rather than about the collision in general.

        Without this, the class is equally satisfied by a `reconcile` that
        refused every conflict it saw.
        """
        collide(twins, ".bashrc")
        status, said = twins.second.say("sync", "--ours")
        assert status == 2, said
        refused = said.split("disagree about", 1)[1].split(" in a way", 1)[0]
        assert f"{paths.META}/state/{TWIN}/.bashrc" in refused
        assert " .bashrc" not in refused

    def test_the_merge_is_undone_rather_than_half_settled(self, twins: support.TwoMachines) -> None:
        """The refusal has to leave the repository where the next run can start.

        A path added to `left` makes `integrate` raise, and its `finally` aborts
        -- so this asserts the guarantee rather than the code path: no
        `MERGE_HEAD`, a clean tree, and `HEAD` where it was.
        """
        collide(twins, ".bashrc")
        # After `collide`, not before: `add` commits, so a `HEAD` read earlier
        # is a different commit for a reason that has nothing to do with the
        # refusal. The first version of this test read it first and failed.
        was = twins.second.git("rev-parse", "HEAD")
        assert twins.second.call("sync", "--ours") == 2
        assert gitrepo.unfinished(twins.second.repo) is None
        assert twins.second.git("status", "--porcelain") == ""
        assert twins.second.git("rev-parse", "HEAD") == was

    def test_the_snapshot_on_disk_is_still_one_machine_s_own(
        self, twins: support.TwoMachines
    ) -> None:
        """What the whole issue is about. The snapshot must remain a state this
        machine was really in -- not a merge of two of them, which is what
        settling it at the prompt produced."""
        collide(twins, ".bashrc")
        assert twins.second.call("sync", "--ours") == 2
        snapshot = twins.second.repo / paths.META / "state" / TWIN / ".bashrc"
        assert snapshot.read_text() == "FROM-B\ntwo\nthree\n"
        assert "FROM-A" not in snapshot.read_text()


@pytest.mark.usefixtures("two_commits")
class TestSettlingWithTheEditor:
    """`[e]` over a commit conflict, which no other test here covers."""

    def test_the_editor_settles_it_like_any_other_conflict(self, two_commits: TwoCommits) -> None:
        where = support.fake_editor(
            two_commits.tmp / "fake-editor", 'printf "SETTLED-BY-HAND\\n" > "$1"'
        )
        two_commits.second.env["EDITOR"] = str(where)
        assert two_commits.second.call("sync", keys="e") == 0
        assert two_commits.second.read(MANAGED) == "SETTLED-BY-HAND\n"
        assert gitrepo.unfinished(two_commits.second.repo) is None


@pytest.mark.usefixtures("two_commits")
class TestWhenTheMergeCannotBeConcluded:
    """Everything settled, and then the commit refused.

    A `pre-commit` hook is the ordinary way -- and `integrate` has to put the
    repository back rather than leave a fully-settled index nobody committed,
    which the next run would find as an unfinished merge and refuse.
    """

    def test_the_merge_is_undone_and_the_reason_reaches_the_user(
        self, two_commits: TwoCommits
    ) -> None:
        was = two_commits.second.git("rev-parse", "HEAD")
        support.break_commits(two_commits.second.home)
        done = two_commits.second.run("sync", "--ours")
        assert done.returncode == 2, done.stdout + done.stderr
        assert "could not commit" in done.stderr
        assert "the merge was undone" in done.stderr
        assert gitrepo.unfinished(two_commits.second.repo) is None
        assert two_commits.second.git("rev-parse", "HEAD") == was


@dataclass(frozen=True)
class Merging(support.Sandbox):
    """A repository left mid-merge, so `undone` has something to undo."""

    repo: Path

    def commit(self, text: bytes) -> None:
        (self.repo / ".bashrc").write_bytes(text)
        support.git(["add", "-A"], cwd=self.repo, env=self.env)
        support.git(["commit", "-m", "x"], cwd=self.repo, env=self.env)


@pytest.fixture
def merging(sandbox: support.Sandbox) -> Merging:
    box = Merging(**vars(sandbox), repo=support.make_repo(sandbox.home / "r", sandbox.env))
    box.commit(b"base\n")
    support.git(["branch", "other"], cwd=box.repo, env=box.env)
    box.commit(b"ours\n")
    support.git(["checkout", "-q", "other"], cwd=box.repo, env=box.env)
    box.commit(b"theirs\n")
    support.git(["checkout", "-q", support.BRANCH], cwd=box.repo, env=box.env)
    # Left to fail: that is what leaves MERGE_HEAD behind.
    gitrepo.merge(box.repo, "other")
    return box


@pytest.mark.usefixtures("merging")
class TestUndoneUndoesTheMergeItself:
    """`sync.undone`, called directly, because through `sync` it cannot be seen.

    Its docstring makes a promise the message repeats to the user -- "the merge
    was undone, so nothing is half-done" -- and says why it lives in one function
    rather than at each of the three raise sites: *a fourth failure added later
    cannot forget it*.

    Both sites that reach it today are inside `integrate`, whose `finally` aborts
    as well, so dropping the call here changes nothing anybody can observe from
    outside: `TestWhenTheMergeCannotBeConcluded` passes either way, and a
    mutation sweep found exactly that. Testing the guarantee where it is made,
    rather than where it currently happens to be doubled, is what makes the
    fourth site safe -- which is the only reason the line exists.
    """

    def test_the_fixture_really_left_a_merge_in_progress(self, merging: Merging) -> None:
        """The assertion below is vacuous without it: a repository that was never
        mid-merge reports nothing unfinished whatever `undone` does."""
        assert gitrepo.unfinished(merging.repo) is not None

    def test_it_raises_and_leaves_no_merge_behind(self, merging: Merging) -> None:
        with pytest.raises(TupferlError) as raised:
            sync.undone(merging.repo, "something went wrong")
        assert "the merge was undone" in str(raised.value)
        assert gitrepo.unfinished(merging.repo) is None


@pytest.fixture
def two_refusals(two_machines: support.TwoMachines) -> support.TwoMachines:
    """Two files, each a delete against an edit, so both come back refused."""
    box = two_machines
    assert box.second.call("init", str(box.remote)) == 0
    for name in (".zshrc", ".inputrc"):
        box.first.write(name, "shared\n")
        assert box.first.call("add", str(box.first.home / name)) == 0
    assert box.first.call("sync") == 0
    assert box.second.call("sync") == 0
    for name in (".zshrc", ".inputrc"):
        box.second.write(name, "edited here\n")
        assert box.second.call("add", str(box.second.home / name)) == 0
        assert box.first.call("remove", str(box.first.home / name)) == 0
    assert box.first.call("sync") == 0
    return box


@pytest.mark.usefixtures("two_refusals")
class TestWhenSeveralFilesCannotBeSettled:
    """The names are listed in a stable order.

    `sorted` on the walk, so two machines and two runs produce the same sentence.
    Nothing could tell before: with one refused file every ordering is the same
    ordering, which is the fixture-too-weak shape.
    """

    def test_both_are_named_in_sorted_order(self, two_refusals: support.TwoMachines) -> None:
        done = two_refusals.second.run("sync", "--ours")
        assert done.returncode == 2, done.stdout + done.stderr
        assert ".inputrc, .zshrc" in done.stderr


@pytest.fixture
def type_changed(two_machines: support.TwoMachines) -> Victimised:
    """`machine-b` keeps `SWAPPED` a file; `machine-a` replaces it with a link."""
    victim = two_machines.tmp / "victim"
    victim.write_text("SECRET-ORIGINAL\n", encoding="utf-8")
    box = Victimised(**vars(two_machines), victim=victim)
    assert box.second.call("init", str(box.remote)) == 0
    for machine in (box.first, box.second):
        (machine.repo / SWAPPED).parent.mkdir(parents=True, exist_ok=True)
    (box.second.repo / SWAPPED).write_text("still a file\n", encoding="utf-8")
    (box.first.repo / SWAPPED).symlink_to(victim)
    for machine in (box.second, box.first):
        machine.git("add", "-A")
        machine.git("commit", "-m", "by hand")
    # **Exit 1, and that is correct.** `manifest` walks the repository, so
    # `machine-a`'s own symlink is picked up as a managed item -- and
    # `copies.read` uses `lstat`, so `settle` refuses it and the run reports
    # a file it would not touch. It still commits and pushes, which is all
    # this fixture needs. (`committed_links` above exits 0 instead, because
    # `is_file()` is false for a link to nothing, so it is never managed at all.)
    assert box.first.call("sync") == 1
    return box


@pytest.mark.usefixtures("type_changed")
class TestWhenOneSideReplacedTheFileWithASymlink:
    """A type change, which git does not record the way I expected.

    I wrote this to give `reconcile` a path with *mixed* stage kinds -- a regular
    file on one side, a symlink on the other -- so that `any(mode not in
    REGULAR)` and `all(...)` would differ. **git does not produce one.** Measured:

        {'.config/thing':       {3: 0o120000},
         '.config/thing~HEAD':  {2: 0o100644}}

    It splits the type change into two paths, each carrying a single stage, and
    each is therefore refused by the *missing-stage* branch rather than by the
    mode check. So the mutant this was written to kill survives, and it is named
    in the PR rather than pretended about.

    What the fixture does prove is worth keeping: a type change is refused, and
    the file the link pointed at -- outside the repository -- is untouched.
    """

    def test_git_splits_a_type_change_into_two_single_staged_paths(
        self, type_changed: Victimised
    ) -> None:
        """Recorded as a test because it is the reason the mode check cannot be
        exercised through a type change, and because a future git that stopped
        doing it would change which branch refuses this."""
        type_changed.second.git("fetch", "origin")
        assert support.git_merged(type_changed.second.repo, type_changed.second.env) != 0
        stages = gitrepo.conflicted(type_changed.second.repo)
        assert stages[SWAPPED] == {gitrepo.THEIRS: 0o120000}
        assert stages[f"{SWAPPED}~HEAD"] == {gitrepo.OURS: 0o100644}
        support.git_aborted(type_changed.second.repo, type_changed.second.env)

    def test_it_is_refused_and_the_link_target_is_untouched(self, type_changed: Victimised) -> None:
        assert type_changed.second.call("sync", "--ours") == 2
        assert type_changed.victim.read_text(encoding="utf-8") == "SECRET-ORIGINAL\n"


@pytest.fixture
def mode_only(two_machines: support.TwoMachines) -> support.TwoMachines:
    """The same bytes committed on both sides, with different modes."""
    box = two_machines
    assert box.second.call("init", str(box.remote)) == 0
    for machine, mode in ((box.second, 0o755), (box.first, 0o644)):
        machine.write(MANAGED, "identical\n")
        (machine.home / MANAGED).chmod(mode)
        assert machine.call("add", str(machine.home / MANAGED)) == 0
    assert box.first.call("sync") == 0
    return box


@pytest.mark.usefixtures("mode_only")
class TestAConflictAboutNothingButTheMode:
    """Same bytes, different modes: git conflicts and `merge-file` does not.

    The one case where the tree-level merge fails and the file-level one
    succeeds, so `resolve` returns settled bytes with no `Sides` and **nobody is
    asked**. Nothing reached it before, so `if outcome.sides is not None` could
    be made unconditional and the suite stayed green.
    """

    def test_the_two_sides_differ_only_in_the_mode(self, mode_only: support.TwoMachines) -> None:
        mode_only.second.git("fetch", "origin")
        assert support.git_merged(mode_only.second.repo, mode_only.second.env) != 0
        stages = gitrepo.conflicted(mode_only.second.repo)[MANAGED]
        assert stages[gitrepo.THEIRS] != stages[gitrepo.OURS]
        assert gitrepo.version(mode_only.second.repo, gitrepo.THEIRS, MANAGED) == gitrepo.version(
            mode_only.second.repo, gitrepo.OURS, MANAGED
        )
        support.git_aborted(mode_only.second.repo, mode_only.second.env)

    def test_it_settles_with_nobody_asked(self, mode_only: support.TwoMachines) -> None:
        """`--no-input` is enough, which is the assertion: a run that had to ask
        would report the conflict and exit 1."""
        assert mode_only.second.call("sync", "--no-input") == 0
        assert mode_only.second.read(MANAGED) == "identical\n"
        assert gitrepo.unfinished(mode_only.second.repo) is None
