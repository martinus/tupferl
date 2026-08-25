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
import subprocess
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
        return gitrepo.reason(gitrepo.Result("", err))

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

    def test_the_first_speaking_line_wins_when_none_is_fatal(self) -> None:
        """Two lines, neither marked. The fallback takes the first for the same
        reason the `fatal:` rule does — git leads with the specific complaint —
        and with a one-line fixture that choice is untested."""
        found = self.reason("error: pathspec 'x' did not match\nerror: see git help\n")
        self.assertEqual("error: pathspec 'x' did not match", found)

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


class TestReadingAConflictedIndex(support.SandboxCase):
    """`gitrepo.version`, and which stage is which side.

    **The numbering is the point.** git's stage 2 is the branch being merged
    *into* and stage 3 is the branch being merged *in*, which lines up with the
    prompt's "this computer" and "the repository" -- but it lines up by luck
    rather than by construction, and backwards it means `--ours` silently keeps
    the side the user asked to discard. That is the class of defect milestone
    4's review caught three of, so it is asserted rather than commented.

    The two sides are given distinct content for the same reason every fixture
    in that milestone does: symmetric inputs make "which side was written"
    unobservable.
    """

    OURS = b"from the local branch\nshared\n"
    THEIRS = b"from the remote branch\nshared\n"
    BASE = b"from neither\nshared\n"

    def setUp(self) -> None:
        super().setUp()
        self.repo = support.make_repo(self.home / "repo", self.env)
        self.write(self.repo / ".gitignore", "")  # something to commit onto
        self.conflict()

    def commit(self, content: bytes, message: str) -> None:
        (self.repo / ".bashrc").write_bytes(content)
        support.git(["add", "-A"], cwd=self.repo, env=self.env)
        support.git(["commit", "-m", message], cwd=self.repo, env=self.env)

    def conflict(self) -> None:
        """Two branches that changed the same line, merged into a dirty index."""
        self.commit(self.BASE, "the base")
        support.git(["branch", "other"], cwd=self.repo, env=self.env)
        self.commit(self.OURS, "ours")
        support.git(["checkout", "other"], cwd=self.repo, env=self.env)
        self.commit(self.THEIRS, "theirs")
        support.git(["checkout", support.BRANCH], cwd=self.repo, env=self.env)
        # Left to fail: that is what puts the three stages in the index.
        gitrepo.merge(self.repo, "other")

    def test_the_fixture_really_left_a_conflicted_index(self) -> None:
        """Every assertion below is vacuous against a merge that succeeded."""
        self.assertEqual([".bashrc"], gitrepo.unmerged(self.repo))

    def test_stage_two_is_the_branch_being_merged_into(self) -> None:
        self.assertEqual(self.OURS, gitrepo.version(self.repo, gitrepo.OURS, ".bashrc"))

    def test_stage_three_is_the_branch_being_merged_in(self) -> None:
        self.assertEqual(self.THEIRS, gitrepo.version(self.repo, gitrepo.THEIRS, ".bashrc"))

    def test_stage_one_is_the_merge_base(self) -> None:
        self.assertEqual(self.BASE, gitrepo.version(self.repo, gitrepo.BASE, ".bashrc"))

    def test_a_stage_that_is_not_there_is_none(self) -> None:
        """A path nothing conflicts over has no stages at all."""
        self.assertIsNone(gitrepo.version(self.repo, gitrepo.OURS, ".gitignore"))

    def test_bytes_come_back_exactly(self) -> None:
        """The reason this does not go through `gitrepo.git`, which returns
        `stdout.strip()`: a dotfile's trailing newline and any leading blank line
        are content, and stripping them corrupts the file on its way to the
        prompt."""
        gitrepo.abort_merge(self.repo)
        self.commit(b"\n\nleading and trailing blank lines\n\n", "spacey")
        support.git(["branch", "-D", "other"], cwd=self.repo, env=self.env)
        support.git(["checkout", "-b", "other", "HEAD~1"], cwd=self.repo, env=self.env)
        self.commit(b"\n\nthe other side\n\n", "spacey too")
        support.git(["checkout", support.BRANCH], cwd=self.repo, env=self.env)
        gitrepo.merge(self.repo, "other")
        self.assertEqual(
            b"\n\nleading and trailing blank lines\n\n",
            gitrepo.version(self.repo, gitrepo.OURS, ".bashrc"),
        )


