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

import unittest

from tests import support
from tupferl import gitrepo

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
        self.assertNotIn("<<<<<<<", self.first.read(MANAGED))


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
        done = support.git_status(
            ["merge", "--no-commit", "--no-ff", "origin/main"], self.second.repo, self.second.env
        )
        self.assertNotEqual(0, done, "git merged cleanly, so there is no conflict to settle")
        self.assertIn(MANAGED, gitrepo.unmerged(self.second.repo))
        support.git_status(["merge", "--abort"], self.second.repo, self.second.env)


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
        was = self.second.git("rev-parse", "HEAD")
        self.assertEqual(2, self.second.call("sync", keys="s"))
        self.assertIsNone(gitrepo.unfinished(self.second.repo))
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
        self.assertIn("removed it", done.stderr)

    def test_the_repository_is_left_exactly_as_it_was(self) -> None:
        was = self.second.git("rev-parse", "HEAD")
        self.assertEqual(2, self.second.call("sync", "--ours"))
        self.assertIsNone(gitrepo.unfinished(self.second.repo))
        self.assertEqual("", self.second.git("status", "--porcelain"))
        self.assertEqual(was, self.second.git("rev-parse", "HEAD"))


if __name__ == "__main__":
    unittest.main()
