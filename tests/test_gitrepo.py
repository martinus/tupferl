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
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from tests import support
from tupferl import doctor, gitrepo


class TestTheEnvironment:
    """`env()` is what stops git asking a human a question nobody will answer."""

    def test_the_terminal_prompt_is_off(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_TERMINAL_PROMPT": "1"}):
            assert gitrepo.env()["GIT_TERMINAL_PROMPT"] == "0"

    def test_ssh_is_put_in_batch_mode(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert "BatchMode=yes" in gitrepo.env()["GIT_SSH_COMMAND"]

    def test_a_users_own_ssh_command_wins(self) -> None:
        """Someone who named their own ssh command said something more specific
        than this default, and overriding it breaks the one configuration that
        was chosen deliberately."""
        with mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -F /my/config"}):
            assert gitrepo.env()["GIT_SSH_COMMAND"] == "ssh -F /my/config"

    def test_the_rest_of_the_environment_is_carried(self) -> None:
        """Not a fresh environment: git needs the user's `HOME` to find their
        `.gitconfig`, which is the whole reason for driving the real binary."""
        with mock.patch.dict(os.environ, {"HOME": "/somewhere"}):
            assert gitrepo.env()["HOME"] == "/somewhere"


class TestWhenGitCannotAnswer:
    def test_a_call_that_hangs_is_reported_as_a_timeout(self) -> None:
        """Produced with a git alias that sleeps, so the wait is real and local:
        no network, and nothing to be flaky about except the machine being 500ms
        slower than the timeout, which is why the alias sleeps ten times it.
        """
        found = gitrepo.git(["-c", "alias.wait=!sleep 5", "wait"], timeout=0.5)
        assert not found.ok
        assert found.timed_out
        assert "did not answer" in found.err

    def test_the_timeout_message_names_the_command_not_a_flag(self) -> None:
        """`args[0]` is `-c` as often as it is the subcommand, and "git -c did
        not answer" sends the reader looking for a command by that name."""
        found = gitrepo.git(["-c", "alias.wait=!sleep 5", "wait"], timeout=0.5)
        assert "wait" in found.err

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
        assert found.timed_out
        assert waited < 3, "the module constant was ignored"
        assert "0.5s" in found.err

    def test_a_missing_git_is_reported_rather_than_raised(self) -> None:
        """`FileNotFoundError` out of `subprocess` would reach the user as a
        traceback; every caller here is asking a question that has "no" as an
        ordinary answer."""
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
            found = gitrepo.git(["--version"])
        assert not found.ok
        assert not found.timed_out, "not answering and not being there are different"
        assert "not installed" in found.err

    def test_a_missing_working_directory_says_so(self) -> None:
        """Not "git is not installed", which is what `subprocess` makes it look
        like: a missing `cwd` raises `FileNotFoundError`, the same exception as a
        missing binary. `doctor` on a machine with no repository reported the
        wrong one of the two until this was checked before spawning."""
        found = gitrepo.git(["--version"], cwd=Path("/nonexistent-directory"))
        assert not found.ok
        assert "not a directory" in found.err
        assert "not installed" not in found.err

    def test_a_plain_file_as_the_working_directory_says_so(self) -> None:
        """The other half, and the one that used to be a traceback:
        `NotADirectoryError` was caught by nothing."""
        with tempfile.TemporaryDirectory() as box:
            where = Path(box) / "file"
            where.write_text("x", encoding="utf-8")
            found = gitrepo.git(["--version"], cwd=where)
        assert not found.ok
        assert "not a directory" in found.err

    def test_a_real_directory_still_runs(self) -> None:
        """The precondition: the guard must not refuse the ordinary case, which
        is every other call in this project."""
        with tempfile.TemporaryDirectory() as box:
            assert gitrepo.git(["--version"], cwd=Path(box)).ok

    def test_a_failing_call_is_not_a_timeout(self) -> None:
        """The precondition for the two above: `timed_out` must distinguish, not
        just be set whenever something went wrong."""
        with tempfile.TemporaryDirectory() as box:
            found = gitrepo.git(["rev-parse", "--show-toplevel"], cwd=Path(box))
        assert not found.ok
        assert not found.timed_out
        assert "not a git repository" in found.err


class TestTheReasonGitGives:
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
        assert found == "fatal: Could not read from remote repository."
        assert "Cloning into" not in found

    def test_the_trailing_advice_is_not_either(self) -> None:
        assert "repository exists" not in self.reason(self.CLONE)

    def test_the_first_fatal_line_wins_when_there_are_two(self) -> None:
        """The specific one, not the generic follow-up. Every other transcript
        here has a single `fatal:`, where first and last are the same string —
        so without this fixture the choice between them is untested."""
        found = self.reason(self.TWO_FATALS)
        assert found == "fatal: '/tmp/absent.git' does not appear to be a git repository"

    def test_a_single_line_failure_is_itself(self) -> None:
        assert self.reason(self.MISSING) == self.MISSING.strip()

    def test_the_credential_case(self) -> None:
        assert self.reason(self.PROMPTS) == self.PROMPTS.strip()

    def test_the_first_speaking_line_wins_when_none_is_fatal(self) -> None:
        """Two lines, neither marked. The fallback takes the first for the same
        reason the `fatal:` rule does — git leads with the specific complaint —
        and with a one-line fixture that choice is untested."""
        found = self.reason("error: pathspec 'x' did not match\nerror: see git help\n")
        assert found == "error: pathspec 'x' did not match"

    def test_a_failure_with_no_fatal_line_still_says_something(self) -> None:
        """Not every git command marks its complaint. The fallback is the first
        line that is neither blank nor progress."""
        assert (
            self.reason("error: pathspec 'x' did not match\n")
            == "error: pathspec 'x' did not match"
        )

    def test_empty_stderr_does_not_raise(self) -> None:
        """A killed process leaves nothing. A message about something going
        wrong must not itself be an `IndexError`."""
        assert self.reason("") == "no reason given"

    def test_progress_alone_does_not_raise(self) -> None:
        """The pathological case the two filters make possible: every line was
        dropped."""
        assert self.reason("Cloning into 'x'...\n") == "no reason given"


class TestIsRepository:
    """Through the shared `sandbox` fixture, which is what this class used to
    build by hand: eleven lines of `TemporaryDirectory`, `seed_home`,
    `sandbox_env` and a `mock.patch.dict`, all of it now one parameter."""

    def test_the_top_of_a_working_tree(self, sandbox: support.Sandbox) -> None:
        repo = support.make_repo(sandbox.tmp / "repo", sandbox.env)
        assert gitrepo.is_repository(repo)

    def test_not_a_subdirectory_of_one(self, sandbox: support.Sandbox) -> None:
        repo = support.make_repo(sandbox.tmp / "repo", sandbox.env)
        inside = repo / ".config"
        inside.mkdir()
        assert not gitrepo.is_repository(inside)

    def test_not_a_plain_directory(self, sandbox: support.Sandbox) -> None:
        plain = sandbox.tmp / "plain"
        plain.mkdir()
        assert not gitrepo.is_repository(plain)

    def test_a_git_that_cannot_run_answers_no(self, sandbox: support.Sandbox) -> None:
        """Without the `if not found.ok` guard this returns *true* for the
        current directory: `--show-toplevel` printed nothing, `Path("")`
        resolves to the working directory, and the comparison then holds.

        So the fixture has to be the current directory, with no git. Any other
        path would return false either way and the guard would look tested
        while nothing had exercised it.
        """
        plain = sandbox.tmp / "plain"
        plain.mkdir()
        here = os.getcwd()
        os.chdir(plain)
        try:
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
                assert not gitrepo.is_repository(Path.cwd())
        finally:
            # Restored here rather than in a fixture: a working directory left
            # moved would break every later test in the process, and the undo
            # belongs where a reader can see it happen.
            os.chdir(here)


#: The three sides of the conflict the fixture below leaves in the index. Given
#: distinct content for the reason every fixture in milestone 4 is: symmetric
#: inputs make "which side was written" unobservable.
OURS = b"from the local branch\nshared\n"
THEIRS = b"from the remote branch\nshared\n"
BASE = b"from neither\nshared\n"


@dataclass(frozen=True)
class Conflict(support.Sandbox):
    """A repository left mid-merge over `.bashrc`, and the way to add to it."""

    repo: Path

    def commit(self, content: bytes, message: str) -> None:
        (self.repo / ".bashrc").write_bytes(content)
        support.git(["add", "-A"], cwd=self.repo, env=self.env)
        support.git(["commit", "-m", message], cwd=self.repo, env=self.env)


@pytest.fixture
def conflict(sandbox: support.Sandbox) -> Conflict:
    """Two branches that changed the same line, merged into a dirty index."""
    repo = support.make_repo(sandbox.home / "repo", sandbox.env)
    sandbox.write(repo / ".gitignore", "")  # something to commit onto
    made = Conflict(**vars(sandbox), repo=repo)
    made.commit(BASE, "the base")
    support.git(["branch", "other"], cwd=repo, env=sandbox.env)
    made.commit(OURS, "ours")
    support.git(["checkout", "other"], cwd=repo, env=sandbox.env)
    made.commit(THEIRS, "theirs")
    support.git(["checkout", support.BRANCH], cwd=repo, env=sandbox.env)
    # Left to fail: that is what puts the three stages in the index.
    gitrepo.merge(repo, "other")
    return made


@pytest.mark.usefixtures("conflict")
class TestReadingAConflictedIndex:
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

    def test_the_fixture_really_left_a_conflicted_index(self, conflict: Conflict) -> None:
        """Every assertion below is vacuous against a merge that succeeded."""
        assert gitrepo.unmerged(conflict.repo) == [".bashrc"]

    def test_stage_two_is_the_branch_being_merged_into(self, conflict: Conflict) -> None:
        assert gitrepo.version(conflict.repo, gitrepo.OURS, ".bashrc") == OURS

    def test_stage_three_is_the_branch_being_merged_in(self, conflict: Conflict) -> None:
        assert gitrepo.version(conflict.repo, gitrepo.THEIRS, ".bashrc") == THEIRS

    def test_stage_one_is_the_merge_base(self, conflict: Conflict) -> None:
        assert gitrepo.version(conflict.repo, gitrepo.BASE, ".bashrc") == BASE

    def test_a_stage_that_is_not_there_is_none(self, conflict: Conflict) -> None:
        """A path nothing conflicts over has no stages at all."""
        assert gitrepo.version(conflict.repo, gitrepo.OURS, ".gitignore") is None

    def test_bytes_come_back_exactly(self, conflict: Conflict) -> None:
        """The reason this does not go through `gitrepo.git`, which returns
        `stdout.strip()`: a dotfile's trailing newline and any leading blank line
        are content, and stripping them corrupts the file on its way to the
        prompt."""
        gitrepo.abort_merge(conflict.repo)
        conflict.commit(b"\n\nleading and trailing blank lines\n\n", "spacey")
        support.git(["branch", "-D", "other"], cwd=conflict.repo, env=conflict.env)
        support.git(["checkout", "-b", "other", "HEAD~1"], cwd=conflict.repo, env=conflict.env)
        conflict.commit(b"\n\nthe other side\n\n", "spacey too")
        support.git(["checkout", support.BRANCH], cwd=conflict.repo, env=conflict.env)
        gitrepo.merge(conflict.repo, "other")
        assert (
            gitrepo.version(conflict.repo, gitrepo.OURS, ".bashrc")
            == b"\n\nleading and trailing blank lines\n\n"
        )


@dataclass(frozen=True)
class Diverging(support.Sandbox):
    """A repository and the two ways to put a conflict in it.

    A fixture rather than a base class, which is what this was. The hazard the
    old comment named is real and is **not** a `unittest` one: a base holding
    tests makes every test run again under each subclass's name, and *pytest
    collects inherited test methods exactly the same way* -- measured, two
    classes deriving one `test_shared` collect as two nodeids. What removes the
    hazard is the fixture, not the framework: a fixture has nothing to inherit,
    so the rule that had to be remembered is unstateable rather than merely
    unenforced.

    Said carefully because the first version of this docstring credited pytest,
    which would have told B4a and B4b that a shared plain base class is safe.
    """

    repo: Path

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


@pytest.fixture
def index(sandbox: support.Sandbox) -> Diverging:
    return Diverging(**vars(sandbox), repo=support.make_repo(sandbox.home / "r", sandbox.env))


@pytest.mark.usefixtures("index")
class TestReadingTheStagesOfAConflict:
    """`gitrepo.conflicted`, asked directly.

    It had no test of its own: everything reached it through `sync`, which is why
    the `-z` parsing, the missing-stage case and the non-UTF-8 path all went
    unnoticed until a review. The modes it returns decide whether a settled file
    keeps its executable bit and whether it is written at all, so they are worth
    asking about here rather than three layers up.
    """

    def test_two_conflicted_files_come_back_in_a_settled_order(self, index: Diverging) -> None:
        """`unmerged` sorts, and one conflicted file cannot show it.

        The name the user is told to go and resolve comes from here, and a list
        whose order moved between two runs of the same repository would read as
        two different answers. One file passes against a sort, a reverse and no
        sort at all -- the shape §2 warns about -- so this diverges two.

        **Committing them in the reverse order does not reach the answer**, and
        this docstring used to claim it did ("committed in the order that is
        *not* the answer"). git's index is sorted by path, so `ls-files -u`
        emits sorted rows whatever order the commits arrived in, and
        `conflicted` therefore cannot hand back anything else. Measured:
        replacing `sorted` with `list` leaves all 63 tests in this file green,
        which is the sweep's SURVIVED said a second way.

        So the `sorted` is an equivalent mutant *and worth keeping*: it is the
        one line that would still hold if git's ordering ever stopped being an
        accident this code relies on. What is asserted below is the order, which
        is the real claim; that no fixture can break it is a property of git.
        """
        for name in (".zshrc", ".bashrc"):
            index.commit(name, b"base\n")
        support.git(["branch", "other"], cwd=index.repo, env=index.env)
        for name in (".zshrc", ".bashrc"):
            index.commit(name, b"ours\n")
        support.git(["checkout", "-q", "other"], cwd=index.repo, env=index.env)
        for name in (".zshrc", ".bashrc"):
            index.commit(name, b"theirs\n")
        support.git(["checkout", "-q", support.BRANCH], cwd=index.repo, env=index.env)
        subprocess.run(
            ["git", "merge", "other"],
            cwd=index.repo,
            env=index.env,
            capture_output=True,
            check=False,
        )
        assert gitrepo.unmerged(index.repo) == [".bashrc", ".zshrc"]

    def test_a_clean_repository_has_nothing_conflicted(self, index: Diverging) -> None:
        """The empty answer, which every caller reads as "nothing to settle"."""
        index.commit(".bashrc", b"one\n")
        assert gitrepo.conflicted(index.repo) == {}

    def test_all_three_stages_with_their_modes(self, index: Diverging) -> None:
        index.diverge(".bashrc", b"ours\n", b"theirs\n")
        found = gitrepo.conflicted(index.repo)[".bashrc"]
        assert found == {1: 0o100644, 2: 0o100644, 3: 0o100644}

    def test_the_executable_bit_is_carried_per_stage(self, index: Diverging) -> None:
        """Asymmetric, so the answer cannot be right by accident: equal modes
        pass against a function that returns the same number for every stage."""
        index.commit(".sh", b"base\n", 0o755)
        support.git(["branch", "other"], cwd=index.repo, env=index.env)
        index.commit(".sh", b"ours\n", 0o755)
        support.git(["checkout", "-q", "other"], cwd=index.repo, env=index.env)
        index.commit(".sh", b"theirs\n", 0o644)
        support.git(["checkout", "-q", support.BRANCH], cwd=index.repo, env=index.env)
        subprocess.run(
            ["git", "merge", "other"],
            cwd=index.repo,
            env=index.env,
            capture_output=True,
            check=False,
        )
        found = gitrepo.conflicted(index.repo)[".sh"]
        assert found[gitrepo.OURS] == 0o100755
        assert found[gitrepo.THEIRS] == 0o100644

    def test_a_side_that_deleted_the_file_has_no_stage(self, index: Diverging) -> None:
        """What `sync.held` reads to tell "that side has none" from "git would
        not answer" -- two very different things it used to conflate."""
        index.commit(".bashrc", b"base\n")
        support.git(["branch", "other"], cwd=index.repo, env=index.env)
        index.commit(".bashrc", b"ours\n")
        support.git(["checkout", "-q", "other"], cwd=index.repo, env=index.env)
        (index.repo / ".bashrc").unlink()
        support.git(["add", "-A"], cwd=index.repo, env=index.env)
        support.git(["commit", "-m", "gone"], cwd=index.repo, env=index.env)
        support.git(["checkout", "-q", support.BRANCH], cwd=index.repo, env=index.env)
        subprocess.run(
            ["git", "merge", "other"],
            cwd=index.repo,
            env=index.env,
            capture_output=True,
            check=False,
        )
        found = gitrepo.conflicted(index.repo)[".bashrc"]
        assert gitrepo.OURS in found
        assert gitrepo.THEIRS not in found

    def test_a_symlink_is_reported_with_its_own_mode(self, index: Diverging) -> None:
        """`0o120000`, which is what `sync.reconcile` refuses on. Without the
        mode it would look like a plain file and be written *through*."""
        (index.repo / "link").symlink_to(index.home / "target")
        support.git(["add", "-A"], cwd=index.repo, env=index.env)
        support.git(["commit", "-m", "link"], cwd=index.repo, env=index.env)
        support.git(["branch", "other"], cwd=index.repo, env=index.env)
        (index.repo / "link").unlink()
        (index.repo / "link").symlink_to(index.home / "elsewhere")
        support.git(["add", "-A"], cwd=index.repo, env=index.env)
        support.git(["commit", "-m", "relink"], cwd=index.repo, env=index.env)
        support.git(["checkout", "-q", "other"], cwd=index.repo, env=index.env)
        (index.repo / "link").unlink()
        (index.repo / "link").symlink_to(index.home / "third")
        support.git(["add", "-A"], cwd=index.repo, env=index.env)
        support.git(["commit", "-m", "relink again"], cwd=index.repo, env=index.env)
        support.git(["checkout", "-q", support.BRANCH], cwd=index.repo, env=index.env)
        subprocess.run(
            ["git", "merge", "other"],
            cwd=index.repo,
            env=index.env,
            capture_output=True,
            check=False,
        )
        assert gitrepo.conflicted(index.repo)["link"][gitrepo.OURS] == 0o120000


@pytest.fixture
def plain(sandbox: support.Sandbox) -> Path:
    """A directory that is not a repository."""
    where = sandbox.home / "not-a-repo"
    where.mkdir()
    return where


@pytest.mark.usefixtures("plain")
class TestWhenGitWillNotAnswerAboutConflicts:
    """The two ways `conflicted` gets no answer, and the empty dict both give.

    Both matter because of what the caller does with the result: `sync.reconcile`
    iterates it and `integrate` reads "nothing unmerged" as "there is no conflict
    to settle". `None` there is an `AttributeError` from inside an unfinished
    merge -- the state the whole `finally` in `integrate` exists to keep the user
    out of -- so the empty answer is a promise about the *type*, not a
    convenience.

    Neither path was reached by any test: everything else here drives a real
    conflict, where git always answers.
    """

    def test_a_directory_that_is_not_a_repository_has_nothing_conflicted(self, plain: Path) -> None:
        """git exits non-zero with "not a git repository". The precondition is
        asserted first: run against a real repository this is vacuous, because a
        clean repository answers `{}` too."""
        assert not (plain / ".git").exists(), "the fixture is a repository"
        assert gitrepo.conflicted(plain) == {}

    def test_a_git_that_cannot_be_run_at_all_has_nothing_conflicted(self, plain: Path) -> None:
        """The `OSError` arm, which is a different line from the exit-status one.

        `PATH=""`, never `del PATH`: with the variable *absent* `subprocess`
        falls back to `confstr("CS_PATH")` and finds `/usr/bin/git` anyway, so
        the obvious spelling of this fixture runs git successfully and passes for
        the wrong reason.
        """
        with mock.patch.dict(os.environ, {"PATH": ""}):
            assert gitrepo.conflicted(plain) == {}


@pytest.mark.usefixtures("index")
class TestAPathThatIsNotUtf8:
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

    def test_a_path_that_is_not_utf8_does_not_raise(self, index: Diverging) -> None:
        index.diverge("caf\udce9rc", b"ours\n", b"theirs\n")
        found = gitrepo.conflicted(index.repo)
        assert len(found) == 1
        assert len(next(iter(found.values()))) == 3

    def test_such_a_name_can_be_staged(self, index: Diverging) -> None:
        """The same hazard mirrored, and it is a regression this class caught.

        `stage` sends its pathspecs on git's *stdin* since #3, and `git()` runs
        with `text=True` -- so the name has to be encoded on the way out where it
        only had to be decoded on the way in. With the default strict handler
        that raises `UnicodeEncodeError`, a `ValueError`, which sails past all
        three `except` arms in `git()`: the exact class of escape #3 exists to
        close, reintroduced by #3's own fix. `errors="surrogateescape"` answers
        both directions.

        Not through `diverge`: this wants an ordinary file staged, not a
        conflict, so it builds the smallest thing that shows it.
        """
        odd = index.repo / "caf\udce9-plain"
        odd.write_bytes(b"x\n")
        answered = gitrepo.stage(index.repo, [odd])
        assert answered.ok, answered.err
        assert gitrepo.staged(index.repo)


@dataclass(frozen=True)
class Clone(support.Sandbox):
    """A clone that has pushed once, and its remote-tracking ref."""

    remote: Path
    repo: Path
    there: str

    def commit(self, text: str, where: Path | None = None) -> None:
        commit_in(self.repo if where is None else where, self.env, text)


def commit_in(root: Path, env: dict[str, str], text: str) -> None:
    """A one-line commit, for a caller that has no `Clone` yet.

    Module-level because the fixture below needs it *before* `there` is
    knowable: the tracking ref is read off the repository after the first push,
    so building a `Clone` to reach its own `commit` meant constructing one with
    an empty `there` and throwing it away.
    """
    (root / "file").write_text(text, encoding="utf-8")
    support.git(["add", "file"], cwd=root, env=env)
    support.git(["commit", "-m", text], cwd=root, env=env)


@pytest.fixture
def clone(sandbox: support.Sandbox) -> Clone:
    remote = support.make_remote(sandbox.tmp / "remote.git", sandbox.env)
    repo = sandbox.tmp / "clone"
    support.git(["clone", str(remote), str(repo)], cwd=sandbox.tmp, env=sandbox.env)
    commit_in(repo, sandbox.env, "first")
    support.git(["push", "origin", "HEAD"], cwd=repo, env=sandbox.env)
    support.git(["fetch", "origin"], cwd=repo, env=sandbox.env)
    return Clone(**vars(sandbox), remote=remote, repo=repo, there=f"origin/{gitrepo.branch(repo)}")


@pytest.mark.usefixtures("clone")
class TestHowFarApartTwoRefsAre:
    """`distance`, which is the only thing `status` knows about the remote.

    Driven against a real clone and a real push rather than a canned string,
    for plan §7.1's reason and for a sharper one: the numbers come out of
    `rev-list --left-right`, whose order is the fact under test. A fixture with
    the same count on both sides could not tell `(ahead, behind)` from
    `(behind, ahead)`, so no fixture here has one.
    """

    def test_two_refs_that_agree_are_zero_apart(self, clone: Clone) -> None:
        assert gitrepo.distance(clone.repo, "HEAD", clone.there) == (0, 0)

    def test_commits_only_here_are_the_first_number(self, clone: Clone) -> None:
        """Two of them, against nothing on the other side -- so a function that
        returned the pair the wrong way round answers `(0, 2)` and fails."""
        clone.commit("second")
        clone.commit("third")
        assert gitrepo.distance(clone.repo, "HEAD", clone.there) == (2, 0)

    def test_commits_only_there_are_the_second_number(self, clone: Clone) -> None:
        other = clone.tmp / "elsewhere"
        support.git(["clone", str(clone.remote), str(other)], cwd=clone.tmp, env=clone.env)
        clone.commit("from elsewhere", where=other)
        support.git(["push", "origin", "HEAD"], cwd=other, env=clone.env)
        support.git(["fetch", "origin"], cwd=clone.repo, env=clone.env)
        assert gitrepo.distance(clone.repo, "HEAD", clone.there) == (0, 1)

    def test_both_directions_at_once_are_two_different_numbers(self, clone: Clone) -> None:
        """The fixture that makes the two tests above more than a coincidence:
        with 2 here and 1 there, every wrong answer -- swapped, doubled, one
        side counted twice -- is a different pair from the right one."""
        other = clone.tmp / "elsewhere"
        support.git(["clone", str(clone.remote), str(other)], cwd=clone.tmp, env=clone.env)
        clone.commit("from elsewhere", where=other)
        support.git(["push", "origin", "HEAD"], cwd=other, env=clone.env)
        clone.commit("second")
        clone.commit("third")
        support.git(["fetch", "origin"], cwd=clone.repo, env=clone.env)
        assert gitrepo.distance(clone.repo, "HEAD", clone.there) == (2, 1)

    @pytest.mark.parametrize("out", ["", "1", "1 2 3", "one two", "1 -2"])
    def test_output_that_is_not_two_numbers_is_unknown(self, clone: Clone, out: str) -> None:
        """The format guard, which real git cannot reach: `rev-list --count`
        either fails -- caught one line earlier -- or prints two integers.

        Forced by patching `gitrepo.git`, tupferl's own wrapper, rather than by
        arranging a git that misbehaves. The branch exists so that a future git,
        or a `rev-list` reached through some alias, produces `None` instead of an
        `IndexError` traceback out of `tupferl status`; plan §5 rules that out
        for anything a user meets. Each spelling below breaks a different half
        of the condition.
        """
        fake = gitrepo.Result(out=out, err="", code=0)
        with mock.patch("tupferl.gitrepo.git", return_value=fake):
            assert gitrepo.distance(clone.repo, "HEAD", clone.there) is None

    def test_a_ref_that_does_not_resolve_is_unknown_rather_than_equal(self, clone: Clone) -> None:
        """`None`, not `(0, 0)`. The difference is the whole reason the return
        type is optional: `(0, 0)` would have `status` print "is exactly what
        this computer has" about a remote it could not read."""
        assert gitrepo.distance(clone.repo, "HEAD", "origin/nonesuch") is None


class TestNoSpawnFailureEscapes:
    """#3: `git()` answers rather than raising, for every way a spawn can fail.

    Two of the three arms already existed because two failures had reached a
    user as a traceback. The third is the catch-all, and the instance that
    prompted it is `OSError: [Errno 7] Argument list too long` -- reachable by a
    single `tupferl add` of tens of thousands of files.

    **The real thing where it is cheap, a raised `OSError` where it is not.**
    `test_a_real_argument_list_that_is_too_long` builds an argv the kernel
    genuinely refuses, which is the honest fixture and costs milliseconds. The
    others patch `subprocess.run`, because `ENOMEM` and `EACCES` on a spawn
    cannot be arranged from a test without breaking the machine it runs on --
    and the claim there is about the `except` arm, not about the kernel.
    """

    #: One argument of 2 MiB, which is refused on every platform this runs on --
    #: and, crucially, for a reason that does **not** vary by machine.
    #:
    #: `ARG_MAX` does vary: on Linux the whole argv is bounded by
    #: `RLIMIT_STACK / 4`, so it is 2 MiB on this container's 8 MiB stack and
    #: larger on a GitHub runner. A fixture sized against it therefore passes on
    #: one machine and fails on another -- measured, and it is why this test is
    #: shaped this way: an earlier version built 3 MB out of 60 000 short
    #: arguments, which the runner **accepted**, and the three Linux legs went
    #: red while macOS passed.
    #:
    #: A *single* argument has its own cap: `MAX_ARG_STRLEN`, a fixed 32 pages
    #: (128 KiB) on Linux whatever the stack is, and `ARG_MAX` on macOS. 2 MiB
    #: clears both with room and costs one spawn.
    ONE_HUGE = 2 << 20

    #: What `git()` should report for that call: `len("status") + 1` for the
    #: subcommand and `ONE_HUGE + 1` for the argument. A literal rather than the
    #: same sum spelled twice -- the sweep found all three mutations of that
    #: arithmetic surviving a `\d{7}` match.
    HUGE_BYTES = 2_097_160

    def test_a_real_argument_list_that_is_too_long(self) -> None:
        """No mock, and no dependence on a limit that differs per machine."""
        with tempfile.TemporaryDirectory() as box:
            answered = gitrepo.git(["status", "x" * self.ONE_HUGE], cwd=Path(box))
        assert not answered.ok
        assert "could not run `git status`" in answered.err
        # The exact total, because `ARG_MAX` bounds bytes and a count would
        # leave the reader to multiply.
        assert f"totalling {self.HUGE_BYTES} bytes" in answered.err
        assert "2 arguments" in answered.err

    def test_it_is_a_result_rather_than_a_traceback(self) -> None:
        """The whole point, stated on its own: the caller gets an answer.

        Without the `except OSError` arm this raises out of `git()`, through
        `manage.record`, and out of `main` -- past the `TupferlError` handler,
        which does not catch `OSError`.
        """
        with tempfile.TemporaryDirectory() as box:
            answered = gitrepo.git(["status", "x" * self.ONE_HUGE], cwd=Path(box))
        assert isinstance(answered, gitrepo.Result)
        assert answered.code is None

    @pytest.mark.parametrize(
        ("number", "name"), [(12, "Cannot allocate memory"), (13, "Permission denied")]
    )
    def test_other_spawn_failures_are_answered_too(self, number: int, name: str) -> None:
        """The arm is a catch-all on purpose, so the next errno needs no code."""
        with mock.patch("subprocess.run", side_effect=OSError(number, name)):
            answered = gitrepo.git(["status"])
        assert not answered.ok
        assert name in answered.err

    def test_a_missing_git_still_gets_its_own_sentence(self) -> None:
        """`FileNotFoundError` is an `OSError`, so its arm has to come first.
        Ordered the other way, "git is not installed" becomes "could not run
        `git status` (No such file or directory)" -- true, and useless."""
        with mock.patch("subprocess.run", side_effect=FileNotFoundError(2, "No such file")):
            answered = gitrepo.git(["status"])
        assert answered.err == "git is not installed, or not on PATH"


@pytest.fixture
def staging(sandbox: support.Sandbox) -> Path:
    """An empty repository to stage into."""
    return support.make_repo(sandbox.tmp / "repo", sandbox.env)


@pytest.mark.usefixtures("staging")
class TestStagingPastTheArgumentLimit:
    """#3: the pathspecs go on stdin, so `ARG_MAX` does not apply to `add`."""

    def test_the_pathspecs_never_reach_the_command_line(self, staging: Path) -> None:
        """The claim, stated so that no kernel limit is involved in checking it.

        This replaced a fixture that built 4 600 real paths and asserted the
        staging succeeded. That test was **green on the GitHub runner with the
        fix reverted**, and it took a red CI leg to notice why: on Linux the
        whole argv is bounded by `RLIMIT_STACK / 4`, so `ARG_MAX` is 2 MiB on
        this container and larger on a runner with a bigger stack. 2.2 MB of
        pathspec cleared the limit here and did not clear it there -- CLAUDE.md
        §2's fixture too weak to tell the two answers apart, and invisible from
        the test's own text.

        What actually changed is *where the paths go*, and that is machine-
        independent: argv holds four fixed flags, and the paths ride on stdin
        NUL-separated. Asserted by watching the one call `stage` makes.
        """
        many = [staging / f"file-{number}.conf" for number in range(3)]
        for where in many:
            where.write_text("x", encoding="utf-8")

        with mock.patch.object(gitrepo, "git", wraps=gitrepo.git) as watched:
            answered = gitrepo.stage(staging, many)
        assert answered.ok, answered.err

        # One call, not several. `manage.record` commits under a message naming
        # every path, so a `stage` that batched could leave a partial set staged
        # and commit it as the whole -- which is why batching is refused.
        watched.assert_called_once()
        argv = watched.call_args.args[0]
        assert argv == ["add", "--all", "--pathspec-from-file=-", "--pathspec-file-nul"]
        for where in many:
            assert where.name not in " ".join(argv)
        assert watched.call_args.kwargs["fed"] == "file-0.conf\0file-1.conf\0file-2.conf\0"

    def test_many_files_really_do_stage_in_one_call(
        self, sandbox: support.Sandbox, staging: Path
    ) -> None:
        """The end-to-end half, and it claims only what it can show.

        4 600 files through the real git, so the stdin path is exercised at a
        size no argv-based `add` would have enjoyed -- but the assertion is that
        they are all staged, not that a limit was exceeded, because whether it
        was depends on the machine's stack rlimit.
        """
        deep = staging / ("d" * 90) / ("e" * 90) / ("f" * 90)
        deep.mkdir(parents=True)
        many = []
        for number in range(4600):
            where = deep / f"{number:05d}-{'g' * 200}.conf"
            where.write_text("x", encoding="utf-8")
            many.append(where)
        answered = gitrepo.stage(staging, many)
        assert answered.ok, answered.err
        listed = support.git(["diff", "--cached", "--name-only"], cwd=staging, env=sandbox.env)
        assert len(listed.splitlines()) == 4600

    def test_a_name_beginning_with_a_dash_is_a_path_not_an_option(
        self, sandbox: support.Sandbox, staging: Path
    ) -> None:
        """What the `--` separator used to guarantee. A pathspec read from a
        file is never parsed as an option, so the guarantee survives its
        removal -- and this is what says so."""
        odd = staging / "-oddly-named"
        odd.write_text("x", encoding="utf-8")
        answered = gitrepo.stage(staging, [odd])
        assert answered.ok, answered.err
        assert "-oddly-named" in support.git(
            ["diff", "--cached", "--name-only"], cwd=staging, env=sandbox.env
        )

    def test_a_name_with_a_newline_in_it_survives_nul_separation(self, staging: Path) -> None:
        """Why `--pathspec-file-nul` rather than newline separation. A managed
        filename may contain a newline, and split on newlines this becomes two
        pathspecs, neither of which exists."""
        odd = staging / "two\nlines"
        odd.write_text("x", encoding="utf-8")
        answered = gitrepo.stage(staging, [odd])
        assert answered.ok, answered.err
        assert gitrepo.staged(staging)

    def test_an_empty_list_still_refuses_rather_than_staging_everything(
        self, staging: Path
    ) -> None:
        """Measured for the *new* mechanism, because a change of mechanism is
        exactly what could have altered it: empty stdin makes `git add --all`
        stage the whole repository, the same as an empty argv pathspec. So the
        guard is still the only thing between a caller's empty list and the most
        dangerous reading git has."""
        (staging / "untracked.txt").write_text("x", encoding="utf-8")
        answered = gitrepo.stage(staging, [])
        assert not answered.ok
        assert "nothing to stage" in answered.err
        assert not gitrepo.staged(staging)


class TestReadingGitsVersion:
    """`doctor.version_of`, over the shapes real gits actually print."""

    @pytest.mark.parametrize(
        ("said", "want"),
        [
            ("git version 2.43.0", (2, 43, 0)),
            ("git version 2.39.5 (Apple Git-154)", (2, 39, 5)),
            ("git version 2.45.2.windows.1", (2, 45, 2)),
            ("git version 2.25", (2, 25)),
        ],
    )
    def test_the_shapes_vendors_print(self, said: str, want: tuple[int, ...]) -> None:
        assert doctor.version_of(said) == want

    def test_a_string_with_no_version_is_unknown_rather_than_ancient(self) -> None:
        """`None`, not `(0,)`. A tuple of zeros compares below the floor, so a
        vendor string this cannot read would be reported as a git too old to
        run -- refusing to work on a guess about a string."""
        assert doctor.version_of("git version unknown") is None
        assert doctor.version_of("") is None

    def test_the_floor_is_where_pathspec_from_file_arrives(self) -> None:
        """Written out rather than imported: asserting `doctor.OLDEST_GIT ==
        doctor.OLDEST_GIT` is a copy of the code and cannot fail. 2.25 is
        January 2020, and `git add --pathspec-from-file` is what needs it."""
        assert doctor.OLDEST_GIT == (2, 25)

    def said(self, version: str) -> doctor.Check:
        """`doctor.git_present` against a git that reports `version`.

        `gitrepo.git` is patched rather than a real old git installed: the
        claim is about the comparison, and no CI leg can offer 2.24.
        """
        answered = gitrepo.Result(version, "", code=0)
        with mock.patch.object(gitrepo, "git", return_value=answered):
            return doctor.git_present()

    def test_a_git_below_the_floor_fails_the_check(self) -> None:
        found = self.said("git version 2.24.0")
        assert not found.ok
        assert "2.25" in found.detail
        assert "upgrade git" in found.detail

    def test_a_git_at_exactly_the_floor_passes(self) -> None:
        """The boundary, and it has to be spelled `2.25` rather than `2.25.0`.

        Tuple comparison makes the longer sequence the greater one when the
        shared prefix is equal, so `(2, 25, 0) <= (2, 25)` is **False** -- a
        `2.25.0` fixture passes whether the code says `<` or `<=`, and the
        mutation sweep reported exactly that survivor. `(2, 25) <= (2, 25)` is
        True, so this one fails against `<=` and would refuse the oldest git
        that actually works.
        """
        assert self.said("git version 2.25").ok

    def test_a_git_one_patch_above_the_floor_passes(self) -> None:
        """The ordinary reading of "2.25 or newer", kept beside the boundary
        because the boundary above is deliberately the unusual spelling."""
        assert self.said("git version 2.25.1").ok

    def test_a_version_it_cannot_read_passes_and_says_so(self) -> None:
        found = self.said("git version huh")
        assert found.ok
        assert "could not read a version" in found.detail
        assert "2.25" in found.detail
