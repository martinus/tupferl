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
from tupferl import conflicts, gitrepo
from tupferl.errors import TupferlError

#: What each machine commits. Distinct, and neither a prefix of the other, so no
#: assertion below can hold against the wrong side.
FROM_A = "FROM-A\ntwo\nthree\n"
FROM_B = "FROM-B\ntwo\nthree\n"

#: What `[b]` keeps. `machine-b` is the one that answers, so its line is "ours"
#: and comes first -- the same order `git merge-file --union` uses.
BOTH_KEPT = "FROM-B\nFROM-A\ntwo\nthree\n"

MANAGED = ".vimrc"


class TwoCommits(support.TwoMachines):
    """`machine-b` holds a commit the remote has never seen, and they disagree.

    The order matters and is the issue's: `machine-b` commits *first* (through
    `add`, which does not push), and only then does `machine-a` push. Reversed,
    `machine-b` would fetch the change before committing over it and there would
    be no conflict at all.
    """

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(0, self.second.call("init", str(self.remote)))
        self.diverge_by_committing(FROM_B, FROM_A)

    def diverge_by_committing(self, mine: str, theirs: str, executable: bool = False) -> None:
        self.second.write(MANAGED, mine)
        self.first.write(MANAGED, theirs)
        if executable:
            (self.second.home / MANAGED).chmod(0o755)
            (self.first.home / MANAGED).chmod(0o755)
        # `add` commits without pushing on both, and then only `machine-a` syncs.
        self.assertEqual(0, self.second.call("add", str(self.second.home / MANAGED)))
        self.assertEqual(0, self.first.call("add", str(self.first.home / MANAGED)))
        self.assertEqual(0, self.first.call("sync"))

    def settle(self, *args: str, keys: str | None = None) -> None:
        """Sync `machine-b` and insist it finished. Exit 0 is the assertion that
        something was decided: an unsettled conflict is 1 and a run that could
        not proceed is 2, so neither can reach the caller's checks."""
        self.assertEqual(0, self.second.call("sync", *args, keys=keys))

    def concluded(self) -> None:
        """The merge is over: nothing unmerged, nothing dirty, no `MERGE_HEAD`.

        Asserted after every settled case, because a run that wrote the right
        bytes and left the merge half-finished makes the *next* sync refuse to
        start -- which is the failure `integrate`'s abort exists to prevent, in
        the branch that is supposed to succeed.
        """
        self.assertIsNone(gitrepo.unfinished(self.second.repo))
        self.assertEqual([], gitrepo.unmerged(self.second.repo))
        self.assertEqual("", self.second.git("status", "--porcelain"))

    def everywhere(self, want: str) -> None:
        """`want` is `machine-b`'s file and its stored copy, and reaches the
        other machine on its next sync.

        The last is the one a weaker test would leave out: a choice that was
        written and then lost on the other computer is not a settled conflict.
        """
        self.assertEqual(want, self.second.read(MANAGED))
        self.assertEqual(want, self.second.stored(MANAGED).read_text(encoding="utf-8"))
        self.assertEqual(0, self.first.call("sync"))
        self.assertEqual(want, self.first.read(MANAGED))


