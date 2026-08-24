"""`tupferl doctor`, against real git and a real bare repository standing in for
a remote.

The distinction every test here turns on is the three-state answer. A check that
*could not run* is `None`, not `True`: on a machine where `init` has never been
run there is no repository, so there is nothing to say about its remote, and
saying ✔ would make `doctor` most reassuring exactly where it knows least.

`Check.ok` has no default for the same reason. In woswoar it defaulted to `None`
and `assertFalse(status.ok)` passed for `None` too, so no test could tell a
failure from a skip (woswoar#206). Every assertion below uses `assertIs`.
"""

from __future__ import annotations

import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import ClassVar
from unittest import mock

from tests import support
from tupferl import doctor, gitrepo, paths
from tupferl.config import Config
from tupferl.doctor import Check


class DoctorCase(support.SandboxCase):
    """A sandboxed home, plus a place to put a remote."""

    def setUp(self) -> None:
        super().setUp()
        self.repo = paths.repo_dir()
        self.remote = self.tmp / "remote.git"


class TestGitPresence(DoctorCase):
    def test_a_real_git_passes_and_says_which(self) -> None:
        """The detail carries the version, so a green line is still evidence."""
        found = doctor.git_present()
        self.assertIs(True, found.ok)
        self.assertIn("git version", found.detail)

    def test_no_git_on_path_fails_and_says_what_to_do(self) -> None:
        """The branch every other check depends on, produced by emptying `PATH`
        rather than by patching `gitrepo.git` -- a mock would also pass against
        a `git_present` that had stopped calling git at all."""
        os.environ["PATH"] = "/nonexistent"
        found = doctor.git_present()
        self.assertIs(False, found.ok)
        self.assertIn("install git", found.detail)


class TestTheRepositoryCheck(DoctorCase):
    def test_nothing_there_says_run_init(self) -> None:
        """On "no repository at", not on "tupferl init": *both* messages end in
        that command, so asserting it alone passes with the existence check
        removed entirely. The mutation sweep found that."""
        found = doctor.repository(self.repo)
        self.assertIs(False, found.ok)
        self.assertIn("no repository at", found.detail)
        self.assertIn("tupferl init", found.detail)

    def test_a_directory_that_is_not_a_repository_says_so_differently(self) -> None:
        """The case worth separating: a half-finished `init` needs "look at this
        first", not "run init", which would be told to overwrite it."""
        self.repo.mkdir(parents=True)
        (self.repo / "stray.txt").write_text("", encoding="utf-8")
        found = doctor.repository(self.repo)
        self.assertIs(False, found.ok)
        self.assertIn("not a git repository", found.detail)
        # The distinguishing half: this message must not be the "nothing here
        # yet" one, which would have the user run `init` over a directory whose
        # contents nobody has looked at.
        self.assertIn("move it aside", found.detail)
        self.assertNotEqual(doctor.repository(self.tmp / "absent").detail, found.detail)

    def test_a_real_repository_passes(self) -> None:
        support.make_repo(self.repo, self.env)
        self.assertIs(True, doctor.repository(self.repo).ok)

    def test_a_subdirectory_of_a_repository_is_not_the_repository(self) -> None:
        """`~/.local/share` is under `$HOME`, and a `$HOME` that is itself a git
        repository is a configuration people have. Treating that as "already
        initialised" would commit dotfiles into whatever tree encloses it."""
        support.make_repo(self.home, self.env)
        self.repo.mkdir(parents=True)
        self.assertFalse(gitrepo.is_repository(self.repo))
        self.assertIs(False, doctor.repository(self.repo).ok)


