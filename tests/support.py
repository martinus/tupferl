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

import atexit
import io
import os
import pty
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from functools import lru_cache
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

#: The environment variable `tools/mutate.py` sets to the per-test alarm it armed
#: for this run. Spelled here rather than imported: `support` is the bottom of
#: the test tree and importing the harness into it to read one string is the
#: wrong direction. `test_support` asserts the two spellings agree, which is the
#: same arrangement `CARRIES` already has with `TUPFERL_MUTATE_BUDGET`.
ALARM = "TUPFERL_MUTATE_EACH_TEST"

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
#: - `TUPFERL_MUTATE_EACH_TEST`: the per-test alarm the harness armed. Dropping
#:   it lets a subprocess fixture bound itself against the default instead.
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
    ALARM,
)

#: How much of the harness's per-test alarm a fixture's own deadline may take.
#:
#: The margin is what makes the fixture win the race, and it has to be a *share*
#: rather than a subtraction: an alarm of 3s and an alarm of 300s want different
#: absolute headroom and the same proportion of it. Two thirds is not a fresh
#: judgement -- it is the ratio the two hand-picked numbers below already had
#: against a 30s alarm, so stating the rule leaves every bound in this file
#: exactly where measurement put it and only changes what happens when the alarm
#: moves.
SHARE = 2 / 3


def bounded(seconds: float) -> float:
    """`seconds`, brought under the per-test alarm this run actually armed.

    **A fixture's own timeout must beat the harness's, not merely exist.** When
    it does not, the alarm fires first, `tools/mutate.py` files the row `BROKE`,
    and `BROKE` is never `caught` -- so the line the fixture guards is guarded by
    nothing while the summary counts the row in neither of the numbers a reader
    looks at. That is not hypothetical: at `PROMPTED = 60` against a 30s alarm,
    three of four sweeps filed `conflicts.ask`'s row `BROKE` and none caught it.

    Lowering the constants to beat `EACH_TEST` fixed the sweeps that use its
    default and nothing else, because `--each-test` is a flag. `--each-test 10`
    reopens the identical hole against `PROMPTED = 20`, and the test written to
    prevent it compares against the *constant*, so it cannot see that.

    Absent, empty or unparseable means no alarm is in force and the fixture's own
    number stands -- which is every ordinary run of the suite, where the value is
    exactly what it was before this existed. `0` means the same thing: it is what
    `--each-test 0` asks for and what `verdict.each_test` returns where `SIGALRM`
    does not exist.
    """
    try:
        armed = float(os.environ.get(ALARM, ""))
    except ValueError:
        return seconds
    return min(seconds, armed * SHARE) if armed > 0 else seconds


#: How long a fixture waits for a keyed CLI run to finish -- a child through a
#: pty, or `typing` in this process. The only thing it bounds is a prompt asking
#: more times than the fixture answered, and a child killed here still has its
#: partial output read back, so the failure says what it was doing.
#:
#: **It has to beat `tools/mutate.py`'s per-test alarm, not merely exist** --
#: see `bounded`, which is what enforces that against the alarm actually armed
#: rather than against its default. 20s is the number when nothing is armed: far
#: above the longest honest wait (a driven `sync` is milliseconds, and its
#: slowest here is under two seconds even under a 32-lane sweep) and two thirds
#: of the 30s default, which is where `SHARE` comes from. `PATIENCE` is the
#: tighter bound for a single *read*; this one covers a whole command.
PROMPTED = bounded(20.0)

#: How long a fixture will wait for a read from a terminal -- or for any call
#: whose subject could loop for ever -- before calling it a failure. Both uses
#: want the same number and the same argument, so they share one: five seconds
#: against a suite that runs 500 tests in twelve, so a legitimate prompt has
#: three orders of magnitude of headroom -- and a mutant
#: that leaves `one_key` blocking fails in five seconds rather than holding a
#: mutation lane for the harness's full per-test alarm. Through `bounded` too,
#: so an `--each-test` below about 7.5s brings this down with it.
PATIENCE = bounded(5.0)


