"""`tupferl sync` driven the way a user drives it: a real CLI, a real remote.

The other half of `tests/test_sync.py`, which holds everything that can be
decided without a repository. See that module's docstring for why the two are
separate files rather than two halves of one -- it is worth 20 seconds per
mutant to a sweep.

What lives here is plan §7.4 item 4, the failure paths: a push the remote
rejected because it moved, a repository left half-merged, a path that cannot be
written, no remote at all -- plus the two-machine flows, which need two `$HOME`s
and so cannot use the one-machine fixture at all.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from unittest import mock

import pytest

from tests import support
from tupferl import copies, gitrepo, paths, sync

#: The ref every wording in `sync.crossed` names.
THERE = "origin/main"


def commit_locally(machine: support.Computer, text: str) -> None:
    """Edit and `add`, which commits without pushing -- the ordinary way a
    machine ends up holding a commit the remote has not seen."""
    machine.write(".bashrc", text)
    assert machine.run("add", str(machine.home / ".bashrc")).returncode == 0


def said(machine: support.Computer, *args: str) -> str:
    """A successful `sync` on `machine`, as the user would see it."""
    status, out = machine.say("sync", *args)
    assert status == 0, out
    return out


@dataclass(frozen=True)
class OneMachine(support.Machine):
    """`support.machine`, with one file already managed and a `sync` shorthand."""

    def sync(self, *flags: str) -> tuple[int, str, str]:
        done = self.run_cli("sync", *flags)
        return done.returncode, done.stdout, done.stderr


def subject(box: support.Machine) -> str:
    """The subject line of this repository's last commit."""
    return support.git(["log", "-1", "--format=%s"], box.repo, box.env)


@pytest.fixture
def one_machine(machine: support.Machine) -> OneMachine:
    box = OneMachine(**vars(machine))
    box.write(box.home / ".bashrc", "one\ntwo\nthree\n")
    box.init()
    assert box.run_cli("add", str(box.home / ".bashrc")).returncode == 0
    return box


@pytest.mark.usefixtures("one_machine")
class TestSyncOneMachine:
    def test_it_commits_and_pushes_what_add_stored(self, one_machine: OneMachine) -> None:
        status, _, _ = one_machine.sync()
        assert status == 0
        on_remote = support.git(
            ["ls-tree", "-r", "--name-only", support.BRANCH], one_machine.remote, one_machine.env
        )
        assert ".bashrc" in on_remote.splitlines()

    def test_the_snapshot_is_committed_so_a_restore_keeps_the_merge_base(
        self, one_machine: OneMachine
    ) -> None:
        """Plan §5: snapshots are per-host and committed, "so every host knows
        its own merge base". A snapshot only on disk is lost with the machine."""
        one_machine.sync()
        on_remote = support.git(
            ["ls-tree", "-r", "--name-only", support.BRANCH], one_machine.remote, one_machine.env
        )
        want = one_machine.snapshot(".bashrc").relative_to(one_machine.repo)
        assert str(want) in on_remote.splitlines()

    def test_a_home_edit_reaches_the_repository(self, one_machine: OneMachine) -> None:
        one_machine.sync()
        one_machine.write(one_machine.home / ".bashrc", "ONE\ntwo\nthree\n")
        status, out, _ = one_machine.sync()
        assert status == 0
        assert "stored .bashrc" in out
        assert (one_machine.repo / ".bashrc").read_text() == "ONE\ntwo\nthree\n"

    def test_the_commit_message_is_the_shape_the_plan_asks_for(
        self, one_machine: OneMachine
    ) -> None:
        """Plan §3.5: `sync from <hostname>: update .bashrc, .gitconfig`."""
        one_machine.sync()
        one_machine.write(one_machine.home / ".bashrc", "ONE\ntwo\nthree\n")
        one_machine.sync()
        subject = support.git(["log", "-1", "--format=%s"], one_machine.repo, one_machine.env)
        assert subject == f"sync from {one_machine.host}: .bashrc"

    def test_a_second_sync_writes_no_commit(self, one_machine: OneMachine) -> None:
        one_machine.sync()
        before = support.git(["rev-parse", "HEAD"], one_machine.repo, one_machine.env)
        status, out, _ = one_machine.sync()
        assert status == 0
        assert support.git(["rev-parse", "HEAD"], one_machine.repo, one_machine.env) == before
        assert "0 changed" in out

    def test_a_sync_with_nothing_to_push_does_not_reach_the_remote(
        self, one_machine: OneMachine
    ) -> None:
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
        one_machine.sync()
        nowhere = one_machine.tmp / "nowhere.git"
        support.git(
            ["remote", "set-url", "--push", "origin", str(nowhere)],
            one_machine.repo,
            one_machine.env,
        )

        assert one_machine.sync()[0] == 0, "a sync with nothing to push tried to push"

        one_machine.write(one_machine.home / ".bashrc", "ONE\ntwo\nthree\n")
        status, _, err = one_machine.sync()
        assert status == 2, "a sync with a change did not try to push"
        assert "could not push" in err

    def test_a_file_a_user_dropped_into_the_repository_is_committed(
        self, one_machine: OneMachine
    ) -> None:
        """`doctor` already promises this -- "`tupferl sync` will commit them" --
        and it is also how a copy an interrupted run left behind gets in."""
        one_machine.sync()
        one_machine.write(one_machine.repo / ".vimrc", "set nocompatible\n")
        status, _, _ = one_machine.sync()
        assert status == 0
        assert support.git(["status", "--porcelain"], one_machine.repo, one_machine.env) == ""

    def test_without_a_remote_it_still_syncs_and_says_so(self, one_machine: OneMachine) -> None:
        support.git(["remote", "remove", "origin"], one_machine.repo, one_machine.env)
        one_machine.write(one_machine.home / ".bashrc", "ONE\ntwo\nthree\n")
        status, out, _ = one_machine.sync()
        assert status == 0
        assert "no remote configured" in out
        assert (one_machine.repo / ".bashrc").read_text() == "ONE\ntwo\nthree\n"


