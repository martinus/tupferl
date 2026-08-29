"""`tupferl doctor`: the checks worth running before something goes wrong.

Plan §4 asks it to check "git presence, remote access, permissions, dangling
state". Two rules shape how they are written:

- **Every failure names the next action.** A check that says "remote not
  reachable" and stops has told the user what they already suspected.
- **A check that cannot run is not a check that passed.** `Check.ok` is a
  `bool | None`, and `None` means "not applicable yet" -- there is no repository,
  so there is nothing to say about its remote. Reporting those as ✔ would make
  `doctor` on a machine with nothing installed the most reassuring run there is.

That third state is also why `ok` is not defaulted. In woswoar, `Check.ok`
defaulted to `None` and `assertFalse(status.ok)` passed for `None` too, so
nothing could tell a failure from a skip (woswoar#206). Here it is required, and
the summary counts the three states separately.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from tupferl import gitrepo, paths
from tupferl.config import Config, load
from tupferl.errors import TupferlError

#: What a reader sees per state. `None` gets its own mark rather than sharing
#: one: "-" is not a quieter tick, it is a different answer.
MARKS: dict[bool | None, str] = {True: "✔", False: "✘", None: "-"}


class Check(NamedTuple):
    """One question, its answer, and what to do about a "no"."""

    ok: bool | None
    title: str
    #: One sentence. For a failure it must say the next action; for a pass it
    #: says what was found, so a green run is still evidence rather than a tick.
    detail: str


#: The oldest git that can run this program, and why (#3).
#:
#: 2.25 (January 2020) is where `git add --pathspec-from-file` arrives, which
#: `gitrepo.stage` uses so that a `tupferl add` of tens of thousands of files
#: cannot exceed `ARG_MAX`. It is a floor with a *reason* rather than a version
#: somebody felt comfortable with: raise it only when a feature needs it, and
#: name the feature here when you do.
OLDEST_GIT = (2, 25)


def version_of(said: str) -> tuple[int, ...] | None:
    """The version out of `git --version`, or `None` if it does not say one.

    `git version 2.43.0` on Linux, `git version 2.39.5 (Apple Git-154)` on a
    macOS runner, and vendors append more. So: take the first field that starts
    with a digit and read the leading numeric parts of it, and stop at the first
    part that is not a number -- `2.39.5` and `2.39.5.windows.1` both come back
    as numbers this can compare, and a string with no version in it comes back
    `None` rather than as `(0,)`, which would read as "very old git".
    """
    for field in said.split():
        if field[:1].isdigit():
            parts = []
            for piece in field.split("."):
                if not piece.isdigit():
                    break
                parts.append(int(piece))
            return tuple(parts) if parts else None
    return None


def git_present() -> Check:
    """Is there a git to call at all, and is it new enough? Everything depends on it."""
    found = gitrepo.git(["--version"])
    if not found.ok:
        return Check(False, "git", "git is not on PATH; install git and run this again")
    version = version_of(found.out)
    wanted = ".".join(str(part) for part in OLDEST_GIT)
    if version is None:
        # Reported rather than assumed either way. Passing would hide a git too
        # old to stage; failing would refuse to run on a vendor build whose
        # `--version` this cannot read, which is a guess about a string.
        return Check(
            True, "git", f"{found.out} (could not read a version; tupferl needs {wanted} or newer)"
        )
    if version < OLDEST_GIT:
        return Check(
            False,
            "git",
            f"{found.out} is older than the {wanted} tupferl needs for `git add "
            f"--pathspec-from-file`; upgrade git",
        )
    return Check(True, "git", found.out)


def repository(repo: Path) -> Check:
    """Is the repository there, and is it a repository?

    The middle case is the one worth separating: a directory that exists and is
    not a git tree is a half-finished `init` or a hand-made mistake, and telling
    it apart from "nothing here yet" is the difference between "run init" and
    "this is not empty, look at it first".
    """
    if not repo.exists():
        return Check(False, "repository", f"no repository at {repo}; run `tupferl init <git-url>`")
    if not gitrepo.is_repository(repo):
        return Check(
            False,
            "repository",
            f"{repo} exists but is not a git repository; move it aside, then run "
            f"`tupferl init <git-url>`",
        )
    return Check(True, "repository", str(repo))


def settings() -> tuple[Check, Config]:
    """Does the config file parse, and what does it say?

    Returns the parsed settings as well as the check, because the next two
    checks need them and parsing twice would let the two answers differ if the
    file changed in between -- a small window, and the kind that produces a
    report nobody can reproduce.
    """
    where = paths.config_file()
    try:
        found = load(where)
    except TupferlError as wrong:
        return Check(False, "settings", str(wrong)), Config()
    if not where.exists():
        return Check(None, "settings", f"no {where.name}, so the defaults apply"), found
    return Check(True, "settings", f"{where} parses"), found


def host(config: Config) -> Check:
    """Does this machine have a name that can key a host overlay?"""
    try:
        return Check(True, "hostname", paths.hostname())
    except TupferlError as wrong:
        return Check(False, "hostname", str(wrong))


def remote(repo: Path, ok: bool) -> Check:
    """Is there a remote, and does it answer?

    `ok` is the repository check's answer, passed in rather than recomputed: with
    no repository there is nothing to ask, and asking anyway would report "no
    remote configured" as a *failure* on a machine where the only real problem is
    that `init` has not been run.
    """
    if not ok:
        return Check(None, "remote", "no repository yet, so there is no remote to reach")
    # Which remote is *the* remote is a decision, and `sync` makes the same one.
    # It lives in `gitrepo.first_remote` so the two cannot report on different
    # remotes -- a green `doctor` for a remote nothing pushes to.
    first = gitrepo.first_remote(repo)
    if first is None:
        return Check(
            False,
            "remote",
            f"no remote configured, so `tupferl sync` cannot share anything; add one "
            f"with `git -C {repo} remote add origin <git-url>`",
        )
    reached = gitrepo.git(["ls-remote", "--exit-code", first, "HEAD"], cwd=repo)
    if reached.timed_out:
        return Check(False, "remote", f"{first} did not answer within {gitrepo.TIMEOUT:g}s")
    if not reached.ok:
        detail = f"{first} refused: {gitrepo.reason(reached)}; check the URL and your credentials"
        return Check(False, "remote", detail)
    return Check(True, "remote", f"{first} answers")


def writable(where: Path) -> Check:
    """Can tupferl write the backups it takes before overwriting a `$HOME` file?

    Tested by creating the directory and asking the OS, not by reading the mode
    bits: the mode is not the whole answer where a read-only mount, an immutable
    flag or a full filesystem is involved, and this check exists precisely to be
    right about the cases nobody predicted.
    """
    try:
        where.mkdir(parents=True, exist_ok=True)
    except OSError as refused:
        return Check(False, "backups", f"cannot create {where} ({refused.strerror}); fix that path")
    # survivor: branch -- tupferl/doctor.py:187 in writable() -- the `if` is never taken --
    #   unreachable in CI and locally, both of which run as root: root ignores the mode bits, so no
    #   directory is unwritable.
    #   `test_doctor.TestTheBackupCheck.test_a_directory_that_exists_and_cannot_be_written_fails` is
    #   the test, and it skips itself with that reason.
    if not os.access(where, os.W_OK):
        # survivor: return-value -- tupferl/doctor.py:188 in writable() -- returns `None` instead of
        #   `Check(False, 'backups', f'{wh…` -- same as `doctor.py:187` -- the line only runs for an
        #   unwritable directory, which root does not have. Its test exists and skips.
        return Check(False, "backups", f"{where} is not writable; fix its permissions")
    return Check(True, "backups", str(where))


def dangling(repo: Path, ok: bool) -> Check:
    """Is the repository mid-operation, or holding changes nobody committed?

    Both are states a killed `sync` leaves behind, and both make the next sync
    do something surprising, so they are reported together as one question: is
    this tree in the state tupferl expects to find it in?
    """
    if not ok:
        return Check(None, "state", "no repository yet, so there is nothing in progress")
    inside = gitrepo.git(["rev-parse", "--git-dir"], cwd=repo)
    if not inside.ok:
        # A failed git call is not "nothing in progress". Folding the two
        # together is CLAUDE.md §8's pass nobody can explain: this check would
        # report ✔ for a repository git could not read at all.
        return Check(False, "state", f"git cannot read {repo}: {gitrepo.reason(inside)}")
    git_dir = repo / inside.out
    # `gitrepo.UNFINISHED` rather than a list here: `sync` refuses to run on the
    # same markers, and two copies of that list is one that will lose an entry.
    marker = next((name for name in gitrepo.UNFINISHED if (git_dir / name).exists()), None)
    if marker is not None:
        return Check(
            False,
            "state",
            f"an operation is unfinished ({marker} is present); finish or abort it with git, "
            f"then sync again",
        )
    changed = gitrepo.git(["status", "--porcelain"], cwd=repo)
    if not changed.ok:
        return Check(False, "state", f"`git status` failed in {repo}: {gitrepo.reason(changed)}")
    if changed.out:
        count = len(changed.out.splitlines())
        return Check(
            False,
            "state",
            f"{count} uncommitted change(s) in {repo}; `tupferl sync` will commit them, or "
            f"inspect them first with `git -C {repo} status`",
        )
    return Check(True, "state", "nothing in progress, nothing uncommitted")


def checks() -> list[Check]:
    """Every check, in dependency order.

    Order is not cosmetic: git before the repository, the repository before its
    remote and its state. A reader who stops at the first ✘ should be looking at
    the cause rather than at one of its symptoms.
    """
    found = [git_present()]
    repo = paths.repo_dir()
    here = repository(repo)
    parsed, config = settings()
    found.extend([here, parsed, host(config)])
    found.append(remote(repo, ok=here.ok is True))
    found.append(writable(paths.backup_dir()))
    found.append(dangling(repo, ok=here.ok is True))
    return found


def report(found: list[Check]) -> str:
    """The checks as one block of text, aligned on the title.

    Printed rather than returned as data because this is the whole product of the
    command; `tests/test_doctor.py` asserts on it, which is the only reason it is
    a function rather than a loop of `print`.
    """
    # survivor: off-by-one -- tupferl/doctor.py:257 in report() -- `0` becomes `1` -- equivalent:
    #   `default=0` is used only when `found` is empty, and an empty `found` produces no lines for
    #   the width to pad. Measured: `'ab'.ljust(-1)` and `'ab'.ljust(0)` are both `'ab'`.
    width = max((len(check.title) for check in found), default=0)
    lines = [f"{MARKS[check.ok]} {check.title.ljust(width)}  {check.detail}" for check in found]
    failed = sum(1 for check in found if check.ok is False)
    skipped = sum(1 for check in found if check.ok is None)
    summary = f"{len(found) - failed - skipped} ok, {failed} failed, {skipped} not applicable"
    return "\n".join([*lines, "", summary])


def main() -> int:
    """Exit 0 only when nothing failed. A skipped check is not a failure -- there
    is no repository *yet* on a machine that has not run `init`, and `doctor`
    exiting non-zero for that would make it useless in an install script."""
    found = checks()
    print(report(found))
    return 1 if any(check.ok is False for check in found) else 0
