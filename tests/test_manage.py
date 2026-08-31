"""`init`, `add`, `remove`, `list`, driven as a user drives them.

Real `git`, a real bare repository standing in for the remote, and the CLI in a
subprocess wherever the exit status or the printed output is the thing under
test. Plan §7.1 forbids mocking git, and these commands are almost entirely
*about* git: a mock would assert that this code calls the functions it calls,
which is true by construction and interesting to nobody.

The two-machine fixture is the one worth knowing about. `TestTwoMachines` gives
each host its own `$HOME` and its own clone of one bare remote, which is the
only shape in which "this host's overlay" means anything — a single-machine test
cannot tell an overlay that works from one that silently applies everywhere.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest

from tests import support
from tupferl import __main__ as cli
from tupferl import copies, gitrepo, manage, paths
from tupferl.errors import TupferlError


def quietly(run: object) -> int:
    """Run a command function with its output swallowed, and hand back its status."""
    with support.quiet():
        return int(run())  # type: ignore[operator]


@pytest.fixture
def started(machine: support.Machine) -> support.Machine:
    """A machine that has run `init` against its own bare remote."""
    machine.init()
    return machine


@pytest.fixture
def holding(machine: support.Machine) -> support.Machine:
    """A machine with `.bashrc` in `$HOME` and **no repository yet**.

    Deliberately not `started`: the class that takes this asserts the status
    `init` itself returns, so it has to run it.
    """
    machine.write(machine.home / ".bashrc", "x")
    return machine


@pytest.fixture
def ready(holding: support.Machine) -> support.Machine:
    """`holding`, with `init` run -- so the file is there and so is a repository."""
    holding.init()
    return holding


@pytest.fixture
def managed(started: support.Machine) -> support.Machine:
    """`started`, with `.bashrc` written and already added."""
    started.write(started.home / ".bashrc", "keep me\n")
    started.run_cli("add", str(started.home / ".bashrc"))
    return started


@pytest.mark.usefixtures("machine")
class TestInit:
    def test_an_empty_remote_is_cloned_and_given_a_first_commit(
        self, machine: support.Machine
    ) -> None:
        """The first-run path: the user has just created an empty repository on
        their host. A clone with no commits sits on an unborn branch, the one
        state where `HEAD` does not resolve, so `init` normalises it."""
        done = machine.init()
        assert "cloned" in done.stdout
        assert gitrepo.is_repository(machine.repo)
        assert gitrepo.has_commits(machine.repo)

    def test_the_first_commit_is_empty_and_writes_no_file(self, machine: support.Machine) -> None:
        """It used to write `.tupferl/config.toml` and commit that, because git
        needed *something*. The settings are a dotfile in `$HOME` now, and
        inventing a file for git's benefit is how a repository grows one nobody
        asked for -- so the commit is empty and the tree is too."""
        machine.init()
        assert not paths.config_file().exists(), "init wrote settings into $HOME"
        listed = support.git(["ls-files"], machine.repo, machine.env)
        assert listed == "", "the first commit carries a file"

    def test_a_remote_with_content_is_cloned_and_then_synced(
        self, machine: support.Machine
    ) -> None:
        """The second-machine path, and the README's one-line promise.

        No settings file is invented -- the repository already has a shape -- but
        plan §4 says `init` "then runs a first sync", and milestone 3 made that
        true. So the file arrives in `$HOME`, and the one commit `init` adds is
        this host's merge base: without it the machine could not merge anything
        later, because it would have no common ancestor to merge against.
        """
        first = support.make_repo(machine.tmp / "seed", machine.env, remote=machine.remote)
        (first / ".bashrc").write_text("export EDITOR=nvim\n", encoding="utf-8")
        support.git(["add", "-A"], first, machine.env)
        support.git(["commit", "-m", "seeded"], first, machine.env)
        support.git(["push"], first, machine.env)

        machine.init()
        stored = (machine.repo / ".bashrc").read_text(encoding="utf-8")
        assert stored == "export EDITOR=nvim\n"
        assert (machine.home / ".bashrc").read_text() == "export EDITOR=nvim\n"
        assert machine.log() == [f"sync from {machine.host}: .bashrc", "seeded", "initial"]
        assert not paths.config_file().is_file()

    def test_a_url_that_cannot_be_cloned_is_reported(self, machine: support.Machine) -> None:
        """And nothing is created. The alternative — falling back to a local
        repository pointed at the URL — hides a typo until the first sync, by
        which time the user has added files and believes they are backed up."""
        done = machine.run_cli("init", str(machine.tmp / "absent.git"))
        assert done.returncode == 2
        assert "could not clone" in done.stderr
        assert not gitrepo.is_repository(machine.repo)

    def test_an_empty_directory_in_the_way_is_cloned_into(self, machine: support.Machine) -> None:
        """`~/.local/share/tupferl/` exists on any machine that has run
        `doctor`, and an empty directory is not something to refuse. It is also
        what tells `any(...)` from `all(...)`: `all([])` is True, so that mutant
        turns every ordinary first run into "already exists and is not empty"."""
        machine.repo.mkdir(parents=True)
        machine.init()
        assert gitrepo.has_commits(machine.repo)

    def test_the_parent_directory_already_existing_is_fine(self, machine: support.Machine) -> None:
        """The state `doctor` leaves behind when it checks the backup path: the
        XDG data directory is there and the repository is not."""
        machine.repo.parent.mkdir(parents=True)
        machine.init()
        assert gitrepo.is_repository(machine.repo)

    def test_the_clone_failure_quotes_the_line_that_explains(
        self, machine: support.Machine
    ) -> None:
        """Neither the first line nor the last, and this fixture is why.

        `git clone` writes progress to stderr, so its *first* line is "Cloning
        into '...'" — which says nothing went wrong, and is what `init` reported
        for about an hour until this test was written. Its *last* is "and the
        repository exists.", half a sentence of generic advice, which is what
        `doctor` reported for a milestone. `gitrepo.reason` takes the line git
        marked `fatal:` instead.

        The URL is ssh at a refused local port rather than the missing directory
        the test above uses: a local path that is not a repository produces one
        line, and one line cannot tell any of these rules apart. No packet
        leaves the machine — 127.0.0.1:1 refuses instantly.
        """
        unreachable = "ssh://127.0.0.1:1/x"
        done = machine.run_cli("init", unreachable)
        assert done.returncode == 2

        said = gitrepo.git(["clone", "--", unreachable, str(machine.tmp / "x")], cwd=machine.tmp)
        lines = [line.strip() for line in said.err.splitlines() if line.strip()]
        assert len(lines) > 2, "the fixture produced too few lines to tell these apart"
        assert lines[0].startswith("Cloning into"), lines[0]

        assert gitrepo.reason(said) in done.stderr
        assert gitrepo.reason(said).startswith("fatal:"), gitrepo.reason(said)
        assert "Cloning into" not in done.stderr
        assert lines[-1] not in done.stderr

    def test_running_it_twice_is_refused(self, machine: support.Machine) -> None:
        machine.init()
        done = machine.run_cli("init", str(machine.remote))
        assert done.returncode == 2
        assert "already a tupferl repository" in done.stderr

    def test_a_first_commit_that_fails_is_reported(self, machine: support.Machine) -> None:
        """`init` on an empty remote makes the repository's first commit, and a
        machine whose hooks refuse it cannot. Without the guard `init` reports
        success and leaves a clone with no branch — the one state where `HEAD`
        does not resolve, which is what the commit exists to avoid."""
        support.break_commits(machine.home)
        done = machine.run_cli("init", str(machine.remote))
        assert done.returncode == 2
        assert "could not make the first commit" in done.stderr

    def test_a_file_where_the_repository_belongs_is_refused(self, machine: support.Machine) -> None:
        """One stray `touch` produces this, and `iterdir` raises
        `NotADirectoryError` on it -- a traceback where a sentence belongs."""
        machine.repo.parent.mkdir(parents=True, exist_ok=True)
        machine.repo.write_text("not a directory", encoding="utf-8")
        done = machine.run_cli("init", str(machine.remote))
        assert done.returncode == 2
        assert "not a directory" in done.stderr
        assert "Traceback" not in done.stderr

    def test_a_non_empty_directory_in_the_way_is_refused(self, machine: support.Machine) -> None:
        """Not cloned over. Whatever is there was put there by someone, and the
        message says to move it rather than doing it for them."""
        machine.repo.mkdir(parents=True)
        (machine.repo / "stray").write_text("mine", encoding="utf-8")
        done = machine.run_cli("init", str(machine.remote))
        assert done.returncode == 2
        assert "not empty" in done.stderr
        assert (machine.repo / "stray").read_text(encoding="utf-8") == "mine"


@pytest.mark.usefixtures("started")
class TestAdd:
    def test_a_file_is_copied_and_committed(self, started: support.Machine) -> None:
        started.write(started.home / ".bashrc", "export EDITOR=nvim\n")
        done = started.run_cli("add", str(started.home / ".bashrc"))
        assert done.returncode == 0, done.stdout + done.stderr
        assert started.stored(".bashrc").read_text(encoding="utf-8") == "export EDITOR=nvim\n"
        assert "add from test-host: .bashrc" in started.log()

    def test_the_repository_is_left_clean(self, started: support.Machine) -> None:
        """Everything commits immediately, so `doctor`'s "uncommitted changes"
        stays a real signal that a run was interrupted."""
        started.write(started.home / ".bashrc", "x")
        started.run_cli("add", str(started.home / ".bashrc"))
        assert support.git(["status", "--porcelain"], started.repo, started.env) == ""

    def test_a_directory_adds_every_file_under_it(self, started: support.Machine) -> None:
        """Two files in the *same* subdirectory, and a third one deeper. The
        pair sharing a parent is what tells `mkdir(exist_ok=True)` from dropping
        it: with distinct parents throughout, the second `mkdir` never sees a
        directory that is already there."""
        started.write(started.home / ".config" / "nvim" / "init.lua", "vim.opt.number = true\n")
        started.write(started.home / ".config" / "nvim" / "other.lua", "return {}\n")
        started.write(started.home / ".config" / "nvim" / "lua" / "opts.lua", "return {}\n")
        done = started.run_cli("add", str(started.home / ".config"))
        assert done.returncode == 0, done.stdout + done.stderr
        assert started.stored(".config/nvim/init.lua").is_file()
        assert started.stored(".config/nvim/other.lua").is_file()
        assert started.stored(".config/nvim/lua/opts.lua").is_file()

    def test_the_executable_bit_survives_and_nothing_else_does(
        self, started: support.Machine
    ) -> None:
        """Plan §5. 0o700 in, 0o755 stored: the bit is kept, the rest is not,
        because git records exactly one bit and any other would be lost on the
        first clone."""
        script = started.write(started.home / ".local" / "bin" / "hello", "#!/bin/sh\necho hi\n")
        script.chmod(0o700)
        started.run_cli("add", str(script))
        assert stat.S_IMODE(started.stored(".local/bin/hello").stat().st_mode) == 0o755

    def test_a_plain_file_is_stored_unexecutable(self, started: support.Machine) -> None:
        started.write(started.home / ".bashrc", "x")
        started.run_cli("add", str(started.home / ".bashrc"))
        assert stat.S_IMODE(started.stored(".bashrc").stat().st_mode) == 0o644

    def test_a_named_path_that_is_refused_stops_the_whole_run(
        self, started: support.Machine
    ) -> None:
        """They asked for that file by name. A run that skipped it and committed
        the others would leave them believing it was stored."""
        started.write(started.home / ".bashrc", "x")
        (started.home / ".linked").symlink_to(started.tmp / "elsewhere")
        done = started.run_cli("add", str(started.home / ".bashrc"), str(started.home / ".linked"))
        assert done.returncode == 2
        assert "symlink" in done.stderr
        assert not started.stored(".bashrc").exists(), "it committed some of them anyway"

    def test_a_file_found_by_walking_is_skipped_and_reported(
        self, started: support.Machine
    ) -> None:
        """The other half: adding `~/.config` with one socket in it must manage
        the rest, and say what it did not."""
        started.write(started.home / ".config" / "good.conf", "x")
        (started.home / ".config" / "linked").symlink_to(started.tmp / "elsewhere")
        done = started.run_cli("add", str(started.home / ".config"))
        assert done.returncode == 0, done.stdout + done.stderr
        assert "skipped" in done.stdout
        assert started.stored(".config/good.conf").is_file()

    def test_re_adding_an_unchanged_file_changes_nothing(self, started: support.Machine) -> None:
        """No commit, and it says so. `add` is how someone re-stores a file they
        have edited; this is what it does when they had not."""
        started.write(started.home / ".bashrc", "x")
        started.run_cli("add", str(started.home / ".bashrc"))
        before = started.log()
        done = started.run_cli("add", str(started.home / ".bashrc"))
        assert done.returncode == 0
        assert "no change" in done.stdout
        assert started.log() == before

    def test_re_adding_an_edited_file_says_updated(self, started: support.Machine) -> None:
        """The precondition for the test above: "no change" has to be observable
        against a run that does change something."""
        started.write(started.home / ".bashrc", "x")
        started.run_cli("add", str(started.home / ".bashrc"))
        started.write(started.home / ".bashrc", "y")
        done = started.run_cli("add", str(started.home / ".bashrc"))
        assert "updated .bashrc" in done.stdout
        assert started.stored(".bashrc").read_text(encoding="utf-8") == "y"

    def test_a_mode_change_alone_is_a_change(self, started: support.Machine) -> None:
        """`chmod +x` with no edit is a real change git will record, so a
        comparison on contents alone would silently drop it."""
        script = started.write(started.home / "s.sh", "#!/bin/sh\n")
        started.run_cli("add", str(script))
        script.chmod(0o755)
        done = started.run_cli("add", str(script))
        assert "updated" in done.stdout
        assert stat.S_IMODE(started.stored("s.sh").stat().st_mode) == 0o755

    def test_home_itself_cannot_be_added(self, started: support.Machine) -> None:
        """The most extreme form of "contains the repository": adding `$HOME`
        would walk into `~/.local/share/tupferl/repo` and manage tupferl's own
        copies of everything, recursively."""
        done = started.run_cli("add", str(started.home))
        assert done.returncode == 2
        assert "own repository" in done.stderr

    def test_a_large_file_is_compared_without_reading_it_whole(
        self, started: support.Machine
    ) -> None:
        """`max_file_size` is a setting and someone will raise it, so the
        unchanged-file comparison must not load both copies into memory. This
        asserts the *answer* rather than the mechanism -- a megabyte is not
        enough to prove the memory claim, but it does prove `filecmp` was given
        the two paths correctly, which is what a swap to it can get wrong."""
        big = started.write(started.home / ".big", "x" * 900_000)
        assert started.run_cli("add", str(big)).returncode == 0
        again = started.run_cli("add", str(big))
        assert "no change" in again.stdout
        started.write(started.home / ".big", "y" * 900_000)
        assert "updated" in started.run_cli("add", str(big)).stdout

    def test_a_file_whose_name_begins_with_a_dash(self, started: support.Machine) -> None:
        """`git add -- -x` is why `stage` passes `--`. Without it git reports
        "unknown switch" for a dotfile somebody really has, and the guard is
        otherwise a line nobody can tell works."""
        started.write(started.home / "-dashfile", "x")
        done = started.run_cli("add", str(started.home / "-dashfile"))
        assert done.returncode == 0, done.stdout + done.stderr
        assert "add from test-host: -dashfile" in started.log()
        gone = started.run_cli("remove", str(started.home / "-dashfile"))
        assert gone.returncode == 0, gone.stdout + gone.stderr

    def test_overlapping_paths_are_stored_once(self, started: support.Machine) -> None:
        """A directory and a file inside it, named in the same run. The commit
        message and the printed list both come from this set, so a duplicate
        would be visible twice in `git log`."""
        started.write(started.home / ".config" / "nvim" / "init.lua", "x")
        done = started.run_cli(
            "add",
            str(started.home / ".config"),
            str(started.home / ".config" / "nvim" / "init.lua"),
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert done.stdout.count("init.lua") == 1, done.stdout
        assert started.log()[0].count("init.lua") == 1, started.log()[0]

    def test_the_order_it_stores_in_does_not_depend_on_the_argument_order(
        self, started: support.Machine
    ) -> None:
        """Two files named in reverse order. The set `add` iterates is a dict
        built in argument order, so this is the fixture that tells a sort from
        no sort — and the order reaches the user twice, in what is printed and
        in the commit message."""
        started.write(started.home / ".zshrc", "z")
        started.write(started.home / ".aaa", "a")
        done = started.run_cli("add", str(started.home / ".zshrc"), str(started.home / ".aaa"))
        assert done.returncode == 0, done.stdout + done.stderr
        assert done.stdout.index(".aaa") < done.stdout.index(".zshrc"), done.stdout
        assert started.log()[0] == "add from test-host: .aaa, .zshrc"

    def test_a_directory_where_everything_is_refused_says_so(
        self, started: support.Machine
    ) -> None:
        """Not "no change", which is what an empty admitted set would otherwise
        fall through to — and which reads as "already stored" rather than
        "stored nothing"."""
        (started.home / ".config" / "one").symlink_to(started.tmp / "elsewhere")
        (started.home / ".config" / "two").symlink_to(started.tmp / "elsewhere")
        done = started.run_cli("add", str(started.home / ".config"))
        assert done.returncode == 2
        assert "nothing to add" in done.stderr

    def test_a_failing_commit_is_reported_rather_than_ignored(
        self, started: support.Machine
    ) -> None:
        """A brand-new machine with no git identity: `git commit` refuses, and
        without the guard `add` would report success having stored nothing.

        The fixture is a `pre-commit` hook that refuses -- see
        `support.break_commits` for why not the missing-identity state this test
        was originally written against.
        """
        support.break_commits(started.home)
        started.write(started.home / ".bashrc", "x")
        done = started.run_cli("add", str(started.home / ".bashrc"))
        assert done.returncode == 2
        assert "could not commit" in done.stderr

    def test_a_failing_stage_is_reported_rather_than_ignored(
        self, started: support.Machine
    ) -> None:
        """A corrupted index: `.git/index` replaced by a directory, so `git add`
        cannot map it. Without the guard `add` walks on and commits nothing
        while reporting success.

        A directory rather than a `chmod`, because the suite runs as root in
        some containers and root ignores the mode bits.
        """
        index = started.repo / ".git" / "index"
        index.unlink()
        index.mkdir()
        started.write(started.home / ".bashrc", "x")
        done = started.run_cli("add", str(started.home / ".bashrc"))
        assert done.returncode == 2
        assert "could not stage" in done.stderr

    def test_a_commit_failure_is_reduced_to_the_line_that_explains(
        self, started: support.Machine
    ) -> None:
        """Not the whole stderr blob.

        `gitrepo.reason` exists for this, and `add` and `remove` interpolated
        raw `.err` while `init` forty lines earlier did not -- three copies of
        one block, drifted. They are one helper now, and this is the assertion
        that would have noticed: the hook writes two lines, and only the first
        may reach the user.
        """
        support.break_commits(started.home)
        started.write(started.home / ".bashrc", "x")
        done = started.run_cli("add", str(started.home / ".bashrc"))
        assert done.returncode == 2
        assert support.HOOK_REFUSED in done.stderr
        assert support.HOOK_TRAILER not in done.stderr

    def test_it_needs_a_repository(self, started: support.Machine) -> None:
        """Run in a home where `init` never was."""
        with support.tempdir() as box:
            home = box / "home"
            home.mkdir()
            support.seed_home(home)
            env = support.sandbox_env(home)
            (home / ".bashrc").write_text("x", encoding="utf-8")
            done = support.run_cli(["add", str(home / ".bashrc")], env)
        assert done.returncode == 2
        # On "no repository at", not on "tupferl init": *both* of `open_repo`'s
        # messages end in that command, so asserting it alone passes with the
        # existence check removed entirely. The mutation sweep found this, in
        # the same shape it found in `doctor.repository` a milestone ago.
        assert "no repository at" in done.stderr


@pytest.mark.usefixtures("managed")
class TestRemove:
    def test_the_copy_goes_and_the_original_stays(self, managed: support.Machine) -> None:
        done = managed.run_cli("remove", str(managed.home / ".bashrc"))
        assert done.returncode == 0, done.stdout + done.stderr
        assert not managed.stored(".bashrc").exists()
        assert (managed.home / ".bashrc").read_text(encoding="utf-8") == "keep me\n"
        assert "remove from test-host: .bashrc" in managed.log()

    def test_a_file_already_deleted_from_home_can_still_be_removed(
        self, managed: support.Machine
    ) -> None:
        """Often the reason someone reaches for it: the file is gone locally and
        they want the repository to stop pushing it to the other machine.
        Requiring existence would refuse exactly then."""
        (managed.home / ".bashrc").unlink()
        done = managed.run_cli("remove", str(managed.home / ".bashrc"))
        assert done.returncode == 0, done.stdout + done.stderr
        assert not managed.stored(".bashrc").exists()

    def test_directories_left_empty_are_pruned(self, managed: support.Machine) -> None:
        """git does not track directories, so an empty one is invisible in the
        commit and present in every clone — `~/.config/nvim/` with nothing in
        it, on a machine that never used nvim."""
        managed.write(managed.home / ".config" / "nvim" / "init.lua", "x")
        managed.run_cli("add", str(managed.home / ".config"))
        managed.run_cli("remove", str(managed.home / ".config" / "nvim" / "init.lua"))
        assert not (managed.repo / ".config").exists()

    def test_the_repository_root_is_never_pruned(self, managed: support.Machine) -> None:
        """The loop stops at the repository. Removing the last managed file must
        not delete the repository out from under the user."""
        managed.run_cli("remove", str(managed.home / ".bashrc"))
        assert gitrepo.is_repository(managed.repo)

    def test_pruning_stops_at_the_repository_even_from_deep_inside(
        self, managed: support.Machine
    ) -> None:
        """The loop deletes directories and walks upwards. This is the deepest
        tree the tests build, so it is the one that would climb furthest if the
        stop condition were wrong -- and `$HOME` is what sits above it."""
        managed.write(managed.home / ".config" / "a" / "b" / "c" / "deep.conf", "x")
        managed.run_cli("add", str(managed.home / ".config"))
        managed.run_cli("remove", str(managed.home / ".config" / "a" / "b" / "c" / "deep.conf"))
        assert not (managed.repo / ".config").exists()
        assert managed.repo.is_dir(), "it pruned the repository itself"
        assert managed.home.is_dir(), "it climbed out of the repository"

    def test_pruning_stops_at_a_directory_that_still_holds_something(
        self, managed: support.Machine
    ) -> None:
        """The ordinary case, and the one that tells `and` from `or` in a loop
        that calls `rmdir`: with `or`, the walk enters a non-empty directory and
        `rmdir` raises."""
        managed.write(managed.home / ".config" / "nvim" / "init.lua", "x")
        managed.write(managed.home / ".config" / "nvim" / "other.lua", "x")
        managed.run_cli("add", str(managed.home / ".config"))
        done = managed.run_cli("remove", str(managed.home / ".config" / "nvim" / "init.lua"))
        assert done.returncode == 0, done.stdout + done.stderr
        assert (managed.repo / ".config" / "nvim" / "other.lua").is_file()
        assert "Traceback" not in done.stderr

    def test_a_failing_stage_during_removal_is_reported(self, managed: support.Machine) -> None:
        """Same corrupted index, on the other command. The copy is already gone
        from the working tree by the time staging fails, so a run that ignored
        it would leave the repository committed-clean and the file still
        tracked."""
        index = managed.repo / ".git" / "index"
        index.unlink()
        index.mkdir()
        done = managed.run_cli("remove", str(managed.home / ".bashrc"))
        assert done.returncode == 2
        assert "could not stage" in done.stderr

    def test_a_failing_commit_during_removal_is_reported(self, managed: support.Machine) -> None:
        """A `pre-commit` hook that refuses, which is a real thing to have and,
        unlike a missing git identity, fails the same way on every platform."""
        support.break_commits(managed.home)
        done = managed.run_cli("remove", str(managed.home / ".bashrc"))
        assert done.returncode == 2
        assert "could not commit" in done.stderr

    def test_a_commit_failure_during_removal_is_reduced_too(self, managed: support.Machine) -> None:
        """The other half of the same helper. One assertion per call site,
        because the drift this replaced was per call site."""
        support.break_commits(managed.home)
        done = managed.run_cli("remove", str(managed.home / ".bashrc"))
        assert done.returncode == 2
        assert support.HOOK_REFUSED in done.stderr
        assert support.HOOK_TRAILER not in done.stderr

    def test_removing_something_unmanaged_is_an_error(self, managed: support.Machine) -> None:
        done = managed.run_cli("remove", str(managed.home / ".never-added"))
        assert done.returncode == 2
        assert "not managed" in done.stderr

    def test_removing_something_outside_home_is_an_error(self, managed: support.Machine) -> None:
        done = managed.run_cli("remove", "/etc/hostname")
        assert done.returncode == 2
        assert "outside" in done.stderr


@pytest.mark.usefixtures("started")
class TestList:
    def test_an_empty_repository_says_so(self, started: support.Machine) -> None:
        done = started.run_cli("status", "--all")
        assert done.returncode == 0
        assert "nothing is managed" in done.stdout

    def test_managed_files_are_listed(self, started: support.Machine) -> None:
        started.write(started.home / ".bashrc", "x")
        started.write(started.home / ".config" / "nvim" / "init.lua", "x")
        started.run_cli("add", str(started.home / ".bashrc"), str(started.home / ".config"))
        done = started.run_cli("status", "--all")
        assert ".bashrc" in done.stdout
        assert ".config/nvim/init.lua" in done.stdout
        assert "2 files managed" in done.stdout

    def test_the_settings_file_is_not_listed_as_managed(self, started: support.Machine) -> None:
        """`init` committed `.tupferl/config.toml`. It is tupferl's, not a
        dotfile, and listing it would also make it removable by name."""
        done = started.run_cli("status", "--all")
        assert "config.toml" not in done.stdout

    def test_an_overlay_file_is_marked(self, started: support.Machine) -> None:
        started.write(started.home / ".gitconfig", "[user]\n")
        started.run_cli("add", "--host", str(started.home / ".gitconfig"))
        done = started.run_cli("status", "--all")
        assert "host  .gitconfig" in done.stdout
        assert "1 from this host's overlay" in done.stdout


@dataclass(frozen=True)
class Hosts(support.Sandbox):
    """One remote and two initialised homes, keyed by hostname."""

    remote: Path
    homes: dict[str, Path]
    envs: dict[str, dict[str, str]]

    def repo_of(self, host: str) -> Path:
        """That host's repository, asked of `tupferl.paths` under its own environment.

        **This used to spell the layout out** -- `XDG_DATA_HOME / "tupferl" /
        "repo"` -- with a docstring saying `paths.repo_dir()` "cannot be used:
        it reads the ambient environment, which here belongs to neither
        machine". That claim was false, and `support.Computer.__init__` had been
        disproving it since it was written: it asks the same question inside
        `mock.patch.dict(os.environ, ..., clear=True)`, with a comment saying
        exactly why a test that spells the layout out itself cannot notice the
        layout changing.
        """
        with mock.patch.dict(os.environ, self.envs[host], clear=True):
            return paths.repo_dir()


@pytest.fixture
def hosts(sandbox: support.Sandbox) -> Hosts:
    remote = support.make_remote(sandbox.tmp / "remote.git", sandbox.env)
    homes: dict[str, Path] = {}
    envs: dict[str, dict[str, str]] = {}
    for host in ("laptop", "desktop"):
        home = sandbox.tmp / host
        home.mkdir()
        support.seed_home(home, host)
        homes[host] = home
        envs[host] = support.sandbox_env(home, host)
        done = support.run_cli(["init", str(remote)], envs[host])
        assert done.returncode == 0, done.stdout + done.stderr
    return Hosts(**vars(sandbox), remote=remote, homes=homes, envs=envs)


@pytest.mark.usefixtures("hosts")
class TestTwoMachines:
    """One remote, two homes, two hostnames.

    A host overlay that silently applied everywhere would pass every
    single-machine test in this file. This is the fixture that can tell the
    difference, so it is the one the overlay's guarantee is asserted against.
    """

    def test_each_host_writes_into_its_own_overlay(self, hosts: Hosts) -> None:
        for host in ("laptop", "desktop"):
            (hosts.homes[host] / ".gitconfig").write_text(support.gitconfig(host), "utf-8")
            done = support.run_cli(
                ["add", "--host", str(hosts.homes[host] / ".gitconfig")], hosts.envs[host]
            )
            assert done.returncode == 0, done.stdout + done.stderr

        for host in ("laptop", "desktop"):
            overlay = paths.host_overlay(hosts.repo_of(host), host) / ".gitconfig"
            assert overlay.is_file(), f"{host} did not write its own overlay"
            assert host in overlay.read_text(encoding="utf-8")

    def test_a_host_lists_only_its_own_overlay(self, hosts: Hosts) -> None:
        """The assertion a single-machine test cannot make. `desktop`'s overlay
        is put into `laptop`'s repository by hand — as a sync would — and must
        not appear in `laptop`'s listing."""
        laptop = hosts.repo_of("laptop")
        theirs = paths.host_overlay(laptop, "desktop") / ".gitconfig"
        theirs.parent.mkdir(parents=True, exist_ok=True)
        theirs.write_text(support.gitconfig("desktop"), encoding="utf-8")

        done = support.run_cli(["status", "--all"], hosts.envs["laptop"])
        assert "nothing is managed" in done.stdout

    def test_an_overlay_replaces_the_shared_file_for_that_host(self, hosts: Hosts) -> None:
        """Plan §3.3, from the listing's point of view: one name, marked."""
        laptop = hosts.repo_of("laptop")
        (laptop / ".gitconfig").write_text(support.gitconfig("shared"), encoding="utf-8")
        mine = paths.host_overlay(laptop, "laptop") / ".gitconfig"
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text(support.gitconfig("laptop"), encoding="utf-8")

        done = support.run_cli(["status", "--all"], hosts.envs["laptop"])
        assert done.stdout.count(".gitconfig") == 1, done.stdout
        assert "host  .gitconfig" in done.stdout


