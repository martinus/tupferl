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

    def test_a_missing_working_directory_says_so(self) -> None:
        """Not "git is not installed", which is what `subprocess` makes it look
        like: a missing `cwd` raises `FileNotFoundError`, the same exception as a
        missing binary. `doctor` on a machine with no repository reported the
        wrong one of the two until this was checked before spawning."""
        found = gitrepo.git(["--version"], cwd=Path("/nonexistent-directory"))
        self.assertFalse(found.ok)
        self.assertIn("not a directory", found.err)
        self.assertNotIn("not installed", found.err)

    def test_a_plain_file_as_the_working_directory_says_so(self) -> None:
        """The other half, and the one that used to be a traceback:
        `NotADirectoryError` was caught by nothing."""
        with tempfile.TemporaryDirectory() as box:
            where = Path(box) / "file"
            where.write_text("x", encoding="utf-8")
            found = gitrepo.git(["--version"], cwd=where)
        self.assertFalse(found.ok)
        self.assertIn("not a directory", found.err)

    def test_a_real_directory_still_runs(self) -> None:
        """The precondition: the guard must not refuse the ordinary case, which
        is every other call in this project."""
        with tempfile.TemporaryDirectory() as box:
            self.assertTrue(gitrepo.git(["--version"], cwd=Path(box)).ok)

    def test_a_failing_call_is_not_a_timeout(self) -> None:
        """The precondition for the two above: `timed_out` must distinguish, not
        just be set whenever something went wrong."""
        with tempfile.TemporaryDirectory() as box:
            found = gitrepo.git(["rev-parse", "--show-toplevel"], cwd=Path(box))
        self.assertFalse(found.ok)
        self.assertFalse(found.timed_out)
        self.assertIn("not a git repository", found.err)


class TestTheReasonGitGives(unittest.TestCase):
    """Which line of git's stderr a user is shown.

    Two wrong answers shipped before this function existed, in opposite
    directions, so both are asserted against here rather than left implied:
    `doctor` reported the last line ("and the repository exists.") and `init`
    reported the first ("Cloning into '...'"). The transcripts below are real
    ones, kept verbatim so the rule is tested against what git prints rather
    than against a paraphrase of it.
    """

    CLONE = (
        "Cloning into '/tmp/x'...\n"
        "ssh: connect to host 127.0.0.1 port 1: Connection refused\n"
        "fatal: Could not read from remote repository.\n"
        "\n"
        "Please make sure you have the correct access rights\n"
        "and the repository exists.\n"
    )
    #: The `ls-remote` shape, and the one with *two* `fatal:` lines. It is the
    #: only fixture here that can tell "the first one git marked" from "the last
    #: one": the first names the repository, the second is the generic follow-up.
    TWO_FATALS = (
        "fatal: '/tmp/absent.git' does not appear to be a git repository\n"
        "fatal: Could not read from remote repository.\n"
        "\n"
        "Please make sure you have the correct access rights\n"
        "and the repository exists.\n"
    )
    MISSING = "fatal: repository '/tmp/absent.git' does not exist\n"
    PROMPTS = "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n"

    def reason(self, err: str) -> str:
        return gitrepo.reason(gitrepo.Result(False, "", err))

    def test_progress_on_stderr_is_not_the_reason(self) -> None:
        found = self.reason(self.CLONE)
        self.assertEqual("fatal: Could not read from remote repository.", found)
        self.assertNotIn("Cloning into", found)

    def test_the_trailing_advice_is_not_either(self) -> None:
        self.assertNotIn("repository exists", self.reason(self.CLONE))

    def test_the_first_fatal_line_wins_when_there_are_two(self) -> None:
        """The specific one, not the generic follow-up. Every other transcript
        here has a single `fatal:`, where first and last are the same string —
        so without this fixture the choice between them is untested."""
        found = self.reason(self.TWO_FATALS)
        self.assertEqual("fatal: '/tmp/absent.git' does not appear to be a git repository", found)

    def test_a_single_line_failure_is_itself(self) -> None:
        self.assertEqual(self.MISSING.strip(), self.reason(self.MISSING))

    def test_the_credential_case(self) -> None:
        self.assertEqual(self.PROMPTS.strip(), self.reason(self.PROMPTS))

    def test_a_failure_with_no_fatal_line_still_says_something(self) -> None:
        """Not every git command marks its complaint. The fallback is the first
        line that is neither blank nor progress."""
        self.assertEqual(
            "error: pathspec 'x' did not match", self.reason("error: pathspec 'x' did not match\n")
        )

    def test_empty_stderr_does_not_raise(self) -> None:
        """A killed process leaves nothing. A message about something going
        wrong must not itself be an `IndexError`."""
        self.assertEqual("no reason given", self.reason(""))

    def test_progress_alone_does_not_raise(self) -> None:
        """The pathological case the two filters make possible: every line was
        dropped."""
        self.assertEqual("no reason given", self.reason("Cloning into 'x'...\n"))


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
