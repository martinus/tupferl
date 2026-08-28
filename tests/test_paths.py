"""Where things land, and what the environment is allowed to move them with.

Every fixture here sets *both* competing variables to distinguishable values.
A test for "`TUPFERL_DIR` wins over `XDG_DATA_HOME`" that leaves the loser unset
passes just as well against code that reads neither and returns the winner by
accident -- the two symmetric-input trap from CLAUDE.md §2, one variable at a
time.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from tupferl import paths
from tupferl.errors import TupferlError


class Environment(unittest.TestCase):
    """Runs each test with `os.environ` holding exactly what it names.

    `clear=True`: a developer with `TUPFERL_DIR` set in their own shell would
    otherwise run a different test from CI, and the one it breaks is whichever
    one asserts a default.
    """

    def only(self, **names: str) -> None:
        patched = mock.patch.dict(os.environ, names, clear=True)
        patched.start()
        self.addCleanup(patched.stop)


class TestWhereTheRepositoryGoes(Environment):
    def test_the_default_is_under_the_xdg_data_directory(self) -> None:
        self.only(HOME="/home/someone")
        self.assertEqual(Path("/home/someone/.local/share/tupferl/repo"), paths.repo_dir())

    def test_xdg_data_home_moves_it(self) -> None:
        self.only(HOME="/home/someone", XDG_DATA_HOME="/data")
        self.assertEqual(Path("/data/tupferl/repo"), paths.repo_dir())

    def test_tupferl_dir_wins_over_xdg_data_home(self) -> None:
        """Both set, to different answers, so the winner is observable."""
        self.only(HOME="/home/someone", XDG_DATA_HOME="/data", TUPFERL_DIR="/elsewhere/repo")
        self.assertEqual(Path("/elsewhere/repo"), paths.repo_dir())

    def test_a_relative_tupferl_dir_is_refused(self) -> None:
        """Not resolved against the current directory, which would make `sync`
        mean a different repository in every shell."""
        self.only(HOME="/home/someone", TUPFERL_DIR="repo")
        with self.assertRaises(TupferlError) as caught:
            paths.repo_dir()
        self.assertIn("absolute", str(caught.exception))

    def test_a_relative_xdg_data_home_is_ignored(self) -> None:
        """The XDG specification says to ignore a relative value, and ignoring is
        safe here because the fallback is inside `$HOME` rather than in `.`."""
        self.only(HOME="/home/someone", XDG_DATA_HOME="relative/data")
        self.assertEqual(Path("/home/someone/.local/share/tupferl/repo"), paths.repo_dir())


class TestWhereTheBackupsGo(Environment):
    def test_the_default_is_under_the_xdg_state_directory(self) -> None:
        self.only(HOME="/home/someone")
        self.assertEqual(Path("/home/someone/.local/state/tupferl/backup"), paths.backup_dir())

    def test_backups_are_outside_the_repository(self) -> None:
        """The reason they live under `XDG_STATE_HOME` at all: a backup taken
        before overwriting a file must survive the repository being deleted and
        re-cloned, which is the documented recovery for half of what can go
        wrong."""
        self.only(HOME="/home/someone", TUPFERL_DIR="/home/someone/repo")
        self.assertFalse(paths.backup_dir().is_relative_to(paths.repo_dir()))


class TestWhereTheSettingsGo(Environment):
    """`config_file`, which answers with no repository at all.

    That is half the point of the move: the settings used to live inside the
    repository, so reading them meant finding it first -- and meant the
    repository imposing them on every machine that cloned it.
    """

    def test_the_config_is_a_dotfile_in_home_not_a_file_in_the_repository(self) -> None:
        """It lived at `<repo>/.tupferl/config.toml` and was therefore the
        repository's word to every machine that cloned it. Now it is this
        machine's, and sharing it is `tupferl add` like any other dotfile."""
        self.only(HOME="/home/someone")
        self.assertEqual(Path("/home/someone/.config/tupferl/config.toml"), paths.config_file())

    def test_xdg_config_home_moves_it(self) -> None:
        self.only(HOME="/home/someone", XDG_CONFIG_HOME="/elsewhere")
        self.assertEqual(Path("/elsewhere/tupferl/config.toml"), paths.config_file())


class TestTheHostname(Environment):
    def test_the_environment_beats_the_system(self) -> None:
        """The only override there is. `.tupferl/config.toml` had a `hostname`
        above the system's answer and below this one; it was removed, because
        that file is committed and therefore shared, so a value in it cannot be
        *this* machine's own name."""
        self.only(HOME="/home/someone", TUPFERL_HOSTNAME="from-env")
        with mock.patch("socket.gethostname", return_value="the-system-name"):
            self.assertEqual("from-env", paths.hostname())

    def test_the_system_name_loses_its_domain(self) -> None:
        self.only(HOME="/home/someone")
        with mock.patch("socket.gethostname", return_value="laptop.lan.example"):
            self.assertEqual("laptop", paths.hostname())

    def test_an_empty_name_is_refused(self) -> None:
        with self.assertRaises(TupferlError) as caught:
            paths.check_hostname("")
        self.assertIn("TUPFERL_HOSTNAME", str(caught.exception))

    def test_a_name_that_is_a_path_is_refused(self) -> None:
        """`..` is the one that matters: a host overlay for `..` writes above
        the directory that was supposed to contain it."""
        for bad in ("..", ".", "a/b", "a\\b", "a\0b"):
            with self.subTest(name=bad), self.assertRaises(TupferlError):
                paths.check_hostname(bad)

    def test_an_ordinary_name_is_returned_unchanged(self) -> None:
        """The other half: a check that refused everything would pass every test
        above."""
        self.assertEqual("work-laptop", paths.check_hostname("work-laptop"))


class TestTheRepositoryLayout(unittest.TestCase):
    """Plan §3.2 and §3.3: the paths inside the repository, which are part of the
    on-disk format and so are asserted literally rather than derived."""

    def test_the_overlay_is_under_hosts(self) -> None:
        self.assertEqual(
            Path("/repo/.tupferl/hosts/work-laptop"),
            paths.host_overlay(Path("/repo"), "work-laptop"),
        )

    def test_the_snapshot_is_under_state(self) -> None:
        self.assertEqual(
            Path("/repo/.tupferl/state/work-laptop"),
            paths.snapshot_dir(Path("/repo"), "work-laptop"),
        )

    def test_a_hostile_hostname_cannot_escape(self) -> None:
        """The check is on the path builders too, not only on `hostname()` --
        a name that arrived from the config file rather than the environment
        reaches these directly."""
        for build in (paths.host_overlay, paths.snapshot_dir):
            with self.subTest(build=build.__name__), self.assertRaises(TupferlError):
                build(Path("/repo"), "../../etc")


if __name__ == "__main__":
    unittest.main()