@pytest.mark.usefixtures("holding")
class TestTheExitStatusEachCommandReturns:
    """Every command returns the status rather than calling `sys.exit`, which is
    what lets these run in-process.

    Asserted here because the subprocess tests *cannot* see it: `sys.exit(None)`
    and `sys.exit(0)` both exit 0, so a command that stopped returning a status
    would pass every one of them. The mutation sweep found seven such lines at
    once, which is what a whole class of unobservable code looks like.
    """

    def test_init(self, holding: support.Machine) -> None:
        assert quietly(lambda: manage.init(str(holding.remote))) == 0

    def test_add(self, holding: support.Machine) -> None:
        quietly(lambda: manage.init(str(holding.remote)))
        assert quietly(lambda: manage.add([str(holding.home / ".bashrc")], False)) == 0

    def test_add_when_nothing_changed(self, holding: support.Machine) -> None:
        """The early return, which is a different line from the one above."""
        quietly(lambda: manage.init(str(holding.remote)))
        quietly(lambda: manage.add([str(holding.home / ".bashrc")], False))
        assert quietly(lambda: manage.add([str(holding.home / ".bashrc")], False)) == 0

    def test_remove(self, holding: support.Machine) -> None:
        quietly(lambda: manage.init(str(holding.remote)))
        quietly(lambda: manage.add([str(holding.home / ".bashrc")], False))
        assert quietly(lambda: manage.remove(str(holding.home / ".bashrc"), False)) == 0