@contextmanager
def deadline(seconds: float, why: str) -> Iterator[None]:
    """Fail the body if it has not finished in `seconds`.

    **A test that hangs is not a test that guards anything.** `tools/mutate.py`
    reports a mutant whose suite blocked as `BROKE`, and `BROKE` is never
    `caught` -- so a line whose only tests hang when it is wrong is a line
    nothing is watching. Six mutants of `conflicts.rest_of_escape`'s `VMIN`/
    `VTIME` lines came back that way, and the fixture's own docstring described
    the hang as if it were the assertion.

    Raising from the handler rather than setting a flag, for `tools/verdict.py`'s
    reason: PEP 475 makes Python *retry* a syscall interrupted by a signal, so a
    handler that returned would be swallowed by exactly the blocking `read` this
    exists to interrupt.

    The previous handler is restored, not assumed to be the default: the harness
    installs its own per-test alarm around this one.

    **Arm it on the class where the subject can hang, not around the one call a
    sweep named.** Measured twice: bounding the call left the *sibling* tests
    hanging on the same mutation, because they reach the line by another route --
    three of four `line_starts` rows and two of six `verdict` walk rows stayed
    `BROKE`. The spelling is a `contextlib.ExitStack` entered in `setUp` and
    closed by `addCleanup`; `TestCase.enterContext` would say it in one line and
    is 3.11, which this project does not require.
    """

    def ring(signum: int, frame: object) -> None:
        raise TimeoutError(why)

    # **What was already armed is restored, not cancelled.** There is one
    # `ITIMER_REAL` per process, and `tools/mutate.py` arms it around every test
    # -- so a bound that zeroed it on the way out left the *rest* of that test
    # unwatched by the harness. A hang after this block then costs `TIMEOUT`'s
    # 300s rather than `EACH_TEST`'s 30, holding a lane ten times as long and
    # filing the row under the outcome that names no test. Measured: the alarm
    # read `(29.999998, 0.0)` before and `(0.0, 0.0)` after.
    outer, _ = signal.getitimer(signal.ITIMER_REAL)
    began = time.monotonic()
    before = signal.signal(signal.SIGALRM, ring)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        # Ours off and the previous handler back *before* re-arming, or the
        # restored alarm could fire into `ring` and be reported as this bound.
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, before)
        if outer:
            # Whatever the outer alarm had left, less the time spent in here.
            # Never zero: `setitimer(0)` means *disarm*, so a bound that
            # overran its parent would silently switch the alarm off rather
            # than let it fire at once.
            left = outer - (time.monotonic() - began)
            signal.setitimer(signal.ITIMER_REAL, max(left, 1e-6))


#: Typed after every set of keys a fixture sends to a conflict prompt.
#:
#: The prompt loops, so a test that types fewer keys than it asks for reads an
#: empty terminal and waits for ever -- a suite that *hangs* rather than one that
#: goes red, which is the failure nobody can read from a log. With skips waiting,
#: the unexpected extra question is answered "skip", the run exits 1, and the
#: test fails on its own assertion instead.
#:
#: **Several, not one.** A mutation sweep is where this earns its keep, and there
#: a mutant does not politely ask exactly one extra time: the first sweep of the
#: conflict prompt produced 15 rows with no verdict at all, because one skip was
#: not enough and each blocked test then held a lane for the full 30s per-test
#: alarm. That load is also what turned an unrelated baseline red. Eight covers
#: an off-by-one in any of the loop's arms; a mutant that loops without end is
#: what the alarm is for, and it reports `BROKE`, which is honest.
#:
#: `s` and not something invalid: an unrecognised key re-asks, which is the
#: behaviour this is guarding against.
FALLBACK = "s" * 8

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


#: How many surviving paths `discard` names before it stops. Enough to identify
#: a writer -- one `tmp_pack_*` or `incoming-*` entry is the whole answer -- and
#: short enough that the message is readable when a whole tree survived.
NAMED_WHEN_STUCK = 12


def discard(box: tempfile.TemporaryDirectory[str]) -> None:
    """Remove a throwaway tree; if it will not go, say what is still in it.

    #17's second half. The first is `NO_HOUSEKEEPING`, which tries to stop the
    race; this is what makes the *next* occurrence diagnosable, because that
    issue could not be worked from what CI printed:

        OSError: [Errno 39] Directory not empty: 'objects'

    -- reported by `tools/run_tests.py` as an error in
    `TestSyncIsIdempotentAndConverges`, which reads as "the sync property
    failed". It had not; every property passed and the fixture failed to tear
    down. Naming the surviving paths turns that into the writer's own signature:
    a `tmp_pack_*`, an `objects/incoming-*` quarantine, a fresh `.pack`.

    **It re-raises.** Not `ignore_cleanup_errors=True`, and not a `try` that
    shrugs: either would convert a loud failure into a silent leak of a whole
    tree per Hypothesis example, filling the runner's disk and failing somewhere
    else entirely -- and would throw away the only signal that a git process is
    outliving the command that started it, which is a hole in `gitrepo` being
    the only place that talks to git.

    **And it does not retry.** A second attempt after a pause would usually
    succeed, which is the same silence by a slower route.
    """
    try:
        box.cleanup()
    except OSError as stuck:
        root = Path(box.name)
        # Best-effort, and after the failure: a listing that raised here would
        # replace the real error with one about this function.
        try:
            left = sorted(str(path.relative_to(root)) for path in root.rglob("*"))
        except OSError:  # pragma: no cover - the tree is going away either way
            left = []
        named = ", ".join(left[:NAMED_WHEN_STUCK]) or "nothing this could list"
        more = f", and {len(left) - NAMED_WHEN_STUCK} more" if len(left) > NAMED_WHEN_STUCK else ""
        raise OSError(
            f"{stuck}; {root} still holds {named}{more} -- something wrote into the tree "
            f"while it was being removed, which is a process outliving the command that "
            f"started it rather than a failure of the test that owned it (see #17)"
        ) from stuck


