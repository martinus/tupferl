"""What `sync` decides, asserted without a repository in sight.

Plan §7.4's decision table, the executable bit, the backup window, the report and
the commit message -- everything `sync` works out before it touches git. The
CLI-driven half is `tests/test_sync_cli.py`, and the split is not tidiness:

**It is what stops a mutation sweep paying 20 seconds to learn something that
takes 0.3.** `tools/mutate.py` runs the modules a mutated file maps to, in order,
with `failfast`. Unittest orders classes alphabetically, so while these lived
beside the CLI tests, `TestAGitLevelConflict` and `TestSyncOneMachine` ran
*before* `TestTheDecisionTable` -- and a mutant in `resolve` was caught only
after the whole integration suite had run. Measured on this suite: 0.30s for the
classes here against 20.48s for the module they were in.

`test_sync` sorts before `test_sync_cli`, which is what puts these first.
"""

from __future__ import annotations

import unittest
from pathlib import PurePosixPath

from tests import support
from tupferl import copies, gitrepo, sync

#: Three versions of one file whose edits do not overlap, so a merge of any two
#: of them is decidable. Named rather than spelled out per test: half the table
#: below is the same three inputs in different roles.
BASE = copies.Blob(b"alpha\nbeta\ngamma\ndelta\nepsilon\n", False)
HOME_EDIT = copies.Blob(b"ALPHA\nbeta\ngamma\ndelta\nepsilon\n", False)
REPO_EDIT = copies.Blob(b"alpha\nbeta\ngamma\ndelta\nEPSILON\n", False)
BOTH = copies.Blob(b"ALPHA\nbeta\ngamma\ndelta\nEPSILON\n", False)

NAME = PurePosixPath(".bashrc")


class TestTheDecisionTable(unittest.TestCase):
    """Plan §7.4 item 1, as decisions. `resolve` reads no files, so each row is
    three values in and one out -- and a row that is wrong here is wrong for
    every path through the engine that reaches it."""

    def test_neither_side_changed(self) -> None:
        got = sync.resolve(NAME, BASE, BASE, BASE)
        self.assertEqual(sync.UNCHANGED, got.action)

    def test_only_home_changed(self) -> None:
        got = sync.resolve(NAME, BASE, HOME_EDIT, BASE)
        self.assertEqual(sync.TO_REPO, got.action)
        self.assertEqual(HOME_EDIT, got.blob)

    def test_only_the_repository_changed(self) -> None:
        got = sync.resolve(NAME, BASE, BASE, REPO_EDIT)
        self.assertEqual(sync.TO_HOME, got.action)
        self.assertEqual(REPO_EDIT, got.blob)

    def test_both_changed_in_different_places(self) -> None:
        got = sync.resolve(NAME, BASE, HOME_EDIT, REPO_EDIT)
        self.assertEqual(sync.MERGED, got.action)
        self.assertEqual(BOTH, got.blob)

    def test_both_changed_the_same_line(self) -> None:
        mine = copies.Blob(b"mine\nbeta\ngamma\ndelta\nepsilon\n", False)
        theirs = copies.Blob(b"theirs\nbeta\ngamma\ndelta\nepsilon\n", False)
        got = sync.resolve(NAME, BASE, mine, theirs)
        self.assertEqual(sync.CONFLICT, got.action)
        self.assertIsNone(got.blob)
        self.assertEqual(1, got.conflicts)

    def test_both_arrived_at_the_same_content(self) -> None:
        """Two machines edited the same file to the same bytes. There is nothing
        to write, but the snapshot has to move -- otherwise the next run merges
        against a base neither side holds any more."""
        got = sync.resolve(NAME, BASE, BOTH, BOTH)
        self.assertEqual(sync.UNCHANGED, got.action)
        self.assertEqual(BOTH, got.blob)

    def test_a_file_missing_from_home_is_restored(self) -> None:
        """`remove` is how someone stops managing a file (plan §4), so a missing
        one is an `rm` or a new machine. Reading it as "delete it everywhere"
        would make one mistake lose the file on every computer the user owns."""
        got = sync.resolve(NAME, BASE, None, REPO_EDIT)
        self.assertEqual(sync.RESTORED, got.action)
        self.assertEqual(REPO_EDIT, got.blob)

    def test_a_missing_file_is_restored_even_when_the_snapshot_is_gone(self) -> None:
        """The check order: missing beats everything, because the comparisons
        below it have nothing to compare."""
        got = sync.resolve(NAME, None, None, REPO_EDIT)
        self.assertEqual(sync.RESTORED, got.action)

    def test_no_snapshot_and_two_different_files_is_a_conflict(self) -> None:
        """Both machines created the file independently. Nothing in the data says
        which is newer, so taking either is a guess that loses the other."""
        got = sync.resolve(NAME, None, HOME_EDIT, REPO_EDIT)
        self.assertEqual(sync.CONFLICT, got.action)

    def test_no_snapshot_and_identical_files_is_not(self) -> None:
        got = sync.resolve(NAME, None, BASE, BASE)
        self.assertEqual(sync.UNCHANGED, got.action)

    def test_a_binary_file_both_sides_changed_is_a_conflict(self) -> None:
        base = copies.Blob(b"\x00\x01base", False)
        got = sync.resolve(NAME, base, copies.Blob(b"\x00\x01mine", False), REPO_EDIT)
        self.assertEqual(sync.CONFLICT, got.action)
        self.assertIsNone(got.blob)