@pytest.mark.usefixtures("ready")
class TestWhatEachCommandPrints:
    """The printed output is the product for `list`, and the record of what
    happened for the others. A command that stopped printing would pass every
    exit-status test in this file."""

    def test_add_names_each_file_it_stored(self, ready: support.Machine) -> None:
        done = ready.run_cli("add", str(ready.home / ".bashrc"))
        assert "added .bashrc" in done.stdout
        # The negative half. `record` reports whether git had anything staged,
        # and a version that always answered "no" still printed "added" above --
        # it printed *both*, which is the shape only this assertion sees.
        assert "no change" not in done.stdout

    def test_add_names_only_what_it_actually_stored(self, ready: support.Machine) -> None:
        """Two files, one of them already stored. A fixture where every named
        file is also a stored file cannot tell "what changed" from "what was
        named" -- they are the same list."""
        ready.run_cli("add", str(ready.home / ".bashrc"))
        ready.write(ready.home / ".vimrc", "set nocompatible\n")
        ready.run_cli("add", str(ready.home / ".bashrc"), str(ready.home / ".vimrc"))
        assert ready.log()[0] == f"add from {ready.host}: .vimrc"

    def test_recording_a_stale_merge_base_does_not_claim_to_have_added(
        self, ready: support.Machine
    ) -> None:
        """`add` commits when the copies are identical but a snapshot is not --
        which is what an earlier run that died between the copy and the commit
        leaves. Naming the files there would describe something that did not
        happen, so the message says what did.

        The stale merge base has to be **committed**, and that took two goes.
        Deleting the snapshot only removes it from the working tree, so `add`
        rewrites the same bytes and the tree matches HEAD again; editing it
        without committing has exactly the same effect for the same reason. Both
        versions reached the branch they were written for not at all, and passed
        against the message they were written to reject.
        """
        ready.write(ready.home / ".vimrc", "set nocompatible\n")
        ready.run_cli("add", str(ready.home / ".bashrc"), str(ready.home / ".vimrc"))
        ready.snapshot(".bashrc").write_text("an older merge base\n", encoding="utf-8")
        support.git(["commit", "-qam", "a merge base from an older run"], ready.repo, ready.env)

        done = ready.run_cli("add", str(ready.home / ".vimrc"), str(ready.home / ".bashrc"))
        assert done.returncode == 0, done.stdout + done.stderr
        assert ready.log()[0] == f"add from {ready.host}: record the merge base for 2 files"
        assert ready.snapshot(".bashrc").is_file()

    def test_add_commits_what_it_stored_and_not_what_it_found(self, ready: support.Machine) -> None:
        """`git add --all --` with an *empty* pathspec stages the whole
        repository -- measured, and the reason dropping `add`'s staging list
        changed nothing observable until this test existed. `sync` stages
        everything on purpose; `add` must not, or a file dropped in the
        repository by hand rides along in a commit that names something else.
        """
        ready.run_cli("add", str(ready.home / ".bashrc"))
        stray = ready.repo / "not-mine.txt"
        stray.write_text("dropped here by hand\n", encoding="utf-8")

        ready.write(ready.home / ".vimrc", "set nocompatible\n")
        ready.run_cli("add", str(ready.home / ".vimrc"))
        staged = support.git(["status", "--porcelain"], ready.repo, ready.env)
        assert "not-mine.txt" in staged, "the stray file was committed by `add`"

    def test_add_marks_the_overlay(self, ready: support.Machine) -> None:
        """`(host)` is the only thing distinguishing the two destinations in the
        output, and the destinations are otherwise invisible to the user."""
        done = ready.run_cli("add", "--host", str(ready.home / ".bashrc"))
        assert "added .bashrc (host)" in done.stdout

    def test_remove_says_the_original_was_left_alone(self, ready: support.Machine) -> None:
        """The sentence that stops someone thinking `remove` deleted their
        dotfile — which is the first thing the name suggests."""
        ready.run_cli("add", str(ready.home / ".bashrc"))
        done = ready.run_cli("remove", str(ready.home / ".bashrc"))
        assert "was not touched" in done.stdout

    def test_init_says_what_it_did_and_what_to_do_next(self, ready: support.Machine) -> None:
        """Three lines, and each is the only place the user learns something:
        where the repository went, that a settings file was created for them,
        and what to type next. `TestInit` asserts the *effects*; these are the
        words, which nothing else covers."""
        with support.tempdir() as box:
            home = box / "home"
            home.mkdir()
            support.seed_home(home)
            env = support.sandbox_env(home)
            # Its own remote, and freshly empty. Since milestone 3 `init` ends in
            # a sync, so it *pushes* -- and the class's shared remote has already
            # been initialised into by `setUp`, which makes it no longer empty.
            done = support.run_cli(["init", str(support.make_remote(box / "r.git", env))], env)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "cloned" in done.stdout
        assert "the remote was empty" in done.stdout
        assert "tupferl add" in done.stdout

    def test_list_counts_what_it_showed(self, ready: support.Machine) -> None:
        ready.run_cli("add", str(ready.home / ".bashrc"))
        done = ready.run_cli("status", "--all")
        assert (
            "1 file managed, 0 to change, 0 in conflict, 0 from this host's overlay" in done.stdout
        )

    def test_list_counts_the_overlay_separately(self, ready: support.Machine) -> None:
        """0 and 1 rather than 1 and 1: with equal counts a swapped pair still
        reads correctly."""
        ready.write(ready.home / ".gitconfig-extra", "x")
        ready.run_cli("add", str(ready.home / ".bashrc"))
        ready.run_cli("add", "--host", str(ready.home / ".gitconfig-extra"))
        done = ready.run_cli("status", "--all")
        assert (
            "2 files managed, 0 to change, 0 in conflict, 1 from this host's overlay" in done.stdout
        )


