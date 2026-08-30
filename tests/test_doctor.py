"""`tupferl doctor`, against real git and a real bare repository standing in for
a remote.

The distinction every test here turns on is the three-state answer. A check that
*could not run* is `None`, not `True`: on a machine where `init` has never been
run there is no repository, so there is nothing to say about its remote, and
saying ✔ would make `doctor` most reassuring exactly where it knows least.

`Check.ok` has no default for the same reason. In woswoar it defaulted to `None`
and `assertFalse(status.ok)` passed for `None` too, so no test could tell a
failure from a skip (woswoar#206). **Every assertion below is `is True`,
`is False` or `is None`, never a truthiness test** -- which is `assertIs`'s job
in the spelling this file used before it was converted, and the one property of
it worth stating rather than the method name.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from tests import support
from tupferl import doctor, gitrepo, paths
from tupferl.config import Config
from tupferl.doctor import Check


@dataclass(frozen=True)
class Doctored(support.Sandbox):
    """A sandboxed home, plus a place to put a remote.

    `repo` and `remote` are computed once here rather than in each test because
    `paths.repo_dir()` only answers correctly *after* `os.environ` is patched,
    and a test that called it too early would look at the developer's own
    installation. Building it in the fixture makes that ordering unstateable.
    """

    repo: Path
    remote: Path


@pytest.fixture
def box(sandbox: support.Sandbox) -> Doctored:
    return Doctored(**vars(sandbox), repo=paths.repo_dir(), remote=sandbox.tmp / "remote.git")


@pytest.mark.usefixtures("box")
class TestGitPresence:
    def test_a_real_git_passes_and_says_which(self) -> None:
        """The detail carries the version, so a green line is still evidence."""
        found = doctor.git_present()
        assert found.ok is True
        assert "git version" in found.detail

    def test_no_git_on_path_fails_and_says_what_to_do(self) -> None:
        """The branch every other check depends on, produced by emptying `PATH`
        rather than by patching `gitrepo.git` -- a mock would also pass against
        a `git_present` that had stopped calling git at all."""
        os.environ["PATH"] = "/nonexistent"
        found = doctor.git_present()
        assert found.ok is False
        assert "install git" in found.detail


@pytest.mark.usefixtures("box")
class TestTheRepositoryCheck:
    def test_nothing_there_says_run_init(self, box: Doctored) -> None:
        """On "no repository at", not on "tupferl init": *both* messages end in
        that command, so asserting it alone passes with the existence check
        removed entirely. The mutation sweep found that."""
        found = doctor.repository(box.repo)
        assert found.ok is False
        assert "no repository at" in found.detail
        assert "tupferl init" in found.detail

    def test_a_directory_that_is_not_a_repository_says_so_differently(self, box: Doctored) -> None:
        """The case worth separating: a half-finished `init` needs "look at this
        first", not "run init", which would be told to overwrite it."""
        box.repo.mkdir(parents=True)
        (box.repo / "stray.txt").write_text("", encoding="utf-8")
        found = doctor.repository(box.repo)
        assert found.ok is False
        assert "not a git repository" in found.detail
        # The distinguishing half: this message must not be the "nothing here
        # yet" one, which would have the user run `init` over a directory whose
        # contents nobody has looked at.
        assert "move it aside" in found.detail
        assert found.detail != doctor.repository(box.tmp / "absent").detail

    def test_a_real_repository_passes(self, box: Doctored) -> None:
        support.make_repo(box.repo, box.env)
        assert doctor.repository(box.repo).ok is True

    def test_a_subdirectory_of_a_repository_is_not_the_repository(self, box: Doctored) -> None:
        """`~/.local/share` is under `$HOME`, and a `$HOME` that is itself a git
        repository is a configuration people have. Treating that as "already
        initialised" would commit dotfiles into whatever tree encloses it."""
        support.make_repo(box.home, box.env)
        box.repo.mkdir(parents=True)
        assert not gitrepo.is_repository(box.repo)
        assert doctor.repository(box.repo).ok is False


@pytest.fixture
def settings_file(box: Doctored) -> Path:
    """A repository, and the settings path with its parent made.

    A fixture rather than a helper the tests call, because the *absence* of the
    file is what one of them asserts -- so the directory has to exist before the
    test runs and the file must not.
    """
    support.make_repo(box.repo, box.env)
    where = paths.config_file()
    where.parent.mkdir(parents=True, exist_ok=True)
    return where


