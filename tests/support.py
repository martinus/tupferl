"""A sandbox that cannot reach the real installation, and the git to drive in it.

**The environment is built from nothing.** Not `{**os.environ, "HOME": ...}`.
The difference is the whole point: a variable tupferl reads that a hand-kept
override list has missed is simply absent here, so `tupferl.paths` falls back to
its `$HOME`-relative default -- which is inside the sandbox. Inheriting and
overriding fails the other way, silently, and for this program the real
installation is the developer's own dotfiles and their own remote. A test that
writes there is not a flaky test, it is a lost afternoon.

`tupferl.paths.ENV_KEYS` is the list, and `test_support.py` asserts that this
module clears every name in it. That assertion is the mechanism: a name added to
`ENV_KEYS` without being handled here fails a test rather than quietly pointing a
"sandbox" at `$HOME`.

**git is given its own configuration too**, and for a second reason. git reads
`$HOME/.gitconfig`, and `$HOME` is the sandbox, so a written config is enough --
but the value that matters is `init.defaultBranch`. A fixture that lets git pick
gets `master` on one machine and `main` on another, so a test naming a branch
passes locally and fails in CI. That exact failure is recorded in woswoar's
working agreements; here the branch is written down instead.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

from hypothesis import strategies as st

from tupferl import manifest, paths

#: The repository root, so a subprocess can import the package under test without
#: it being installed. Derived from this file rather than from `os.getcwd()`,
#: because `tools/mutate.py` runs the suite from a *copy* of the tree and the
#: copy is the tree that must be imported.
ROOT = Path(__file__).resolve().parents[1]

#: The only names carried in from the ambient environment, each with a reason:
#:
#: - `PATH`: there is no sandbox-relative answer to where `git` and `python` are.
#: - `TMPDIR`: a sandbox that ignores it lands somewhere the machine may not want
#:   temporary files, and on a full filesystem that is a confusing failure.
#: - `LANG`/`LC_ALL`: git's messages are parsed in one place (`doctor`'s remote
#:   check quotes the last line), so the locale should be whatever the developer
#:   would see rather than a second configuration nobody looks at.
#: - the two `PYTHON*` bytecode names: `tools/mutate.py` sets them so that its
#:   sandboxes cannot leave a `.pyc` behind for the next mutation to read, and a
#:   subprocess started here would otherwise drop them.
#: - `TUPFERL_MUTATE_BUDGET`: how much memory a nested mutation harness may
#:   assume. Dropping it lets an inner harness size itself for the whole host.
#: - `PYTHONWARNINGS`: CI sets it to `error::DeprecationWarning`, and a sandbox
#:   that drops it downgrades that guard to a warning *only in the subprocesses*
#:   -- which is every test that drives the CLI as a user does. Measured before
#:   it was added: a child raising a `DeprecationWarning` exited 0.
CARRIES = (
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONPYCACHEPREFIX",
    "PYTHONWARNINGS",
    "TUPFERL_MUTATE_BUDGET",
)

#: What the branch is called in every fixture -- `seed_home` writes it as
#: `init.defaultBranch`, both repository builders pass it to `--initial-branch`,
#: and `move_on_first_push` names it in a hook. Written down rather than left to
#: git, whose default changed between versions: a test naming a branch would
#: otherwise pass on one machine and fail on another for a reason invisible in
#: its own text. Declared before the fixtures so all four use it -- it began as a
#: constant with one use beside four literals, which is a claim of
#: single-sourcing that was not true.
BRANCH = "main"

#: What a sandboxed hostname is, unless a test says otherwise. A plain name with
#: a hyphen, because that is the shape of a real hostname and a fixture that used
#: `a` would not notice a rule that rejects hyphens.
HOST = "test-host"


@contextmanager
def tempdir(prefix: str = "tupferl-test-") -> Iterator[Path]:
    """A throwaway directory that is removed even when the body raises.

    A context manager rather than `TestCase.enterContext`, which is 3.11+ and
    this project supports 3.10 -- and rather than an `rmtree` in `tearDown`,
    which does not run when `setUp` itself fails and is a delete written by hand
    where one is not needed.
    """
    with tempfile.TemporaryDirectory(prefix=prefix) as box:
        yield Path(box)


def sandbox_env(home: Path, host: str = HOST) -> dict[str, str]:
    """The whole environment a sandboxed `tupferl` runs with, and nothing else.

    `TUPFERL_HOSTNAME` is set rather than left to `socket.gethostname()`: a
    fixture that took the real machine's name would key its snapshots and
    overlays under whatever the developer's laptop is called, so the same test
    would exercise a different path on every machine -- and two-machine tests
    could not exist at all.
    """
    return {
        **{name: os.environ[name] for name in CARRIES if name in os.environ},
        "HOME": str(home),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "TUPFERL_HOSTNAME": host,
        "PYTHONPATH": str(ROOT),
        # A terminal would turn on colour in git's output, which is never what a
        # test is asserting about and varies with how the suite was invoked.
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def gitconfig(host: str) -> str:
    """A complete `~/.gitconfig` for `host` -- identity included.

    Shared with the tests that *manage* `.gitconfig` as a dotfile, which is plan
    §3.3's own example of a host overlay. Writing only the line that differs per
    host silently removes git's identity, and the next commit fails with "Author
    identity unknown" -- so the fixture and the subject need the same generator,
    or one of them is a trap.
    """
    return f"[user]\n\tname = {host}\n\temail = {host}@example.invalid\n"


def seed_home(home: Path, host: str = HOST) -> None:
    """Make `home` look like a real one: the directories, and git's identity.

    The identity is set in `$HOME/.gitconfig` rather than in `GIT_AUTHOR_NAME`
    and friends, because `.gitconfig` is also *a dotfile a test may manage* --
    the fixture and the subject are the same kind of thing here, and a fixture
    that used a mechanism no user has would be testing a path nobody walks.
    """
    for part in (".local/share", ".local/state", ".config"):
        (home / part).mkdir(parents=True, exist_ok=True)
    (home / ".gitconfig").write_text(
        gitconfig(host) + f"[init]\n\tdefaultBranch = {BRANCH}\n[commit]\n\tgpgsign = false\n",
        encoding="utf-8",
    )


@contextmanager
def sandboxed(home: Path, host: str = HOST) -> Iterator[dict[str, str]]:
    """Run the body with `os.environ` replaced by the sandbox, for in-process use.

    `clear=True` rather than an overlay, for the reason in the module docstring:
    what is not listed must be *absent*, not inherited. That covers the names git
    reads as well as the ones tupferl does -- `XDG_CONFIG_HOME`, `GIT_DIR`,
    `GIT_CONFIG_GLOBAL` -- which no list of tupferl's own could.
    """
    env = sandbox_env(home, host)
    with mock.patch.dict(os.environ, env, clear=True):
        yield env


@contextmanager
def quiet() -> Iterator[io.StringIO]:
    """Swallow stdout and stderr, and hand back what was written.

    Both, and returning the text rather than discarding it: a test that silences
    output it never asserts on is one that would pass if the output stopped
    happening. Used where the *noise* is argparse's usage message and the
    assertion is about the exit status.
    """
    spill = io.StringIO()
    with redirect_stdout(spill), redirect_stderr(spill):
        yield spill


def git(args: list[str], cwd: Path, env: dict[str, str]) -> str:
    """Drive the real git and insist it worked.

    Plan §7.1 forbids mocking git. This raises on a non-zero exit rather than
    returning a status, because it is only used for *fixture* setup: a fixture
    that half-failed and carried on is the fixture-too-weak failure mode, and the
    test built on it would be asserting about a repository that does not exist in
    the shape its name claims.
    """
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{done.stderr}")
    return done.stdout.strip()


#: What the refusing hook writes. Two lines: the first is the reason, the second
#: is the trailer `gitrepo.reason` must drop -- which is the shape git's own
#: failures have, and the shape that told `doctor` and `init` apart from each
#: other's bugs.
HOOK_REFUSED = "refusing: policy check failed"
HOOK_TRAILER = "see docs/policy.md"


def break_commits(home: Path) -> None:
    """Make every subsequent `git commit` under `home` fail, on any platform.

    A `core.hooksPath` pointing at a `pre-commit` that exits 1. The obvious
    fixture -- delete `~/.gitconfig` so git has no identity -- was tried first
    and is **wrong**: git falls back to `user@hostname`, and whether that
    succeeds depends on the machine. In a Linux container the hostname is
    `(none)` and git refuses; on a macOS runner it is a real name and the commit
    goes through. Three tests written that way passed on every Linux leg and
    failed on macOS, which is CLAUDE.md §2's "a test that can only fail on one
    platform" in its least obvious form -- it was not even the half anyone
    intended.

    A hook is also a real reason a user's commit fails, which the identity case
    stopped being the moment git learned to guess.
    """
    hooks = home / "hooks"
    hooks.mkdir(exist_ok=True)
    refuse = hooks / "pre-commit"
    # Two lines on stderr, not silence. A real hook explains itself, and the
    # *shape* matters to what tupferl prints: `gitrepo.reason` must reduce a
    # multi-line complaint to the line that explains, so a fixture producing no
    # output at all could not tell that from printing the whole blob.
    refuse.write_text(
        f"#!/bin/sh\necho '{HOOK_REFUSED}' >&2\necho '{HOOK_TRAILER}' >&2\nexit 1\n",
        encoding="utf-8",
    )
    refuse.chmod(0o755)
    with (home / ".gitconfig").open("a", encoding="utf-8") as config:
        config.write(f"[core]\n\thooksPath = {hooks}\n")


def make_remote(where: Path, env: dict[str, str]) -> Path:
    """A bare repository standing in for the remote. No network, ever."""
    where.mkdir(parents=True, exist_ok=True)
    git(["init", "--bare", f"--initial-branch={BRANCH}", str(where)], cwd=where.parent, env=env)
    return where


def make_repo(where: Path, env: dict[str, str], remote: Path | None = None) -> Path:
    """A working repository with one commit, optionally pointed at `remote`.

    One commit rather than none: a repository with no commits answers differently
    to `status`, `rev-parse HEAD` and `ls-remote` than any real one ever will, so
    a fixture without it tests a state that exists for about a second in
    practice.
    """
    where.mkdir(parents=True, exist_ok=True)
    git(["init", f"--initial-branch={BRANCH}"], cwd=where, env=env)
    (where / paths.META).mkdir(exist_ok=True)
    (where / paths.META / ".keep").write_text("", encoding="utf-8")
    git(["add", "-A"], cwd=where, env=env)
    git(["commit", "-m", "initial"], cwd=where, env=env)
    if remote is not None:
        git(["remote", "add", "origin", str(remote)], cwd=where, env=env)
        git(["push", "-u", "origin", BRANCH], cwd=where, env=env)
    return where


def run_cli(
    args: list[str], env: dict[str, str], cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Drive the CLI the way a user does: a separate process, through `-m`.

    Plan §7.1 prefers driving the real thing where speed allows. It does here --
    these are milliseconds -- and it is the only way to check what actually
    reaches stdout, stderr and the exit status, which is `tupferl doctor`'s whole
    product.
    """
    return subprocess.run(
        [sys.executable, "-m", "tupferl", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class SandboxCase(unittest.TestCase):
    """A test with a throwaway `$HOME`, and `os.environ` pointed inside it.

    The temporary directory is torn down by `TemporaryDirectory` rather than by
    an `rmtree` in `tearDown`: if the test fails mid-way the cleanup still runs,
    and there is no path by which a bug in `tearDown` deletes something outside
    the box it created.
    """

    host = HOST

    def setUp(self) -> None:
        box = tempfile.TemporaryDirectory(prefix="tupferl-test-")
        self.addCleanup(box.cleanup)
        self.tmp = Path(box.name)
        self.home = self.tmp / "home"
        self.home.mkdir()
        seed_home(self.home, self.host)
        self.env = sandbox_env(self.home, self.host)
        patched = mock.patch.dict(os.environ, self.env, clear=True)
        patched.start()
        self.addCleanup(patched.stop)

    def write(self, where: Path, text: str) -> Path:
        """Write a file, making its parents. Returns it, so calls can chain."""
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(text, encoding="utf-8")
        return where

    def assertContains(self, haystack: str, needle: str, *args: Any) -> None:
        """`assertIn` with the haystack in the message.

        `assertIn` prints both sides, which for a multi-line report is a wall of
        text with the interesting part in the middle. This says what was looked
        for first.
        """
        if needle not in haystack:
            self.fail(f"{needle!r} not found in:\n{haystack}")


class Computer:
    """One machine in a multi-machine test: a `$HOME`, an environment, a repository.

    Plan §7.1 asks for the real thing where speed allows, and the two methods
    here are that trade-off made explicit rather than per-test:

    - `run` starts a real `python -m tupferl`, which is what a user does and the
      only way to see stdout, stderr and the exit status as they really are;
    - `call` runs the same command in this process, which is 30ms against 70ms
      and is what makes a Hypothesis state machine firing a dozen syncs per
      example finish in seconds. git is still the real binary either way.

    Written once here because two test files build the same two-machine fixture,
    and a fixture that drifts between them is one where a failure in one file
    cannot be reproduced in the other.
    """

    def __init__(self, root: Path, name: str) -> None:
        self.name = name
        self.home = root / name
        self.home.mkdir(parents=True)
        seed_home(self.home, name)
        self.env = sandbox_env(self.home, name)
        # Asked of `tupferl.paths` under this machine's environment rather than
        # retyped as `.local/share/tupferl/repo`. A test that spells the layout
        # out itself cannot notice the layout changing -- and the assertions
        # built on these are `assertFalse(...exists())`, which passes vacuously
        # against a path nothing ever writes.
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.repo = paths.repo_dir()
            self.backups = paths.backup_dir()

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        """One command, as a subprocess, the way a user runs it."""
        return run_cli(list(args), self.env)

    def call(self, *args: str) -> int:
        """One command, in this process. Returns the exit status; output is eaten."""
        from tupferl import __main__ as cli

        with mock.patch.dict(os.environ, self.env, clear=True), quiet():
            return cli.main(list(args))

    def git(self, *args: str) -> str:
        """A git command in this machine's repository, for a test to look with."""
        return git(list(args), cwd=self.repo, env=self.env)

    def write(self, name: str, text: str) -> Path:
        """Write a file in this machine's `$HOME`, making its parents."""
        where = self.home / name
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(text, encoding="utf-8")
        return where

    def read(self, name: str) -> str:
        return (self.home / name).read_text(encoding="utf-8")

    def stored(self, name: str, host: bool = False) -> Path:
        """Where the copy of `name` this machine would use lives, shared or overlay."""
        return manifest.location(self.repo, self.name, host) / name

    def snapshot(self, name: str) -> Path:
        """Where this machine's merge base for `name` lives."""
        return paths.snapshot_dir(self.repo, self.name) / name


def move_on_first_push(remote: Path, env: dict[str, str], root: Path) -> None:
    """Make the remote's next push fail *because somebody else pushed first*.

    Plan §3.4 step 5 is "if the push fails because the remote moved, pull, redo,
    push again", and that race cannot be arranged by two sequential test steps:
    a sync fetches before it pushes, so anything pushed beforehand is simply
    merged in. This produces the real thing -- a `pre-receive` hook that, on its
    first run only, advances the branch and then rejects the push it is
    examining. No git is mocked; the remote genuinely moved in the window.

    The commit it advances to is prepared here and parked on `refs/heads/moved`,
    so the hook itself needs no working tree. Under `refs/heads/` and not a
    namespace of its own: git refuses to push to a ref directly under `refs/`
    with "funny refname", so a parking spot outside the branch namespace cannot
    be created remotely at all.
    """
    work = root / "someone-else"
    git(["clone", str(remote), str(work)], cwd=root, env=env)
    settings = work / paths.META / "config.toml"
    settings.parent.mkdir(parents=True, exist_ok=True)
    with settings.open("a", encoding="utf-8") as extra:
        extra.write("# edited on another machine\n")
    git(["add", "-A"], cwd=work, env=env)
    git(["commit", "-m", "someone else was here"], cwd=work, env=env)
    git(["push", "origin", "HEAD:refs/heads/moved"], cwd=work, env=env)

    hook = remote / "hooks" / "pre-receive"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(
        "#!/bin/sh\n"
        "if [ -f moved-already ]; then exit 0; fi\n"
        ": > moved-already\n"
        # git puts a receiving hook in a quarantine and refuses ref updates from
        # inside it -- "ref updates forbidden inside quarantine environment".
        # Unsetting the variable that marks the quarantine lifts it, and the
        # commit being moved to is already in the real object store because it
        # was pushed above.
        "unset GIT_QUARANTINE_PATH\n"
        f"git update-ref refs/heads/{BRANCH} refs/heads/moved\n"
        "echo 'the remote moved while you were pushing' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    hook.chmod(0o755)


#: One line of a generated file, without the characters that would make the
#: fixtures below mean something else. Shared by both property-test modules,
#: because the excluded set is a fact about how those fixtures are *built* and a
#: character found to break one construction must not be excluded in only one of
#: them.
#:
#: - `\n` and `\r`, because `joined` adds the newlines: a generated one would
#:   change how many lines a region has, and both constructions rest on that
#:   count.
#: - `\x00`, because a file containing one is not text and has no 3-way merge --
#:   `merge.is_text` reports the whole file as one conflict, which is the honest
#:   answer and not what the merge properties are about. Hypothesis found that on
#:   `tests/test_merge_properties.py`'s first run; the excluded case is covered
#:   by `test_merge.TestBinaryFilesHaveNoMerge`, which is what stops the
#:   exclusion being a hole.
def line(max_size: int) -> st.SearchStrategy[str]:
    return st.text(
        alphabet=st.characters(blacklist_characters="\n\r\x00", blacklist_categories=("Cs",)),
        max_size=max_size,
    )


def region(index: int, middle: str) -> list[str]:
    """Three lines whose middle one is editable, all three naming their region.

    The shape both property modules merge over, and the reason each is decidable
    by construction: an edit to one region's middle line has two unchanged lines
    between it and the next, and `git merge-file` merges hunks that *touch* --
    adjacent single-line changes are one hunk, and therefore a conflict.

    The index appears in every line so that no two regions are textually
    identical. Without it git's diff can attribute a change to the wrong region,
    two edits meant for different places land on top of each other, and the
    result is an intermittent conflict that reads as a bug in the merge.
    """
    return [f"{index}: top", f"{index}: {middle}", f"{index}: bottom"]


def joined(lines: list[str]) -> bytes:
    return ("".join(line + "\n" for line in lines)).encode("utf-8")


def regions(middles: list[str]) -> str:
    """A whole file built from the middle line of each region, as text."""
    return joined(
        [part for index, middle in enumerate(middles) for part in region(index, middle)]
    ).decode()


class Machine(SandboxCase):
    """A sandboxed home with a bare remote beside it, and the CLI pointed there.

    The one-machine fixture, here rather than in a test module because two of
    them build it -- `test_manage.py` for the repository commands and
    `test_sync.py` for the engine. `Computer` above is the same idea for the
    *two*-machine tests, which cannot use this one: `SandboxCase` patches
    `os.environ` for a single `$HOME`, and two hostnames have to exist at once.
    """

    def setUp(self) -> None:
        super().setUp()
        self.remote = make_remote(self.tmp / "remote.git", self.env)
        self.repo = paths.repo_dir()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run_cli(list(args), self.env)

    def init(self) -> subprocess.CompletedProcess[str]:
        done = self.run_cli("init", str(self.remote))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        return done

    def log(self) -> list[str]:
        return git(["log", "--format=%s"], self.repo, self.env).splitlines()

    def stored(self, name: str, host: bool = False) -> Path:
        """Where a managed file lives, from `manifest.location` rather than from a
        ternary retyped here -- a test that spells the rule out itself cannot
        notice the rule changing."""
        return manifest.location(self.repo, self.host, host) / name

    def snapshot(self, name: str) -> Path:
        """Where this machine's merge base for `name` lives."""
        return paths.snapshot_dir(self.repo, self.host) / name