class TestCommitMessages:
    def test_the_plans_shape(self) -> None:
        """Plan §3.5: `<what> from <hostname>: <names>`."""
        names = [PurePosixPath(".bashrc"), PurePosixPath(".gitconfig")]
        found = manage.describe("add", names, "laptop")
        assert found == "add from laptop: .bashrc, .gitconfig"

    def test_a_long_list_is_summarised(self) -> None:
        """`git log --oneline` after adding a directory of two hundred files
        should still be readable."""
        names = [PurePosixPath(f".f{n}") for n in range(9)]
        found = manage.describe("add", names, "laptop")
        assert "and 4 more" in found
        assert ".f8" not in found

    def test_the_boundary_names_everything(self) -> None:
        names = [PurePosixPath(f".f{n}") for n in range(manage.NAMED_IN_MESSAGE)]
        assert "more" not in manage.describe("add", names, "laptop")


@pytest.mark.usefixtures("sandbox")
class TestModes:
    def test_an_executable_file_is_stored_executable(self, sandbox: support.Sandbox) -> None:
        script = sandbox.write(sandbox.home / "script.sh", "#!/bin/sh\n")
        script.chmod(0o700)
        assert copies.mode_for(script) == 0o755

    def test_a_plain_file_is_not(self, sandbox: support.Sandbox) -> None:
        assert copies.mode_for(sandbox.write(sandbox.home / "plain", "x")) == 0o644

    def test_storing_something_that_stopped_being_a_file_is_an_error(
        self, sandbox: support.Sandbox
    ) -> None:
        """`manifest.check` saw a regular file; by the time the copy is made it
        is a fifo. That is a race rather than a rule the caller broke -- and
        answering `None` would report it as "nothing to do", which is how a file
        the user asked to manage ends up silently unmanaged."""
        where = sandbox.home / "vanished"
        os.mkfifo(where)
        # `read_bytes()` on a fifo blocks until a writer appears, so a mutation
        # dropping `copies.read`'s `S_ISREG` guard *hangs* this test rather than
        # failing it -- and the harness files a hang as `BROKE`, which is never
        # `caught`, leaving the guard itself unguarded. Measured: that row came
        # back `BROKE` on the whole-tree sweep before this bound existed.
        with (
            pytest.raises(OSError) as caught,
            support.deadline(support.PATIENCE, f"copies.store blocked reading {where}"),
        ):
            copies.store(where, sandbox.tmp / "target")
        # Read the type back, because `TimeoutError` **is** an `OSError`: the
        # `assertRaises` above accepts the hang as though it were the error under
        # test. And the message names the path -- a bound that fires should say
        # which file -- so the `assertIn` below passes on a hang too. That leaves
        # this line as the only thing telling the two apart, which is the point:
        # without it the bound would turn one unguarded line into a test that
        # cannot fail, and a diagnostic worth having would be what disarmed it.
        assert not isinstance(caught.value, TimeoutError)
        assert "vanished" in str(caught.value)

    def test_executable_by_anyone_counts(self, sandbox: support.Sandbox) -> None:
        """0o711 arrives from tarballs and is a script. Storing it
        non-executable puts it back unrunnable on the other machine."""
        script = sandbox.write(sandbox.home / "odd.sh", "#!/bin/sh\n")
        script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXOTH)
        assert copies.mode_for(script) == 0o755