@pytest.mark.usefixtures("box")
class TestTheSettingsCheck:
    def test_no_file_is_not_applicable_rather_than_a_pass(self, settings_file: Path) -> None:
        assert not settings_file.exists()
        found, config = doctor.settings()
        assert found.ok is None
        assert config == Config()

    def test_a_broken_file_fails_with_the_parser_s_sentence(self, settings_file: Path) -> None:
        settings_file.write_text("ignroe = []\n", encoding="utf-8")
        found, _ = doctor.settings()
        assert found.ok is False
        assert "ignroe" in found.detail

    def test_a_good_file_passes_and_its_values_come_back(self, settings_file: Path) -> None:
        """The settings are returned as well as checked, so the checks below
        read the same parse rather than a second one."""
        settings_file.write_text("max_file_size = 4096\n", encoding="utf-8")
        found, config = doctor.settings()
        assert found.ok is True
        assert config.max_file_size == 4096


@pytest.mark.usefixtures("box")
class TestTheHostnameCheck:
    def test_a_usable_name_passes(self) -> None:
        found = doctor.host(Config())
        assert found.ok is True
        assert found.detail == support.HOST

    def test_a_name_that_cannot_be_a_directory_fails(self) -> None:
        """Set in the environment rather than in the config file, because that
        is the path a bad name actually arrives by.

        Safe because the `box` fixture has already patched `os.environ` -- which
        is why this class carries `usefixtures` as well: the patch is the whole
        reason this write does not reach the developer's own environment, and it
        would be invisible from the test's own text otherwise.
        """
        os.environ["TUPFERL_HOSTNAME"] = ".."
        found = doctor.host(Config())
        assert found.ok is False
        assert ".." in found.detail


@pytest.mark.usefixtures("box")
class TestTheRemoteCheck:
    def test_without_a_repository_there_is_nothing_to_ask(self, box: Doctored) -> None:
        found = doctor.remote(box.repo, ok=False)
        assert found.ok is None

    def test_a_repository_with_no_remote_fails_with_the_command_to_fix_it(
        self, box: Doctored
    ) -> None:
        support.make_repo(box.repo, box.env)
        found = doctor.remote(box.repo, ok=True)
        assert found.ok is False
        assert "remote add origin" in found.detail

    def test_a_reachable_remote_passes(self, box: Doctored) -> None:
        """A real bare repository on disk, driven by real git. No network."""
        support.make_remote(box.remote, box.env)
        support.make_repo(box.repo, box.env, remote=box.remote)
        found = doctor.remote(box.repo, ok=True)
        assert found.ok is True
        assert "origin" in found.detail

    def test_a_remote_that_is_not_there_fails(self, box: Doctored) -> None:
        """Pointed at a path that does not exist, so git fails locally and fast
        -- the same code path an unreachable host takes, without the wait."""
        support.make_repo(box.repo, box.env)
        support.git(["remote", "add", "origin", str(box.tmp / "absent.git")], box.repo, box.env)
        found = doctor.remote(box.repo, ok=True)
        assert found.ok is False
        assert "credentials" in found.detail
        # git's own reason, not just the advice: without this the message could
        # say "refused: None" and still pass.
        assert "does not appear to be a git repository" in found.detail

    def test_the_reason_is_gits_first_line(self, box: Doctored) -> None:
        """git leads with the specific failure and follows it with generic
        advice, so the *first* line is the one worth printing. Its last is
        "and the repository exists.", which is what `doctor` reported as the
        reason a remote refused until this test was written.

        A single-line stderr cannot tell the two apart, which is why the fixture
        checks that it produced several.
        """
        support.make_repo(box.repo, box.env)
        support.git(["remote", "add", "origin", str(box.tmp / "absent.git")], box.repo, box.env)
        said = gitrepo.git(["ls-remote", "--exit-code", "origin", "HEAD"], cwd=box.repo)
        assert len(said.err.splitlines()) > 1, "the fixture produced one line"
        found = doctor.remote(box.repo, ok=True)
        assert found.ok is False
        assert said.err.splitlines()[0] in found.detail
        assert said.err.splitlines()[-1] not in found.detail

    def test_the_first_remote_is_the_one_asked(self, box: Doctored) -> None:
        """Two remotes, so "the first" is observable. `git remote` lists them
        alphabetically, so `alpha` is first and `origin` is not."""
        remote = support.make_remote(box.remote, box.env)
        support.make_repo(box.repo, box.env, remote=remote)
        support.git(["remote", "add", "alpha", str(remote)], box.repo, box.env)
        found = doctor.remote(box.repo, ok=True)
        assert found.ok is True
        assert "alpha" in found.detail

    def test_a_remote_that_never_answers_is_a_timeout_not_a_refusal(self, box: Doctored) -> None:
        """The two need different sentences: "did not answer" is a network or a
        wedged host, "refused" is a URL or a credential. Produced with an ssh
        command that sleeps, so no packet leaves the machine."""
        support.make_repo(box.repo, box.env)
        support.git(["remote", "add", "origin", "ssh://example.invalid/x"], box.repo, box.env)
        # `sh -c`, not a bare `sleep 30`: git appends the host and the remote
        # command to whatever this names, and `sleep 30 example.invalid ...`
        # exits immediately with "invalid time interval" -- which arrives as a
        # *refusal*, and would make this test pass for the wrong reason.
        os.environ["GIT_SSH_COMMAND"] = "sh -c 'sleep 30'"
        # Bounded, because the thing under test is a *timeout*: a mutation that
        # loses the bound -- `gitrepo.git`'s `TIMEOUT if timeout is None`
        # inverted, so `waiting` becomes `None` -- leaves this waiting the full
        # 30s of the sleeper, past the harness's per-test alarm. That is filed
        # `BROKE` rather than `caught`, so the one line making a wedged remote
        # answerable at all was guarded by nothing. Measured on the whole-tree
        # sweep before this bound existed. Five seconds against a 0.5s TIMEOUT
        # is ten times the honest wait and a sixth of the sleeper.
        with (
            mock.patch.object(gitrepo, "TIMEOUT", 0.5),
            support.deadline(support.PATIENCE, "doctor.remote never gave up on the sleeper"),
        ):
            found = doctor.remote(box.repo, ok=True)
        assert found.ok is False
        assert "did not answer" in found.detail
        assert "refused" not in found.detail