class TestTheSettingsCheck(DoctorCase):
    def setUp(self) -> None:
        super().setUp()
        support.make_repo(self.repo, self.env)
        self.where = paths.config_file(self.repo)
        self.where.parent.mkdir(parents=True, exist_ok=True)

    def test_no_file_is_not_applicable_rather_than_a_pass(self) -> None:
        found, config = doctor.settings(self.repo)
        self.assertIs(None, found.ok)
        self.assertEqual(Config(), config)

    def test_a_broken_file_fails_with_the_parser_s_sentence(self) -> None:
        self.where.write_text("ignroe = []\n", encoding="utf-8")
        found, _ = doctor.settings(self.repo)
        self.assertIs(False, found.ok)
        self.assertIn("ignroe", found.detail)

    def test_a_good_file_passes_and_its_values_come_back(self) -> None:
        """The settings are returned as well as checked, so the hostname check
        below reads the same parse rather than a second one."""
        self.where.write_text('hostname = "from-config"\n', encoding="utf-8")
        found, config = doctor.settings(self.repo)
        self.assertIs(True, found.ok)
        self.assertEqual("from-config", config.hostname)


class TestTheHostnameCheck(DoctorCase):
    def test_a_usable_name_passes(self) -> None:
        found = doctor.host(Config())
        self.assertIs(True, found.ok)
        self.assertEqual(support.HOST, found.detail)

    def test_a_name_that_cannot_be_a_directory_fails(self) -> None:
        """Set in the environment rather than in the config file, because that
        is the path a bad name actually arrives by -- `SandboxCase` has already
        patched `os.environ`, so this writes into the sandbox copy."""
        os.environ["TUPFERL_HOSTNAME"] = ".."
        found = doctor.host(Config())
        self.assertIs(False, found.ok)
        self.assertIn("..", found.detail)


class TestTheRemoteCheck(DoctorCase):
    def test_without_a_repository_there_is_nothing_to_ask(self) -> None:
        found = doctor.remote(self.repo, ok=False)
        self.assertIs(None, found.ok)

    def test_a_repository_with_no_remote_fails_with_the_command_to_fix_it(self) -> None:
        support.make_repo(self.repo, self.env)
        found = doctor.remote(self.repo, ok=True)
        self.assertIs(False, found.ok)
        self.assertIn("remote add origin", found.detail)

    def test_a_reachable_remote_passes(self) -> None:
        """A real bare repository on disk, driven by real git. No network."""
        support.make_remote(self.remote, self.env)
        support.make_repo(self.repo, self.env, remote=self.remote)
        found = doctor.remote(self.repo, ok=True)
        self.assertIs(True, found.ok)
        self.assertIn("origin", found.detail)

    def test_a_remote_that_is_not_there_fails(self) -> None:
        """Pointed at a path that does not exist, so git fails locally and fast
        -- the same code path an unreachable host takes, without the wait."""
        support.make_repo(self.repo, self.env)
        support.git(["remote", "add", "origin", str(self.tmp / "absent.git")], self.repo, self.env)
        found = doctor.remote(self.repo, ok=True)
        self.assertIs(False, found.ok)
        self.assertIn("credentials", found.detail)
        # git's own reason, not just the advice: without this the message could
        # say "refused: None" and still pass.
        self.assertIn("does not appear to be a git repository", found.detail)

    def test_the_reason_is_gits_first_line(self) -> None:
        """git leads with the specific failure and follows it with generic
        advice, so the *first* line is the one worth printing. Its last is
        "and the repository exists.", which is what `doctor` reported as the
        reason a remote refused until this test was written.

        A single-line stderr cannot tell the two apart, which is why the fixture
        checks that it produced several.
        """
        support.make_repo(self.repo, self.env)
        support.git(["remote", "add", "origin", str(self.tmp / "absent.git")], self.repo, self.env)
        said = gitrepo.git(["ls-remote", "--exit-code", "origin", "HEAD"], cwd=self.repo)
        self.assertGreater(len(said.err.splitlines()), 1, "the fixture produced one line")
        found = doctor.remote(self.repo, ok=True)
        self.assertIs(False, found.ok)
        self.assertIn(said.err.splitlines()[0], found.detail)
        self.assertNotIn(said.err.splitlines()[-1], found.detail)

    def test_the_first_remote_is_the_one_asked(self) -> None:
        """Two remotes, so "the first" is observable. `git remote` lists them
        alphabetically, so `alpha` is first and `origin` is not."""
        remote = support.make_remote(self.remote, self.env)
        support.make_repo(self.repo, self.env, remote=remote)
        support.git(["remote", "add", "alpha", str(remote)], self.repo, self.env)
        found = doctor.remote(self.repo, ok=True)
        self.assertIs(True, found.ok)
        self.assertIn("alpha", found.detail)

    def test_a_remote_that_never_answers_is_a_timeout_not_a_refusal(self) -> None:
        """The two need different sentences: "did not answer" is a network or a
        wedged host, "refused" is a URL or a credential. Produced with an ssh
        command that sleeps, so no packet leaves the machine."""
        support.make_repo(self.repo, self.env)
        support.git(["remote", "add", "origin", "ssh://example.invalid/x"], self.repo, self.env)
        # `sh -c`, not a bare `sleep 30`: git appends the host and the remote
        # command to whatever this names, and `sleep 30 example.invalid ...`
        # exits immediately with "invalid time interval" -- which arrives as a
        # *refusal*, and would make this test pass for the wrong reason.
        os.environ["GIT_SSH_COMMAND"] = "sh -c 'sleep 30'"
        with mock.patch.object(gitrepo, "TIMEOUT", 0.5):
            found = doctor.remote(self.repo, ok=True)
        self.assertIs(False, found.ok)
        self.assertIn("did not answer", found.detail)
        self.assertNotIn("refused", found.detail)