class TestTheExecutableBit(unittest.TestCase):
    """Plan §5 asks for the bit to be preserved. Sync has to *decide* it, which
    is a merge of its own -- and one that cannot conflict, since a bit both sides
    changed they changed to the same value."""

    def test_a_chmod_with_no_edit_is_a_change(self) -> None:
        got = sync.resolve(NAME, BASE, copies.Blob(BASE.data, True), BASE)
        self.assertEqual(sync.TO_REPO, got.action)
        self.assertTrue(got.blob is not None and got.blob.executable)

    def test_it_travels_with_a_merge(self) -> None:
        """The content merges cleanly and the bit comes from the side that
        changed it, so a `chmod +x` on one machine survives an edit on the
        other."""
        got = sync.resolve(NAME, BASE, copies.Blob(HOME_EDIT.data, True), REPO_EDIT)
        self.assertEqual(sync.MERGED, got.action)
        self.assertEqual(copies.Blob(BOTH.data, True), got.blob)

    def test_the_side_that_changed_it_wins(self) -> None:
        for ours, theirs, want in ((False, True, True), (True, False, True)):
            with self.subTest(ours=ours, theirs=theirs):
                self.assertEqual(
                    want,
                    sync.executable_after(
                        copies.Blob(b"", False), copies.Blob(b"", ours), copies.Blob(b"", theirs)
                    ),
                )

    def test_taking_it_away_is_a_change_too(self) -> None:
        """The mirror of the test above, and not the same assertion: a rule that
        only ever answered `True` would pass that one."""
        self.assertFalse(
            sync.executable_after(
                copies.Blob(b"", True), copies.Blob(b"", False), copies.Blob(b"", True)
            )
        )

    def test_with_no_base_it_resolves_towards_executable(self) -> None:
        """Nothing says which side is right, and the two mistakes are not equal:
        a script restored without the bit fails when the user runs it."""
        self.assertTrue(
            sync.executable_after(None, copies.Blob(b"", False), copies.Blob(b"", True))
        )
        self.assertFalse(
            sync.executable_after(None, copies.Blob(b"", False), copies.Blob(b"", False))
        )


class TestReadingAndWriting(support.SandboxCase):
    def test_a_symlink_is_not_the_file(self) -> None:
        """`manifest` refuses a symlink at `add` time; this is the same rule at
        sync time, for a path that has become one since. Following it would read
        -- and then overwrite -- something the user never named."""
        real = self.write(self.tmp / "real", "content\n")
        link = self.tmp / "link"
        link.symlink_to(real)
        self.assertIsNone(copies.read(link))

    def test_a_directory_is_not_the_file_either(self) -> None:
        (self.tmp / "adir").mkdir()
        self.assertIsNone(copies.read(self.tmp / "adir"))

    def test_writing_the_same_bytes_and_bit_changes_nothing(self) -> None:
        """What makes a second sync touch nothing at all. Asserted through the
        return value rather than through mtime, which some filesystems round to
        the second."""
        where = self.tmp / "x"
        blob = copies.Blob(b"hello\n", False)
        self.assertTrue(copies.write(where, blob))
        self.assertFalse(copies.write(where, blob))

    def test_the_executable_bit_round_trips(self) -> None:
        where = self.tmp / "x"
        copies.write(where, copies.Blob(b"#!/bin/sh\n", True))
        self.assertEqual(copies.EXECUTABLE, where.stat().st_mode & 0o777)
        self.assertEqual(copies.Blob(b"#!/bin/sh\n", True), copies.read(where))

    def test_a_mode_change_alone_is_written(self) -> None:
        where = self.tmp / "x"
        copies.write(where, copies.Blob(b"same\n", False))
        self.assertTrue(copies.write(where, copies.Blob(b"same\n", True)))