class TestTheFixtureReallyProducesACommitConflict(TwoCommits):
    """Stated first, because every test below is vacuous without it.

    If `machine-b`'s sync merged cleanly, or if the conflict were the ordinary
    `$HOME`-against-repository kind, these would all pass against code that never
    runs `reconcile`.
    """

    def test_the_second_machine_holds_an_unpushed_commit(self) -> None:
        """What `add` leaves behind, and the reason the issue calls this easy to
        reach."""
        self.assertNotEqual(
            self.second.git("rev-parse", "HEAD"),
            self.second.git("rev-parse", "origin/main"),
        )

    def test_git_cannot_merge_the_two_branches_on_its_own(self) -> None:
        """The precondition, asserted by asking git rather than by assuming.

        `--no-commit` so the check leaves nothing behind; the sync under test
        does its own merge from a clean tree.
        """
        self.second.git("fetch", "origin")
        self.assertNotEqual(
            0,
            support.git_merged(self.second.repo, self.second.env),
            "git merged cleanly, so there is no conflict to settle",
        )
        self.assertIn(MANAGED, gitrepo.unmerged(self.second.repo))
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
        self.assertEqual(2, self.second.call("sync", "--no-input"))
        self.assertIsNone(gitrepo.unfinished(self.second.repo))
        self.assertEqual("", self.second.git("status", "--porcelain"))
        self.assertEqual(was, self.second.git("rev-parse", "HEAD"))

    def test_the_next_sync_still_works_after_one_was_left_unsettled(self) -> None:
        """The point of aborting. Without it the second run finds an unfinished
        merge and refuses, turning one conflict into a machine that cannot sync
        at all."""
        self.assertEqual(2, self.second.call("sync", "--no-input"))
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
        self.assertEqual(2, self.second.call("sync", keys="s"))
        self.assertIsNone(gitrepo.unfinished(self.second.repo))
        self.assertEqual("", self.second.git("status", "--porcelain"))
        self.assertEqual(was, self.second.git("rev-parse", "HEAD"))


class TestTheExecutableBit(TwoCommits):
    """Plan §5 asks for the one mode bit to travel, and during a conflict it
    lives in the index rather than in the working tree -- which holds git's
    marked-up merge, whose bits say nothing about what either side recorded."""

    def setUp(self) -> None:
        support.TwoMachines.setUp(self)
        self.assertEqual(0, self.second.call("init", str(self.remote)))
        self.diverge_by_committing(FROM_B, FROM_A, executable=True)

    def test_the_fixture_really_committed_an_executable_file(self) -> None:
        """Otherwise the assertion below holds for a fixture that never set the
        bit, which is a negative claim with no precondition."""
        self.assertIn("100755", self.second.git("ls-files", "-s", MANAGED))

    def test_a_settled_file_is_still_executable(self) -> None:
        self.settle("--ours")
        self.assertTrue((self.second.home / MANAGED).stat().st_mode & 0o111)
        self.assertIn("100755", self.second.git("ls-files", "-s", MANAGED))


class TestAFileOnlyOneSideStillHas(TwoCommits):
    """A delete against an edit is not a disagreement about lines.

    The prompt has no key that means "keep it" or "let it go", so offering `[l]`
    and `[r]` would be inventing an answer to a question nobody asked. It is
    reported and the merge is undone.
    """

    def setUp(self) -> None:
        support.TwoMachines.setUp(self)
        # **Both machines agree about the file first.** Diverging by content and
        # by existence at once is two conflicts, and the first one settles before
        # this one is ever reached -- which is what the first attempt at this
        # fixture did, and it failed in `setUp` rather than in a test.
        self.first.write(MANAGED, FROM_A)
        self.assertEqual(0, self.first.call("add", str(self.first.home / MANAGED)))
        self.assertEqual(0, self.first.call("sync"))
        self.assertEqual(0, self.second.call("init", str(self.remote)))
        self.assertEqual(FROM_A, self.second.read(MANAGED))

        # Now they disagree about whether it should exist at all: `machine-b`
        # edits and commits without pushing, `machine-a` stops managing it.
        self.second.write(MANAGED, FROM_B)
        self.assertEqual(0, self.second.call("add", str(self.second.home / MANAGED)))
        self.assertEqual(0, self.first.call("remove", str(self.first.home / MANAGED)))
        self.assertEqual(0, self.first.call("sync"))

    def test_it_is_reported_rather_than_guessed_at(self) -> None:
        done = self.second.run("sync", "--ours")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn(MANAGED, done.stderr)
        self.assertIn("removed or replaced it", done.stderr)

    def test_the_repository_is_left_exactly_as_it_was(self) -> None:
        was = self.second.git("rev-parse", "HEAD")
        self.assertEqual(2, self.second.call("sync", "--ours"))
        self.assertIsNone(gitrepo.unfinished(self.second.repo))
        self.assertEqual("", self.second.git("status", "--porcelain"))
        self.assertEqual(was, self.second.git("rev-parse", "HEAD"))


