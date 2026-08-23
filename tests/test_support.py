"""The sandbox's own guarantee: nothing in here can reach the real installation.

This is the test the rest of the suite rests on. Every other test writes files,
runs git and would happily do both in the developer's `$HOME` if the sandbox were
wrong -- and it would do it *quietly*, because a dotfiles manager pointed at real
dotfiles does exactly what it is supposed to do.

So the fixture poisons every name in `tupferl.paths.ENV_KEYS` first. A sandbox
that inherits rather than replaces then resolves to `/poison/...`, which no
assertion about "under the sandbox home" can accept. Poisoning from `ENV_KEYS`
rather than from a list written here is what makes these assertions keep up with
the code: a variable added there is poisoned by this fixture the same day.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests import support
from tupferl import paths

#: A directory that does not exist and never will. Absolute, because
#: `TUPFERL_DIR` rejects a relative value -- the poison has to survive that check
#: to be able to leak at all.
POISON = "/poison"


class Boxed(unittest.TestCase):
    """A throwaway directory and a seeded home, without the environment patch.

    `support.SandboxCase` would patch `os.environ` in `setUp`, which is exactly
    what these tests are trying to observe. So this stops one step short.
    """

    def setUp(self) -> None:
        box = tempfile.TemporaryDirectory(prefix="tupferl-support-")
        self.addCleanup(box.cleanup)
        self.box = Path(box.name)
        self.home = self.box / "home"
        self.home.mkdir()
        support.seed_home(self.home)
        self.env = support.sandbox_env(self.home)


class TestTheSandboxReplacesTheEnvironment(Boxed):
    def setUp(self) -> None:
        super().setUp()
        poisoned = {name: f"{POISON}/{name}" for name in paths.ENV_KEYS}
        patched = mock.patch.dict(os.environ, poisoned)
        patched.start()
        self.addCleanup(patched.stop)

    def test_no_poisoned_value_survives(self) -> None:
        with support.sandboxed(self.home):
            leaked = [name for name, value in os.environ.items() if value.startswith(POISON)]
        self.assertEqual([], leaked)

    def test_every_path_resolves_inside_the_sandbox(self) -> None:
        """The property that matters, stated as paths rather than as variables.

        `ENV_KEYS` is the list of what gets cleared, but clearing is only the
        means. What must be true is that the functions those variables feed all
        answer inside the box -- so this asks them, rather than asking about the
        environment a second time.
        """
        with support.sandboxed(self.home):
            answers = [paths.home(), paths.repo_dir(), paths.state_dir(), paths.backup_dir()]
        for answer in answers:
            self.assertTrue(answer.is_relative_to(self.home), f"{answer} is outside {self.home}")

    def test_the_poison_really_would_be_visible(self) -> None:
        """The precondition, asserted rather than assumed.

        Without this, both tests above pass just as well against a fixture that
        never managed to set anything -- "no poisoned value survives" is
        trivially true when there was no poison. CLAUDE.md §2 calls that a
        negative assertion whose precondition was never established.
        """
        self.assertTrue(paths.repo_dir().is_relative_to(POISON))

    def test_the_hostname_is_the_sandbox_one(self) -> None:
        """Not the real machine's, which differs per developer and per CI leg."""
        with support.sandboxed(self.home, host="other-host"):
            self.assertEqual("other-host", paths.hostname())


class TestTheFixturesAreReal(Boxed):
    """The helpers build git repositories, not directories that look like them."""

    def test_make_repo_is_a_repository_with_a_commit(self) -> None:
        repo = support.make_repo(self.box / "repo", self.env)
        self.assertEqual("main", support.git(["branch", "--show-current"], repo, self.env))
        self.assertEqual("initial", support.git(["log", "-1", "--format=%s"], repo, self.env))

    def test_a_pushed_repo_and_its_remote_agree(self) -> None:
        """A remote is only a remote if something can be read back out of it."""
        remote = support.make_remote(self.box / "remote.git", self.env)
        repo = support.make_repo(self.box / "repo", self.env, remote=remote)
        here = support.git(["rev-parse", "HEAD"], repo, self.env)
        there = support.git(["ls-remote", str(remote), "refs/heads/main"], repo, self.env)
        self.assertTrue(there.startswith(here), f"{there} does not start with {here}")

    def test_git_raises_rather_than_returning_a_failure(self) -> None:
        """The fixture helper must be loud: a half-built repository is the weak
        fixture every other test would then be written against."""
        with self.assertRaises(AssertionError):
            support.git(["rev-parse", "HEAD"], self.box, self.env)


if __name__ == "__main__":
    unittest.main()