@pytest.mark.usefixtures("sandbox")
class TestOpenRepo:
    def test_a_directory_that_is_not_a_repository_is_told_apart(
        self, sandbox: support.Sandbox
    ) -> None:
        """From "nothing here yet", which needs a different answer: run `init`
        over an empty path, look at a non-empty one first."""
        paths.repo_dir().mkdir(parents=True)
        (paths.repo_dir() / "stray").write_text("x", encoding="utf-8")
        with pytest.raises(TupferlError) as caught:
            manage.open_repo()
        assert "not a git repository" in str(caught.value)

    def test_the_settings_come_back_with_it(self, sandbox: support.Sandbox) -> None:
        """So a command reads the config once, rather than each of them
        deciding for itself where it lives."""
        support.make_repo(paths.repo_dir(), sandbox.env)
        settings = paths.config_file()
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text("max_file_size = 4096\n", encoding="utf-8")
        _, config = manage.open_repo()
        assert config.max_file_size == 4096


class TestCounting:
    def test_the_plural(self) -> None:
        assert manage.count(1) == "1 file"
        assert manage.count(2) == "2 files"
        assert manage.count(0) == "0 files"

    def test_a_second_noun_gets_the_same_rule(self) -> None:
        """`status` counts commits. Zero as well as one and many, because zero
        is where "1 file" logic written as `many > 1` goes wrong and it is the
        count a machine that agrees with its remote would have."""
        assert manage.count(1, "commit") == "1 commit"
        assert manage.count(2, "commit") == "2 commits"
        assert manage.count(0, "commit") == "0 commits"

    def test_the_default_noun_is_still_files(self) -> None:
        """The argument was added in milestone 6, and every existing caller
        passes none. A default that had changed would rewrite `sync`'s report
        and `list`'s tail line without either of them being touched."""
        assert manage.count(3, "file") == manage.count(3)


