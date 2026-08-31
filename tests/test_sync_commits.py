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
import unittest
from unittest import mock

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


class TwoCommits(support.TwoMachinesCase):
    """`machine-b` holds a commit the remote has never seen, and they disagree.

    The order matters and is the issue's: `machine-b` commits *first* (through
    `add`, which does not push), and only then does `machine-a` push. Reversed,
    `machine-b` would fetch the change before committing over it and there would
    be no conflict at all.
    """

    def setUp(self) -> None:
        super().setUp()
        assert self.second.call("init", str(self.remote)) == 0
        self.diverge_by_committing(FROM_B, FROM_A)

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


class TestTheFixtureReallyProducesACommitConflict(TwoCommits):
    """Stated first, because every test below is vacuous without it.

    If `machine-b`'s sync merged cleanly, or if the conflict were the ordinary
    `$HOME`-against-repository kind, these would all pass against code that never
    runs `reconcile`.
    """

    def test_the_second_machine_holds_an_unpushed_commit(self) -> None:
        """What `add` leaves behind, and the reason the issue calls this easy to
        reach."""
        assert self.second.git("rev-parse", "origin/main") != self.second.git("rev-parse", "HEAD")

    def test_git_cannot_merge_the_two_branches_on_its_own(self) -> None:
        """The precondition, asserted by asking git rather than by assuming.

        `--no-commit` so the check leaves nothing behind; the sync under test
        does its own merge from a clean tree.
        """
        self.second.git("fetch", "origin")
        assert support.git_merged(self.second.repo, self.second.env) != 0, (
            "git merged cleanly, so there is no conflict to settle"
        )
        assert MANAGED in gitrepo.unmerged(self.second.repo)
        support.git_aborted(self.second.repo, self.second.env)


class TestTheFlags(TwoCommits):
    def test_ours_keeps_this_machines_commit(self) -> None:
        self.settle("--ours")
        self.concluded()
        self.everywhere(FROM_B)

    def test_theirs_keeps_the_repositorys_commit(self) -> None:
        """The other side, and the test that would fail if stage 2 and stage 3
        were read the wrong way round -- `--ours` alone cannot tell."""
        self.settle("--theirs")
        self.concluded()
        self.everywhere(FROM_A)

    def test_no_input_leaves_the_repository_exactly_as_it_was(self) -> None:
        """Nobody is there to answer, so nothing is decided -- and the merge is
        undone rather than left half-done, because a half-merged tree makes the
        next run refuse to start."""
        was = self.second.git("rev-parse", "HEAD")
        assert self.second.call("sync", "--no-input") == 2
        assert gitrepo.unfinished(self.second.repo) is None
        assert self.second.git("status", "--porcelain") == ""
        assert self.second.git("rev-parse", "HEAD") == was

    def test_the_next_sync_still_works_after_one_was_left_unsettled(self) -> None:
        """The point of aborting. Without it the second run finds an unfinished
        merge and refuses, turning one conflict into a machine that cannot sync
        at all."""
        assert self.second.call("sync", "--no-input") == 2
        self.settle("--theirs")
        self.everywhere(FROM_A)


class TestTheKeys(TwoCommits):
    """The same three answers, typed at a real terminal rather than passed."""

    def test_l_keeps_this_machines_commit(self) -> None:
        self.settle(keys="l")
        self.concluded()
        self.everywhere(FROM_B)

    def test_r_keeps_the_repositorys_commit(self) -> None:
        self.settle(keys="r")
        self.concluded()
        self.everywhere(FROM_A)

    def test_b_keeps_both(self) -> None:
        """`[b]` is a union merge of the two *commits*, which is the same
        operation on different inputs -- and the one answer that proves the
        settled bytes are neither side rather than a copy of one."""
        self.settle(keys="b")
        self.concluded()
        self.everywhere(BOTH_KEPT)

    def test_s_leaves_the_repository_exactly_as_it_was(self) -> None:
        """`--porcelain` as well as the commit, because a file settled before the
        skipped one is written into the working tree, and only the abort takes it
        back out. Without this the test cannot see it left behind."""
        was = self.second.git("rev-parse", "HEAD")
        assert self.second.call("sync", keys="s") == 2
        assert gitrepo.unfinished(self.second.repo) is None
        assert self.second.git("status", "--porcelain") == ""
        assert self.second.git("rev-parse", "HEAD") == was