class TestWhenTheTwoCommitsShareAnAncestor(support.TwoMachines):
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
        self.assertEqual(0, self.first.call("add", str(self.first.home / MANAGED)))
        self.assertEqual(0, self.first.call("sync"))
        self.assertEqual(0, self.second.call("init", str(self.remote)))
        # Both hold the same committed version. `machine-b` changes the first
        # line *and* line 20; `machine-a` changes only the first. So they overlap
        # in one place and `machine-b` is alone in the other.
        self.second.write(
            MANAGED, self.SHARED.replace("l0\n", "B-FIRST\n").replace("l20\n", "B-ONLY\n")
        )
        self.first.write(MANAGED, self.SHARED.replace("l0\n", "A-FIRST\n"))
        self.assertEqual(0, self.second.call("add", str(self.second.home / MANAGED)))
        self.assertEqual(0, self.first.call("add", str(self.first.home / MANAGED)))
        self.assertEqual(0, self.first.call("sync"))

    def test_the_fixture_really_has_a_merge_base(self) -> None:
        """The precondition, and the whole reason this class exists."""
        self.second.git("fetch", "origin")
        self.assertNotEqual(0, support.git_merged(self.second.repo, self.second.env))
        stages = gitrepo.conflicted(self.second.repo)
        self.assertIn(gitrepo.BASE, stages.get(MANAGED, {}), "no stage 1: there is no base")
        self.assertEqual(
            self.SHARED.encode(), gitrepo.version(self.second.repo, gitrepo.BASE, MANAGED)
        )
        support.git_aborted(self.second.repo, self.second.env)

    def test_the_base_settles_the_edit_only_one_side_made(self) -> None:
        """`[b]` keeps both versions of what the two disagree about -- and the
        base is what says they do not disagree about line 20.

        Without it that line is a second conflict and the union keeps `l20`
        alongside `B-ONLY`, which is a line the user deleted coming back. That
        absence is the assertion; `B-ONLY` being present is only the precondition
        for it meaning anything.
        """
        self.assertEqual(0, self.second.call("sync", keys="b"))
        settled = self.second.read(MANAGED)
        self.assertIn("B-ONLY", settled)
        self.assertNotIn("l20", settled, "the base was not used: a one-sided edit came back")
        self.assertIn("A-FIRST", settled)
        self.assertIn("B-FIRST", settled)


class TestTheExecutableBitComesFromTheIndex(support.TwoMachines):
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
        self.assertEqual(0, self.second.call("init", str(self.remote)))
        self.assertEqual(0, self.second.call("add", str(self.second.home / MANAGED)))
        self.assertEqual(0, self.first.call("add", str(self.first.home / MANAGED)))
        self.assertEqual(0, self.first.call("sync"))

    def test_the_two_sides_really_disagree_about_the_bit(self) -> None:
        """Otherwise everything below holds for a fixture that set one mode."""
        self.second.git("fetch", "origin")
        self.assertNotEqual(0, support.git_merged(self.second.repo, self.second.env))
        stages = gitrepo.conflicted(self.second.repo)[MANAGED]
        self.assertEqual(0o100755, stages[gitrepo.OURS])
        self.assertEqual(0o100644, stages[gitrepo.THEIRS])
        support.git_aborted(self.second.repo, self.second.env)

    def test_keeping_the_repositorys_side_takes_its_mode_too(self) -> None:
        """The working tree is `755` throughout, so this can only pass by
        reading stage 3's mode out of the index."""
        self.assertEqual(0, self.second.call("sync", "--theirs"))
        self.assertFalse((self.second.home / MANAGED).stat().st_mode & 0o111)

    def test_keeping_this_machines_side_keeps_its_own(self) -> None:
        self.assertEqual(0, self.second.call("sync", "--ours"))
        self.assertTrue((self.second.home / MANAGED).stat().st_mode & 0o111)


