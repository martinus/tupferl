"""The sync engine's example tests: plan §7.4's boundary cases, one by one.

The properties in `test_sync_properties.py` and `test_merge_properties.py` cover
the bulk of §7.4 item 1 -- every combination of what changed, over generated
content, across two machines. This file is what a generator is bad at:

- the exact rows of the decision table, asserted as decisions rather than as
  outcomes of a whole sync (§7.4 item 1's boundary cases);
- the failure paths (§7.4 item 4): a push the remote rejects because it moved, a
  repository left half-merged, a path that cannot be written, no remote at all;
- the two things milestone 3 does *not* do, so that a later milestone changing
  them is a visible change: a conflict is reported and nothing is written, and
  `--ours`/`--theirs` are refused rather than ignored.

Host overlays are milestone 5, but `add --host` shipped in milestone 2 and sync
has to respect it now or the overlay would be silently overwritten from the
shared copy. That test is here for the same reason the rule is in `manifest`.
"""

from __future__ import annotations

import os
import shutil
import stat
import unittest
from pathlib import PurePosixPath

from tests import support
from tupferl import copies, gitrepo, paths, sync

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
        wrong: with five or fewer runs kept there is nothing to forget. A test
        that only ever overflows the window cannot tell `max(0, ...)` from
        `max(1, ...)`, which quietly deletes the oldest backup every run."""
        for index in range(4):
            (self.root / f"2026082{index}T000000.000000").mkdir()
        sync.Backups(self.root).take(NAME, copies.Blob(b"x", False))
        self.assertEqual(5, len(list(self.root.iterdir())))
        self.assertTrue((self.root / "20260820T000000.000000").is_dir())

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


class OneMachine(support.Machine):
    """`support.Machine`, with one file already managed and a `sync` shorthand."""

    def setUp(self) -> None:
        super().setUp()
        self.write(self.home / ".bashrc", "one\ntwo\nthree\n")
        self.init()
        self.assertEqual(0, self.run_cli("add", str(self.home / ".bashrc")).returncode)

    def sync(self, *flags: str) -> tuple[int, str, str]:
        done = self.run_cli("sync", *flags)
        return done.returncode, done.stdout, done.stderr


class TestSyncOneMachine(OneMachine):
    def test_it_commits_and_pushes_what_add_stored(self) -> None:
        status, _, _ = self.sync()
        self.assertEqual(0, status)
        on_remote = support.git(
            ["ls-tree", "-r", "--name-only", support.BRANCH], self.remote, self.env
        )
        self.assertIn(".bashrc", on_remote.splitlines())

    def test_the_snapshot_is_committed_so_a_restore_keeps_the_merge_base(self) -> None:
        """Plan §5: snapshots are per-host and committed, "so every host knows
        its own merge base". A snapshot only on disk is lost with the machine."""
        self.sync()
        on_remote = support.git(
            ["ls-tree", "-r", "--name-only", support.BRANCH], self.remote, self.env
        )
        want = self.snapshot(".bashrc").relative_to(self.repo)
        self.assertIn(str(want), on_remote.splitlines())

    def test_a_home_edit_reaches_the_repository(self) -> None:
        self.sync()
        self.write(self.home / ".bashrc", "ONE\ntwo\nthree\n")
        status, out, _ = self.sync()
        self.assertEqual(0, status)
        self.assertContains(out, "stored .bashrc")
        self.assertEqual("ONE\ntwo\nthree\n", (self.repo / ".bashrc").read_text())

    def test_the_commit_message_is_the_shape_the_plan_asks_for(self) -> None:
        """Plan §3.5: `sync from <hostname>: update .bashrc, .gitconfig`."""
        self.sync()
        self.write(self.home / ".bashrc", "ONE\ntwo\nthree\n")
        self.sync()
        subject = support.git(["log", "-1", "--format=%s"], self.repo, self.env)
        self.assertEqual(f"sync from {self.host}: .bashrc", subject)

    def test_a_second_sync_writes_no_commit(self) -> None:
        self.sync()
        before = support.git(["rev-parse", "HEAD"], self.repo, self.env)
        status, out, _ = self.sync()
        self.assertEqual(0, status)
        self.assertEqual(before, support.git(["rev-parse", "HEAD"], self.repo, self.env))
        self.assertContains(out, "0 changed")

    def test_a_sync_with_nothing_to_push_does_not_reach_the_remote(self) -> None:
        """A push that prints "Everything up-to-date" still opens the connection
        and negotiates refs. A machine that syncs on a timer takes that path
        almost every time, so one local `merge-base` buys the round trip back:
        6.9ms against 2.3ms here, median of ten, against a local bare repository
        -- and against a real ssh remote the saving is a connection setup, which
        nothing in this suite can measure.

        **The obvious fixture cannot fail.** A `pre-receive` hook on the remote
        counting pushes reports zero either way, because git compares the ref
        advertisement first and never starts `receive-pack` for an up-to-date
        push. That version of this test passed with the optimisation disabled.

        So: push, and only push, is pointed somewhere that does not exist.
        `fetch` still uses the real URL, so a sync gets as far as deciding; one
        that decides to push fails, and one that skips it does not. The second
        half asserts exactly that, which is what stops this passing against a
        `sync` that never pushes at all -- the more serious bug of the two.
        """
        self.sync()
        nowhere = self.tmp / "nowhere.git"
        support.git(["remote", "set-url", "--push", "origin", str(nowhere)], self.repo, self.env)

        self.assertEqual(0, self.sync()[0], "a sync with nothing to push tried to push")

        self.write(self.home / ".bashrc", "ONE\ntwo\nthree\n")
        status, _, err = self.sync()
        self.assertEqual(2, status, "a sync with a change did not try to push")
        self.assertContains(err, "could not push")

    def test_a_file_a_user_dropped_into_the_repository_is_committed(self) -> None:
        """`doctor` already promises this -- "`tupferl sync` will commit them" --
        and it is also how a copy an interrupted run left behind gets in."""
        self.sync()
        self.write(self.repo / ".vimrc", "set nocompatible\n")
        status, _, _ = self.sync()
        self.assertEqual(0, status)
        self.assertEqual("", support.git(["status", "--porcelain"], self.repo, self.env))

    def test_without_a_remote_it_still_syncs_and_says_so(self) -> None:
        support.git(["remote", "remove", "origin"], self.repo, self.env)
        self.write(self.home / ".bashrc", "ONE\ntwo\nthree\n")
        status, out, _ = self.sync()
        self.assertEqual(0, status)
        self.assertContains(out, "no remote configured")
        self.assertEqual("ONE\ntwo\nthree\n", (self.repo / ".bashrc").read_text())