@contextmanager
def tempdir(prefix: str = "tupferl-test-") -> Iterator[Path]:
    """A throwaway directory that is removed even when the body raises.

    A context manager rather than `TestCase.enterContext`, which is 3.11+ and
    this project supports 3.10 -- and rather than an `rmtree` in `tearDown`,
    which does not run when `setUp` itself fails and is a delete written by hand
    where one is not needed.
    """
    box = tempfile.TemporaryDirectory(prefix=prefix)
    try:
        yield Path(box.name)
    finally:
        # `discard`, not the `with` form: these trees hold git repositories, and
        # a cleanup that fails should name what survived rather than an errno.
        discard(box)


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


#: git's background housekeeping, turned off for every sandbox (#17).
#:
#: git runs `gc --auto` after a commit and `maintenance run --auto` after a
#: fetch, and **detaches** them by default. A detached process writing a pack, a
#: `tmp_pack_*`, or an `objects/incoming-*` quarantine is what makes an
#: `objects/` directory non-empty a moment after `shutil` scanned it as empty --
#: and `TemporaryDirectory.cleanup()` then dies with `Directory not empty`,
#: naming whichever test happened to own the tree. It has done so twice in this
#: repository's CI, both times reported as a failure of the sync property, which
#: had passed.
#:
#: The mechanism is **not** established: it does not reproduce on this
#: container's git 2.43, and the runs that failed were on the runner's 2.55. See
#: #17 for what was measured and what was not. What *is* established is that
#: these three keys are read back by git through the sandbox
#: (`test_support.TestBackgroundGitIsOff` asks git rather than reading the file),
#: and that no test wants a detached process mutating a repository the suite is
#: about to delete.
#:
#: `autoDetach` as well as `auto`: with `auto` at 0 nothing should start, and if
#: a future git starts one anyway it then runs in the foreground, where the
#: command that triggered it waits for it.
NO_HOUSEKEEPING = "[gc]\n\tauto = 0\n\tautoDetach = false\n[maintenance]\n\tauto = false\n"


def seed_home(home: Path, host: str = HOST) -> None:
    """Make `home` look like a real one: the directories, and git's identity.

    The identity is set in `$HOME/.gitconfig` rather than in `GIT_AUTHOR_NAME`
    and friends, because `.gitconfig` is also *a dotfile a test may manage* --
    the fixture and the subject are the same kind of thing here, and a fixture
    that used a mechanism no user has would be testing a path nobody walks.

    `NO_HOUSEKEEPING` goes in the same file, and for the same reason it is one
    place: every sandbox in the suite is built from here, so a repository that
    escaped it would be the one that raced.
    """
    for part in (".local/share", ".local/state", ".config"):
        (home / part).mkdir(parents=True, exist_ok=True)
    (home / ".gitconfig").write_text(
        gitconfig(host)
        + f"[init]\n\tdefaultBranch = {BRANCH}\n[commit]\n\tgpgsign = false\n"
        + NO_HOUSEKEEPING,
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


#: The most output an in-process run is allowed to accumulate, in characters.
#: Eight mebibytes is three orders of magnitude above the largest thing any test
#: here asserts on, and three orders below the memory share a mutation lane gets.
KEPT = 8 * 1024 * 1024


class Spill(io.StringIO):
    """A `StringIO` that stops growing once it holds `KEPT` characters.

    The bound is the point. `Computer.call` runs the CLI *in this process*, so a
    program that prints without end fills this object rather than a pipe -- and
    under `tools/mutate.py` that is charged to the lane's memory share, which
    kills the session and reports the mutant `BROKE`. `BROKE` is never `caught`,
    so the line the mutant touched ends up guarded by nothing.

    That is not hypothetical: removing `conflicts.ask`'s end-of-input guard makes
    it loop for ever printing, and the row came back `BROKE` at 4019 MiB against
    a 4018 MiB share -- tipping over whatever the share was, which is the
    signature of an unbounded grower rather than of a lane that is slightly too
    small. `support.run_cli` has the same defect through a pipe and the same fix.

    Truncating rather than raising: the test's own assertion is what should
    report the failure, and an exception from inside `print` would arrive as a
    fixture error pointing here instead of at the code.
    """

    def write(self, text: str) -> int:
        if self.tell() >= KEPT:
            return len(text)
        return super().write(text)


class Screen(Spill):
    """A `Spill` that says it is a terminal, which `io.StringIO` does not.

    Not `Terminal`, which is below and is a real pty: this is the *cheap* one,
    and the two are told apart by name because the difference is the whole
    reason both exist.

    For the half of `tools/paint.py` that a captured run cannot show. Everything
    a test captures is a `StringIO`, whose `isatty()` is False -- which is
    exactly why adding colour to the tools moved no existing assertion, and
    equally why nothing here could see a colour if this did not exist.

    A real pty is the better fixture and `tests/test_paint.py` uses one, where
    what a terminal *is* is the subject. Where the subject is what a *tool*
    prints, `isatty` is the whole of what `paint.coloured` asks, and a pty would
    add a buffer that has to be drained by somebody.
    """

    def isatty(self) -> bool:
        return True


@contextmanager
def quiet(terminal: bool = False) -> Iterator[io.StringIO]:
    """Swallow stdout and stderr, and hand back what was written.

    Both, and returning the text rather than discarding it: a test that silences
    output it never asserts on is one that would pass if the output stopped
    happening. Used where the *noise* is argparse's usage message and the
    assertion is about the exit status.

    ``terminal`` makes the capture claim to be one, so a test can see what a
    person would see rather than what a log file gets. Off by default, because
    a log file is what every other assertion in this suite is about.

    Bounded -- see `Spill`.
    """
    spill = Screen() if terminal else Spill()
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


#: External tools a suite needs, as a skip rather than an error. Defined here
#: beside the other fixtures rather than as a `shutil.which` at the one call
#: site: a check that has to grow later -- a git version floor, say, which
#: issue #3 will want -- must not be updatable in one file and forgotten in
#: another. Only `tests/test_mutants.py` uses it today; the rest of the suite
#: treats git as the hard requirement plan §5 makes it.
requires_git = unittest.skipUnless(shutil.which("git"), "git required")

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


def git_merged(repo: Path, env: dict[str, str]) -> int:
    """Try to merge `origin/main` and say what git thought of it.

    For the preconditions that ask git whether two branches conflict rather than
    assuming it. `git` above raises on a non-zero exit because it is used for
    *fixture setup*, where a half-failed step builds a repository that is not the
    shape the test's name claims; here the non-zero exit is the answer.

    `--no-commit --no-ff` so a clean merge leaves something to abort rather than
    a commit to undo; every caller pairs this with `git_aborted`.
    """
    done = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "origin/main"],
        cwd=repo,
        env=env,
        capture_output=True,
        check=False,
    )
    return done.returncode