class TestTheExecutableBit(TwoCommits):
    """Plan §5 asks for the one mode bit to travel, and during a conflict it
    lives in the index rather than in the working tree -- which holds git's
    marked-up merge, whose bits say nothing about what either side recorded."""

    def setUp(self) -> None:
        support.TwoMachinesCase.setUp(self)
        assert self.second.call("init", str(self.remote)) == 0
        self.diverge_by_committing(FROM_B, FROM_A, executable=True)

    def test_the_fixture_really_committed_an_executable_file(self) -> None:
        """Otherwise the assertion below holds for a fixture that never set the
        bit, which is a negative claim with no precondition."""
        assert "100755" in self.second.git("ls-files", "-s", MANAGED)

    def test_a_settled_file_is_still_executable(self) -> None:
        self.settle("--ours")
        assert (self.second.home / MANAGED).stat().st_mode & 0o111
        assert "100755" in self.second.git("ls-files", "-s", MANAGED)


class TestAFileOnlyOneSideStillHas(TwoCommits):
    """A delete against an edit is not a disagreement about lines.

    The prompt has no key that means "keep it" or "let it go", so offering `[l]`
    and `[r]` would be inventing an answer to a question nobody asked. It is
    reported and the merge is undone.
    """

    def setUp(self) -> None:
        support.TwoMachinesCase.setUp(self)
        # **Both machines agree about the file first.** Diverging by content and
        # by existence at once is two conflicts, and the first one settles before
        # this one is ever reached -- which is what the first attempt at this
        # fixture did, and it failed in `setUp` rather than in a test.
        self.first.write(MANAGED, FROM_A)
        assert self.first.call("add", str(self.first.home / MANAGED)) == 0
        assert self.first.call("sync") == 0
        assert self.second.call("init", str(self.remote)) == 0
        assert self.second.read(MANAGED) == FROM_A

        # Now they disagree about whether it should exist at all: `machine-b`
        # edits and commits without pushing, `machine-a` stops managing it.
        self.second.write(MANAGED, FROM_B)
        assert self.second.call("add", str(self.second.home / MANAGED)) == 0
        assert self.first.call("remove", str(self.first.home / MANAGED)) == 0
        assert self.first.call("sync") == 0

    def test_it_is_reported_rather_than_guessed_at(self) -> None:
        done = self.second.run("sync", "--ours")
        assert done.returncode == 2, done.stdout + done.stderr
        assert MANAGED in done.stderr
        assert "removed or replaced it" in done.stderr

    def test_the_repository_is_left_exactly_as_it_was(self) -> None:
        was = self.second.git("rev-parse", "HEAD")
        assert self.second.call("sync", "--ours") == 2
        assert gitrepo.unfinished(self.second.repo) is None
        assert self.second.git("status", "--porcelain") == ""
        assert self.second.git("rev-parse", "HEAD") == was