@pytest.mark.usefixtures("one_machine")
class TestWhatSyncWillNotTouch:
    """`settle`'s two refusal branches. Both are the `manifest` admission rules
    applied again at sync time, for a path that has changed *kind* since it was
    added -- and both were unreached until the mutation sweep said so."""

    def test_a_symlink_in_the_repository_is_refused_rather_than_followed(
        self, one_machine: OneMachine
    ) -> None:
        """`manifest.managed` finds it, because `is_file()` follows links; the
        read does not, because it `lstat`s. Following it would copy whatever it
        points at into `$HOME` under a name the user never associated with it."""
        one_machine.sync()
        (one_machine.repo / ".vimrc").symlink_to(one_machine.repo / ".bashrc")
        support.git(["add", "-A"], one_machine.repo, one_machine.env)
        support.git(["commit", "-m", "a link crept in"], one_machine.repo, one_machine.env)

        status, out, _ = one_machine.sync()
        assert status == 1
        assert "skipped .vimrc" in out
        assert "not a regular file" in out
        assert not (one_machine.home / ".vimrc").exists(), "the link was followed into $HOME"

    def test_something_that_is_not_a_file_in_home_is_left_alone(
        self, one_machine: OneMachine
    ) -> None:
        """A fifo where a managed file should be. `os.mkfifo` rather than a unix
        socket: `sun_path` is 104 bytes on macOS and a sandboxed repository path
        exceeds it, so the socket version errors instead of testing."""
        one_machine.sync()
        (one_machine.home / ".bashrc").unlink()
        os.mkfifo(one_machine.home / ".bashrc")

        status, out, _ = one_machine.sync()
        assert status == 1
        assert "skipped .bashrc" in out
        assert stat.S_ISFIFO(os.lstat(one_machine.home / ".bashrc").st_mode)

    def test_a_detached_head_is_named_as_the_problem(self, one_machine: OneMachine) -> None:
        """A real state -- someone checked out a commit in the repository to look
        at it. Without this the branch is `None`, and what reaches the user is
        git complaining about a ref called "None"."""
        support.git(["checkout", "--detach", "HEAD"], one_machine.repo, one_machine.env)
        status, _, err = one_machine.sync()
        assert status == 2
        assert "no branch checked out" in err


@pytest.fixture
def two_managed(one_machine: OneMachine) -> OneMachine:
    """`one_machine` with a second file managed and everything synced."""
    one_machine.write(one_machine.home / ".vimrc", "set nocompatible\n")
    one_machine.run_cli("add", str(one_machine.home / ".vimrc"))
    one_machine.sync()
    return one_machine