@pytest.mark.usefixtures("box")
class TestTheBackupCheck:
    def test_a_writable_directory_passes_and_is_created(self) -> None:
        where = paths.backup_dir()
        assert not where.exists()
        assert doctor.writable(where).ok is True
        assert where.is_dir()

    @pytest.mark.skipif(
        os.geteuid() == 0, reason="root ignores the mode bits, so nothing is unwritable"
    )
    def test_a_directory_that_exists_and_cannot_be_written_fails(self, box: Doctored) -> None:
        """The second half of the check, and the half `mkdir` cannot reach: a
        directory that is already there and is not writable.

        Labelled rather than left silent, as CLAUDE.md §2 asks. It runs on CI,
        whose runners are an ordinary user, and skips in a root container --
        where `os.access(..., W_OK)` is true for every directory, so a test
        written without the guard would pass there whatever the code did.
        """
        where = box.tmp / "readonly"
        where.mkdir()
        where.chmod(0o500)
        try:
            found = doctor.writable(where)
        finally:
            # Restored so the sandbox's own teardown can remove it. `try`/
            # `finally` rather than a fixture, because it is one directory in
            # one test and a fixture would put the restore a screen away from
            # the chmod it undoes.
            where.chmod(0o700)
        assert found.ok is False
        assert "not writable" in found.detail

    def test_a_path_that_cannot_be_created_fails(self, box: Doctored) -> None:
        """The parent is a *file*, so `mkdir` fails for a reason no mode-bit
        check would see -- and root, which ignores mode bits, still sees this."""
        blocked = box.tmp / "file"
        blocked.write_text("", encoding="utf-8")
        found = doctor.writable(blocked / "backup")
        assert found.ok is False
        assert "cannot create" in found.detail


@pytest.fixture
def repository(box: Doctored) -> None:
    """The repository, already created.

    Returns nothing on purpose: it is a precondition rather than a value, so it
    is asked for with `usefixtures` on the class and the tests take `box` alone.
    A fixture that returned `box` would give every test two names for one
    object.
    """
    support.make_repo(box.repo, box.env)