class TestTheBackupCheck(DoctorCase):
    def test_a_writable_directory_passes_and_is_created(self) -> None:
        where = paths.backup_dir()
        self.assertFalse(where.exists())
        self.assertIs(True, doctor.writable(where).ok)
        self.assertTrue(where.is_dir())

    @unittest.skipIf(os.geteuid() == 0, "root ignores the mode bits, so nothing is unwritable")
    def test_a_directory_that_exists_and_cannot_be_written_fails(self) -> None:
        """The second half of the check, and the half `mkdir` cannot reach: a
        directory that is already there and is not writable.

        Labelled rather than left silent, as CLAUDE.md §2 asks. It runs on CI,
        whose runners are an ordinary user, and skips in a root container --
        where `os.access(..., W_OK)` is true for every directory, so a test
        written without the guard would pass there whatever the code did.
        """
        where = self.tmp / "readonly"
        where.mkdir()
        where.chmod(0o500)
        self.addCleanup(where.chmod, 0o700)
        found = doctor.writable(where)
        self.assertIs(False, found.ok)
        self.assertIn("not writable", found.detail)

    def test_a_path_that_cannot_be_created_fails(self) -> None:
        """The parent is a *file*, so `mkdir` fails for a reason no mode-bit
        check would see -- and root, which ignores mode bits, still sees this."""
        blocked = self.tmp / "file"
        blocked.write_text("", encoding="utf-8")
        found = doctor.writable(blocked / "backup")
        self.assertIs(False, found.ok)
        self.assertIn("cannot create", found.detail)


