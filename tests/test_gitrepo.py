"""The git wrapper, and the three ways a bare `subprocess.run` would go wrong.

Two of these are about *not hanging*, and they are the reason this module exists
at all. A git that waits for a credential on the terminal, or a remote that
accepts the connection and then says nothing, both turn `tupferl doctor` into a
process with no output that never returns — the failure mode hardest to report
and easiest to leave untested, because nothing in a normal run produces it.

So both are produced deliberately here: a git alias that sleeps for the timeout,
and a `PATH` with no git on it.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import support
from tupferl import gitrepo


class TestTheEnvironment(unittest.TestCase):
    """`env()` is what stops git asking a human a question nobody will answer."""

    def test_the_terminal_prompt_is_off(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_TERMINAL_PROMPT": "1"}):
            self.assertEqual("0", gitrepo.env()["GIT_TERMINAL_PROMPT"])

    def test_ssh_is_put_in_batch_mode(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn("BatchMode=yes", gitrepo.env()["GIT_SSH_COMMAND"])

    def test_a_users_own_ssh_command_wins(self) -> None:
        """Someone who named their own ssh command said something more specific
        than this default, and overriding it breaks the one configuration that
        was chosen deliberately."""
        with mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -F /my/config"}):
            self.assertEqual("ssh -F /my/config", gitrepo.env()["GIT_SSH_COMMAND"])

    def test_the_rest_of_the_environment_is_carried(self) -> None:
        """Not a fresh environment: git needs the user's `HOME` to find their
        `.gitconfig`, which is the whole reason for driving the real binary."""
        with mock.patch.dict(os.environ, {"HOME": "/somewhere"}):
            self.assertEqual("/somewhere", gitrepo.env()["HOME"])


class TestWhenGitCannotAnswer(unittest.TestCase):
    def test_a_call_that_hangs_is_reported_as_a_timeout(self) -> None:
        """Produced with a git alias that sleeps, so the wait is real and local:
        no network, and nothing to be flaky about except the machine being 500ms
        slower than the timeout, which is why the alias sleeps ten times it.
        """
        found = gitrepo.git(["-c", "alias.wait=!sleep 5", "wait"], timeout=0.5)
        self.assertFalse(found.ok)
        self.assertTrue(found.timed_out)
        self.assertIn("did not answer", found.err)

    def test_the_timeout_message_names_the_command_not_a_flag(self) -> None:
        """`args[0]` is `-c` as often as it is the subcommand, and "git -c did
        not answer" sends the reader looking for a command by that name."""
        found = gitrepo.git(["-c", "alias.wait=!sleep 5", "wait"], timeout=0.5)
        self.assertIn("wait", found.err)

    def test_the_module_timeout_is_read_at_call_time(self) -> None:
        """`TIMEOUT` as a default argument is evaluated once, at import, so it
        could not be changed afterwards -- while `doctor.remote` reads the
        module attribute when it composes its message. The two disagreed: a test
        that shortened the constant waited the full thirty seconds and was told
        it had waited half of one.

        Asserted on the elapsed time, because the message alone was exactly what
        was wrong before.
        """
        with mock.patch.object(gitrepo, "TIMEOUT", 0.5):
            started = time.monotonic()
            found = gitrepo.git(["-c", "alias.wait=!sleep 5", "wait"])
            waited = time.monotonic() - started
        self.assertTrue(found.timed_out)
        self.assertLess(waited, 3, "the module constant was ignored")
        self.assertIn("0.5s", found.err)

    def test_a_missing_git_is_reported_rather_than_raised(self) -> None:
        """`FileNotFoundError` out of `subprocess` would reach the user as a
        traceback; every caller here is asking a question that has "no" as an
        ordinary answer."""
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            found = gitrepo.git(["--version"])
        self.assertFalse(found.ok)
        self.assertFalse(found.timed_out, "not answering and not being there are different")
        self.assertIn("not installed", found.err)

    def test_a_failing_call_is_not_a_timeout(self) -> None:
        """The precondition for the two above: `timed_out` must distinguish, not
        just be set whenever something went wrong."""
        with tempfile.TemporaryDirectory() as box:
            found = gitrepo.git(["rev-parse", "--show-toplevel"], cwd=Path(box))
        self.assertFalse(found.ok)
        self.assertFalse(found.timed_out)
        self.assertIn("not a git repository", found.err)


class TestIsRepository(unittest.TestCase):
    def setUp(self) -> None:
        box = tempfile.TemporaryDirectory(prefix="tupferl-gitrepo-")
        self.addCleanup(box.cleanup)
        self.box = Path(box.name)
        self.home = self.box / "home"
        self.home.mkdir()
        support.seed_home(self.home)
        self.env = support.sandbox_env(self.home)
        patched = mock.patch.dict(os.environ, self.env, clear=True)
        patched.start()
        self.addCleanup(patched.stop)

    def test_the_top_of_a_working_tree(self) -> None:
        repo = support.make_repo(self.box / "repo", self.env)
        self.assertTrue(gitrepo.is_repository(repo))

    def test_not_a_subdirectory_of_one(self) -> None:
        repo = support.make_repo(self.box / "repo", self.env)
        inside = repo / ".config"
        inside.mkdir()
        self.assertFalse(gitrepo.is_repository(inside))

    def test_not_a_plain_directory(self) -> None:
        plain = self.box / "plain"
        plain.mkdir()
        self.assertFalse(gitrepo.is_repository(plain))

    def test_a_git_that_cannot_run_answers_no(self) -> None:
        """Without the `if not found.ok` guard this returns *true* for the
        current directory: `--show-toplevel` printed nothing, `Path("")`
        resolves to the working directory, and the comparison then holds.

        So the fixture has to be the current directory, with no git. Any other
        path would return false either way and the guard would look tested
        while nothing had exercised it.
        """
        plain = self.box / "plain"
        plain.mkdir()
        here = os.getcwd()
        os.chdir(plain)
        self.addCleanup(os.chdir, here)
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            self.assertFalse(gitrepo.is_repository(Path.cwd()))


if __name__ == "__main__":
    unittest.main()