class TestWhatIsNotAFileOnBothSides(support.TwoMachines):
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
        self.assertEqual(0, self.second.call("init", str(self.remote)))
        self.victim = self.tmp / "victim"
        self.victim.write_text("SECRET-ORIGINAL\n", encoding="utf-8")
        for machine, target in ((self.second, self.victim), (self.first, self.tmp / "elsewhere")):
            link = machine.repo / self.LINK
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(target)
            machine.git("add", "-A")
            machine.git("commit", "-m", "a symlink, committed by hand")
        self.assertEqual(0, self.first.call("sync"))

    def test_the_fixture_really_committed_a_symlink(self) -> None:
        """Otherwise the refusal below is a claim about nothing."""
        self.assertIn("120000", self.second.git("ls-files", "-s", self.LINK))

    def test_it_is_refused_rather_than_written_through(self) -> None:
        done = self.second.run("sync", "--ours")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn(self.LINK, done.stderr)

    def test_the_file_the_link_pointed_at_is_untouched(self) -> None:
        """The assertion that matters, and it is about a file *outside* the
        repository -- which is why "it was refused" is not enough on its own."""
        self.second.call("sync", "--ours")
        self.assertEqual("SECRET-ORIGINAL\n", self.victim.read_text(encoding="utf-8"))


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
        self.assertEqual(2, self.second.call("sync"))
        self.assertIsNone(gitrepo.unfinished(self.second.repo))
        self.assertEqual("", self.second.git("status", "--porcelain"))
        self.assertEqual(was, self.second.git("rev-parse", "HEAD"))

    def test_a_keyboard_interrupt_leaves_no_half_merged_tree(self) -> None:
        """`BaseException`, not `Exception`: Ctrl-C at a prompt is the most
        ordinary way this happens and it is not an `Exception`."""
        self.raising(KeyboardInterrupt)
        with self.assertRaises(KeyboardInterrupt):
            self.second.call("sync")
        self.assertIsNone(gitrepo.unfinished(self.second.repo))
        self.assertEqual("", self.second.git("status", "--porcelain"))

    def test_the_next_sync_still_works_afterwards(self) -> None:
        """The whole point: an interrupted prompt must not cost the machine its
        ability to sync."""
        self.raising(TupferlError)
        self.assertEqual(2, self.second.call("sync"))
        self.stack.close()
        self.assertEqual(0, self.second.call("sync", "--theirs"))
        self.assertEqual(FROM_A, self.second.read(MANAGED))


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
        self.assertEqual(2, status, said)
        self.assertIn("could not stage the settled files", said)
        self.assertIn("tupferl doctor", said)

    def test_the_message_is_true_about_the_merge_being_undone(self) -> None:
        """The substantive half. That sentence is a claim about what happened to
        the repository, and it holds only because `integrate`'s `finally` aborts
        whatever raises out of `reconcile` -- so it is worth checking rather
        than trusting the comment that says so.
        """
        was = self.second.git("rev-parse", "HEAD")
        self.breaking()
        status, said = self.second.say("sync", "--theirs")
        self.assertEqual(2, status, said)
        self.assertIn("the merge was undone", said)
        self.assertIsNone(gitrepo.unfinished(self.second.repo))
        self.assertEqual("", self.second.git("status", "--porcelain"))
        self.assertEqual(was, self.second.git("rev-parse", "HEAD"))


class TestSettlingWithTheEditor(TwoCommits):
    """`[e]` over a commit conflict, which no other test here covers."""

    def test_the_editor_settles_it_like_any_other_conflict(self) -> None:
        where = support.fake_editor(self.tmp / "fake-editor", 'printf "SETTLED-BY-HAND\\n" > "$1"')
        self.second.env["EDITOR"] = str(where)
        self.assertEqual(0, self.second.call("sync", keys="e"))
        self.assertEqual("SETTLED-BY-HAND\n", self.second.read(MANAGED))
        self.assertIsNone(gitrepo.unfinished(self.second.repo))


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
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("could not commit", done.stderr)
        self.assertIn("the merge was undone", done.stderr)
        self.assertIsNone(gitrepo.unfinished(self.second.repo))
        self.assertEqual(was, self.second.git("rev-parse", "HEAD"))