@pytest.mark.usefixtures("two_managed")
class TestWhatTheCommitNames:
    """The commit message names what this run decided, and nothing else.

    Three survivors lived here, all of the same shape: a mutation that adds a
    name to the message is invisible to a fixture that manages *one* file, since
    "the file that changed" and "every managed file" are then the same list.
    """

    def test_only_the_file_that_changed_is_named(self, two_managed: OneMachine) -> None:
        two_managed.write(two_managed.home / ".bashrc", "ONE\ntwo\nthree\n")
        two_managed.sync()
        assert subject(two_managed) == f"sync from {two_managed.host}: .bashrc"

    def test_a_conflicted_file_is_not_named_as_something_that_changed(
        self, two_managed: OneMachine
    ) -> None:
        """A conflict writes nothing, so naming it in the commit would describe a
        change the commit does not contain. Needs a second file that *did*
        change, or there is no commit to inspect."""
        support.git(["checkout", "-q", "-b", "elsewhere"], two_managed.repo, two_managed.env)
        two_managed.write(two_managed.repo / ".vimrc", "set number\n")
        support.git(["commit", "-qam", "another machine"], two_managed.repo, two_managed.env)
        support.git(["checkout", "-q", "main"], two_managed.repo, two_managed.env)
        support.git(["merge", "-q", "--no-edit", "elsewhere"], two_managed.repo, two_managed.env)
        two_managed.write(two_managed.home / ".vimrc", "set nonumber\n")
        two_managed.write(two_managed.home / ".bashrc", "ONE\ntwo\nthree\n")

        status, out, _ = two_managed.sync()
        assert status == 1
        assert "conflict in .vimrc" in out
        assert subject(two_managed) == f"sync from {two_managed.host}: .bashrc"