def names(many: int, prefix: str = "f") -> list[PurePosixPath]:
    """`many` distinct managed names, for the report `manage.stored` builds."""
    return [PurePosixPath(f".config/{prefix}{n:03d}.conf") for n in range(many)]


class TestWhatAddSays:
    """`manage.stored`, which is a decision about text and needs no repository.

    The end-to-end half is in `TestAddingADirectoryOfMany` below. This is where
    the two shapes and the split between the words are pinned, because five
    fixtures for five cases would be five real `add` runs.
    """

    def test_a_short_list_names_every_file(self) -> None:
        """Unchanged behaviour, and most runs. `NAMED_ONE_BY_ONE` files is the
        boundary and it is included, not excluded."""
        lines = manage.stored({"added": names(manage.NAMED_ONE_BY_ONE)}, to_host=False)
        assert len(lines) == manage.NAMED_ONE_BY_ONE
        assert all(line.startswith("added .config/") for line in lines), lines

    def test_one_more_than_that_is_summarised(self) -> None:
        """The other side of the boundary, so `<=` written as `<` fails here."""
        lines = manage.stored({"added": names(manage.NAMED_ONE_BY_ONE + 1)}, to_host=False)
        assert len(lines) == 1, lines
        assert f"added {manage.NAMED_ONE_BY_ONE + 1} files" in lines[0]

    def test_the_summary_names_a_few_and_counts_the_rest(self) -> None:
        """Through `a_few`, the same rule the commit message uses -- two
        thresholds would be two answers to "is this too long to read?"."""
        lines = manage.stored({"added": names(100)}, to_host=False)
        assert ".config/f000.conf" in lines[0]
        assert f"and {100 - manage.NAMED_IN_MESSAGE} more" in lines[0]
        assert ".config/f099.conf" not in lines[0]

    def test_added_and_updated_are_never_counted_together(self) -> None:
        """The constraint that makes this more than a `len()`.

        `copies.store` answers "added", "updated" or `None`, and a run that
        stored one new file and rewrote ninety-nine must not report a hundred of
        either. `manage.added` carries the same split for the commit message,
        and its docstring says why.
        """
        lines = manage.stored(
            {"added": names(1, "new"), "updated": names(99, "old")}, to_host=False
        )
        assert len(lines) == 2, lines
        assert "added 1 file" in lines[0]
        assert "updated 99 files" in lines[1]

    def test_the_words_come_out_in_the_same_order_whatever_went_in(self) -> None:
        """`stored` sorts by the word, so two machines that did the same things
        print the same lines. Insertion order is whichever file happened to sort
        first, which is not a property of the run.

        The fixture inserts **updated before added**, because a dict built the
        other way round is already in sorted order and cannot tell a sort from
        no sort at all -- CLAUDE.md §2's two symmetric inputs. The mutation
        sweep found exactly that: `sorted` becoming `list`, twice, and a
        reversed ordering, all three surviving.
        """
        backwards = {"updated": names(1, "u"), "added": names(1, "a")}
        for lines in (
            manage.stored(backwards, to_host=False),
            manage.stored({"updated": names(20, "u"), "added": names(20, "a")}, to_host=False),
        ):
            assert len(lines) == 2, lines
            assert lines[0].startswith("added"), lines
            assert lines[1].startswith("updated"), lines

    def test_nothing_stored_says_nothing(self) -> None:
        """Every file was already byte-for-byte identical, so `store` answered
        `None` for all of them and none reaches here. `add` then prints its own
        "no change" sentence, which this must not pre-empt."""
        assert manage.stored({}, to_host=False) == []

    def test_the_host_marker_survives_both_shapes(self) -> None:
        """`add --host` marks its lines, and a summary that dropped the mark
        would say a shared file was stored when an overlay was."""
        short = manage.stored({"added": names(2)}, to_host=True)
        long = manage.stored({"added": names(50)}, to_host=True)
        assert all("(host)" in line for line in short), short
        assert "(host)" in long[0]