class TestWhatSyncWillNotTouch(OneMachine):
    """`settle`'s two refusal branches. Both are the `manifest` admission rules
    applied again at sync time, for a path that has changed *kind* since it was
    added -- and both were unreached until the mutation sweep said so."""

    def test_a_symlink_in_the_repository_is_refused_rather_than_followed(self) -> None:
        """`manifest.managed` finds it, because `is_file()` follows links; the
        read does not, because it `lstat`s. Following it would copy whatever it
        points at into `$HOME` under a name the user never associated with it."""
        self.sync()
        (self.repo / ".vimrc").symlink_to(self.repo / ".bashrc")
        support.git(["add", "-A"], self.repo, self.env)
        support.git(["commit", "-m", "a link crept in"], self.repo, self.env)

        status, out, _ = self.sync()
        self.assertEqual(1, status)
        self.assertContains(out, "skipped .vimrc")
        self.assertContains(out, "not a regular file")
        self.assertFalse((self.home / ".vimrc").exists(), "the link was followed into $HOME")

    def test_something_that_is_not_a_file_in_home_is_left_alone(self) -> None:
        """A fifo where a managed file should be. `os.mkfifo` rather than a unix
        socket: `sun_path` is 104 bytes on macOS and a sandboxed repository path
        exceeds it, so the socket version errors instead of testing."""
        self.sync()
        (self.home / ".bashrc").unlink()
        os.mkfifo(self.home / ".bashrc")

        status, out, _ = self.sync()
        self.assertEqual(1, status)
        self.assertContains(out, "skipped .bashrc")
        self.assertTrue(stat.S_ISFIFO(os.lstat(self.home / ".bashrc").st_mode))

    def test_a_detached_head_is_named_as_the_problem(self) -> None:
        """A real state -- someone checked out a commit in the repository to look
        at it. Without this the branch is `None`, and what reaches the user is
        git complaining about a ref called "None"."""
        support.git(["checkout", "--detach", "HEAD"], self.repo, self.env)
        status, _, err = self.sync()
        self.assertEqual(2, status)
        self.assertContains(err, "no branch checked out")