class TestWhenSeveralFilesCannotBeSettled(support.TwoMachines):
    """The names are listed in a stable order.

    `sorted` on the walk, so two machines and two runs produce the same sentence.
    Nothing could tell before: with one refused file every ordering is the same
    ordering, which is the fixture-too-weak shape.
    """

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(0, self.second.call("init", str(self.remote)))
        # Two files, each a delete against an edit, so both come back refused.
        for name in (".zshrc", ".inputrc"):
            self.first.write(name, "shared\n")
            self.assertEqual(0, self.first.call("add", str(self.first.home / name)))
        self.assertEqual(0, self.first.call("sync"))
        self.assertEqual(0, self.second.call("sync"))
        for name in (".zshrc", ".inputrc"):
            self.second.write(name, "edited here\n")
            self.assertEqual(0, self.second.call("add", str(self.second.home / name)))
            self.assertEqual(0, self.first.call("remove", str(self.first.home / name)))
        self.assertEqual(0, self.first.call("sync"))

    def test_both_are_named_in_sorted_order(self) -> None:
        done = self.second.run("sync", "--ours")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn(".inputrc, .zshrc", done.stderr)


class TestWhenOneSideReplacedTheFileWithASymlink(support.TwoMachines):
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
        self.assertEqual(0, self.second.call("init", str(self.remote)))
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
        self.assertEqual(1, self.first.call("sync"))

    def test_git_splits_a_type_change_into_two_single_staged_paths(self) -> None:
        """Recorded as a test because it is the reason the mode check cannot be
        exercised through a type change, and because a future git that stopped
        doing it would change which branch refuses this."""
        self.second.git("fetch", "origin")
        self.assertNotEqual(0, support.git_merged(self.second.repo, self.second.env))
        stages = gitrepo.conflicted(self.second.repo)
        self.assertEqual({gitrepo.THEIRS: 0o120000}, stages[self.NAME])
        self.assertEqual({gitrepo.OURS: 0o100644}, stages[f"{self.NAME}~HEAD"])
        support.git_aborted(self.second.repo, self.second.env)

    def test_it_is_refused_and_the_link_target_is_untouched(self) -> None:
        self.assertEqual(2, self.second.call("sync", "--ours"))
        self.assertEqual("SECRET-ORIGINAL\n", self.victim.read_text(encoding="utf-8"))


class TestAConflictAboutNothingButTheMode(support.TwoMachines):
    """Same bytes, different modes: git conflicts and `merge-file` does not.

    The one case where the tree-level merge fails and the file-level one
    succeeds, so `resolve` returns settled bytes with no `Sides` and **nobody is
    asked**. Nothing reached it before, so `if outcome.sides is not None` could
    be made unconditional and the suite stayed green.
    """

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(0, self.second.call("init", str(self.remote)))
        for machine, mode in ((self.second, 0o755), (self.first, 0o644)):
            machine.write(MANAGED, "identical\n")
            (machine.home / MANAGED).chmod(mode)
            self.assertEqual(0, machine.call("add", str(machine.home / MANAGED)))
        self.assertEqual(0, self.first.call("sync"))

    def test_the_two_sides_differ_only_in_the_mode(self) -> None:
        self.second.git("fetch", "origin")
        self.assertNotEqual(0, support.git_merged(self.second.repo, self.second.env))
        stages = gitrepo.conflicted(self.second.repo)[MANAGED]
        self.assertNotEqual(stages[gitrepo.OURS], stages[gitrepo.THEIRS])
        self.assertEqual(
            gitrepo.version(self.second.repo, gitrepo.OURS, MANAGED),
            gitrepo.version(self.second.repo, gitrepo.THEIRS, MANAGED),
        )
        support.git_aborted(self.second.repo, self.second.env)

    def test_it_settles_with_nobody_asked(self) -> None:
        """`--no-input` is enough, which is the assertion: a run that had to ask
        would report the conflict and exit 1."""
        self.assertEqual(0, self.second.call("sync", "--no-input"))
        self.assertEqual("identical\n", self.second.read(MANAGED))
        self.assertIsNone(gitrepo.unfinished(self.second.repo))