class ConflictedIndex(support.SandboxCase):
    """A repository left mid-merge, for the two classes that read its index.

    Not a `Test...` class and holding no tests of its own: subclassing one that
    *does* makes every test in it run again under the subclass's name, which is
    six duplicate runs and six names for `--exclude` to have to know about.
    """

    def setUp(self) -> None:
        super().setUp()
        self.repo = support.make_repo(self.home / "r", self.env)

    def commit(self, name: str, text: bytes, mode: int = 0o644) -> None:
        where = self.repo / name
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_bytes(text)
        where.chmod(mode)
        support.git(["add", "-A"], cwd=self.repo, env=self.env)
        support.git(["commit", "-m", name], cwd=self.repo, env=self.env)

    def diverge(self, name: str, ours: bytes, theirs: bytes, mode: int = 0o644) -> None:
        """Two branches that both changed `name`, left mid-merge."""
        self.commit(name, b"base\n", mode)
        support.git(["branch", "other"], cwd=self.repo, env=self.env)
        self.commit(name, ours, mode)
        support.git(["checkout", "-q", "other"], cwd=self.repo, env=self.env)
        self.commit(name, theirs, mode)
        support.git(["checkout", "-q", support.BRANCH], cwd=self.repo, env=self.env)
        subprocess.run(
            ["git", "merge", "other"], cwd=self.repo, env=self.env, capture_output=True, check=False
        )