class TestBackups(support.SandboxCase):
    """Plan §5: a copy before anything in `$HOME` is overwritten, last 5 kept."""

    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "backup"
        self.root.mkdir()

    def test_nothing_is_created_until_something_needs_saving(self) -> None:
        """A quiet sync must leave the disk as it found it. A directory per run
        would also push the last real backup out of the window of five."""
        sync.Backups(self.root)
        self.assertEqual([], sorted(self.root.iterdir()))

    def test_a_saved_file_keeps_its_bytes_and_its_bit(self) -> None:
        where = sync.Backups(self.root).take(
            PurePosixPath(".local/bin/x"), copies.Blob(b"hi\n", True)
        )
        self.assertEqual(copies.Blob(b"hi\n", True), copies.read(where))
        self.assertEqual(".local/bin/x", str(where.relative_to(where.parents[2])))

    def test_only_the_newest_five_runs_survive(self) -> None:
        for index in range(8):
            (self.root / f"2026082{index}T000000.000000").mkdir()
        sync.Backups(self.root).take(NAME, copies.Blob(b"x", False))
        left = sorted(found.name for found in self.root.iterdir())
        self.assertEqual(5, len(left))
        # The oldest three are gone and the newest of the old ones is not.
        self.assertNotIn("20260820T000000.000000", left)
        self.assertIn("20260827T000000.000000", left)

    def test_one_directory_per_run_however_many_files_it_saves(self) -> None:
        """`self.where` is set on the first save and reused. A fixture that backs
        up one file cannot tell that from a directory per *file*, which is what
        the mutation that always takes the branch produces -- and which would
        push the last real backup out of the window of five after five files."""
        backups = sync.Backups(self.root)
        backups.take(PurePosixPath(".bashrc"), copies.Blob(b"a", False))
        backups.take(PurePosixPath(".vimrc"), copies.Blob(b"b", False))
        self.assertEqual(1, len(list(self.root.iterdir())))

    def test_nothing_is_deleted_while_there_is_room(self) -> None:
        """The other side of the window, and the one that loses data if it is
        wrong: with `BACKUPS_KEPT` or fewer runs kept there is nothing to forget.

        **Every count below the window, not one of them.** The first version of
        this test put four directories in and expected five out -- and five is
        the *single* count at which the correct `max(0, n - 5)` and a mutated
        `max(-1, n - 5)` agree, because both come to zero. At two, three and four
        the mutant deletes everything but the newest: it loses the user's saved
        copies precisely when there are fewest of them. The mutation sweep found
        it; the test as written could not.
        """
        for existing in range(5):
            with self.subTest(existing=existing):
                root = self.tmp / f"window-{existing}"
                root.mkdir()
                for index in range(existing):
                    (root / f"2026082{index}T000000.000000").mkdir()
                sync.Backups(root).take(NAME, copies.Blob(b"x", False))
                self.assertEqual(
                    existing + 1,
                    len(list(root.iterdir())),
                    f"a run with {existing + 1} backups deleted one",
                )

    def test_a_file_a_user_left_here_is_not_deleted(self) -> None:
        """This removes trees. Anything that is not one of its own directories
        belongs to somebody else."""
        keep = self.write(self.root / "notes.txt", "mine\n")
        for index in range(8):
            (self.root / f"2026082{index}T000000.000000").mkdir()
        sync.Backups(self.root).take(NAME, copies.Blob(b"x", False))
        self.assertTrue(keep.is_file())