class TestWhenTheTwoCommitsShareAnAncestor(support.TwoMachinesCase):
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

    #: Long enough that two edits are separate hunks. `l20` is the line only
    #: `machine-b` touches, and whether it survives is the whole assertion.
    SHARED = "\n".join(f"l{number}" for number in range(30)) + "\n"

    def setUp(self) -> None:
        super().setUp()
        self.first.write(MANAGED, self.SHARED)
        assert self.first.call("add", str(self.first.home / MANAGED)) == 0
        assert self.first.call("sync") == 0
        assert self.second.call("init", str(self.remote)) == 0
        # Both hold the same committed version. `machine-b` changes the first
        # line *and* line 20; `machine-a` changes only the first. So they overlap
        # in one place and `machine-b` is alone in the other.
        self.second.write(
            MANAGED, self.SHARED.replace("l0\n", "B-FIRST\n").replace("l20\n", "B-ONLY\n")
        )
        self.first.write(MANAGED, self.SHARED.replace("l0\n", "A-FIRST\n"))
        assert self.second.call("add", str(self.second.home / MANAGED)) == 0
        assert self.first.call("add", str(self.first.home / MANAGED)) == 0
        assert self.first.call("sync") == 0

    def test_the_fixture_really_has_a_merge_base(self) -> None:
        """The precondition, and the whole reason this class exists."""
        self.second.git("fetch", "origin")
        assert support.git_merged(self.second.repo, self.second.env) != 0
        stages = gitrepo.conflicted(self.second.repo)
        assert gitrepo.BASE in stages.get(MANAGED, {}), "no stage 1: there is no base"
        assert gitrepo.version(self.second.repo, gitrepo.BASE, MANAGED) == self.SHARED.encode()
        support.git_aborted(self.second.repo, self.second.env)

    def test_the_base_settles_the_edit_only_one_side_made(self) -> None:
        """`[b]` keeps both versions of what the two disagree about -- and the
        base is what says they do not disagree about line 20.

        Without it that line is a second conflict and the union keeps `l20`
        alongside `B-ONLY`, which is a line the user deleted coming back. That
        absence is the assertion; `B-ONLY` being present is only the precondition
        for it meaning anything.
        """
        assert self.second.call("sync", keys="b") == 0
        settled = self.second.read(MANAGED)
        assert "B-ONLY" in settled
        assert "l20" not in settled, "the base was not used: a one-sided edit came back"
        assert "A-FIRST" in settled
        assert "B-FIRST" in settled


class TestTheExecutableBitComesFromTheIndex(support.TwoMachinesCase):
    """Asymmetric modes, because equal ones cannot tell the index from disk.

    `held`'s docstring claims the mode is read from the index rather than from
    the working tree, which during a conflict holds git's marked-up merge. With
    both sides `755` the working tree is `755` too, so mutating `held` to
    `os.stat` the path left the suite green. Here `machine-b` commits it
    executable and `machine-a` commits it plain: the working tree is still `755`,
    so a `--theirs` run can only come out non-executable by reading stage 3.
    """

    def setUp(self) -> None:
        super().setUp()
        self.second.write(MANAGED, FROM_B)
        (self.second.home / MANAGED).chmod(0o755)
        self.first.write(MANAGED, FROM_A)
        (self.first.home / MANAGED).chmod(0o644)
        assert self.second.call("init", str(self.remote)) == 0
        assert self.second.call("add", str(self.second.home / MANAGED)) == 0
        assert self.first.call("add", str(self.first.home / MANAGED)) == 0
        assert self.first.call("sync") == 0

    def test_the_two_sides_really_disagree_about_the_bit(self) -> None:
        """Otherwise everything below holds for a fixture that set one mode."""
        self.second.git("fetch", "origin")
        assert support.git_merged(self.second.repo, self.second.env) != 0
        stages = gitrepo.conflicted(self.second.repo)[MANAGED]
        assert stages[gitrepo.OURS] == 0o100755
        assert stages[gitrepo.THEIRS] == 0o100644
        support.git_aborted(self.second.repo, self.second.env)

    def test_keeping_the_repositorys_side_takes_its_mode_too(self) -> None:
        """The working tree is `755` throughout, so this can only pass by
        reading stage 3's mode out of the index."""
        assert self.second.call("sync", "--theirs") == 0
        assert not (self.second.home / MANAGED).stat().st_mode & 0o111

    def test_keeping_this_machines_side_keeps_its_own(self) -> None:
        assert self.second.call("sync", "--ours") == 0
        assert (self.second.home / MANAGED).stat().st_mode & 0o111


