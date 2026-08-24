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

from tupferl import paths

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
        "[user]\n"
        f"\tname = {host}\n"
        f"\temail = {host}@example.invalid\n"
        "[init]\n"
        # Written down rather than left to git: the default changed between git
        # versions, so a test that names a branch would pass on one machine and
        # fail on another for a reason invisible in its own text.
        "\tdefaultBranch = main\n"
        "[commit]\n"
        "\tgpgsign = false\n",
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


def make_remote(where: Path, env: dict[str, str]) -> Path:
    """A bare repository standing in for the remote. No network, ever."""
    where.mkdir(parents=True, exist_ok=True)
    git(["init", "--bare", "--initial-branch=main", str(where)], cwd=where.parent, env=env)
    return where


def make_repo(where: Path, env: dict[str, str], remote: Path | None = None) -> Path:
    """A working repository with one commit, optionally pointed at `remote`.

    One commit rather than none: a repository with no commits answers differently
    to `status`, `rev-parse HEAD` and `ls-remote` than any real one ever will, so
    a fixture without it tests a state that exists for about a second in
    practice.
    """
    where.mkdir(parents=True, exist_ok=True)
    git(["init", "--initial-branch=main"], cwd=where, env=env)
    (where / paths.META).mkdir(exist_ok=True)
    (where / paths.META / ".keep").write_text("", encoding="utf-8")
    git(["add", "-A"], cwd=where, env=env)
    git(["commit", "-m", "initial"], cwd=where, env=env)
    if remote is not None:
        git(["remote", "add", "origin", str(remote)], cwd=where, env=env)
        git(["push", "-u", "origin", "main"], cwd=where, env=env)
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