@pytest.mark.usefixtures("box", "repository")
class TestTheDanglingStateCheck:
    def test_a_clean_repository_passes(self, box: Doctored) -> None:
        assert doctor.dangling(box.repo, ok=True).ok is True

    def test_an_uncommitted_change_is_reported_with_a_count(self, box: Doctored) -> None:
        (box.repo / ".bashrc").write_text("export EDITOR=nvim\n", encoding="utf-8")
        (box.repo / ".gitconfig").write_text("[user]\n", encoding="utf-8")
        found = doctor.dangling(box.repo, ok=True)
        assert found.ok is False
        assert "2 uncommitted" in found.detail

    def test_an_unfinished_merge_is_named(self, box: Doctored) -> None:
        """A killed `sync` leaves this behind, and the next sync would do
        something surprising on top of it."""
        (box.repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        found = doctor.dangling(box.repo, ok=True)
        assert found.ok is False
        assert "MERGE_HEAD" in found.detail

    def test_without_a_repository_there_is_nothing_in_progress(self, box: Doctored) -> None:
        assert doctor.dangling(box.repo, ok=False).ok is None

    def test_a_repository_git_cannot_read_is_a_failure_not_a_pass(self, box: Doctored) -> None:
        """`ok=True` with a directory that is not a repository: both git calls
        fail, and folding "git said no" into "nothing in progress" would report
        ✔ for a tree nothing could be read from.

        Reachable in anger when the repository is removed between the
        `repository` check and this one, and by any future caller that passes a
        wrong `ok`. The point is that the *shape* of the answer is right --
        CLAUDE.md §8's pass nobody can explain.
        """
        empty = box.tmp / "not-a-repo"
        empty.mkdir()
        found = doctor.dangling(empty, ok=True)
        assert found.ok is False
        assert "git cannot read" in found.detail

    def test_a_repository_whose_status_cannot_be_read_is_a_failure(self, box: Doctored) -> None:
        """The second half, and it needs its own fixture: `rev-parse` must
        succeed while `git status` fails, or this passes for the reason the test
        above already covers.

        A directory where the index file belongs does exactly that -- git maps
        the index to read the work tree's state, and `rev-parse --git-dir`
        never touches it. Not a `chmod`, which root ignores.
        """
        index = box.repo / ".git" / "index"
        index.unlink()
        index.mkdir()
        assert gitrepo.git(["rev-parse", "--git-dir"], cwd=box.repo).ok
        found = doctor.dangling(box.repo, ok=True)
        assert found.ok is False
        assert "git status" in found.detail

    @pytest.mark.parametrize("marker", ["MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD"])
    def test_every_unfinished_marker_is_looked_for(self, box: Doctored, marker: str) -> None:
        """One test per marker -- literally one now, where it was one `subTest`:
        a rebase and a cherry-pick leave the tree in the same half-done state as
        a merge, and a list that had lost an entry would still pass a test
        naming only the first.

        The names are written out here rather than read from `doctor.UNFINISHED`.
        That was the first version, and the mutation harness killed it: with the
        constant shortened to `("MERGE_HEAD",)` the loop simply ran once and
        passed. A test containing a copy of the code it checks cannot fail --
        CLAUDE.md §2 -- and this is what that looks like when it is only two
        characters of `for ... in`.

        The write-then-unlink the loop needed is gone: each case gets its own
        repository, so one marker cannot be left behind for the next.
        """
        (box.repo / ".git" / marker).write_text("deadbeef\n", encoding="utf-8")
        found = doctor.dangling(box.repo, ok=True)
        assert found.ok is False
        assert marker in found.detail


#: A hand-built report, so the counting is tested without needing a machine in
#: six different states. 3 ok, 2 failed and 1 not applicable rather than one of
#: each: with equal counts, a summary printing them in the wrong order would
#: still read correctly.
FOUND = [
    Check(True, "one", "fine"),
    Check(True, "two", "fine"),
    Check(True, "three", "fine"),
    Check(False, "four", "broken"),
    Check(False, "five", "broken"),
    Check(None, "six", "not applicable"),
]


class TestTheReport:
    """The text, from `FOUND` above."""

    def test_the_summary_counts_the_three_states_apart(self) -> None:
        """3, 2 and 1 rather than one of each: with equal counts, a summary that
        printed them in the wrong order would still read correctly."""
        assert "3 ok, 2 failed, 1 not applicable" in doctor.report(FOUND)

    def test_each_state_gets_its_own_mark(self) -> None:
        lines = doctor.report(FOUND).splitlines()
        assert lines[0].startswith("✔")
        assert lines[3].startswith("✘")
        assert lines[5].startswith("-")

    def test_the_details_line_up(self) -> None:
        """What the width computation is *for*. The titles here differ in length
        (`one` against `three`), so a width taken from the shortest -- or from
        the first -- puts the details in different columns."""
        lines = doctor.report(FOUND).splitlines()[: len(FOUND)]
        columns = {line.index(check.detail) for line, check in zip(lines, FOUND, strict=True)}
        assert len(columns) == 1, f"details start at {sorted(columns)}"

    def test_the_details_are_all_there(self) -> None:
        text = doctor.report(FOUND)
        for check in FOUND:
            assert check.detail in text


def quietly() -> tuple[int, str]:
    """`doctor.main()`, as (status, what it printed).

    The report is `main`'s product, and it is captured so that a real failure
    elsewhere is not buried under seven ticks per test. Returned as a pair
    rather than stashed on the test instance, which is what this was: pytest
    builds a fresh instance per test, so a value written by a helper and read by
    an assertion is a value that happens to survive rather than one that is
    passed.

    Through `support.quiet` rather than a raw `redirect_stdout(io.StringIO())`,
    which is what it was: `main` runs *in this process*, so a mutant that loops
    while printing fills an unbounded buffer, and that memory is charged to the
    mutation lane's share. `Spill` is the bound, and its docstring is the
    argument. It captures stderr too, which nothing here writes.
    """
    with support.quiet() as caught:
        status = doctor.main()
    return status, caught.getvalue()


@pytest.mark.usefixtures("box")
class TestTheExitStatus:
    def test_a_failure_exits_one(self) -> None:
        """Nothing is set up, so the repository check fails."""
        assert quietly()[0] == 1

    def test_the_report_is_printed(self) -> None:
        """`main`'s other half: a status with no report would tell a user
        nothing, and the exit-code tests cannot see the difference."""
        _, printed = quietly()
        assert "repository" in printed
        assert "ok, " in printed

    def test_a_healthy_machine_exits_zero(self, box: Doctored) -> None:
        support.make_remote(box.remote, box.env)
        support.make_repo(box.repo, box.env, remote=box.remote)
        assert quietly()[0] == 0

    def test_a_skipped_check_alone_is_not_a_failure(self, box: Doctored) -> None:
        """A machine that has run `init` but has no `config.toml` is healthy:
        `settings` is `None` there, and exiting non-zero for it would make
        `doctor` useless in an install script."""
        support.make_remote(box.remote, box.env)
        support.make_repo(box.repo, box.env, remote=box.remote)
        assert not paths.config_file().exists()
        found = doctor.checks()
        assert None in [check.ok for check in found]
        assert quietly()[0] == 0


@pytest.mark.usefixtures("box")
class TestTheChecksRunInOrder:
    def test_git_is_asked_about_before_the_repository(self) -> None:
        """A reader who stops at the first ✘ should be looking at the cause
        rather than at one of its symptoms."""
        titles = [check.title for check in doctor.checks()]
        assert titles == ["git", "repository", "settings", "hostname", "remote", "backups", "state"]

    def test_a_bare_machine_skips_what_it_cannot_ask(self) -> None:
        """`checks()` passes `here.ok is True` down to `remote` and `dangling`.
        Inverted, a bare machine would report those two as ✔ and ✘ the wrong way
        round -- and every other assertion in this file still passes, because
        they call those functions directly."""
        found = {check.title: check.ok for check in doctor.checks()}
        assert found["repository"] is False
        assert found["remote"] is None
        assert found["state"] is None

    def test_a_healthy_machine_asks_everything(self, box: Doctored) -> None:
        """The other side of the same wiring, and the reason the test above is
        not satisfied by a `checks()` that skips those two unconditionally."""
        remote = support.make_remote(box.remote, box.env)
        support.make_repo(box.repo, box.env, remote=remote)
        found = {check.title: check.ok for check in doctor.checks()}
        assert found["repository"] is True
        assert found["remote"] is True
        assert found["state"] is True

    def test_the_repository_path_comes_from_the_environment(self, box: Doctored) -> None:
        """`checks()` takes no arguments, so this is the only thing that decides
        which repository it looked at."""
        support.make_repo(box.repo, box.env)
        found = {check.title: check for check in doctor.checks()}
        assert found["repository"].detail == str(paths.repo_dir())
        assert Path(found["repository"].detail).is_relative_to(box.home)