class TestWhatIsNotAFileOnBothSides(support.TwoMachinesCase):
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

    LINK = ".config/link"

    def setUp(self) -> None:
        super().setUp()
        assert self.second.call("init", str(self.remote)) == 0
        self.victim = self.tmp / "victim"
        self.victim.write_text("SECRET-ORIGINAL\n", encoding="utf-8")
        for machine, target in ((self.second, self.victim), (self.first, self.tmp / "elsewhere")):
            link = machine.repo / self.LINK
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
            machine.git("add", "-A")
            machine.git("commit", "-m", "a symlink, committed by hand")
        assert self.first.call("sync") == 0

    def test_the_fixture_really_committed_a_symlink(self) -> None:
        """Otherwise the refusal below is a claim about nothing."""
        assert "120000" in self.second.git("ls-files", "-s", self.LINK)

    def test_it_is_refused_rather_than_written_through(self) -> None:
        done = self.second.run("sync", "--ours")
        assert done.returncode == 2, done.stdout + done.stderr
        assert self.LINK in done.stderr

    def test_the_file_the_link_pointed_at_is_untouched(self) -> None:
        """The assertion that matters, and it is about a file *outside* the
        repository -- which is why "it was refused" is not enough on its own."""
        self.second.call("sync", "--ours")
        assert self.victim.read_text(encoding="utf-8") == "SECRET-ORIGINAL\n"