class TestStaleSnapshots(support.SandboxCase):
    def test_snapshots_of_unmanaged_files_are_found(self) -> None:
        snaps = self.tmp / "state"
        self.write(snaps / ".bashrc", "a")
        self.write(snaps / ".config" / "foo.conf", "b")
        found = sync.stale(snaps, {PurePosixPath(".bashrc")})
        self.assertEqual([PurePosixPath(".config/foo.conf")], found)

    def test_a_machine_that_has_never_synced_has_none(self) -> None:
        self.assertEqual([], sync.stale(self.tmp / "never", set()))


class TestTheReport(unittest.TestCase):
    def test_unchanged_files_are_not_listed_but_are_counted(self) -> None:
        """Most files on most runs. Forty lines saying nothing happened would
        bury the one line saying something did."""
        text = sync.report(
            [
                sync.Outcome(PurePosixPath(".bashrc"), sync.UNCHANGED, BASE),
                sync.Outcome(PurePosixPath(".vimrc"), sync.TO_REPO, HOME_EDIT),
            ]
        )
        self.assertNotIn(".bashrc", text)
        self.assertIn("stored .vimrc", text)
        self.assertIn("2 files managed, 1 changed, 0 in conflict", text)

    def test_a_conflict_says_how_much_there_is_to_settle(self) -> None:
        text = sync.report([sync.Outcome(PurePosixPath(".bashrc"), sync.CONFLICT, None, 3)])
        self.assertIn("conflict in .bashrc (3 to settle)", text)
        self.assertIn("1 in conflict", text)


class TestTheCommitMessage(unittest.TestCase):
    def test_it_names_what_changed(self) -> None:
        got = sync.message([PurePosixPath(".vimrc"), PurePosixPath(".bashrc")], "laptop")
        self.assertEqual("sync from laptop: .bashrc, .vimrc", got)

    def test_with_nothing_decided_it_says_what_actually_happened(self) -> None:
        """Staged but untouched means an earlier run left a copy behind, or the
        user put a file in the repository by hand. Naming files would be a lie."""
        got = sync.message([], "laptop")
        self.assertIn("an earlier run", got)


class TestUnfinishedMarkers(support.SandboxCase):
    def test_every_marker_is_looked_for(self) -> None:
        """One per marker: a rebase and a cherry-pick leave the same half-done
        tree as a merge. The names are written out rather than read from the
        constant, because a loop over a shortened constant still passes."""
        repo = support.make_repo(self.tmp / "repo", self.env)
        self.assertIsNone(gitrepo.unfinished(repo))
        for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD"):
            with self.subTest(marker=marker):
                where = repo / ".git" / marker
                where.write_text("deadbeef\n", encoding="utf-8")
                try:
                    self.assertEqual(marker, gitrepo.unfinished(repo))
                finally:
                    where.unlink()

    def test_a_marker_outside_a_repository_is_not_reported_as_one(self) -> None:
        """The guard's whole job, and the fixture that can see it: when git
        cannot say where the git directory is, `inside.out` is empty and the
        lookup falls back to `repo` itself. A directory holding a file called
        `MERGE_HEAD` would then be reported as a half-finished merge -- and
        `sync` refuses to run on one, so a stray file would wedge the command.
        """
        empty = self.tmp / "not-a-repo-either"
        empty.mkdir()
        (empty / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        self.assertIsNone(gitrepo.unfinished(empty))

    def test_staging_nothing_is_refused_rather_than_staging_everything(self) -> None:
        """`git add --all --` with an empty pathspec stages the whole repository,
        untracked files included -- measured. A caller whose list came out empty
        means "nothing", and git's default reading is the most destructive one
        available, so it is answered rather than passed on."""
        repo = support.make_repo(self.tmp / "guarded", self.env)
        (repo / "stray.txt").write_text("not mine\n", encoding="utf-8")
        refused = gitrepo.stage(repo, [])
        self.assertFalse(refused.ok)
        self.assertIn("nothing to stage", refused.err)
        self.assertEqual("?? stray.txt", support.git(["status", "--porcelain"], repo, self.env))

    def test_a_directory_git_cannot_read_reports_nothing_rather_than_guessing(self) -> None:
        """`None` because there is no answer, not because the answer is "no" --
        `sync` fails on its own next git call, and `doctor` reports the git
        failure separately."""
        empty = self.tmp / "not-a-repo"
        empty.mkdir()
        self.assertIsNone(gitrepo.unfinished(empty))


if __name__ == "__main__":
    unittest.main()