def git_aborted(repo: Path, env: dict[str, str]) -> None:
    """Undo whatever `git_merged` started, so the test under it begins clean."""
    subprocess.run(["git", "merge", "--abort"], cwd=repo, env=env, capture_output=True, check=False)


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
    args: list[str], env: dict[str, str], cwd: Path | None = None, keys: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Drive the CLI the way a user does: a separate process, through `-m`.

    Plan §7.1 prefers driving the real thing where speed allows. It does here --
    these are milliseconds -- and it is the only way to check what actually
    reaches stdout, stderr and the exit status, which is `tupferl doctor`'s whole
    product.

    **`keys=None` gives the child no terminal at all**, and that is the load-
    bearing part rather than a detail. `sync` asks `sys.stdin.isatty()` to decide
    whether anyone is there to answer a conflict; inheriting this process's stdin
    makes the answer depend on how the *suite* was launched, so the same test
    prompts and blocks for ever in a developer's terminal and skips silently in
    CI. `DEVNULL` makes "nobody is there" a property of the fixture.

    `keys` opens a real pty and types them, which is the only way to exercise the
    prompt: `conflicts.one_key` clears `ICANON` with `termios`, and there is no
    pipe that behaves like that. `FALLBACK` is typed after them -- see there.

    **The child's output goes to a file, never a pipe.** A pipe makes the
    *parent* hold everything the child writes, so a child that prints without end
    -- which is what `conflicts.ask` becomes if its end-of-input guard is removed
    -- balloons this process instead of that one. Measured: 724 MiB of parent RSS
    in three seconds. Under `tools/mutate.py` that is charged to the lane's
    memory share, so the whole session is killed and the mutant is reported
    `BROKE` -- no verdict at all, for a line that *is* guarded.
    `tools/mutate.py`'s `_run` writes its probe's output to a file for the same
    reason and says so in a comment.

    A file also survives the timeout: whatever the child managed to say is still
    there to read, where `communicate` on a killed pipe gives back nothing.
    """
    if keys is None:
        return subprocess.run(
            [sys.executable, "-m", "tupferl", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            check=False,
        )

    master, slave = pty.openpty()
    with tempfile.TemporaryDirectory(prefix="tupferl-cli-") as box:
        spoken = Path(box) / "stdout"
        complained = Path(box) / "stderr"
        try:
            hush(slave)
            os.write(master, (keys + FALLBACK).encode("utf-8"))
            with spoken.open("w") as out, complained.open("w") as err:
                started = subprocess.Popen(
                    [sys.executable, "-m", "tupferl", *args],
                    cwd=cwd,
                    env=env,
                    stdin=slave,
                    stdout=out,
                    stderr=err,
                    text=True,
                )
                os.close(slave)
                try:
                    # A timeout, because the failure this fixture can produce is
                    # a prompt that asks once more than `keys` answers -- a suite
                    # that hangs rather than one that fails.
                    started.wait(timeout=PROMPTED)
                except subprocess.TimeoutExpired:
                    # Killed and reaped before the files are read, so whatever it
                    # had already written is part of the failure the caller sees.
                    started.kill()
                    started.wait()
        finally:
            os.close(master)
        return subprocess.CompletedProcess(
            started.args,
            started.returncode,
            spoken.read_text(encoding="utf-8", errors="replace"),
            complained.read_text(encoding="utf-8", errors="replace"),
        )


def hush(slave: int) -> None:
    """Turn the pty's own echo off before anything is typed at it.

    A pty starts with `ECHO` on, so every byte written to the master is echoed
    straight back into the terminal's *output* queue -- and in these fixtures
    nobody reads that queue. It fills, and the next `tcsetattr` that waits for
    output to drain never returns.

    `conflicts.one_key` no longer asks for that wait, which is the real fix; this
    is the other half, and it is worth having on its own. The prompt echoes the
    key it read deliberately, to its own output stream, so the terminal's echo
    was never anything but noise queued behind the test.
    """
    mode = termios.tcgetattr(slave)
    mode[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, mode)


class Terminal:
    """A pty, and the two halves of it as files.

    `type` writes to the master, which is what a person pressing a key does;
    `source` is the slave, which the prompt reads and which answers `isatty`
    with `True`.

    Here rather than in a test module because three fixtures in one commit
    opened their own, and each has to get the same two things right: the keys go
    in *before* the reader starts, and both ends are closed exactly once.
    """

    def __init__(self) -> None:
        self.master, self.slave = pty.openpty()
        hush(self.slave)
        self.source = os.fdopen(self.slave, "r")

    def type(self, keys: str) -> None:
        os.write(self.master, keys.encode("utf-8"))

    def close(self) -> None:
        self.source.close()
        os.close(self.master)


def fake_editor(where: Path, body: str) -> Path:
    """A real program to put in `$EDITOR`: a shell script whose body is `body`.

    Returned rather than exported through the environment, because the two
    callers set it in different places -- one in `os.environ`, one in a single
    machine's environment dict -- and only one of them may touch the process's
    own.
    """
    where.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    where.chmod(0o755)
    return where


@contextmanager
def typing(keys: str | None) -> Iterator[None]:
    """Replace `sys.stdin` for an in-process run. `None` means nobody is there.

    `run_cli`'s argument, for the half of the suite that calls `tupferl.__main__`
    directly -- and for the same reason: what stdin *is* has to come from the
    fixture, never from how the suite was launched.
    """
    if keys is None:
        with mock.patch("sys.stdin", io.StringIO()):
            yield
        return
    terminal = Terminal()
    try:
        terminal.type(keys + FALLBACK)
        # **Bounded, because `FALLBACK` is not a bound.** `conflicts.one_key`
        # sets `VMIN` to 1, so a read on a pty whose master is still open waits
        # for ever rather than reporting exhaustion -- right for a real terminal,
        # and it means the eight `s` keys are the only thing standing between a
        # prompt that asks once too often and a suite that hangs. A mutant making
        # `ask` reject *every* key eats all of them and then blocks: measured,
        # `conflicts.py:635` came back `BROKE` in three of four ordered sweeps
        # and `caught` in none, so the line was guarded by nothing.
        #
        # `deadline` rather than closing the pty's master, which was tried first
        # and cannot be made exact: neither `select` nor `FIONREAD` can tell
        # "canonical mode, nothing read yet" from "every key spent" -- both
        # report zero -- so a watcher either fires before the first read or
        # races the reader and misses. `SIGALRM` interrupts the blocking read
        # itself, which is what this helper exists for.
        with (
            mock.patch("sys.stdin", terminal.source),
            deadline(PROMPTED, f"the prompt never settled on {keys!r}"),
        ):
            yield
    finally:
        terminal.close()


@dataclass(frozen=True)
class Sandbox:
    """A throwaway `$HOME` with `os.environ` already pointed inside it.

    What `SandboxCase` used to *be*, extracted so that the class and
    `tests/conftest.py`'s `sandbox` fixture are two adapters over one
    definition. Both exist through Phase B -- B3 converted five of this base's
    modules and B4a/B4b convert the rest -- and two hand-maintained copies of
    "what a sandbox is" would be free to drift for exactly as long as it takes
    nobody to notice. Measured cost of not extracting: `env` alone is read 93
    times across the five modules B3 converted.

    **A module wanting more than this subclasses it** rather than holding one
    and re-exporting the fields: `@dataclass(frozen=True) class Doctored(Sandbox)`
    adding `repo` and `remote` inherits `tmp`, `home`, `env` and `write` with no
    property stubs. The first version of B3 wrote ten of those stubs across five
    modules, and B4a/B4b would have copied the pattern into nine more.

    No `host` field: nothing reads one, and `env["TUPFERL_HOSTNAME"]` has it for
    anything that ever needs to. `sandbox()` still takes the argument, because
    that is the input rather than a fact about the result.
    """

    tmp: Path
    home: Path
    env: dict[str, str]

    def write(self, where: Path, text: str) -> Path:
        """Write a file, making its parents. Returns it, so calls can chain."""
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(text, encoding="utf-8")
        return where


@contextmanager
def sandbox(host: str = HOST) -> Iterator[Sandbox]:
    """A `Sandbox`, torn down on the way out.

    Composed from `tempdir` and `sandboxed` rather than repeating either. The
    first version of this inlined both -- the same `TemporaryDirectory`/`discard`
    pair and the same `mock.patch.dict(..., clear=True)` -- which is precisely
    the drift this function exists to prevent, one level further down: a step
    added to `sandboxed` (a `GIT_CONFIG_NOSYSTEM`, say) would have reached its
    five callers and silently missed this one.
    """
    with tempdir() as tmp:
        home = tmp / "home"
        home.mkdir()
        seed_home(home, host)
        with sandboxed(home, host) as env:
            yield Sandbox(tmp=tmp, home=home, env=env)


class SandboxCase(unittest.TestCase):
    """A test with a throwaway `$HOME`, and `os.environ` pointed inside it.

    **The `unittest` adapter over `sandbox` above**, kept until its last user
    converts (`docs/pytest-plan.md`, clusters B4a and B4b) and deleted in that
    PR. It builds no sandbox of its own -- it copies three fields off one -- so
    the two spellings cannot disagree about what a sandbox *is*.
    """

    host = HOST

    def setUp(self) -> None:
        self.box = self.enterSandbox()
        self.tmp = self.box.tmp
        self.home = self.box.home
        self.env = self.box.env

    def enterSandbox(self) -> Sandbox:
        """`sandbox(self.host)`, unwound by `addCleanup`.

        Spelled out rather than `enterContext`, which is 3.11 and this project
        supports 3.10 -- the same reason the rest of this file reaches for
        `ExitStack`.

        **`(None, None, None)` reports every test as having exited cleanly, so
        `sandbox()` must not care whether the body raised.** It does not today:
        it is `tempdir` and `sandboxed`, both of which unwind the same way
        either direction. Said here because the equivalence of the two adapters
        rests on it, and `discard`'s docstring gestures at exactly the change
        that would break it -- keeping the tree when a test failed, so a person
        can look at it.
        """
        made = sandbox(self.host)
        built = made.__enter__()
        self.addCleanup(made.__exit__, None, None, None)
        return built

    def write(self, where: Path, text: str) -> Path:
        """Write a file, making its parents. Returns it, so calls can chain."""
        return self.box.write(where, text)

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
        # `exist_ok`, so this also *adopts* a home that `two_machines` copied
        # from the template. `seed_home` then rewrites `.gitconfig` with the
        # bytes it already holds, which is a no-op worth paying to keep one
        # constructor rather than two.
        self.home.mkdir(parents=True, exist_ok=True)
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

    def run(self, *args: str, keys: str | None = None) -> subprocess.CompletedProcess[str]:
        """One command, as a subprocess, the way a user runs it.

        `keys` gives it a terminal to read them from -- see `run_cli`. Without
        one it has no stdin at all, so a conflict is reported and skipped.
        """
        return run_cli(list(args), self.env, keys=keys)

    def call(self, *args: str, keys: str | None = None) -> int:
        """One command, in this process. Returns the exit status; output is eaten."""
        return self.say(*args, keys=keys)[0]

    def say(self, *args: str, keys: str | None = None) -> tuple[int, str]:
        """`call`, and hand back what it printed as well as the exit status.

        For the commands whose output *is* the product -- `status`, `diff`,
        `list` -- where `run` would also do but costs a subprocess apiece. What
        it cannot show is which stream a line went to, so a test about stderr
        still wants `run`; `quiet` merges the two.
        """
        from tupferl import __main__ as cli

        with mock.patch.dict(os.environ, self.env, clear=True), quiet() as said, typing(keys):
            status = cli.main(list(args))
        return status, said.getvalue()

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


#: What `.bashrc` starts as on both machines. Here rather than inside the
#: fixture because the template is built once and `TwoMachines` no longer writes
#: it -- a test that wants to know what it began as reads this.
STARTS_AS = "one\ntwo\nthree\nfour\nfive\n"


@lru_cache(maxsize=1)
def template() -> Path:
    """The two-machine tree, built once per process and never handed out.

    #19. `TwoMachines.setUp` built this from scratch for every test: a real
    `init`, `add` and `sync`, which is **146 of the suite's tests** across 40
    classes. Measured on an idle machine, seven builds each:

    | | median |
    |---|---|
    | build from scratch | 120.4 ms |
    | `copytree` of a built one | 4.3 ms |

    -- so about 17 seconds of a serial run went on constructing the same two
    repositories over and over, and every mutant in a sweep paid it again.

    **Once per process, not once per class.** Per class would be 40 builds; the
    tree is only ever read from here, so one is enough. `two_machines` copies it
    and nothing else ever touches it -- a test that mutated the template would
    poison every later test in the process, which is why this returns a path
    that only that function knows how to use.

    The `Computer` objects built here are deliberately discarded: their
    environments point *inside the template*, and one escaping would be a test
    writing where the copies come from.
    """
    box = tempfile.TemporaryDirectory(prefix="tupferl-template-")
    atexit.register(discard, box)
    root = Path(box.name)
    first = Computer(root, "machine-a")
    Computer(root, "machine-b")
    remote = make_remote(root / "remote.git", first.env)
    first.write(".bashrc", STARTS_AS)
    assert first.call("init", str(remote)) == 0, "the template's init failed"
    assert first.call("add", str(first.home / ".bashrc")) == 0, "the template's add failed"
    assert first.call("sync") == 0, "the template's sync failed"
    return root


def copy_template(into: Path) -> tuple[Computer, Computer, Path]:
    """A copy of `template()` at `into`: two machines and their bare remote.

    Two things in the copy still name the tree it came from, and both are fixed
    here rather than left to surprise somebody. Found by grepping the built tree
    for its own root, which turned up exactly these:

    - **`.git/config`'s `remote.origin.url`**, which would otherwise point every
      test at the template's remote -- so they would push to each other, and a
      test would see another test's commits.
    - **`.git/FETCH_HEAD`**, which records the URL of the last fetch. Nothing
      here reads it (`sync` merges `<remote>/<branch>`, never `FETCH_HEAD`), so
      it is inert -- but it is a stale absolute path sitting in a fixture, and
      removing it costs one `unlink`.
    """
    shutil.copytree(template(), into, symlinks=True, dirs_exist_ok=True)
    first = Computer(into, "machine-a")
    second = Computer(into, "machine-b")
    remote = into / "remote.git"
    git(["remote", "set-url", "origin", str(remote)], cwd=first.repo, env=first.env)
    (first.repo / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
    return first, second, remote


@dataclass(frozen=True)
class TwoMachines:
    """Two `$HOME`s and one bare remote, with `.bashrc` already managed and
    synced from the first -- plan §3.5's daily flow, at the point where a second
    computer can be brought up.

    Not a `Sandbox`, which patches `os.environ` for *one* machine: each
    `Computer` carries its own environment, and `run`/`call` apply it per
    command, which is how two hostnames coexist without two processes. That is
    also why this is a plain dataclass rather than a subclass of `Sandbox` --
    there is no single `home` or `env` here to inherit, and a `home` field
    naming one of the two would be read as naming the pair.

    **The tree is copied, not built** (#19). This fixture used to run a real
    `init`, `add` and `sync` for each of the 146 tests that take it; it now
    copies `template()`, which is 4.3 ms against 120.4 ms. See there for the
    numbers and for why the template is per *process* rather than per class.

    The build itself runs in-process, which is the older half of the same
    argument: it was three subprocesses until the property tests demonstrated the
    same fixture built with `call`. That measurement said 0.45s against 0.24s per
    `setUp`; a build costs 120 ms on this machine today, so the *ratio* is what
    survived and the absolute figures did not. Nothing here inspects stdout or
    the exit status beyond "it worked", which is the only thing a subprocess
    would add; the tests that *do* still use `run`.

    Here rather than in a test module because five of them build it, and a
    fixture that drifts between them is one where a failure in one file cannot be
    reproduced in another.
    """

    tmp: Path
    first: Computer
    second: Computer
    remote: Path

    def diverge(self, name: str, mine: bytes, theirs: bytes) -> None:
        """Make the two machines disagree about `name`, with `machine-a` pushing.

        `machine-b` is left holding `theirs` in its `$HOME` and the repository
        holding `mine`, so its next sync is the conflict. Bytes rather than text
        because one caller's fixture is a file with a NUL in it.

        `machine-b` must already have run `init`; three tests built this by hand
        and the one that forgot produced a first sync rather than a conflict.
        """
        (self.first.home / name).write_bytes(mine)
        (self.second.home / name).write_bytes(theirs)
        assert self.first.call("sync") == 0, "the pushing machine's sync failed"


@contextmanager
def two_machines() -> Iterator[TwoMachines]:
    """A `TwoMachines`, torn down on the way out.

    Composed from `tempdir` and `copy_template` rather than repeating either,
    for the reason `sandbox()` gives one level down: the two adapters over this
    -- `TwoMachinesCase` and `tests/conftest.py`'s fixture -- must not be able to
    disagree about what the fixture *is*.
    """
    with tempdir() as tmp:
        first, second, remote = copy_template(tmp)
        yield TwoMachines(tmp=tmp, first=first, second=second, remote=remote)


class TwoMachinesCase(unittest.TestCase):
    """The `unittest` adapter over `two_machines` above.

    Kept until its last user converts (`docs/pytest-plan.md`, cluster B4b) and
    deleted in that PR. It builds nothing of its own -- it copies four fields off
    one `TwoMachines` -- so the two spellings cannot disagree.

    Named with the `Case` suffix `SandboxCase` already established, so that the
    definition can keep the good name. The rename cost 23 call sites in the four
    modules B4b converts, and it is a pure substitution: B4b then deletes the
    class rather than renaming anything back.
    """

    def setUp(self) -> None:
        made = two_machines()
        self.box = made.__enter__()
        self.addCleanup(made.__exit__, None, None, None)
        self.tmp = self.box.tmp
        self.first = self.box.first
        self.second = self.box.second
        self.remote = self.box.remote

    def diverge(self, name: str, mine: bytes, theirs: bytes) -> None:
        """See `TwoMachines.diverge`."""
        self.box.diverge(name, mine, theirs)


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
#: `codec="utf-8"` rather than excluding the `Cs` category by name. It says the
#: real reason -- these strings get encoded, and a lone surrogate raises when they
#: do -- and it is the modern spelling: `blacklist_categories` is a compatibility
#: shim whose stub is typed differently in newer hypothesis, which passed mypy
#: locally and failed it in CI. Verified against the `hypothesis>=6.100` floor,
#: and 400 generated characters checked to be utf-8 encodable.
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
        alphabet=st.characters(codec="utf-8", exclude_characters="\n\r\x00"),
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


@dataclass(frozen=True)
class Machine(Sandbox):
    """A sandboxed home with a bare remote beside it, and the CLI pointed there.

    The one-machine fixture, here rather than in a test module because two of
    them build it -- `test_manage.py` for the repository commands and
    `test_sync_cli.py` for the engine. `Computer` above is the same idea for the
    *two*-machine tests, which cannot use this one: a `Sandbox` patches
    `os.environ` for a single `$HOME`, and two hostnames have to exist at once.
    """

    remote: Path
    repo: Path

    @property
    def host(self) -> str:
        """This machine's name, read back out of its own environment.

        Rather than a fourth field. `Sandbox`'s docstring already declines to
        carry one on the grounds that `env["TUPFERL_HOSTNAME"]` has it, and a
        copy here could disagree with the environment the CLI actually runs
        under -- which is the only thing `stored` and `snapshot` are asking
        about.
        """
        return self.env["TUPFERL_HOSTNAME"]

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return run_cli(list(args), self.env)

    def init(self) -> subprocess.CompletedProcess[str]:
        done = self.run_cli("init", str(self.remote))
        assert done.returncode == 0, done.stdout + done.stderr
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


@contextmanager
def machine(host: str = HOST) -> Iterator[Machine]:
    """A `Machine`, torn down on the way out.

    `repo_dir()` is asked of `tupferl.paths` under this machine's environment
    rather than retyped as `.local/share/tupferl/repo`, for the reason
    `Computer.__init__` gives: a test that spells the layout out itself cannot
    notice the layout changing. It has to be asked *inside* `sandbox()`, which
    is what patches `os.environ` for it to read.
    """
    with sandbox(host) as box:
        remote = make_remote(box.tmp / "remote.git", box.env)
        yield Machine(**vars(box), remote=remote, repo=paths.repo_dir())


class MachineCase(SandboxCase):
    """The `unittest` adapter over `machine` above.

    Kept until its last user converts (`docs/pytest-plan.md`, cluster B4b) and
    deleted in that PR. `enterSandbox` is the whole of it: `SandboxCase.setUp`
    already assigns `tmp`, `home` and `env` off whatever that returns, and a
    `Machine` *is* a `Sandbox`.
    """

    box: Machine

    def enterSandbox(self) -> Machine:
        """`machine(self.host)`, unwound by `addCleanup`. See `SandboxCase.enterSandbox`."""
        made = machine(self.host)
        built = made.__enter__()
        self.addCleanup(made.__exit__, None, None, None)
        return built

    def setUp(self) -> None:
        super().setUp()
        self.remote = self.box.remote
        self.repo = self.box.repo

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.box.run_cli(*args)

    def init(self) -> subprocess.CompletedProcess[str]:
        return self.box.init()

    def log(self) -> list[str]:
        return self.box.log()

    def stored(self, name: str, host: bool = False) -> Path:
        return self.box.stored(name, host)

    def snapshot(self, name: str) -> Path:
        return self.box.snapshot(name)