class TestReadingTheStagesOfAConflict(ConflictedIndex):
    """`gitrepo.conflicted`, asked directly.

    It had no test of its own: everything reached it through `sync`, which is why
    the `-z` parsing, the missing-stage case and the non-UTF-8 path all went
    unnoticed until a review. The modes it returns decide whether a settled file
    keeps its executable bit and whether it is written at all, so they are worth
    asking about here rather than three layers up.
    """

    def test_a_clean_repository_has_nothing_conflicted(self) -> None:
        """The empty answer, which every caller reads as "nothing to settle"."""
        self.commit(".bashrc", b"one\n")
        self.assertEqual({}, gitrepo.conflicted(self.repo))

    def test_all_three_stages_with_their_modes(self) -> None:
        self.diverge(".bashrc", b"ours\n", b"theirs\n")
        found = gitrepo.conflicted(self.repo)[".bashrc"]
        self.assertEqual({1: 0o100644, 2: 0o100644, 3: 0o100644}, found)

    def test_the_executable_bit_is_carried_per_stage(self) -> None:
        """Asymmetric, so the answer cannot be right by accident: equal modes
        pass against a function that returns the same number for every stage."""
        self.commit(".sh", b"base\n", 0o755)
        support.git(["branch", "other"], cwd=self.repo, env=self.env)
        self.commit(".sh", b"ours\n", 0o755)
        support.git(["checkout", "-q", "other"], cwd=self.repo, env=self.env)
        self.commit(".sh", b"theirs\n", 0o644)
        support.git(["checkout", "-q", support.BRANCH], cwd=self.repo, env=self.env)
        subprocess.run(
            ["git", "merge", "other"], cwd=self.repo, env=self.env, capture_output=True, check=False
        )
        found = gitrepo.conflicted(self.repo)[".sh"]
        self.assertEqual(0o100755, found[gitrepo.OURS])
        self.assertEqual(0o100644, found[gitrepo.THEIRS])

    def test_a_side_that_deleted_the_file_has_no_stage(self) -> None:
        """What `sync.held` reads to tell "that side has none" from "git would
        not answer" -- two very different things it used to conflate."""
        self.commit(".bashrc", b"base\n")
        support.git(["branch", "other"], cwd=self.repo, env=self.env)
        self.commit(".bashrc", b"ours\n")
        support.git(["checkout", "-q", "other"], cwd=self.repo, env=self.env)
        (self.repo / ".bashrc").unlink()
        support.git(["add", "-A"], cwd=self.repo, env=self.env)
        support.git(["commit", "-m", "gone"], cwd=self.repo, env=self.env)
        support.git(["checkout", "-q", support.BRANCH], cwd=self.repo, env=self.env)
        subprocess.run(
            ["git", "merge", "other"], cwd=self.repo, env=self.env, capture_output=True, check=False
        )
        found = gitrepo.conflicted(self.repo)[".bashrc"]
        self.assertIn(gitrepo.OURS, found)
        self.assertNotIn(gitrepo.THEIRS, found)

    def test_a_symlink_is_reported_with_its_own_mode(self) -> None:
        """`0o120000`, which is what `sync.reconcile` refuses on. Without the
        mode it would look like a plain file and be written *through*."""
        (self.repo / "link").symlink_to(self.home / "target")
        support.git(["add", "-A"], cwd=self.repo, env=self.env)
        support.git(["commit", "-m", "link"], cwd=self.repo, env=self.env)
        support.git(["branch", "other"], cwd=self.repo, env=self.env)
        (self.repo / "link").unlink()
        (self.repo / "link").symlink_to(self.home / "elsewhere")
        support.git(["add", "-A"], cwd=self.repo, env=self.env)
        support.git(["commit", "-m", "relink"], cwd=self.repo, env=self.env)
        support.git(["checkout", "-q", "other"], cwd=self.repo, env=self.env)
        (self.repo / "link").unlink()
        (self.repo / "link").symlink_to(self.home / "third")
        support.git(["add", "-A"], cwd=self.repo, env=self.env)
        support.git(["commit", "-m", "relink again"], cwd=self.repo, env=self.env)
        support.git(["checkout", "-q", support.BRANCH], cwd=self.repo, env=self.env)
        subprocess.run(
            ["git", "merge", "other"], cwd=self.repo, env=self.env, capture_output=True, check=False
        )
        self.assertEqual(0o120000, gitrepo.conflicted(self.repo)["link"][gitrepo.OURS])


class TestAPathThatIsNotUtf8(ConflictedIndex):
    """A conflicted path whose name is not valid UTF-8.

    **Linux only, and the CI job says so rather than this file skipping.** APFS
    and HFS+ reject a filename that is not valid UTF-8, so the fixture cannot be
    built on macOS at all -- `write_bytes` fails with `Illegal byte sequence`
    before any assertion is reached. That is why the `macos` leg passes
    `--exclude tests.test_gitrepo.TestAPathThatIsNotUtf8`: a skip would be a lie under
    `--no-skips`, which exists precisely to catch a test that quietly does
    nothing.

    CLAUDE.md §2 asks that a one-platform test be labelled as one, because a
    green run on the others otherwise reads as proof it guards something. What it
    guards is real and Linux can see it: `git()` runs with `text=True`, so
    reading `ls-files` through it raised `UnicodeDecodeError` out of
    `subprocess.run` -- past the two exceptions `git()` catches, and out of a
    half-finished merge. A latin-1 dotfile name needs no hostile input.
    """

    def test_a_path_that_is_not_utf8_does_not_raise(self) -> None:
        self.diverge("caf\udce9rc", b"ours\n", b"theirs\n")
        found = gitrepo.conflicted(self.repo)
        self.assertEqual(1, len(found))
        self.assertEqual(3, len(next(iter(found.values()))))


if __name__ == "__main__":
    unittest.main()