def call(*argv: str) -> int:
    """One CLI command in this process, output swallowed."""
    with support.quiet():
        return cli.main(list(argv))


def spoken(*argv: str) -> str:
    """One CLI command that must exit 0, and what it printed."""
    with support.quiet() as said:
        assert cli.main(list(argv)) == 0, said.getvalue()
    return said.getvalue()


@pytest.fixture
def unshared(initialised: support.Machine) -> support.Machine:
    """`initialised`, with an unmanaged `.bashrc` waiting."""
    initialised.write(initialised.home / ".bashrc", "one\n")
    return initialised


@pytest.mark.usefixtures("unshared")
class TestSayingTheWorkIsNotSharedYet:
    """`add` and `remove` commit locally and do not push, so until a sync runs
    the change exists on this machine and nowhere else.

    Issue #60 asked whether they should sync by themselves. They should not: a
    sync can stop at a conflict prompt and open `$EDITOR`, so `tupferl add
    .bashrc` would be able to pause and ask about an unrelated file. What was
    missing is that neither command *said* so -- both report success, which is
    the whole reason the gap is easy to miss.
    """

    def test_add_says_the_file_is_not_shared_yet(self, unshared: support.Machine) -> None:
        assert manage.NOT_SHARED in spoken("add", str(unshared.home / ".bashrc"))

    def test_it_names_the_command_that_would_share_it(self, unshared: support.Machine) -> None:
        """**Not `assertIn(manage.NOT_SHARED, ...)`, which is the constant
        compared with itself.** Every other test here asserts the message
        *arrives*, and all of them go on passing if it is shortened to "not
        shared yet" -- measured: that mutation survived them.

        What the line is for is telling someone who has just been told their
        `add` succeeded what to do next, so the command name is the part worth
        pinning. Read from the output rather than from the constant.
        """
        said = spoken("add", str(unshared.home / ".bashrc"))
        advice = next(line for line in said.splitlines() if "not shared" in line)
        assert "tupferl sync" in advice

    def test_remove_says_it_too(self, unshared: support.Machine) -> None:
        """The same gap in the other direction: the file is gone from this
        machine's repository and still on every other one."""
        assert call("add", str(unshared.home / ".bashrc")) == 0
        assert manage.NOT_SHARED in spoken("remove", str(unshared.home / ".bashrc"))

    def test_an_add_that_changed_nothing_does_not_send_the_user_to_sync(
        self, unshared: support.Machine
    ) -> None:
        """The arm where nothing was committed. There is no work waiting, so
        the advice would be to run a sync with nothing in it -- and a line that
        appears whether or not it means anything is one nobody reads.

        This is the half that makes the two tests above assertions rather than
        a line that is always printed.
        """
        assert call("add", str(unshared.home / ".bashrc")) == 0
        again = spoken("add", str(unshared.home / ".bashrc"))
        assert "no change" in again
        assert manage.NOT_SHARED not in again


@pytest.fixture
def initialised(machine: support.Machine) -> support.Machine:
    """`started`, but with `init` run **in this process** rather than as a subprocess.

    The two are not interchangeable and the difference is the point: `started`
    goes through `Machine.init`, which spawns `python -m tupferl` and is what
    the classes asserting on a real exit status want. The classes below assert
    on what was *printed*, which `support.quiet` can only capture in-process.
    """
    assert call("init", str(machine.remote)) == 0
    return machine


def summary(*paths: str) -> list[str]:
    """The lines `add` writes *about the files*, without the trailing advisory.

    `NOT_SHARED` is one line on every successful `add`, and the class below
    counts lines to check that a hundred files summarise to one. Filtered by
    identity rather than by position, so a future line added anywhere does not
    silently shift what these tests believe they are counting.
    """
    return [
        line
        for line in spoken("add", *paths).splitlines()
        if line.strip() and line != manage.NOT_SHARED
    ]