class TestTheDanglingStateCheck(DoctorCase):
    def setUp(self) -> None:
        super().setUp()
        support.make_repo(self.repo, self.env)

    def test_a_clean_repository_passes(self) -> None:
        self.assertIs(True, doctor.dangling(self.repo, ok=True).ok)

    def test_an_uncommitted_change_is_reported_with_a_count(self) -> None:
        (self.repo / ".bashrc").write_text("export EDITOR=nvim\n", encoding="utf-8")
        (self.repo / ".gitconfig").write_text("[user]\n", encoding="utf-8")
        found = doctor.dangling(self.repo, ok=True)
        self.assertIs(False, found.ok)
        self.assertIn("2 uncommitted", found.detail)

    def test_an_unfinished_merge_is_named(self) -> None:
        """A killed `sync` leaves this behind, and the next sync would do
        something surprising on top of it."""
        (self.repo / ".git" / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        found = doctor.dangling(self.repo, ok=True)
        self.assertIs(False, found.ok)
        self.assertIn("MERGE_HEAD", found.detail)

    def test_without_a_repository_there_is_nothing_in_progress(self) -> None:
        self.assertIs(None, doctor.dangling(self.repo, ok=False).ok)

    def test_a_repository_git_cannot_read_is_a_failure_not_a_pass(self) -> None:
        """`ok=True` with a directory that is not a repository: both git calls
        fail, and folding "git said no" into "nothing in progress" would report
        ✔ for a tree nothing could be read from.

        Reachable in anger when the repository is removed between the
        `repository` check and this one, and by any future caller that passes a
        wrong `ok`. The point is that the *shape* of the answer is right --
        CLAUDE.md §8's pass nobody can explain.
        """
        empty = self.tmp / "not-a-repo"
        empty.mkdir()
        found = doctor.dangling(empty, ok=True)
        self.assertIs(False, found.ok)
        self.assertIn("git cannot read", found.detail)

    def test_a_repository_whose_status_cannot_be_read_is_a_failure(self) -> None:
        """The second half, and it needs its own fixture: `rev-parse` must
        succeed while `git status` fails, or this passes for the reason the test
        above already covers.

        A directory where the index file belongs does exactly that -- git maps
        the index to read the work tree's state, and `rev-parse --git-dir`
        never touches it. Not a `chmod`, which root ignores.
        """
        index = self.repo / ".git" / "index"
        index.unlink()
        index.mkdir()
        self.assertTrue(gitrepo.git(["rev-parse", "--git-dir"], cwd=self.repo).ok)
        found = doctor.dangling(self.repo, ok=True)
        self.assertIs(False, found.ok)
        self.assertIn("git status", found.detail)

    def test_every_unfinished_marker_is_looked_for(self) -> None:
        """One test per marker: a rebase and a cherry-pick leave the tree in the
        same half-done state as a merge, and a list that had lost an entry would
        still pass a test naming only the first.

        The names are written out here rather than read from `doctor.UNFINISHED`.
        That was the first version, and the mutation harness killed it: with the
        constant shortened to `("MERGE_HEAD",)` the loop simply ran once and
        passed. A test containing a copy of the code it checks cannot fail --
        CLAUDE.md §2 -- and this is what that looks like when it is only two
        characters of `for ... in`.
        """
        git_dir = self.repo / ".git"
        for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD"):
            with self.subTest(marker=marker):
                where = git_dir / marker
                where.write_text("deadbeef\n", encoding="utf-8")
                try:
                    found = doctor.dangling(self.repo, ok=True)
                    self.assertIs(False, found.ok)
                    self.assertIn(marker, found.detail)
                finally:
                    where.unlink()


class TestTheReport(unittest.TestCase):
    """The text, from a hand-built list -- so the counting is tested without
    needing a machine in six different states."""

    found: ClassVar[list[Check]] = [
        Check(True, "one", "fine"),
        Check(True, "two", "fine"),
        Check(True, "three", "fine"),
        Check(False, "four", "broken"),
        Check(False, "five", "broken"),
        Check(None, "six", "not applicable"),
    ]

    def test_the_summary_counts_the_three_states_apart(self) -> None:
        """3, 2 and 1 rather than one of each: with equal counts, a summary that
        printed them in the wrong order would still read correctly."""
        self.assertIn("3 ok, 2 failed, 1 not applicable", doctor.report(self.found))

    def test_each_state_gets_its_own_mark(self) -> None:
        lines = doctor.report(self.found).splitlines()
        self.assertTrue(lines[0].startswith("✔"))
        self.assertTrue(lines[3].startswith("✘"))
        self.assertTrue(lines[5].startswith("-"))

    def test_the_details_line_up(self) -> None:
        """What the width computation is *for*. The titles here differ in length
        (`one` against `three`), so a width taken from the shortest -- or from
        the first -- puts the details in different columns."""
        lines = doctor.report(self.found).splitlines()[: len(self.found)]
        columns = {line.index(check.detail) for line, check in zip(lines, self.found, strict=True)}
        self.assertEqual(1, len(columns), f"details start at {sorted(columns)}")

    def test_the_details_are_all_there(self) -> None:
        text = doctor.report(self.found)
        for check in self.found:
            self.assertIn(check.detail, text)


class TestTheExitStatus(DoctorCase):
    def quietly(self) -> int:
        """`main` prints the report, which is its product; captured so that a
        real failure elsewhere is not buried under seven ticks per test."""
        with redirect_stdout(io.StringIO()) as caught:
            status = doctor.main()
        self.printed = caught.getvalue()
        return status

    def test_a_failure_exits_one(self) -> None:
        """Nothing is set up, so the repository check fails."""
        self.assertEqual(1, self.quietly())

    def test_the_report_is_printed(self) -> None:
        """`main`'s other half: a status with no report would tell a user
        nothing, and the exit-code tests cannot see the difference."""
        self.quietly()
        self.assertIn("repository", self.printed)
        self.assertIn("ok, ", self.printed)

    def test_a_healthy_machine_exits_zero(self) -> None:
        support.make_remote(self.remote, self.env)
        support.make_repo(self.repo, self.env, remote=self.remote)
        self.assertEqual(0, self.quietly())

    def test_a_skipped_check_alone_is_not_a_failure(self) -> None:
        """A machine that has run `init` but has no `config.toml` is healthy:
        `settings` is `None` there, and exiting non-zero for it would make
        `doctor` useless in an install script."""
        support.make_remote(self.remote, self.env)
        support.make_repo(self.repo, self.env, remote=self.remote)
        self.assertFalse(paths.config_file(self.repo).exists())
        found = doctor.checks()
        self.assertIn(None, [check.ok for check in found])
        self.assertEqual(0, self.quietly())


class TestTheChecksRunInOrder(DoctorCase):
    def test_git_is_asked_about_before_the_repository(self) -> None:
        """A reader who stops at the first ✘ should be looking at the cause
        rather than at one of its symptoms."""
        titles = [check.title for check in doctor.checks()]
        self.assertEqual(
            ["git", "repository", "settings", "hostname", "remote", "backups", "state"], titles
        )

    def test_a_bare_machine_skips_what_it_cannot_ask(self) -> None:
        """`checks()` passes `here.ok is True` down to `remote` and `dangling`.
        Inverted, a bare machine would report those two as ✔ and ✘ the wrong way
        round -- and every other assertion in this file still passes, because
        they call those functions directly."""
        found = {check.title: check.ok for check in doctor.checks()}
        self.assertIs(False, found["repository"])
        self.assertIs(None, found["remote"])
        self.assertIs(None, found["state"])

    def test_a_healthy_machine_asks_everything(self) -> None:
        """The other side of the same wiring, and the reason the test above is
        not satisfied by a `checks()` that skips those two unconditionally."""
        remote = support.make_remote(self.remote, self.env)
        support.make_repo(self.repo, self.env, remote=remote)
        found = {check.title: check.ok for check in doctor.checks()}
        self.assertIs(True, found["repository"])
        self.assertIs(True, found["remote"])
        self.assertIs(True, found["state"])

    def test_the_repository_path_comes_from_the_environment(self) -> None:
        """`checks()` takes no arguments, so this is the only thing that decides
        which repository it looked at."""
        support.make_repo(self.repo, self.env)
        found = {check.title: check for check in doctor.checks()}
        self.assertEqual(str(paths.repo_dir()), found["repository"].detail)
        self.assertTrue(Path(found["repository"].detail).is_relative_to(self.home))


if __name__ == "__main__":
    unittest.main()
