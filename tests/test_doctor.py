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


class TestTheRepositoryCheck(DoctorCase):
    def test_nothing_there_says_run_init(self) -> None:
        found = doctor.repository(self.repo)
        self.assertIs(False, found.ok)
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


class TestTheBackupCheck(DoctorCase):
    def test_a_writable_directory_passes_and_is_created(self) -> None:
        where = paths.backup_dir()
        self.assertFalse(where.exists())
        self.assertIs(True, doctor.writable(where).ok)
        self.assertTrue(where.is_dir())

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

    def test_the_repository_path_comes_from_the_environment(self) -> None:
        """`checks()` takes no arguments, so this is the only thing that decides
        which repository it looked at."""
        support.make_repo(self.repo, self.env)
        found = {check.title: check for check in doctor.checks()}
        self.assertEqual(str(paths.repo_dir()), found["repository"].detail)
        self.assertTrue(Path(found["repository"].detail).is_relative_to(self.home))


if __name__ == "__main__":
    unittest.main()