class TestWhatTheCommitNames(OneMachine):
    """The commit message names what this run decided, and nothing else.

    Three survivors lived here, all of the same shape: a mutation that adds a
    name to the message is invisible to a fixture that manages *one* file, since
    "the file that changed" and "every managed file" are then the same list.
    """

    def setUp(self) -> None:
        super().setUp()
        self.write(self.home / ".vimrc", "set nocompatible\n")
        self.run_cli("add", str(self.home / ".vimrc"))
        self.sync()

    def subject(self) -> str:
        return support.git(["log", "-1", "--format=%s"], self.repo, self.env)

    def test_only_the_file_that_changed_is_named(self) -> None:
        self.write(self.home / ".bashrc", "ONE\ntwo\nthree\n")
        self.sync()
        self.assertEqual(f"sync from {self.host}: .bashrc", self.subject())

    def test_a_conflicted_file_is_not_named_as_something_that_changed(self) -> None:
        """A conflict writes nothing, so naming it in the commit would describe a
        change the commit does not contain. Needs a second file that *did*
        change, or there is no commit to inspect."""
        support.git(["checkout", "-q", "-b", "elsewhere"], self.repo, self.env)
        self.write(self.repo / ".vimrc", "set number\n")
        support.git(["commit", "-qam", "another machine"], self.repo, self.env)
        support.git(["checkout", "-q", "main"], self.repo, self.env)
        support.git(["merge", "-q", "--no-edit", "elsewhere"], self.repo, self.env)
        self.write(self.home / ".vimrc", "set nonumber\n")
        self.write(self.home / ".bashrc", "ONE\ntwo\nthree\n")

        status, out, _ = self.sync()
        self.assertEqual(1, status)
        self.assertContains(out, "conflict in .vimrc")
        self.assertEqual(f"sync from {self.host}: .bashrc", self.subject())


