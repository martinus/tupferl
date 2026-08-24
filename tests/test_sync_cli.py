"""`tupferl sync` driven the way a user drives it: a real CLI, a real remote.

The other half of `tests/test_sync.py`, which holds everything that can be
decided without a repository. See that module's docstring for why the two are
separate files rather than two halves of one -- it is worth 20 seconds per
mutant to a sweep.

What lives here is plan §7.4 item 4, the failure paths: a push the remote
rejected because it moved, a repository left half-merged, a path that cannot be
written, no remote at all -- plus the two-machine flows, which need two `$HOME`s
and so cannot use `SandboxCase` at all.
"""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import PurePosixPath

from tests import support
from tupferl import copies, gitrepo, paths

NAME = PurePosixPath(".bashrc")


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


class TestWhatStopsASync(OneMachine):
    """What a run refuses, and what it survives. The resolution flags are
    `tests/test_conflict_cli.py`, which needs a conflict to point them at."""

    def test_the_resolution_flags_change_nothing_when_there_is_no_conflict(self) -> None:
        """All three, on a run with nothing to settle. Not a placeholder: each
        one installs a settler that is never called, and a settler that ran
        anyway would have to invent three versions of a file nobody disagreed
        about."""
        for flag in ("--ours", "--theirs", "--no-input"):
            with self.subTest(flag=flag):
                self.assertEqual(0, self.sync(flag)[0])

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


class TestTwoMachines(support.TwoMachines):
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


class TestAGitLevelConflict(support.TwoMachines):
    """Two machines that have each *committed* a change to the same lines.

    The path the mutation sweep found untested, and six of its survivors lived
    here: `sync.integrate`'s failure branch and the two `gitrepo` calls it makes.
    It is reached whenever a machine has an unpushed commit -- which `tupferl
    add` creates, since it commits without pushing -- and the remote has moved.

    git's own merge has the real merge base and is a better answer than anything
    this module could compute, so a conflict there is two *commits* disagreeing.
    That is not the conflict prompt's case, which settles `$HOME` against the
    repository over this machine's snapshot -- three files, where this needs the
    three index stages. What `sync` owes the user until then is a sentence naming
    the file, a way out, and a repository left exactly as it was found.
    """

    def commit_locally(self, machine: support.Computer, text: str) -> None:
        """Edit and `add`, which commits without pushing -- the ordinary way a
        machine ends up holding a commit the remote has not seen."""
        machine.write(".bashrc", text)
        self.assertEqual(0, machine.run("add", str(machine.home / ".bashrc")).returncode)

    def test_it_names_the_file_and_a_way_out(self) -> None:
        self.second.run("init", str(self.remote))
        self.commit_locally(self.second, "THEIRS\ntwo\nthree\nfour\nfive\n")
        self.first.write(".bashrc", "MINE\ntwo\nthree\nfour\nfive\n")
        self.assertEqual(0, self.first.run("sync").returncode)

        done = self.second.run("sync")
        self.assertEqual(2, done.returncode, done.stdout + done.stderr)
        self.assertIn(".bashrc", done.stderr)
        # The sentence has to be actionable, which is what plan §5 asks of every
        # error. `git pull` is the way out until tupferl settles this itself.
        self.assertIn("git", done.stderr)

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


class TestTheSnapshotIsWrittenLast(support.TwoMachines):
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
