"""Where everything lives, and the one list of what the environment can move.

Two things here are load-bearing beyond "compute a path".

**`ENV_KEYS` is the single list of what tupferl reads from the environment**, and
`tests/support.py` builds its sandbox by clearing exactly that list rather than
by naming variables of its own. A variable added here but missed in a hand-kept
copy would silently point a "sandbox" at the *real* installation -- and for a
dotfiles manager the real installation is the developer's own `$HOME` and their
own dotfiles repository. So the rule is: a variable becomes readable in the same
commit that adds it here, never before.

**A hostname becomes a directory name** -- `.tupferl/hosts/<hostname>/` and
`.tupferl/state/<hostname>/` -- so `check_hostname` refuses the values that would
make that a path rather than a name. `..` is the one that matters: a host overlay
for `..` writes above the directory that was supposed to contain it.
"""

from __future__ import annotations

import os
from pathlib import Path

from tupferl.errors import TupferlError

#: Every environment variable tupferl reads, and nothing else. See the module
#: docstring: `tests/support.py` clears this list to build a sandbox, so a name
#: that is read but missing here is a test that runs against the real machine.
ENV_KEYS = (
    "EDITOR",
    "HOME",
    "NO_COLOR",
    "TUPFERL_DIR",
    "TUPFERL_HOSTNAME",
    "VISUAL",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)

#: The directory inside the repository that belongs to tupferl rather than to the
#: user's dotfiles: settings, host overlays, and from milestone 3 the sync
#: snapshots. A constant because a walk that forgot it would manage tupferl's own
#: state as if it were a dotfile.
#:
#: One module excludes it today, `manifest.under`. This said "three modules"
#: when it was written, which was before any of them existed.
META = ".tupferl"


def home() -> Path:
    """The `$HOME` managed files are mirrored against.

    From the environment rather than `Path.home()`, which on POSIX consults
    `getpwuid` when `$HOME` is unset -- so a test that clears `HOME` to get a
    sandbox would silently get the real account's home directory instead. The
    fallback is only for `HOME` genuinely unset, where `Path.home()` is the best
    remaining answer and a wrong one is loud rather than silent.
    """
    said = os.environ.get("HOME")
    return Path(said) if said else Path.home()


def _base(variable: str, fallback: str) -> Path:
    """An XDG-style base directory: the variable if it is set and absolute.

    The XDG specification says a relative value in one of these is invalid and
    must be ignored, and that is also the safe reading: a relative path would be
    resolved against the current directory, so where the repository lives would
    depend on where the command was run from.
    """
    said = os.environ.get(variable)
    if said and Path(said).is_absolute():
        return Path(said)
    return home() / fallback


def repo_dir() -> Path:
    """The git repository holding the managed files.

    `TUPFERL_DIR` wins over `XDG_DATA_HOME` because it is the more specific
    statement: someone who sets it is naming *this* repository, and someone who
    sets `XDG_DATA_HOME` is naming where all data goes.

    A relative `TUPFERL_DIR` is an error rather than a resolution against the
    current directory. It is the kind of value that gets set once in a shell rc
    and then makes `tupferl sync` mean a different repository in every directory
    -- a failure that looks like data loss and is not.
    """
    said = os.environ.get("TUPFERL_DIR")
    if said:
        where = Path(said)
        if not where.is_absolute():
            raise TupferlError(
                f"TUPFERL_DIR is relative ({said!r}), so the repository would depend on "
                f"the current directory; set it to an absolute path."
            )
        return where
    return _base("XDG_DATA_HOME", ".local/share") / "tupferl" / "repo"


def state_dir() -> Path:
    """Where tupferl keeps what is *not* in the repository: backups.

    Under `XDG_STATE_HOME` rather than in the repository, because a backup of a
    file taken before overwriting it must survive the repository being deleted
    and re-cloned -- which is the documented recovery for half of what can go
    wrong.
    """
    return _base("XDG_STATE_HOME", ".local/state") / "tupferl"


def backup_dir() -> Path:
    """Where `sync` writes a copy of a `$HOME` file before overwriting it."""
    return state_dir() / "backup"


def config_file(repo: Path) -> Path:
    """The settings file, inside the repository and therefore shared.

    Shared is the point for `ignore` and `max_file_size` -- every machine should
    agree about those -- and is exactly why `hostname` is *not* only read from
    here. See `hostname`.
    """
    return repo / META / "config.toml"


def check_hostname(name: str) -> str:
    """The same name back, or an error naming what is wrong with it.

    Checked rather than sanitised. Silently rewriting `work/laptop` to
    `work_laptop` would put this host's overlay somewhere the user did not ask
    for and cannot find, and the two would then disagree across machines, which
    is worse than refusing.
    """
    if not name:
        raise TupferlError(
            "the hostname is empty, so this machine has no name to store host "
            "overlays under; set TUPFERL_HOSTNAME or `hostname` in "
            f"{META}/config.toml."
        )
    if name in (".", "..") or "/" in name or "\\" in name or "\0" in name:
        raise TupferlError(
            f"the hostname {name!r} cannot be a directory name, and tupferl stores "
            f"per-host files under {META}/hosts/<hostname>/; set TUPFERL_HOSTNAME "
            f"to a plain name."
        )
    return name


def hostname(configured: str | None = None) -> str:
    """This machine's name, as host overlays and snapshots are keyed by.

    Precedence is environment, then config, then the system -- and the
    environment being *above* the config file is the part worth explaining.
    `config.toml` lives in the repository and is committed, so a `hostname` set
    there applies to every machine that clones it, which is the opposite of what
    "this host is called work-laptop" means. The config key is honoured because
    plan §5 asks for it and it is the right answer for a single-machine
    installation; `TUPFERL_HOSTNAME` is what a *second* machine uses to disagree.

    The system name is cut at the first dot: `laptop.local` and `laptop` are the
    same machine, and a DNS suffix that appears on one network and not another
    would otherwise silently start a second host overlay.
    """
    said = os.environ.get("TUPFERL_HOSTNAME")
    if said:
        return check_hostname(said)
    if configured:
        return check_hostname(configured)
    # Imported here rather than at the top: `socket` costs 4.1ms of a 63.7ms
    # `tupferl --version`, and this is the only line that needs it -- a machine
    # that has said its own name in the environment or the settings never asks
    # the system. Measured; see CLAUDE.md.
    import socket

    return check_hostname(socket.gethostname().split(".")[0])


def host_overlay(repo: Path, host: str) -> Path:
    """Where a file that replaces the shared version on `host` is stored."""
    return repo / META / "hosts" / check_hostname(host)


def snapshot_dir(repo: Path, host: str) -> Path:
    """Where the merge base for `host` is kept: the file as it was after its last
    successful sync (plan §3.4)."""
    return repo / META / "state" / check_hostname(host)