class TestWhatMilestoneThreeRefuses(OneMachine):
    def test_ours_and_theirs_name_the_milestone_that_builds_them(self) -> None:
        """Refused rather than ignored: a flag that silently does nothing is how
        a script ends up believing its conflicts were resolved."""
        for flag in ("--ours", "--theirs"):
            with self.subTest(flag=flag):
                status, _, err = self.sync(flag)
                self.assertEqual(2, status)
                self.assertContains(err, "milestone 4")

    def test_no_input_is_accepted_because_it_is_already_what_happens(self) -> None:
        self.assertEqual(0, self.sync("--no-input")[0])

    def test_an_unfinished_merge_stops_it(self) -> None:
        """A killed sync leaves this behind, and committing on top of it would
        conclude a merge nobody resolved."""
        (self.repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        status, _, err = self.sync()
        self.assertEqual(2, status)
        self.assertContains(err, "MERGE_HEAD")

    def test_a_path_that_cannot_be_written_is_reported_and_the_rest_go_on(self) -> None:
        """One bad path does not stop the run, and it does not arrive as a
        traceback. Here `$HOME/.config/nvim` is a *file*, so the directory the
        managed `init.lua` needs cannot be made."""
        self.write(self.home / ".config" / "nvim" / "init.lua", "vim.o.number = true\n")
        support.run_cli(["add", str(self.home / ".config" / "nvim")], self.env)
        self.sync()
        shutil.rmtree(self.home / ".config" / "nvim")
        self.write(self.home / ".config" / "nvim", "not a directory\n")
        self.write(self.home / ".bashrc", "ONE\ntwo\nthree\n")

        status, out, err = self.sync()
        self.assertEqual(1, status)
        self.assertContains(out, "skipped .config/nvim/init.lua")
        self.assertNotIn("Traceback", err)
        # The other file still synced.
        self.assertEqual("ONE\ntwo\nthree\n", (self.repo / ".bashrc").read_text())


class TwoMachines(unittest.TestCase):
    """Two `$HOME`s and one bare remote -- plan §3.5's daily flow.

    Not `SandboxCase`, which patches `os.environ` for *one* machine; these drive
    the CLI as subprocesses with one environment each, which is also the only way
    two hostnames can exist at once.
    """

    def setUp(self) -> None:
        box = support.tempdir()
        self.tmp = box.__enter__()
        self.addCleanup(box.__exit__, None, None, None)
        self.first = support.Computer(self.tmp, "machine-a")
        self.second = support.Computer(self.tmp, "machine-b")
        self.remote = support.make_remote(self.tmp / "remote.git", self.first.env)

        self.first.write(".bashrc", "one\ntwo\nthree\nfour\nfive\n")
        self.assertEqual(0, self.first.run("init", str(self.remote)).returncode)
        self.assertEqual(0, self.first.run("add", str(self.first.home / ".bashrc")).returncode)
        self.assertEqual(0, self.first.run("sync").returncode)


class TestTwoMachines(TwoMachines):
    def test_init_on_the_second_machine_brings_everything_down(self) -> None:
        """The README's promise: `tupferl init <url>` alone sets up a second
        computer, because plan §4 says init "then runs a first sync"."""
        done = self.second.run("init", str(self.remote))
        self.assertEqual(0, done.returncode)
        self.assertEqual("one\ntwo\nthree\nfour\nfive\n", self.second.read(".bashrc"))
        self.assertIn("restored .bashrc", done.stdout)

    def test_edits_that_do_not_overlap_merge_without_asking(self) -> None:
        self.second.run("init", str(self.remote))
        self.first.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        self.second.write(".bashrc", "one\ntwo\nthree\nfour\nFIVE\n")
        self.assertEqual(0, self.first.run("sync").returncode)
        done = self.second.run("sync")
        self.assertEqual(0, done.returncode)
        self.assertIn("merged .bashrc", done.stdout)
        self.assertEqual("ONE\ntwo\nthree\nfour\nFIVE\n", self.second.read(".bashrc"))
        self.assertEqual(0, self.first.run("sync").returncode)
        self.assertEqual("ONE\ntwo\nthree\nfour\nFIVE\n", self.first.read(".bashrc"))

    def test_a_real_conflict_is_reported_and_nothing_is_written(self) -> None:
        """Milestone 3's whole answer to a conflict, and the thing milestone 4
        changes: both copies are left exactly as they were, and the exit status
        says a human is needed."""
        self.second.run("init", str(self.remote))
        self.first.write(".bashrc", "MINE\ntwo\nthree\nfour\nfive\n")
        self.second.write(".bashrc", "THEIRS\ntwo\nthree\nfour\nfive\n")
        self.first.run("sync")

        done = self.second.run("sync")
        self.assertEqual(1, done.returncode)
        self.assertIn("conflict in .bashrc", done.stdout)
        # Nothing written means nothing left uncommitted either: a conflict must
        # not leave the repository dirty for the next run to commit blindly.
        self.assertEqual("", self.second.git("status", "--porcelain"))
        self.assertEqual("THEIRS\ntwo\nthree\nfour\nfive\n", self.second.read(".bashrc"))
        self.assertEqual(
            "MINE\ntwo\nthree\nfour\nfive\n", self.second.stored(".bashrc").read_text()
        )
        self.assertNotIn("<<<<<<<", self.second.read(".bashrc"))

    def test_a_conflict_leaves_the_snapshot_where_it_was(self) -> None:
        """Otherwise the next run would merge against a state neither side ever
        had, and the conflict would resolve itself into one of the two by
        accident."""
        self.second.run("init", str(self.remote))
        was = self.second.snapshot(".bashrc").read_text()
        self.first.write(".bashrc", "MINE\ntwo\nthree\nfour\nfive\n")
        self.second.write(".bashrc", "THEIRS\ntwo\nthree\nfour\nfive\n")
        self.first.run("sync")
        self.second.run("sync")
        self.assertEqual(was, self.second.snapshot(".bashrc").read_text())

    def test_a_backup_is_taken_before_home_is_overwritten(self) -> None:
        """Plan §5. The file being replaced is the user's, and this is the only
        copy of it that survives."""
        self.second.run("init", str(self.remote))
        self.second.write(".bashrc", "local edit\ntwo\nthree\nfour\nfive\n")
        self.first.write(".bashrc", "one\ntwo\nthree\nfour\nFIVE\n")
        self.first.run("sync")
        self.second.run("sync")

        saved = sorted(self.second.backups.rglob(".bashrc"))
        self.assertEqual(1, len(saved), f"expected one backup, found {saved}")
        self.assertEqual("local edit\ntwo\nthree\nfour\nfive\n", saved[0].read_text())

    def test_the_executable_bit_reaches_the_other_machine(self) -> None:
        self.second.run("init", str(self.remote))
        (self.first.home / ".bashrc").chmod(copies.EXECUTABLE)
        self.first.run("sync")
        self.second.run("sync")
        self.assertEqual(copies.EXECUTABLE, (self.second.home / ".bashrc").stat().st_mode & 0o777)

    def test_a_pruned_snapshot_is_named_and_leaves_no_empty_directory(self) -> None:
        """git does not track directories, so one left empty here is invisible in
        the commit and present in every clone. The name is asserted too: this run
        did decide something about that file, and a commit message that named
        nothing would be the "an earlier run" sentence, which would be a lie."""
        self.first.write(".config/nvim/init.lua", "vim.o.number = true\n")
        self.first.run("add", str(self.first.home / ".config"))
        self.first.run("sync")
        self.second.run("init", str(self.remote))
        self.assertTrue(self.second.snapshot(".config/nvim/init.lua").is_file())

        self.first.run("remove", str(self.first.home / ".config" / "nvim" / "init.lua"))
        self.first.run("sync")
        self.second.run("sync")

        self.assertIn(".config/nvim/init.lua", self.second.git("log", "-1", "--format=%s"))
        left = self.second.snapshot(".config")
        self.assertFalse(left.exists(), f"{left} was left behind empty")

    def test_a_removal_elsewhere_prunes_this_machines_snapshot(self) -> None:
        """A snapshot left behind is committed, and would become the merge base
        for a file that came back later under the same name."""
        self.second.run("init", str(self.remote))
        self.assertTrue(self.second.snapshot(".bashrc").is_file())
        self.first.run("remove", str(self.first.home / ".bashrc"))
        self.first.run("sync")
        self.second.run("sync")
        self.assertFalse(self.second.snapshot(".bashrc").exists())
        # Plan §4: `remove` keeps the file in $HOME -- on every machine.
        self.assertTrue((self.second.home / ".bashrc").is_file())

    def test_a_host_overlay_is_what_syncs_on_that_host(self) -> None:
        """Plan §3.3: the overlay replaces the shared file on this host. Writing
        the shared copy instead would overwrite the overlay from the other
        machine's version on the next run."""
        self.first.write(".gitconfig", support.gitconfig("machine-a") + "[core]\n\tpager = less\n")
        self.first.run("add", "--host", str(self.first.home / ".gitconfig"))
        self.first.run("sync")
        shared = self.first.stored(".gitconfig")
        overlay = self.first.stored(".gitconfig", host=True)
        self.assertTrue(overlay.is_file())
        self.assertFalse(shared.exists())

        self.first.write(".gitconfig", support.gitconfig("machine-a") + "[core]\n\tpager = bat\n")
        self.assertEqual(0, self.first.run("sync").returncode)
        self.assertIn("pager = bat", overlay.read_text())

    def test_a_push_the_remote_rejected_is_retried_after_pulling(self) -> None:
        """Plan §3.4 step 5. The remote genuinely moves inside the window between
        this sync's fetch and its push -- see `support.move_on_first_push`."""
        support.move_on_first_push(self.remote, self.first.env, self.tmp)
        self.first.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")

        done = self.first.run("sync")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        # `support.git` strips, so the trailing newline is not in the comparison.
        on_remote = support.git(["show", f"{support.BRANCH}:.bashrc"], self.remote, self.first.env)
        self.assertEqual("ONE\ntwo\nthree\nfour\nfive", on_remote)
        # And what the other machine pushed is still there.
        settings = support.git(
            ["show", f"{support.BRANCH}:{paths.META}/config.toml"], self.remote, self.first.env
        )
        self.assertIn("edited on another machine", settings)

    def test_a_push_refused_for_any_other_reason_is_reported(self) -> None:
        """The other half of the retry: if nothing came in, pushing again would
        fail the same way, so it says so instead of looping."""
        support.git(
            ["remote", "set-url", "origin", str(self.tmp / "gone.git")],
            self.first.repo,
            self.first.env,
        )
        self.first.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        done = self.first.run("sync")
        self.assertEqual(2, done.returncode)
        self.assertIn("could not fetch", done.stderr)


class TestAGitLevelConflict(TwoMachines):
    """Two machines that have each *committed* a change to the same lines.

    The path the mutation sweep found untested, and six of its survivors lived
    here: `sync.integrate`'s failure branch and the two `gitrepo` calls it makes.
    It is reached whenever a machine has an unpushed commit -- which `tupferl
    add` creates, since it commits without pushing -- and the remote has moved.

    git's own merge has the real merge base and is a better answer than anything
    this module could compute, so a conflict there is two committed versions
    disagreeing: milestone 4's prompt, not milestone 3's business. What milestone
    3 owes the user is a sentence naming the file and a repository left exactly
    as it was found.
    """

    def commit_locally(self, machine: support.Computer, text: str) -> None:
        """Edit and `add`, which commits without pushing -- the ordinary way a
        machine ends up holding a commit the remote has not seen."""
        machine.write(".bashrc", text)
        self.assertEqual(0, machine.run("add", str(machine.home / ".bashrc")).returncode)

    def test_it_names_the_file_and_the_milestone_that_settles_it(self) -> None:
        self.second.run("init", str(self.remote))
        self.commit_locally(self.second, "THEIRS\ntwo\nthree\nfour\nfive\n")
        self.first.write(".bashrc", "MINE\ntwo\nthree\nfour\nfive\n")
        self.assertEqual(0, self.first.run("sync").returncode)

        done = self.second.run("sync")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn(".bashrc", done.stderr)
        self.assertIn("milestone 4", done.stderr)

    def test_the_repository_is_left_exactly_as_it_was_found(self) -> None:
        """The abort is the point. A half-merged tree makes the *next* run refuse
        to start, which turns one conflict into a machine that cannot sync at
        all -- and `sync` would then be committing on top of a merge nobody
        resolved."""
        self.second.run("init", str(self.remote))
        self.commit_locally(self.second, "THEIRS\ntwo\nthree\nfour\nfive\n")
        self.first.write(".bashrc", "MINE\ntwo\nthree\nfour\nfive\n")
        self.first.run("sync")
        was = self.second.git("rev-parse", "HEAD")

        self.assertEqual(2, self.second.run("sync").returncode)
        self.assertIsNone(gitrepo.unfinished(self.second.repo), "the merge was not aborted")
        self.assertEqual("", self.second.git("status", "--porcelain"), "the tree was left dirty")
        self.assertEqual(was, self.second.git("rev-parse", "HEAD"))
        self.assertNotIn("<<<<<<<", self.second.stored(".bashrc").read_text())

    def test_a_merge_that_fails_without_conflicting_files_says_so_instead(self) -> None:
        """The other half of `if stuck:`, and it needs its own fixture: a merge
        can fail with *no* unmerged paths. Pointing the machine at a remote with
        an unrelated history is the honest way to produce one -- git refuses
        outright, so there is no file to name, and a message that named none
        would read as a bug in the naming."""
        stranger = support.make_remote(self.tmp / "stranger.git", self.second.env)
        seed = support.make_repo(self.tmp / "seed", self.second.env, remote=stranger)
        self.assertTrue(seed.is_dir())
        self.second.run("init", str(self.remote))
        support.git(
            ["remote", "set-url", "origin", str(stranger)], self.second.repo, self.second.env
        )

        done = self.second.run("sync")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn("could not merge", done.stderr)
        self.assertIsNone(gitrepo.unfinished(self.second.repo))


class TestTheSnapshotIsWrittenLast(TwoMachines):
    """Plan §7.4 item 4: a sync killed part-way must leave the state consistent.

    The ordering in `apply` is the whole of that guarantee, and it is invisible
    in a successful run -- so this makes the `$HOME` write *fail* and asserts
    that the snapshot did not move. Written the other way round, the snapshot
    would claim `$HOME` already held the new version, and the next run would copy
    the stale `$HOME` file over the new one: silent loss, one line away.
    """

    def test_a_failed_home_write_leaves_the_snapshot_alone(self) -> None:
        self.first.write(".config/nvim/init.lua", "vim.o.number = true\n")
        self.first.run("add", str(self.first.home / ".config" / "nvim"))
        self.first.run("sync")
        self.second.run("init", str(self.remote))
        was = self.second.snapshot(".config/nvim/init.lua").read_text()

        # `$HOME/.config/nvim` becomes a file, so the directory the managed
        # `init.lua` needs cannot be created and the write fails for real.
        shutil.rmtree(self.second.home / ".config" / "nvim")
        self.second.write(".config/nvim", "not a directory\n")
        self.first.write(".config/nvim/init.lua", "vim.o.number = false\n")
        self.first.run("sync")

        done = self.second.run("sync")
        self.assertEqual(1, done.returncode)
        self.assertIn("skipped .config/nvim/init.lua", done.stdout)
        self.assertEqual(was, self.second.snapshot(".config/nvim/init.lua").read_text())


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