class TestWhenSettlingIsInterrupted(TwoCommits):
    """Everything in `reconcile` runs inside an unfinished merge.

    So a `TupferlError`, an `OSError`, or a Ctrl-C at the prompt can arrive with
    some files already settled and staged. Without the `finally` that aborts,
    each leaves `MERGE_HEAD` behind -- and `sync.main`'s own `unfinished` check
    then refuses **every** subsequent run until the user does git surgery. One
    interrupted prompt, and the machine cannot sync at all.
    """

    def raising(self, kind: type[BaseException]) -> None:
        """Make the settler raise, the way a broken `$EDITOR` or a Ctrl-C does."""

        def boom(sides: object) -> conflicts.Answer:
            raise kind("interrupted")

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(conflicts, "answering", lambda *a: boom))

    def test_an_error_at_the_prompt_leaves_no_half_merged_tree(self) -> None:
        was = self.second.git("rev-parse", "HEAD")
        self.raising(TupferlError)
        assert self.second.call("sync") == 2
        assert gitrepo.unfinished(self.second.repo) is None
        assert self.second.git("status", "--porcelain") == ""
        assert self.second.git("rev-parse", "HEAD") == was

    def test_a_keyboard_interrupt_leaves_no_half_merged_tree(self) -> None:
        """`BaseException`, not `Exception`: Ctrl-C at a prompt is the most
        ordinary way this happens and it is not an `Exception`."""
        self.raising(KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.second.call("sync")
        assert gitrepo.unfinished(self.second.repo) is None
        assert self.second.git("status", "--porcelain") == ""

    def test_the_next_sync_still_works_afterwards(self) -> None:
        """The whole point: an interrupted prompt must not cost the machine its
        ability to sync."""
        self.raising(TupferlError)
        assert self.second.call("sync") == 2
        self.stack.close()
        assert self.second.call("sync", "--theirs") == 0
        assert self.second.read(MANAGED) == FROM_A


class TestWhenTheSettledFilesCannotBeStaged(TwoCommits):
    """`reconcile` settles every side and then `git add` fails.

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

    def breaking(self) -> None:
        real = gitrepo.stage

        def refuse(repo: object, paths: list[object]) -> gitrepo.Result:
            if gitrepo.unfinished(self.second.repo) is not None:
                return gitrepo.Result(out="", err="fatal: could not add", code=128)
            return real(repo, paths)  # type: ignore[arg-type]

        patched = mock.patch.object(gitrepo, "stage", refuse)
        patched.start()
        self.addCleanup(patched.stop)

    def test_it_says_so_and_names_a_next_step(self) -> None:
        self.breaking()
        status, said = self.second.say("sync", "--theirs")
        assert status == 2, said
        assert "could not stage the settled files" in said
        assert "tupferl doctor" in said

    def test_the_message_is_true_about_the_merge_being_undone(self) -> None:
        """The substantive half. That sentence is a claim about what happened to
        the repository, and it holds only because `integrate`'s `finally` aborts
        whatever raises out of `reconcile` -- so it is worth checking rather
        than trusting the comment that says so.
        """
        was = self.second.git("rev-parse", "HEAD")
        self.breaking()
        status, said = self.second.say("sync", "--theirs")
        assert status == 2, said
        assert "the merge was undone" in said
        assert gitrepo.unfinished(self.second.repo) is None
        assert self.second.git("status", "--porcelain") == ""
        assert self.second.git("rev-parse", "HEAD") == was


class TestWhatThisMachineWillNotMerge(support.TwoMachinesCase):
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

    #: What both machines call themselves. The collision is the fixture.
    TWIN = "laptop"

    def setUp(self) -> None:
        super().setUp()
        for machine in (self.first, self.second):
            machine.env["TUPFERL_HOSTNAME"] = self.TWIN
        # A sync under the shared hostname *before* anything diverges, so a
        # snapshot exists at `state/laptop/`. Without it the first collision is
        # a conflict with no merge base rather than the one the issue describes,
        # and the fixture fails in `setUp` for a reason unrelated to it.
        assert self.first.call("sync") == 0
        assert self.second.call("init", str(self.remote)) == 0
        assert self.second.call("sync") == 0

    def collide(self, name: str) -> None:
        """Make both machines commit to `name` without either seeing the other.

        `add` commits and does not push, which is the ordinary way a machine
        gets a commit the remote has never seen -- the issue's own reproduction.
        """
        self.second.write(name, "FROM-B\ntwo\nthree\n")
        assert self.second.call("add", str(self.second.home / name)) == 0
        self.first.write(name, "FROM-A\ntwo\nthree\n")
        assert self.first.call("sync") == 0

    def test_a_snapshot_is_never_offered_at_the_prompt(self) -> None:
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
        self.collide(".bashrc")
        status, said = self.second.say("sync", "--ours")
        assert status == 2, said
        assert f"{paths.META}/state/{self.TWIN}/.bashrc" in said
        assert "not a dotfile this machine merges" in said

    def test_the_ordinary_dotfile_beside_it_is_not_what_stopped_the_sync(self) -> None:
        """`.bashrc` collides too, and it *is* mergeable -- so the refusal has to
        be about the snapshot rather than about the collision in general.

        Without this, the class is equally satisfied by a `reconcile` that
        refused every conflict it saw.
        """
        self.collide(".bashrc")
        status, said = self.second.say("sync", "--ours")
        assert status == 2, said
        refused = said.split("disagree about", 1)[1].split(" in a way", 1)[0]
        assert f"{paths.META}/state/{self.TWIN}/.bashrc" in refused
        assert " .bashrc" not in refused

    def test_the_merge_is_undone_rather_than_half_settled(self) -> None:
        """The refusal has to leave the repository where the next run can start.

        A path added to `left` makes `integrate` raise, and its `finally` aborts
        -- so this asserts the guarantee rather than the code path: no
        `MERGE_HEAD`, a clean tree, and `HEAD` where it was.
        """
        self.collide(".bashrc")
        # After `collide`, not before: `add` commits, so a `HEAD` read earlier
        # is a different commit for a reason that has nothing to do with the
        # refusal. The first version of this test read it first and failed.
        was = self.second.git("rev-parse", "HEAD")
        assert self.second.call("sync", "--ours") == 2
        assert gitrepo.unfinished(self.second.repo) is None
        assert self.second.git("status", "--porcelain") == ""
        assert self.second.git("rev-parse", "HEAD") == was

    def test_the_snapshot_on_disk_is_still_one_machine_s_own(self) -> None:
        """What the whole issue is about. The snapshot must remain a state this
        machine was really in -- not a merge of two of them, which is what
        settling it at the prompt produced."""
        self.collide(".bashrc")
        assert self.second.call("sync", "--ours") == 2
        snapshot = self.second.repo / paths.META / "state" / self.TWIN / ".bashrc"
        assert snapshot.read_text() == "FROM-B\ntwo\nthree\n"
        assert "FROM-A" not in snapshot.read_text()


class TestSettlingWithTheEditor(TwoCommits):
    """`[e]` over a commit conflict, which no other test here covers."""

    def test_the_editor_settles_it_like_any_other_conflict(self) -> None:
        where = support.fake_editor(self.tmp / "fake-editor", 'printf "SETTLED-BY-HAND\\n" > "$1"')
        self.second.env["EDITOR"] = str(where)
        assert self.second.call("sync", keys="e") == 0
        assert self.second.read(MANAGED) == "SETTLED-BY-HAND\n"
        assert gitrepo.unfinished(self.second.repo) is None


if __name__ == "__main__":
    unittest.main()


class TestWhenTheMergeCannotBeConcluded(TwoCommits):
    """Everything settled, and then the commit refused.

    A `pre-commit` hook is the ordinary way -- and `integrate` has to put the
    repository back rather than leave a fully-settled index nobody committed,
    which the next run would find as an unfinished merge and refuse.
    """

    def test_the_merge_is_undone_and_the_reason_reaches_the_user(self) -> None:
        was = self.second.git("rev-parse", "HEAD")
        support.break_commits(self.second.home)
        done = self.second.run("sync", "--ours")
        assert done.returncode == 2, done.stdout + done.stderr
        assert "could not commit" in done.stderr
        assert "the merge was undone" in done.stderr
        assert gitrepo.unfinished(self.second.repo) is None
        assert self.second.git("rev-parse", "HEAD") == was


class TestUndoneUndoesTheMergeItself(support.SandboxCase):
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

    def setUp(self) -> None:
        super().setUp()
        self.repo = support.make_repo(self.home / "r", self.env)
        self.commit(b"base\n")
        support.git(["branch", "other"], cwd=self.repo, env=self.env)
        self.commit(b"ours\n")
        support.git(["checkout", "-q", "other"], cwd=self.repo, env=self.env)
        self.commit(b"theirs\n")
        support.git(["checkout", "-q", support.BRANCH], cwd=self.repo, env=self.env)
        # Left to fail: that is what leaves MERGE_HEAD behind.
        gitrepo.merge(self.repo, "other")

    def commit(self, text: bytes) -> None:
        (self.repo / ".bashrc").write_bytes(text)
        support.git(["add", "-A"], cwd=self.repo, env=self.env)
        support.git(["commit", "-m", "x"], cwd=self.repo, env=self.env)

    def test_the_fixture_really_left_a_merge_in_progress(self) -> None:
        """The assertion below is vacuous without it: a repository that was never
        mid-merge reports nothing unfinished whatever `undone` does."""
        assert gitrepo.unfinished(self.repo) is not None

    def test_it_raises_and_leaves_no_merge_behind(self) -> None:
        with self.assertRaises(TupferlError) as raised:
            sync.undone(self.repo, "something went wrong")
        assert "the merge was undone" in str(raised.exception)
        assert gitrepo.unfinished(self.repo) is None


class TestWhenSeveralFilesCannotBeSettled(support.TwoMachinesCase):
    """The names are listed in a stable order.

    `sorted` on the walk, so two machines and two runs produce the same sentence.
    Nothing could tell before: with one refused file every ordering is the same
    ordering, which is the fixture-too-weak shape.
    """

    def setUp(self) -> None:
        super().setUp()
        assert self.second.call("init", str(self.remote)) == 0
        # Two files, each a delete against an edit, so both come back refused.
        for name in (".zshrc", ".inputrc"):
            self.first.write(name, "shared\n")
            assert self.first.call("add", str(self.first.home / name)) == 0
        assert self.first.call("sync") == 0
        assert self.second.call("sync") == 0
        for name in (".zshrc", ".inputrc"):
            self.second.write(name, "edited here\n")
            assert self.second.call("add", str(self.second.home / name)) == 0
            assert self.first.call("remove", str(self.first.home / name)) == 0
        assert self.first.call("sync") == 0

    def test_both_are_named_in_sorted_order(self) -> None:
        done = self.second.run("sync", "--ours")
        assert done.returncode == 2, done.stdout + done.stderr
        assert ".inputrc, .zshrc" in done.stderr


class TestWhenOneSideReplacedTheFileWithASymlink(support.TwoMachinesCase):
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

    NAME = ".config/thing"

    def setUp(self) -> None:
        super().setUp()
        assert self.second.call("init", str(self.remote)) == 0
        self.victim = self.tmp / "victim"
        self.victim.write_text("SECRET-ORIGINAL\n", encoding="utf-8")
        for machine in (self.first, self.second):
            (machine.repo / self.NAME).parent.mkdir(parents=True, exist_ok=True)
        # `machine-b` keeps it a file; `machine-a` replaces it with a link.
        (self.second.repo / self.NAME).write_text("still a file\n", encoding="utf-8")
        (self.first.repo / self.NAME).symlink_to(self.victim)
        for machine in (self.second, self.first):
            machine.git("add", "-A")
            machine.git("commit", "-m", "by hand")
        # **Exit 1, and that is correct.** `manifest` walks the repository, so
        # `machine-a`'s own symlink is picked up as a managed item -- and
        # `copies.read` uses `lstat`, so `settle` refuses it and the run reports
        # a file it would not touch. It still commits and pushes, which is all
        # this fixture needs. (The broken-link fixture above exits 0 instead,
        # because `is_file()` is false for a link to nothing, so it is never
        # managed at all.)
        assert self.first.call("sync") == 1

    def test_git_splits_a_type_change_into_two_single_staged_paths(self) -> None:
        """Recorded as a test because it is the reason the mode check cannot be
        exercised through a type change, and because a future git that stopped
        doing it would change which branch refuses this."""
        self.second.git("fetch", "origin")
        assert support.git_merged(self.second.repo, self.second.env) != 0
        stages = gitrepo.conflicted(self.second.repo)
        assert stages[self.NAME] == {gitrepo.THEIRS: 0o120000}
        assert stages[f"{self.NAME}~HEAD"] == {gitrepo.OURS: 0o100644}
        support.git_aborted(self.second.repo, self.second.env)

    def test_it_is_refused_and_the_link_target_is_untouched(self) -> None:
        assert self.second.call("sync", "--ours") == 2
        assert self.victim.read_text(encoding="utf-8") == "SECRET-ORIGINAL\n"


class TestAConflictAboutNothingButTheMode(support.TwoMachinesCase):
    """Same bytes, different modes: git conflicts and `merge-file` does not.

    The one case where the tree-level merge fails and the file-level one
    succeeds, so `resolve` returns settled bytes with no `Sides` and **nobody is
    asked**. Nothing reached it before, so `if outcome.sides is not None` could
    be made unconditional and the suite stayed green.
    """

    def setUp(self) -> None:
        super().setUp()
        assert self.second.call("init", str(self.remote)) == 0
        for machine, mode in ((self.second, 0o755), (self.first, 0o644)):
            machine.write(MANAGED, "identical\n")
            (machine.home / MANAGED).chmod(mode)
            assert machine.call("add", str(machine.home / MANAGED)) == 0
        assert self.first.call("sync") == 0

    def test_the_two_sides_differ_only_in_the_mode(self) -> None:
        self.second.git("fetch", "origin")
        assert support.git_merged(self.second.repo, self.second.env) != 0
        stages = gitrepo.conflicted(self.second.repo)[MANAGED]
        assert stages[gitrepo.THEIRS] != stages[gitrepo.OURS]
        assert gitrepo.version(self.second.repo, gitrepo.THEIRS, MANAGED) == gitrepo.version(
            self.second.repo, gitrepo.OURS, MANAGED
        )
        support.git_aborted(self.second.repo, self.second.env)

    def test_it_settles_with_nobody_asked(self) -> None:
        """`--no-input` is enough, which is the assertion: a run that had to ask
        would report the conflict and exit 1."""
        assert self.second.call("sync", "--no-input") == 0
        assert self.second.read(MANAGED) == "identical\n"
        assert gitrepo.unfinished(self.second.repo) is None
