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


def git(args: list[str], cwd: Path | None = None, timeout: float | None = None) -> Result:
    """Run git with `args`, and answer rather than raise.

    `cwd=None` means the current directory, which is right for `git --version`
    and wrong for everything else -- so every other caller passes one. Not
    defaulted to the repository, because that would make a call against the wrong
    tree the quiet option.

    `timeout=None` means `TIMEOUT`, resolved *here* rather than as
    `timeout: float = TIMEOUT` in the signature. A default argument is evaluated
    once, when the module is imported, so the signature version cannot be changed
    afterwards -- and `doctor.remote` reads `gitrepo.TIMEOUT` when it composes
    its message, so the two would disagree: a test that shortened the constant
    waited the full thirty seconds and was told it had waited half of one.
    """
    waiting = TIMEOUT if timeout is None else timeout
    if cwd is not None and not cwd.is_dir():
        # Checked before spawning, because `subprocess` reports both of these as
        # errors about the *command*: a missing directory raises
        # `FileNotFoundError`, indistinguishable from git not being installed, so
        # `doctor` on a machine with no repository said "git is not installed";
        # and a plain file raises `NotADirectoryError`, which nothing caught at
        # all and which reached the user as a traceback. Both were found by
        # reviewing this milestone rather than by anything failing.
        return Result(False, "", f"{cwd} is not a directory")
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=waiting,
            env=env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        # The whole argument list, not `args[0]`, which is `-c` as often as it is
        # the subcommand -- "git -c did not answer" names a flag and sends the
        # reader looking for a command by that name.
        shown = " ".join(args)
        return Result(False, "", f"`git {shown}` did not answer within {waiting:g}s", True)
    except FileNotFoundError:
        # Now unambiguous: `cwd` was checked above, so the only file `subprocess`
        # can fail to find here is `git` itself.
        return Result(False, "", "git is not installed, or not on PATH")
    return Result(done.returncode == 0, done.stdout.strip(), done.stderr.strip())


def reason(result: Result) -> str:
    """The one line of git's stderr worth showing a user.

    Not the first line, and not the last. Both were tried and both are wrong on
    a real failure, in opposite directions -- measured across four of them:

        $ git clone ssh://unreachable/x
        Cloning into 'x'...                      <- progress, on stderr
        ssh: connect ...: Connection refused
        fatal: Could not read from remote repository.
        <blank>
        Please make sure you have the correct access rights
        and the repository exists.               <- the last line

    Taking the first gives "Cloning into 'x'...", which says nothing went wrong.
    Taking the last gives "and the repository exists.", half a sentence of
    generic advice. `doctor` shipped the second of those for a milestone, and
    `init` shipped the first for about an hour.

    So: the first line git marked `fatal:`, which is git's own word for the line
    that explains. The fallbacks are for the commands that fail without one --
    the first line that is neither blank nor progress, and then a placeholder,
    because a message about something going wrong must not itself raise
    `IndexError`.
    """
    lines = [line.strip() for line in result.err.splitlines() if line.strip()]
    fatal = [line for line in lines if line.startswith("fatal:")]
    if fatal:
        return fatal[0]
    speaking = [line for line in lines if not line.startswith("Cloning into")]
    return speaking[0] if speaking else "no reason given"


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


def has_commits(repo: Path) -> bool:
    """Whether anything has been committed yet.

    A freshly cloned *empty* remote is a real state -- it is what someone gets
    on the first run, having just created the repository on their host -- and it
    is the one state where `HEAD` does not resolve. Asking before assuming keeps
    every caller from having to interpret a `rev-parse HEAD` failure, which is
    also what "the repository is broken" looks like.
    """
    return git(["rev-parse", "--verify", "HEAD"], cwd=repo).ok


def clone(url: str, into: Path) -> Result:
    """Clone `url` into `into`, which must not exist yet.

    `--` before the URL: a URL beginning with a dash would otherwise be read as
    an option by git. tupferl passes whatever the user typed straight through, so
    this is the one place to stop it.
    """
    into.parent.mkdir(parents=True, exist_ok=True)
    return git(["clone", "--", url, str(into)], cwd=into.parent)


def stage(repo: Path, paths: list[Path]) -> Result:
    """Stage exactly these paths, including deletions.

    `--` again, and for a sharper reason than the URL case: a managed file is
    named by the user and can legitimately begin with a dash. `git add -- -x`
    stages a file called `-x`; without the separator git reports "unknown
    switch" for a dotfile somebody really has.

    Paths are given absolute and passed relative, which is what git records.
    `relative_to` raising is the point rather than a nuisance: every caller
    builds these from `repo`, so one that does not is a bug, and failing here
    names the path instead of handing git something it will interpret against
    its own working directory.
    """
    relative = [str(path.relative_to(repo)) for path in paths]
    return git(["add", "--all", "--", *relative], cwd=repo)


def commit(repo: Path, message: str) -> Result:
    """Commit what is staged. Returns git's own answer, unexamined.

    Deliberately *not* checking "is anything staged" first: that is a race, and
    git already answers it. A caller that cares looks at `changed` before
    staging, which is a question about the working tree rather than about a
    moment in the middle of this function.
    """
    return git(["commit", "-m", message], cwd=repo)