@pytest.mark.usefixtures("initialised")
class TestAddingADirectoryOfMany:
    """#28 end to end: the README's own example is a directory of hundreds."""

    def test_a_hundred_files_are_one_line(self, initialised: support.Machine) -> None:
        where = initialised.home / ".local" / "share" / "app"
        where.mkdir(parents=True)
        for number in range(100):
            (where / f"f{number:03d}.conf").write_text("x\n", encoding="utf-8")

        said = summary(str(where))
        assert len(said) == 1, said
        assert "added 100 files" in said[0]

    def test_a_re_add_reports_only_what_changed(self, initialised: support.Machine) -> None:
        """The case the summary must not get wrong: 100 files, two edited.

        The other 98 are byte-for-byte identical, so `store` answers `None` and
        they are silent -- which puts the run back under `NAMED_ONE_BY_ONE` and
        names the two. A summary counting all hundred would tell the user it had
        stored ninety-eight files it did not touch.
        """
        where = initialised.home / ".local" / "share" / "app"
        where.mkdir(parents=True)
        for number in range(100):
            (where / f"f{number:03d}.conf").write_text("x\n", encoding="utf-8")
        spoken("add", str(where))

        for number in (0, 1):
            (where / f"f{number:03d}.conf").write_text("changed\n", encoding="utf-8")
        said = summary(str(where))
        assert len(said) == 2, said
        assert all(line.startswith("updated ") for line in said), said

    def test_refusals_are_still_one_line_each(self, initialised: support.Machine) -> None:
        """The part a long listing used to push off the screen, and the reason
        this issue is about noise rather than tidiness."""
        where = initialised.home / ".local" / "share" / "app"
        where.mkdir(parents=True)
        for number in range(20):
            (where / f"f{number:03d}.conf").write_text("x\n", encoding="utf-8")
        (where / "link").symlink_to(initialised.home / ".bashrc")
        (where / "big.bin").write_bytes(b"x" * (2 << 20))

        said = spoken("add", str(where))
        skipped = [line for line in said.splitlines() if line.startswith("skipped ")]
        assert len(skipped) == 2, said
        assert "added 20 files" in said


@dataclass(frozen=True)
class Keyed(support.Machine):
    """An initialised machine whose `$HOME` holds a realistic `~/.ssh`."""

    ssh: Path

    def add(self, *args: str) -> tuple[int, str]:
        with support.quiet() as said:
            return cli.main(["add", *args]), said.getvalue()

    def kept(self) -> set[str]:
        """What of `~/.ssh` reached the repository.

        Not `stored`, which `support.Machine` already means something else by --
        "where would the copy of this one name live". Two questions, and the
        name went to the one that asks about a whole directory only because this
        class was written before it was a `Machine`.
        """
        repo = paths.repo_dir()
        return {
            str(path.relative_to(repo)) for path in (repo / ".ssh").rglob("*") if path.is_file()
        }


@pytest.fixture
def keyed(initialised: support.Machine) -> Keyed:
    ssh = initialised.home / ".ssh"
    ssh.mkdir()
    for name, body in (
        ("id_ed25519", "PRIVATE KEY\n"),
        ("id_ed25519.pub", "ssh-ed25519 AAAA\n"),
        ("config", "Host *\n"),
        ("known_hosts", "example.com ssh-rsa AAAA\n"),
    ):
        (ssh / name).write_text(body, encoding="utf-8")
    return Keyed(**vars(initialised), ssh=ssh)


@pytest.mark.usefixtures("keyed")
class TestAddingSomethingThatHoldsACredential:
    """#35 end to end: refused by name, skipped in a walk, allowed by `--anyway`.

    The three shapes are different code paths -- `check` raises for a named file,
    `collect` turns the same refusal into a `Refused` for a walked one, and the
    flag has to reach both. A test of only the first would leave
    `tupferl add ~/.ssh` pushing the key it is most likely to meet.
    """

    def test_naming_the_key_is_refused(self, keyed: Keyed) -> None:
        status, said = keyed.add(str(keyed.ssh / "id_ed25519"))
        assert status == 2, said
        assert ".ssh/id_*" in said
        assert "--anyway" in said
        assert not (paths.repo_dir() / ".ssh").exists(), "it was stored anyway"

    def test_the_message_says_what_the_danger_is(self, keyed: Keyed) -> None:
        """Not "this looks like a secret", which tells a user nothing they can
        act on. The reason tupferl refuses is that it stores plaintext and
        pushes it, and that is the sentence."""
        said = keyed.add(str(keyed.ssh / "id_ed25519"))[1]
        assert "plaintext" in said
        assert "remote" in said

    def test_walking_the_directory_skips_it_and_keeps_the_rest(self, keyed: Keyed) -> None:
        """`collect`'s half. Refusing the whole walk would be the wrong answer:
        `.ssh/config` and `known_hosts` are exactly what someone adding `~/.ssh`
        wants, and the public key is public."""
        status, said = keyed.add(str(keyed.ssh))
        assert status == 0, said
        assert "skipped" in said
        assert "id_ed25519" in said
        assert keyed.kept() == {".ssh/config", ".ssh/known_hosts", ".ssh/id_ed25519.pub"}

    def test_anyway_stores_it(self, keyed: Keyed) -> None:
        """The refusal has to be overrulable, or it is worked around by moving
        the file -- which is worse for the user and teaches them to distrust the
        rule."""
        status, said = keyed.add("--anyway", str(keyed.ssh / "id_ed25519"))
        assert status == 0, said
        assert ".ssh/id_ed25519" in keyed.kept()

    def test_anyway_reaches_a_directory_walk_too(self, keyed: Keyed) -> None:
        """The flag threads through `collect`, not only `check`. Wired to one of
        them, `tupferl add --anyway ~/.ssh` silently keeps skipping the file the
        user just said to store."""
        status, said = keyed.add("--anyway", str(keyed.ssh))
        assert status == 0, said
        assert "skipped" not in said
        assert ".ssh/id_ed25519" in keyed.kept()


@pytest.mark.usefixtures("two_machines")
class TestRemoveTakesTheNameListPrints:
    """#27's other caller. `remove` goes through `manifest.relative` too.

    The unit cases are in `test_manifest.TestTurningWhatWasTypedIntoAName`;
    this is the end-to-end half, and it exists because the two commands used to
    share a bug and could as easily share a fix that reached only one of them.
    """

    def test_a_name_from_list_is_removed(self, two_machines: support.TwoMachines) -> None:
        """The working directory is set away from `$HOME` deliberately: from
        `$HOME` the old cwd-relative reading happened to be right, which is why
        a suite that drives everything from a sandbox never saw this."""
        listed = two_machines.first.say("status", "--all")[1]
        assert ".bashrc" in listed

        with mock.patch.object(Path, "cwd", return_value=two_machines.tmp):
            status, said = two_machines.first.say("remove", ".bashrc")
        assert status == 0, said
        assert "removed .bashrc" in said
        assert not (two_machines.first.repo / ".bashrc").exists()
        # Plan §4: `remove` keeps the file in `$HOME`.
        assert (two_machines.first.home / ".bashrc").is_file()
