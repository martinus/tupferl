"""Where things land, and what the environment is allowed to move them with.

Every fixture here sets *both* competing variables to distinguishable values.
A test for "`TUPFERL_DIR` wins over `XDG_DATA_HOME`" that leaves the loser unset
passes just as well against code that reads neither and returns the winner by
accident -- the two symmetric-input trap from CLAUDE.md §2, one variable at a
time.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

import pytest

from tupferl import paths
from tupferl.errors import TupferlError


@pytest.fixture
def only() -> Iterator[Callable[..., None]]:
    """Runs the test with `os.environ` holding exactly the names it passes here.

    `clear=True`: a developer with `TUPFERL_DIR` set in their own shell would
    otherwise run a different test from CI, and the one it breaks is whichever
    one asserts a default.

    A fixture handing back a *callable* rather than one that patches on entry,
    because each test names a different environment, and a fixture that patched
    on entry could not know which. Every one of the eleven call sites calls it
    exactly once; the `ExitStack` is what unwinds at teardown, which is what the
    `addCleanup` this replaced did without needing a base class to hang it on.
    """
    with ExitStack() as stack:

        def named(**names: str) -> None:
            stack.enter_context(mock.patch.dict(os.environ, names, clear=True))

        yield named


class TestWhereTheRepositoryGoes:
    def test_the_default_is_under_the_xdg_data_directory(self, only: Callable[..., None]) -> None:
        only(HOME="/home/someone")
        assert paths.repo_dir() == Path("/home/someone/.local/share/tupferl/repo")

    def test_xdg_data_home_moves_it(self, only: Callable[..., None]) -> None:
        only(HOME="/home/someone", XDG_DATA_HOME="/data")
        assert paths.repo_dir() == Path("/data/tupferl/repo")

    def test_tupferl_dir_wins_over_xdg_data_home(self, only: Callable[..., None]) -> None:
        """Both set, to different answers, so the winner is observable."""
        only(HOME="/home/someone", XDG_DATA_HOME="/data", TUPFERL_DIR="/elsewhere/repo")
        assert paths.repo_dir() == Path("/elsewhere/repo")

    def test_a_relative_tupferl_dir_is_refused(self, only: Callable[..., None]) -> None:
        """Not resolved against the current directory, which would make `sync`
        mean a different repository in every shell."""
        only(HOME="/home/someone", TUPFERL_DIR="repo")
        with pytest.raises(TupferlError) as caught:
            paths.repo_dir()
        assert "absolute" in str(caught.value)

    def test_a_relative_xdg_data_home_is_ignored(self, only: Callable[..., None]) -> None:
        """The XDG specification says to ignore a relative value, and ignoring is
        safe here because the fallback is inside `$HOME` rather than in `.`."""
        only(HOME="/home/someone", XDG_DATA_HOME="relative/data")
        assert paths.repo_dir() == Path("/home/someone/.local/share/tupferl/repo")


class TestWhereTheBackupsGo:
    def test_the_default_is_under_the_xdg_state_directory(self, only: Callable[..., None]) -> None:
        only(HOME="/home/someone")
        assert paths.backup_dir() == Path("/home/someone/.local/state/tupferl/backup")

    def test_backups_are_outside_the_repository(self, only: Callable[..., None]) -> None:
        """The reason they live under `XDG_STATE_HOME` at all: a backup taken
        before overwriting a file must survive the repository being deleted and
        re-cloned, which is the documented recovery for half of what can go
        wrong."""
        only(HOME="/home/someone", TUPFERL_DIR="/home/someone/repo")
        assert not paths.backup_dir().is_relative_to(paths.repo_dir())


class TestWhereTheSettingsGo:
    """`config_file`, which answers with no repository at all.

    That is half the point of the move: the settings used to live inside the
    repository, so reading them meant finding it first -- and meant the
    repository imposing them on every machine that cloned it.
    """

    def test_the_config_is_a_dotfile_in_home_not_a_file_in_the_repository(
        self, only: Callable[..., None]
    ) -> None:
        """It lived at `<repo>/.tupferl/config.toml` and was therefore the
        repository's word to every machine that cloned it. Now it is this
        machine's, and sharing it is `tupferl add` like any other dotfile."""
        only(HOME="/home/someone")
        assert paths.config_file() == Path("/home/someone/.config/tupferl/config.toml")

    def test_xdg_config_home_moves_it(self, only: Callable[..., None]) -> None:
        only(HOME="/home/someone", XDG_CONFIG_HOME="/elsewhere")
        assert paths.config_file() == Path("/elsewhere/tupferl/config.toml")


class TestTheHostname:
    def test_the_environment_beats_the_system(self, only: Callable[..., None]) -> None:
        """The only override there is. `.tupferl/config.toml` had a `hostname`
        above the system's answer and below this one; it was removed, because
        that file is committed and therefore shared, so a value in it cannot be
        *this* machine's own name."""
        only(HOME="/home/someone", TUPFERL_HOSTNAME="from-env")
        with mock.patch("socket.gethostname", return_value="the-system-name"):
            assert paths.hostname() == "from-env"

    def test_the_system_name_loses_its_domain(self, only: Callable[..., None]) -> None:
        only(HOME="/home/someone")
        with mock.patch("socket.gethostname", return_value="laptop.lan.example"):
            assert paths.hostname() == "laptop"

    def test_an_empty_name_is_refused(self) -> None:
        with pytest.raises(TupferlError) as caught:
            paths.check_hostname("")
        assert "TUPFERL_HOSTNAME" in str(caught.value)

    @pytest.mark.parametrize("bad", ["..", ".", "a/b", "a\\b", "a\0b"])
    def test_a_name_that_is_a_path_is_refused(self, bad: str) -> None:
        """`..` is the one that matters: a host overlay for `..` writes above
        the directory that was supposed to contain it."""
        with pytest.raises(TupferlError):
            paths.check_hostname(bad)

    def test_an_ordinary_name_is_returned_unchanged(self) -> None:
        """The other half: a check that refused everything would pass every test
        above."""
        assert paths.check_hostname("work-laptop") == "work-laptop"


class TestTheRepositoryLayout:
    """Plan §3.2 and §3.3: the paths inside the repository, which are part of the
    on-disk format and so are asserted literally rather than derived."""

    def test_the_overlay_is_under_hosts(self) -> None:
        assert paths.host_overlay(Path("/repo"), "work-laptop") == Path(
            "/repo/.tupferl/hosts/work-laptop"
        )

    def test_the_snapshot_is_under_state(self) -> None:
        assert paths.snapshot_dir(Path("/repo"), "work-laptop") == Path(
            "/repo/.tupferl/state/work-laptop"
        )

    @pytest.mark.parametrize(
        "build",
        [paths.host_overlay, paths.snapshot_dir],
        ids=["host_overlay", "snapshot_dir"],
    )
    def test_a_hostile_hostname_cannot_escape(self, build: Callable[[Path, str], Path]) -> None:
        """The check is on the path builders too, not only on `hostname()` --
        a name that arrived from the config file rather than the environment
        reaches these directly."""
        with pytest.raises(TupferlError):
            build(Path("/repo"), "../../etc")