@pytest.mark.usefixtures("one_machine")
class TestWhatStopsASync:
    """What a run refuses, and what it survives. The resolution flags are
    `tests/test_conflict_cli.py`, which needs a conflict to point them at."""

    @pytest.mark.parametrize("flag", ("--ours", "--theirs", "--no-input"))
    def test_the_resolution_flags_are_accepted_when_there_is_no_conflict(
        self, one_machine: OneMachine, flag: str
    ) -> None:
        """A smoke test, and no more than one: with nothing to settle it cannot
        see whether a settler was installed or called, because there is no
        conflict to hand one. What each flag *does* is
        `tests/test_sync_conflicts.py`, which has one."""
        assert one_machine.sync(flag)[0] == 0

    def test_an_unfinished_merge_stops_it(self, one_machine: OneMachine) -> None:
        """A killed sync leaves this behind, and committing on top of it would
        conclude a merge nobody resolved."""
        (one_machine.repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        status, _, err = one_machine.sync()
        assert status == 2
        assert "MERGE_HEAD" in err

    def test_a_path_that_cannot_be_written_is_reported_and_the_rest_go_on(
        self, one_machine: OneMachine
    ) -> None:
        """One bad path does not stop the run, and it does not arrive as a
        traceback. Here `$HOME/.config/nvim` is a *file*, so the directory the
        managed `init.lua` needs cannot be made."""
        one_machine.write(
            one_machine.home / ".config" / "nvim" / "init.lua", "vim.o.number = true\n"
        )
        support.run_cli(["add", str(one_machine.home / ".config" / "nvim")], one_machine.env)
        one_machine.sync()
        shutil.rmtree(one_machine.home / ".config" / "nvim")
        one_machine.write(one_machine.home / ".config" / "nvim", "not a directory\n")
        one_machine.write(one_machine.home / ".bashrc", "ONE\ntwo\nthree\n")

        status, out, err = one_machine.sync()
        assert status == 1
        assert "skipped .config/nvim/init.lua" in out
        assert "Traceback" not in err
        # The other file still synced.
        assert (one_machine.repo / ".bashrc").read_text() == "ONE\ntwo\nthree\n"


@pytest.mark.usefixtures("two_machines")
class TestTwoMachines:
    def test_init_on_the_second_machine_brings_everything_down(
        self, two_machines: support.TwoMachines
    ) -> None:
        """The README's promise: `tupferl init <url>` alone sets up a second
        computer, because plan §4 says init "then runs a first sync"."""
        done = two_machines.second.run("init", str(two_machines.remote))
        assert done.returncode == 0
        assert two_machines.second.read(".bashrc") == "one\ntwo\nthree\nfour\nfive\n"
        assert "restored .bashrc" in done.stdout

    def test_edits_that_do_not_overlap_merge_without_asking(
        self, two_machines: support.TwoMachines
    ) -> None:
        two_machines.second.run("init", str(two_machines.remote))
        two_machines.first.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        two_machines.second.write(".bashrc", "one\ntwo\nthree\nfour\nFIVE\n")
        assert two_machines.first.run("sync").returncode == 0
        done = two_machines.second.run("sync")
        assert done.returncode == 0
        assert "merged .bashrc" in done.stdout
        assert two_machines.second.read(".bashrc") == "ONE\ntwo\nthree\nfour\nFIVE\n"
        assert two_machines.first.run("sync").returncode == 0
        assert two_machines.first.read(".bashrc") == "ONE\ntwo\nthree\nfour\nFIVE\n"

    def test_a_real_conflict_is_reported_and_nothing_is_written(
        self, two_machines: support.TwoMachines
    ) -> None:
        """Milestone 3's whole answer to a conflict, and the thing milestone 4
        changes: both copies are left exactly as they were, and the exit status
        says a human is needed."""
        two_machines.second.run("init", str(two_machines.remote))
        two_machines.first.write(".bashrc", "MINE\ntwo\nthree\nfour\nfive\n")
        two_machines.second.write(".bashrc", "THEIRS\ntwo\nthree\nfour\nfive\n")
        two_machines.first.run("sync")

        done = two_machines.second.run("sync")
        assert done.returncode == 1
        assert "conflict in .bashrc" in done.stdout
        # Nothing written means nothing left uncommitted either: a conflict must
        # not leave the repository dirty for the next run to commit blindly.
        assert two_machines.second.git("status", "--porcelain") == ""
        assert two_machines.second.read(".bashrc") == "THEIRS\ntwo\nthree\nfour\nfive\n"
        assert two_machines.second.stored(".bashrc").read_text() == "MINE\ntwo\nthree\nfour\nfive\n"
        assert "<<<<<<<" not in two_machines.second.read(".bashrc")

    def test_a_conflict_leaves_the_snapshot_where_it_was(
        self, two_machines: support.TwoMachines
    ) -> None:
        """Otherwise the next run would merge against a state neither side ever
        had, and the conflict would resolve itself into one of the two by
        accident."""
        two_machines.second.run("init", str(two_machines.remote))
        was = two_machines.second.snapshot(".bashrc").read_text()
        two_machines.first.write(".bashrc", "MINE\ntwo\nthree\nfour\nfive\n")
        two_machines.second.write(".bashrc", "THEIRS\ntwo\nthree\nfour\nfive\n")
        two_machines.first.run("sync")
        two_machines.second.run("sync")
        assert two_machines.second.snapshot(".bashrc").read_text() == was

    def test_a_backup_is_taken_before_home_is_overwritten(
        self, two_machines: support.TwoMachines
    ) -> None:
        """Plan §5. The file being replaced is the user's, and this is the only
        copy of it that survives."""
        two_machines.second.run("init", str(two_machines.remote))
        two_machines.second.write(".bashrc", "local edit\ntwo\nthree\nfour\nfive\n")
        two_machines.first.write(".bashrc", "one\ntwo\nthree\nfour\nFIVE\n")
        two_machines.first.run("sync")
        two_machines.second.run("sync")

        saved = sorted(two_machines.second.backups.rglob(".bashrc"))
        assert len(saved) == 1, f"expected one backup, found {saved}"
        assert saved[0].read_text() == "local edit\ntwo\nthree\nfour\nfive\n"

    def test_the_executable_bit_reaches_the_other_machine(
        self, two_machines: support.TwoMachines
    ) -> None:
        two_machines.second.run("init", str(two_machines.remote))
        (two_machines.first.home / ".bashrc").chmod(copies.EXECUTABLE)
        two_machines.first.run("sync")
        two_machines.second.run("sync")
        assert (two_machines.second.home / ".bashrc").stat().st_mode & 0o777 == copies.EXECUTABLE

    def test_a_pruned_snapshot_is_named_and_leaves_no_empty_directory(
        self, two_machines: support.TwoMachines
    ) -> None:
        """git does not track directories, so one left empty here is invisible in
        the commit and present in every clone. The name is asserted too: this run
        did decide something about that file, and a commit message that named
        nothing would be the "an earlier run" sentence, which would be a lie."""
        two_machines.first.write(".config/nvim/init.lua", "vim.o.number = true\n")
        two_machines.first.run("add", str(two_machines.first.home / ".config"))
        two_machines.first.run("sync")
        two_machines.second.run("init", str(two_machines.remote))
        assert two_machines.second.snapshot(".config/nvim/init.lua").is_file()

        two_machines.first.run(
            "remove", str(two_machines.first.home / ".config" / "nvim" / "init.lua")
        )
        two_machines.first.run("sync")
        two_machines.second.run("sync")

        assert ".config/nvim/init.lua" in two_machines.second.git("log", "-1", "--format=%s")
        left = two_machines.second.snapshot(".config")
        assert not left.exists(), f"{left} was left behind empty"

    def test_a_removal_elsewhere_prunes_this_machines_snapshot(
        self, two_machines: support.TwoMachines
    ) -> None:
        """A snapshot left behind is committed, and would become the merge base
        for a file that came back later under the same name."""
        two_machines.second.run("init", str(two_machines.remote))
        assert two_machines.second.snapshot(".bashrc").is_file()
        two_machines.first.run("remove", str(two_machines.first.home / ".bashrc"))
        two_machines.first.run("sync")
        two_machines.second.run("sync")
        assert not two_machines.second.snapshot(".bashrc").exists()
        # Plan §4: `remove` keeps the file in $HOME -- on every machine.
        assert (two_machines.second.home / ".bashrc").is_file()

    def test_a_host_overlay_is_what_syncs_on_that_host(
        self, two_machines: support.TwoMachines
    ) -> None:
        """Plan §3.3: the overlay replaces the shared file on this host. Writing
        the shared copy instead would overwrite the overlay from the other
        machine's version on the next run."""
        two_machines.first.write(
            ".gitconfig", support.gitconfig("machine-a") + "[core]\n\tpager = less\n"
        )
        two_machines.first.run("add", "--host", str(two_machines.first.home / ".gitconfig"))
        two_machines.first.run("sync")
        shared = two_machines.first.stored(".gitconfig")
        overlay = two_machines.first.stored(".gitconfig", host=True)
        assert overlay.is_file()
        assert not shared.exists()

        two_machines.first.write(
            ".gitconfig", support.gitconfig("machine-a") + "[core]\n\tpager = bat\n"
        )
        assert two_machines.first.run("sync").returncode == 0
        assert "pager = bat" in overlay.read_text()

    def test_a_push_the_remote_rejected_is_retried_after_pulling(
        self, two_machines: support.TwoMachines
    ) -> None:
        """Plan §3.4 step 5. The remote genuinely moves inside the window between
        this sync's fetch and its push -- see `support.move_on_first_push`."""
        support.move_on_first_push(two_machines.remote, two_machines.first.env, two_machines.tmp)
        two_machines.first.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")

        done = two_machines.first.run("sync")
        assert done.returncode == 0, done.stdout + done.stderr
        # `support.git` strips, so the trailing newline is not in the comparison.
        on_remote = support.git(
            ["show", f"{support.BRANCH}:.bashrc"], two_machines.remote, two_machines.first.env
        )
        assert on_remote == "ONE\ntwo\nthree\nfour\nfive"
        # And what the other machine pushed is still there.
        settings = support.git(
            ["show", f"{support.BRANCH}:{paths.META}/config.toml"],
            two_machines.remote,
            two_machines.first.env,
        )
        assert "edited on another machine" in settings

    def test_a_push_refused_for_any_other_reason_is_reported(
        self, two_machines: support.TwoMachines
    ) -> None:
        """The other half of the retry: if nothing came in, pushing again would
        fail the same way, so it says so instead of looping."""
        support.git(
            ["remote", "set-url", "origin", str(two_machines.tmp / "gone.git")],
            two_machines.first.repo,
            two_machines.first.env,
        )
        two_machines.first.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        done = two_machines.first.run("sync")
        assert done.returncode == 2
        assert "could not fetch" in done.stderr


@pytest.mark.usefixtures("two_machines")
class TestAGitLevelConflict:
    """Two machines that have each *committed* a change to the same lines.

    The path the mutation sweep found untested, and six of its survivors lived
    here: `sync.integrate`'s failure branch and the two `gitrepo` calls it makes.
    It is reached whenever a machine has an unpushed commit -- which `tupferl
    add` creates, since it commits without pushing -- and the remote has moved.

    git's own merge has the real merge base and is a better answer than anything
    this module could compute, so a conflict there is two *commits* disagreeing.
    `sync.reconcile` settles those from the three index stages now, at the same
    prompt -- `tests/test_sync_commits.py` is where that is tested.

    **What is left here is the paths `reconcile` refuses**, and these two tests
    reach them because `support.run_cli` gives the child no terminal: with nobody
    to answer, the settler is `always(SKIP)`, nothing is settled, and the merge is
    undone. So this class still asserts what it always did -- a sentence naming
    the file, a way out, and a repository left exactly as it was found -- but the
    reason it gets there is the skip, not the absence of a prompt.
    """

    def test_it_names_the_file_and_a_way_out(self, two_machines: support.TwoMachines) -> None:
        two_machines.second.run("init", str(two_machines.remote))
        commit_locally(two_machines.second, "THEIRS\ntwo\nthree\nfour\nfive\n")
        two_machines.first.write(".bashrc", "MINE\ntwo\nthree\nfour\nfive\n")
        assert two_machines.first.run("sync").returncode == 0

        done = two_machines.second.run("sync")
        assert done.returncode == 2, done.stdout + done.stderr
        assert ".bashrc" in done.stderr
        # The sentence has to be actionable, which is what plan §5 asks of every
        # error, and `git pull` is the way out until tupferl settles this itself
        # (issue #10). The whole command, not the word "git": git's own stderr
        # reaches the user through `gitrepo.reason` on the *other* branch of this
        # function, and it contains "git" there too -- so the loose spelling
        # passed for a failure this test is not about.
        assert f"git -C {two_machines.second.repo} pull" in done.stderr

    def test_the_repository_is_left_exactly_as_it_was_found(
        self, two_machines: support.TwoMachines
    ) -> None:
        """The abort is the point. A half-merged tree makes the *next* run refuse
        to start, which turns one conflict into a machine that cannot sync at
        all -- and `sync` would then be committing on top of a merge nobody
        resolved."""
        two_machines.second.run("init", str(two_machines.remote))
        commit_locally(two_machines.second, "THEIRS\ntwo\nthree\nfour\nfive\n")
        two_machines.first.write(".bashrc", "MINE\ntwo\nthree\nfour\nfive\n")
        two_machines.first.run("sync")
        was = two_machines.second.git("rev-parse", "HEAD")

        assert two_machines.second.run("sync").returncode == 2
        assert gitrepo.unfinished(two_machines.second.repo) is None, "the merge was not aborted"
        assert two_machines.second.git("status", "--porcelain") == "", "the tree was left dirty"
        assert two_machines.second.git("rev-parse", "HEAD") == was
        assert "<<<<<<<" not in two_machines.second.stored(".bashrc").read_text()

    def test_a_merge_that_fails_without_conflicting_files_says_so_instead(
        self, two_machines: support.TwoMachines
    ) -> None:
        """The other half of `if stuck:`, and it needs its own fixture: a merge
        can fail with *no* unmerged paths. Pointing the machine at a remote with
        an unrelated history is the honest way to produce one -- git refuses
        outright, so there is no file to name, and a message that named none
        would read as a bug in the naming."""
        stranger = support.make_remote(two_machines.tmp / "stranger.git", two_machines.second.env)
        seed = support.make_repo(
            two_machines.tmp / "seed", two_machines.second.env, remote=stranger
        )
        assert seed.is_dir()
        two_machines.second.run("init", str(two_machines.remote))
        support.git(
            ["remote", "set-url", "origin", str(stranger)],
            two_machines.second.repo,
            two_machines.second.env,
        )

        done = two_machines.second.run("sync")
        assert done.returncode == 2, done.stdout + done.stderr
        assert "could not merge" in done.stderr
        assert gitrepo.unfinished(two_machines.second.repo) is None


@pytest.mark.usefixtures("two_machines")
class TestTheSnapshotIsWrittenLast:
    """Plan §7.4 item 4: a sync killed part-way must leave the state consistent.

    The ordering in `apply` is the whole of that guarantee, and it is invisible
    in a successful run -- so this makes the `$HOME` write *fail* and asserts
    that the snapshot did not move. Written the other way round, the snapshot
    would claim `$HOME` already held the new version, and the next run would copy
    the stale `$HOME` file over the new one: silent loss, one line away.
    """

    def test_a_failed_home_write_leaves_the_snapshot_alone(
        self, two_machines: support.TwoMachines
    ) -> None:
        two_machines.first.write(".config/nvim/init.lua", "vim.o.number = true\n")
        two_machines.first.run("add", str(two_machines.first.home / ".config" / "nvim"))
        two_machines.first.run("sync")
        two_machines.second.run("init", str(two_machines.remote))
        was = two_machines.second.snapshot(".config/nvim/init.lua").read_text()

        # `$HOME/.config/nvim` becomes a file, so the directory the managed
        # `init.lua` needs cannot be created and the write fails for real.
        shutil.rmtree(two_machines.second.home / ".config" / "nvim")
        two_machines.second.write(".config/nvim", "not a directory\n")
        two_machines.first.write(".config/nvim/init.lua", "vim.o.number = false\n")
        two_machines.first.run("sync")

        done = two_machines.second.run("sync")
        assert done.returncode == 1
        assert "skipped .config/nvim/init.lua" in done.stdout
        assert two_machines.second.snapshot(".config/nvim/init.lua").read_text() == was


@pytest.mark.usefixtures("two_machines")
class TestWhatSyncSaysAboutTheRemote:
    """#26: the command that reaches the remote has to say that it did.

    `sync` used to report only what it wrote in `$HOME`. The asymmetry is what
    made it a defect rather than a taste question: the *no remote* case already
    had a sentence of its own, so the tool spoke up in the harmless case and was
    silent in the one that matters -- and `status`, which only looks, reported
    the remote while `sync`, which changes it, did not.

    Every test here reads the line out of a real run rather than calling
    `crossed`, because "it is printed, above the file list" is half the claim.
    `TestTheRemoteLine` below is the other half: the four wordings, without a
    repository.
    """

    def test_a_push_is_reported(self, two_machines: support.TwoMachines) -> None:
        """The first sync of a new file, which is a user's first sync."""
        two_machines.first.write(".vimrc", "set number\n")
        assert two_machines.first.call("add", str(two_machines.first.home / ".vimrc")) == 0
        out = said(two_machines.first)
        assert "pushed" in out
        assert f"origin/{support.BRANCH}" in out

    def test_a_quiet_run_says_it_reached_the_remote_anyway(
        self, two_machines: support.TwoMachines
    ) -> None:
        """The run that used to print almost nothing, and the one where "did it
        work?" is hardest to answer from the output."""
        out = said(two_machines.first)
        assert "already up to date" in out
        assert "pushed" not in out

    def test_what_came_in_is_counted(self, two_machines: support.TwoMachines) -> None:
        """More than one, so a report saying "1" or "some" fails.

        The expected number is **asked of git rather than counted from the
        fixture's steps**. Written out as "2" it was wrong -- the other machine's
        own sync commits too, so three arrive -- and a number tied to how many
        commands this test happens to run is one that breaks whenever the fixture
        gains a step, for a reason that has nothing to do with the report.

        Not a copy of the code under test either: `sync` gets its number from
        `distance(HEAD, origin/main)` on *this* machine, and the test gets it
        from `rev-list --count` on the *other* one, whose HEAD is what arrives.
        Two commands, two repositories, one fact.
        """
        was = two_machines.first.git("rev-parse", "HEAD")
        assert two_machines.second.call("init", str(two_machines.remote)) == 0
        for name in (".vimrc", ".zshrc"):
            two_machines.second.write(name, f"# {name}\n")
            assert two_machines.second.call("add", str(two_machines.second.home / name)) == 0
        assert two_machines.second.call("sync") == 0

        coming = int(two_machines.second.git("rev-list", "--count", f"{was}..HEAD"))
        assert coming > 1, "the fixture brings in one commit, so it cannot show a count"
        assert f"took in {coming} commits" in said(two_machines.first)

    def test_the_line_comes_before_the_files(self, two_machines: support.TwoMachines) -> None:
        """Above the per-file list, not after it. It answers a different question
        from `report`'s, and burying it under the list is what made the old
        silence easy to miss."""
        assert two_machines.second.call("init", str(two_machines.remote)) == 0
        two_machines.second.write(".bashrc", "changed on the other machine\n")
        assert two_machines.second.call("sync") == 0
        rows = [row for row in said(two_machines.first).splitlines() if row.strip()]
        assert rows[0].startswith(f"origin/{support.BRANCH}:"), rows
        assert "updated .bashrc" in rows[1]

    def test_a_retry_counts_what_it_pulled_on_the_way(
        self, two_machines: support.TwoMachines
    ) -> None:
        """The remote moving mid-sync, which is the one run where both halves of
        `Traffic` are non-zero -- and the only thing that can tell `came +
        moved.pulled` from `came - moved.pulled`.

        The first `integrate` brings in nothing (the remote has not moved yet),
        so the count the line reports is entirely `deliver`'s retry. Both
        additions in the chain -- `pulled += came` inside `deliver` and the sum
        in `main` -- are unobservable without it, and the mutation sweep reported
        exactly that: `+` to `-` and `+=` to `-=`, both survivors.

        `support.move_on_first_push` makes the remote genuinely move inside the
        window between this sync's fetch and its push. Nothing is mocked.
        """
        support.move_on_first_push(two_machines.remote, two_machines.first.env, two_machines.tmp)
        two_machines.first.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        out = said(two_machines.first)
        assert "took in 1 commit" in out
        assert "pushed" in out

    def test_a_count_git_will_not_give_is_reported_as_arriving_anyway(
        self, two_machines: support.TwoMachines
    ) -> None:
        """`gitrepo.distance` answering `None` while a merge is about to happen.

        Real git will not do this here -- both refs resolve -- so it is forced by
        patching tupferl's own wrapper. The branch exists because the fallback
        must not be **zero**: zero is `integrate`'s word for "nothing came in",
        and `deliver` reads it as "the remote did not move, so the failed push is
        not worth re-trying". A merge reported as no merge turns a recoverable
        push into an error.

        One is low by one at worst. The sweep found this line twice, as `1`
        becoming `2` and `1` becoming `0`; only the second matters, and this is
        what refuses it.
        """
        assert two_machines.second.call("init", str(two_machines.remote)) == 0
        two_machines.second.write(".bashrc", "changed on the other machine\n")
        assert two_machines.second.call("sync") == 0
        with mock.patch.object(gitrepo, "distance", return_value=None):
            out = said(two_machines.first)
        assert "took in 1 commit" in out
        assert "updated .bashrc" in out

    def test_a_machine_with_no_remote_still_says_so(
        self, two_machines: support.TwoMachines
    ) -> None:
        """The sentence that was already there, and the reason this issue was
        visible at all. It must not have been replaced by the new one."""
        two_machines.first.git("remote", "remove", "origin")
        out = said(two_machines.first)
        assert "no remote configured" in out
        assert "already up to date" not in out


class TestTheRemoteLine:
    """`sync.crossed`'s four wordings, with no repository in it.

    Pure, so each of the four is a case rather than a fixture. The two-machine
    class above proves they are printed and where; this proves they are
    distinguishable, which a test that only ever built one of them could not.
    """

    def test_the_four_are_all_different(self) -> None:
        """As a set, because two wordings that collided would make one of the
        tests above pass for the wrong reason."""
        said = {
            sync.crossed(THERE, sync.Traffic(pulled=0, pushed=False)),
            sync.crossed(THERE, sync.Traffic(pulled=0, pushed=True)),
            sync.crossed(THERE, sync.Traffic(pulled=3, pushed=False)),
            sync.crossed(THERE, sync.Traffic(pulled=3, pushed=True)),
        }
        assert len(said) == 4, said

    def test_nothing_either_way_is_up_to_date(self) -> None:
        assert (
            sync.crossed(THERE, sync.Traffic(pulled=0, pushed=False))
            == "origin/main: already up to date"
        )

    def test_the_plural_is_counted(self) -> None:
        """One and many, because `1 commits` is the mistake this shape invites."""
        assert "1 commit" in sync.crossed(THERE, sync.Traffic(1, False))
        assert "1 commits" not in sync.crossed(THERE, sync.Traffic(1, False))
        assert "2 commits" in sync.crossed(THERE, sync.Traffic(2, False))

    @pytest.mark.parametrize(
        "traffic",
        (
            sync.Traffic(0, False),
            sync.Traffic(0, True),
            sync.Traffic(2, False),
            sync.Traffic(2, True),
        ),
    )
    def test_every_line_names_the_ref(self, traffic: sync.Traffic) -> None:
        """A sentence that said "pushed" without saying where would be true of a
        machine pushing to the wrong remote."""
        assert sync.crossed(THERE, traffic).startswith(THERE)
