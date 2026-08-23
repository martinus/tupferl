"""Every call to git, in one place, because the ways it hangs are one fact.

Plan §5: call the `git` binary through `subprocess`, never GitPython. That keeps
behaviour identical to the user's own git -- their `.gitconfig`, their
credential helper, their merge driver -- and it is why the tests can drive the
real thing instead of a mock.

The wrapper exists for what a bare `subprocess.run(["git", ...])` gets wrong on
the paths that touch a remote:

- **git waits for a human.** An https remote with no cached credential asks on
  the terminal; an ssh remote with a passphrase asks too. In `tupferl doctor`,
  and in any non-interactive use, that is not a prompt -- it is a hang with no
  output. `GIT_TERMINAL_PROMPT=0` and ssh's `BatchMode=yes` turn both into a
  failure that can be reported.
- **and it waits without a bound.** A remote that accepts the connection and
  never answers holds the process for ever, so every call carries a timeout and
  the caller gets `TimedOut` rather than silence.

`GIT_SSH_COMMAND` is only set when the user has not set it. Someone who has
named their own ssh command has said something more specific than this default,
and overriding it would break the one configuration that was chosen deliberately.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

#: Long enough for a slow network, short enough that a wedged remote is reported
#: within the time someone will wait before pressing ctrl-c. Local operations do
#: not need it; they get it anyway, because a timeout that only some calls carry
#: is one somebody will forget to pass.
TIMEOUT = 30.0


class Result(NamedTuple):
    """What git said. `ok` rather than an exception, because most callers here
    are asking a question ("is this a repository?") where the answer "no" is not
    exceptional."""

    ok: bool
    out: str
    err: str
    #: True when the call was killed by `TIMEOUT` rather than exiting. A separate
    #: field rather than a magic return code: "the remote never answered" and
    #: "the remote said no" need different sentences, and a caller that forgets
    #: the difference should not be able to print the wrong one by accident.
    timed_out: bool = False


def env() -> dict[str, str]:
    """The environment git is run with: the caller's, plus the two no-prompt keys."""
    prepared = dict(os.environ)
    prepared["GIT_TERMINAL_PROMPT"] = "0"
    prepared.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return prepared


def git(args: list[str], cwd: Path | None = None, timeout: float = TIMEOUT) -> Result:
    """Run git with `args`, and answer rather than raise.

    `cwd=None` means the current directory, which is right for `git --version`
    and wrong for everything else -- so every other caller passes one. Not
    defaulted to the repository, because that would make a call against the wrong
    tree the quiet option.
    """
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Result(False, "", f"git {args[0]} did not answer within {timeout:g}s", True)
    except FileNotFoundError:
        return Result(False, "", "git is not installed, or not on PATH")
    return Result(done.returncode == 0, done.stdout.strip(), done.stderr.strip())


def is_repository(where: Path) -> bool:
    """Whether `where` is the top of a git working tree.

    `--show-toplevel` compared against `where`, not `--is-inside-work-tree`,
    which answers yes for any *sub*directory. tupferl's repository directory
    being inside someone else's repository is a real shape -- `~/.local/share`
    is under `$HOME`, and a `$HOME` that is itself a git repository is a
    configuration people have -- and treating that as "already initialised"
    would have tupferl commit dotfiles into whatever tree happened to enclose it.
    """
    found = git(["rev-parse", "--show-toplevel"], cwd=where)
    if not found.ok:
        return False
    # Resolved on both sides: `--show-toplevel` reports the real path, so a
    # repository reached through a symlink -- which `/tmp` is on macOS -- would
    # otherwise compare unequal to the path the caller holds.
    return Path(found.out).resolve() == where.resolve()
