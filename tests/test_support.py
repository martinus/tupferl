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

import ast
import contextlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from tests import support
from tools import mutate
from tupferl import conflicts, paths

#: The two modules B5 routed, and what `bound_of` demonstrates on.
#:
#: A hand-written pair, and that is now honest because it carries no
#: guarantee: `TestEveryWaitOnAChildIsBounded` is the guard, and it discovers
#: what to check. These two are the demonstration that the *spelling* the walk
#: insists on has the effect it claims -- a constant written
#: `support.bounded(...)` really does come out smaller in a child whose alarm
#: is tighter. Every other routed constant is spelled identically, so a third
#: entry here would buy two more interpreter spawns and no new fact.
DRIVEN = ("tests.test_watch", "tests.test_reached")

#: What a `timeout=` on one of these is: a fixture waiting on a child process.
#:
#: `argparse.Namespace(timeout=60.0)` in `test_mutate.py` is *not* one -- that
#: is the harness's own `--timeout` setting in a fake `args` -- and asking what
#: is being called keeps it out with no exception list to maintain.
WAITS_ON_A_CHILD = ("run", "Popen", "communicate", "wait")

#: The fewest waits the walk may find before it is believed.
#:
#: `tests/test_errors.py`'s `FLOOR` is the precedent and the reason: a walk that
#: matched nothing would pass every assertion below, and "this suite waits on no
#: child process" is the one answer that cannot be true. Set well under the 21
#: found on 2026-08-31 so that deleting a test is not a failure here.
FLOOR = 12


def _called(node: ast.Call) -> str:
    """The attribute or name being called, for `x.run(...)` and `run(...)`."""
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return node.func.id if isinstance(node.func, ast.Name) else ""


def _under_raises(tree: ast.Module) -> set[int]:
    """The lines inside a `with pytest.raises(...)`, where a timeout is the point.

    `running.wait(timeout=0.5)` in `test_watch.py` is not a guard against a
    hang -- the `TimeoutExpired` it raises *is* the assertion, and bounding it
    against the harness's alarm would be bounding the subject. That is the one
    shape the rule below has to let through, and it is recognised structurally
    rather than listed.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and any(
            isinstance(item.context_expr, ast.Call) and _called(item.context_expr) == "raises"
            for item in node.items
        ):
            lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    return lines


def waits_on_a_child(tree: ast.Module) -> Iterator[ast.expr]:
    """Every `timeout=` this module hands to something that waits on a child."""
    skip = _under_raises(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _called(node) not in WAITS_ON_A_CHILD:
            continue
        for keyword in node.keywords:
            if keyword.arg == "timeout" and keyword.value.lineno not in skip:
                yield keyword.value


def _bound_to(name: str, tree: ast.Module) -> ast.expr | None:
    """What `name` is assigned at module level, or defaulted to as a parameter.

    The parameter arm is not hypothetical: `test_mutate.py` spells its bound
    `def collect(self, each, wait=BOUND)` and then `timeout=wait`, so a reader
    that stopped at the call site would see a bare name and conclude nothing.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return node.value
        if isinstance(node, ast.FunctionDef):
            taking = node.args.args + node.args.kwonlyargs
            given = [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]
            for argument, default in zip(taking[len(taking) - len(given) :], given, strict=True):
                if argument.arg == name:
                    return default
    return None


def routed(value: ast.expr, tree: ast.Module) -> bool:
    """Whether `value` reaches `support.bounded`, following one name at a time.

    `support.PROMPTED` and `support.PATIENCE` count without being followed:
    they are `bounded` calls in `support.py`, and `TestBoundingAFixture...`
    above is what holds them there.
    """
    if isinstance(value, ast.Call):
        return ast.unparse(value.func) in ("bounded", "support.bounded")
    if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
        if value.value.id == "support":
            return True
        if value.value.id == "self":
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    for statement in node.body:
                        if isinstance(statement, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == value.attr
                            for t in statement.targets
                        ):
                            return routed(statement.value, tree)
        return False
    if isinstance(value, ast.Name):
        found = _bound_to(value.id, tree)
        return found is not None and routed(found, tree)
    return False


def every_wait() -> Iterator[tuple[str, int, str, bool]]:
    """`(module, line, source, routed)` for every wait in `tests/`."""
    for found in sorted(Path(support.ROOT, "tests").glob("*.py")):
        tree = ast.parse(found.read_text(encoding="utf-8"))
        for value in waits_on_a_child(tree):
            yield found.name, value.lineno, ast.unparse(value), routed(value, tree)


def bounds_when_armed(armed: str | None) -> dict[str, float]:
    """Every `DRIVEN` module's `BOUND`, as a fresh interpreter computes it.

    A subprocess rather than `importlib.reload`, which would rebind the globals
    of a module this same run is about to execute tests from.

    **One child for all of `DRIVEN`, not one per module**, which is the whole
    difference between this and the first version: a spawn is 0.16s, all of it
    importing pytest and `tests.support`, and four of them were 0.65s -- 86% of
    everything B5 added to the serial suite, for two facts. Two children is one
    per environment, which is the floor, since the environment is what is being
    varied.
    """
    environ = dict(os.environ)
    environ.pop(support.ALARM, None)
    if armed is not None:
        environ[support.ALARM] = armed
    asking = "\n".join(f"import {m}; print('{m}', {m}.BOUND)" for m in DRIVEN)
    done = subprocess.run(
        [sys.executable, "-c", asking],
        cwd=support.ROOT,
        env=environ,
        capture_output=True,
        text=True,
        check=True,
        timeout=support.PROMPTED,
    )
    said = (line.split() for line in done.stdout.splitlines())
    return {name: float(value) for name, value in said}


#: A factory handing back one fresh copy of the two-machine template per call.
Copies = Callable[[], tuple["support.Computer", "support.Computer", Path]]

#: A directory that does not exist and never will. Absolute, because
#: `TUPFERL_DIR` rejects a relative value -- the poison has to survive that check
#: to be able to leak at all.
POISON = "/poison"


#: The failure as CI printed it, down to the errno. A module constant rather
#: than a class attribute because the fixture below is a function now, and a
#: function cannot see one.
REFUSED = OSError(39, "Directory not empty", "objects")


@dataclass(frozen=True)
class Boxed:
    """A throwaway directory and a seeded home, without the environment patch.

    `support.sandbox` patches `os.environ`, which is exactly what these tests
    are trying to observe. So this stops one step short -- and that is why this
    is a dataclass of its own rather than `conftest.py`'s `sandbox` fixture,
    which is the same construction with the patch left in.
    """

    box: Path
    home: Path
    env: dict[str, str]


@dataclass(frozen=True)
class Repo(Boxed):
    """`Boxed` with a real git repository in it, for the housekeeping settings."""

    repo: Path


@pytest.fixture
def boxed() -> Iterator[Boxed]:
    """`support.tempdir` rather than a bare `TemporaryDirectory`, which is what
    this fixture's `unittest` ancestor used: the rule CLAUDE.md states for a
    throwaway directory, and it upgrades the cleanup to `discard`, so a tree
    that will not go names what survived instead of raising an errno."""
    with support.tempdir(prefix="tupferl-support-") as box:
        home = box / "home"
        home.mkdir()
        support.seed_home(home)
        yield Boxed(box, home, support.sandbox_env(home))


@pytest.fixture
def poisoned(boxed: Boxed) -> Iterator[Boxed]:
    """`boxed`, with every name in `ENV_KEYS` pointed at `/poison` first."""
    with mock.patch.dict(os.environ, {name: f"{POISON}/{name}" for name in paths.ENV_KEYS}):
        yield boxed


@pytest.fixture
def git_box(boxed: Boxed) -> Repo:
    repo = boxed.box / "repo"
    support.git(["init", "--quiet", str(repo)], cwd=boxed.box, env=boxed.env)
    return Repo(**vars(boxed), repo=repo)


@pytest.fixture
def stuck() -> Iterator[tempfile.TemporaryDirectory[str]]:
    """A real tree holding a real pack temporary, whose cleanup refuses.

    The original `cleanup` is kept and called in a `finally`, so the tree still
    goes away when the test ends: a test about a leaked tree that leaked one
    would be its own bug.

    A raw `TemporaryDirectory` rather than `support.tempdir`, which is the one
    place in this file that wants one: what is under test is `discard` reacting
    to a `cleanup` that raises, and `tempdir` calls `discard` itself.
    """
    box = tempfile.TemporaryDirectory(prefix="tupferl-stuck-")
    root = Path(box.name)
    (root / "repo" / ".git" / "objects").mkdir(parents=True)
    (root / "repo" / ".git" / "objects" / "tmp_pack_abcdef").write_text("x")
    really = box.cleanup

    def refuse() -> None:
        raise REFUSED

    box.cleanup = refuse  # type: ignore[method-assign]
    try:
        yield box
    finally:
        really()


@pytest.fixture
def copy() -> Iterator[Copies]:
    """A fresh copy of the two-machine template, as often as a test asks for one.

    A factory rather than a fixture holding one copy, because
    `test_two_copies_do_not_share_a_remote` needs two and their not seeing each
    other is the whole claim.
    """
    with contextlib.ExitStack() as stack:

        def made() -> tuple[support.Computer, support.Computer, Path]:
            return support.copy_template(
                stack.enter_context(support.tempdir(prefix="tupferl-copies-"))
            )

        yield made


def raised(box: tempfile.TemporaryDirectory[str]) -> OSError:
    """The `OSError` `discard` raises over `box`, read back for its wording."""
    with pytest.raises(OSError) as caught:
        support.discard(box)
    return caught.value


def asked(box: Repo, key: str) -> subprocess.CompletedProcess[str]:
    """What git answers for `key` inside the sandbox repository."""
    return subprocess.run(
        ["git", "config", "--get", key],
        cwd=box.repo,
        env=box.env,
        capture_output=True,
        text=True,
        check=False,
    )


def popen_kwargs(env: dict[str, str]) -> dict[str, object]:
    """The keyword arguments `run_cli` hands `subprocess.Popen` for a keyed run.

    Driven rather than read out of `run_cli`: what is under test is which
    objects reach Popen, and the only way to know is to watch the call.
    """
    seen: dict[str, object] = {}
    real = subprocess.Popen

    def watch(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real(*args, **kwargs)

    with mock.patch.object(subprocess, "Popen", watch):
        support.run_cli(["--version"], env, keys="s")
    return seen


def bounded(value: float, armed: str | None) -> float:
    """`support.bounded(value)` with the harness's alarm set to `armed`."""
    environ = {} if armed is None else {support.ALARM: armed}
    with mock.patch.dict(os.environ, environ, clear=(armed is None)):
        return support.bounded(value)


def now_armed() -> float:
    """Seconds left on this process's interval timer, or 0."""
    return signal.getitimer(signal.ITIMER_REAL)[0]


@pytest.mark.usefixtures("poisoned")
class TestTheSandboxReplacesTheEnvironment:
    """Every test here runs with `ENV_KEYS` pointed at `/poison`.

    On the class, which is CLAUDE.md's rule and not a style: the mark states
    *this class runs poisoned*, and `test_the_poison_really_would_be_visible`
    never names the fixture -- it reads the environment the fixture patched.
    Converted by giving each test the fixtures its body mentions, that test
    would get none and would silently assert against the developer's own.

    The tests that need the *paths* take `boxed`, which is the same object:
    `poisoned` is `boxed` with the patch around it, so pytest hands back one
    instance for both names.
    """

    def test_no_poisoned_value_survives(self, boxed: Boxed) -> None:
        with support.sandboxed(boxed.home):
            leaked = [name for name, value in os.environ.items() if value.startswith(POISON)]
        assert leaked == []

    def test_every_path_resolves_inside_the_sandbox(self, boxed: Boxed) -> None:
        """The property that matters, stated as paths rather than as variables.

        `ENV_KEYS` is the list of what gets cleared, but clearing is only the
        means. What must be true is that the functions those variables feed all
        answer inside the box -- so this asks them, rather than asking about the
        environment a second time.
        """
        with support.sandboxed(boxed.home):
            answers = [paths.home(), paths.repo_dir(), paths.state_dir(), paths.backup_dir()]
        for answer in answers:
            assert answer.is_relative_to(boxed.home), f"{answer} is outside {boxed.home}"

    def test_the_poison_really_would_be_visible(self) -> None:
        """The precondition, asserted rather than assumed.

        Without this, both tests above pass just as well against a fixture that
        never managed to set anything -- "no poisoned value survives" is
        trivially true when there was no poison. CLAUDE.md §2 calls that a
        negative assertion whose precondition was never established.
        """
        assert paths.repo_dir().is_relative_to(POISON)

    def test_the_hostname_is_the_sandbox_one(self, boxed: Boxed) -> None:
        """Not the real machine's, which differs per developer and per CI leg."""
        with support.sandboxed(boxed.home, host="other-host"):
            assert paths.hostname() == "other-host"


class TestTheSandboxKeepsTheGuardsOn:
    """What a sandbox must *carry*, which is the opposite failure to leaking.

    Building the environment from nothing is right, and it has one cost: a
    variable that makes the run stricter is dropped along with the ones that
    would point it at the real installation. CI's
    `PYTHONWARNINGS=error::DeprecationWarning` is the case -- without it every
    test that drives the CLI as a subprocess silently stops enforcing it.
    """

    def test_a_deprecation_warning_still_fails_a_sandboxed_child(self, boxed: Boxed) -> None:
        with mock.patch.dict(os.environ, {"PYTHONWARNINGS": "error::DeprecationWarning"}):
            env = support.sandbox_env(boxed.home)
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import warnings; warnings.warn('x', DeprecationWarning)",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode != 0, "the sandbox dropped PYTHONWARNINGS"
        assert "DeprecationWarning" in done.stderr

    def test_without_it_set_the_child_is_unaffected(self, boxed: Boxed) -> None:
        """The precondition: the test above must be observing the variable
        rather than a python that errors on every warning regardless."""
        with mock.patch.dict(os.environ, {}, clear=True):
            env = support.sandbox_env(boxed.home)
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import warnings; warnings.warn('x', DeprecationWarning)",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert done.returncode == 0


class TestTheFixturesAreReal:
    """The helpers build git repositories, not directories that look like them."""

    def test_make_repo_is_a_repository_with_a_commit(self, boxed: Boxed) -> None:
        repo = support.make_repo(boxed.box / "repo", boxed.env)
        assert support.git(["branch", "--show-current"], repo, boxed.env) == "main"
        assert support.git(["log", "-1", "--format=%s"], repo, boxed.env) == "initial"

    def test_a_pushed_repo_and_its_remote_agree(self, boxed: Boxed) -> None:
        """A remote is only a remote if something can be read back out of it."""
        remote = support.make_remote(boxed.box / "remote.git", boxed.env)
        repo = support.make_repo(boxed.box / "repo", boxed.env, remote=remote)
        here = support.git(["rev-parse", "HEAD"], repo, boxed.env)
        there = support.git(["ls-remote", str(remote), "refs/heads/main"], repo, boxed.env)
        assert there.startswith(here), f"{there} does not start with {here}"

    def test_git_raises_rather_than_returning_a_failure(self, boxed: Boxed) -> None:
        """The fixture helper must be loud: a half-built repository is the weak
        fixture every other test would then be written against."""
        with pytest.raises(AssertionError):
            support.git(["rev-parse", "HEAD"], boxed.box, boxed.env)


class TestADrivenChildIsNotCollectedThroughAPipe:
    """`run_cli` hands the child real files, never `subprocess.PIPE`.

    A pipe makes *this* process hold everything the child writes -- measured at
    724 MiB of parent RSS in three seconds against a child printing 4 KiB at a
    time. Under `tools/mutate.py` the parent's memory is charged to the lane's
    share, so a mutant that made `conflicts.ask` loop at EOF killed the whole
    session and was reported `BROKE`: no verdict at all, for a line that
    `test_end_of_input_skips` does guard. It was the only `BROKE` in milestone
    4's sweep.

    This asserts the mechanism rather than the megabytes, and that is the honest
    shape here: `run_cli` always runs `python -m tupferl`, and no tupferl command
    prints without end, so a fixture built to measure growth through this
    function measures a child that exits immediately. The first attempt did
    exactly that and passed in 0.256s. Handing Popen a file is the *only* way the
    parent avoids accumulating, so it is the whole property, and
    `tests/test_sync_conflicts.py` asserts the consequence a file buys.
    """

    @pytest.mark.parametrize("stream", ("stdout", "stderr"))
    def test_popen_is_given_files_for_both_streams(self, stream: str, boxed: Boxed) -> None:
        seen = popen_kwargs(boxed.env)
        assert subprocess.PIPE is not seen[stream], "collected through a pipe"
        assert hasattr(seen[stream], "fileno"), f"{stream} is not a file: {seen[stream]!r}"

    def test_the_precondition_that_the_pty_path_was_taken(self, boxed: Boxed) -> None:
        """Both assertions above are vacuous if `keys` was ignored and the
        pipe-free `subprocess.run` branch ran instead -- that branch uses
        `capture_output`, which never reaches Popen's kwargs as this test reads
        them."""
        assert hasattr(popen_kwargs(boxed.env).get("stdin"), "__index__"), "no pty was attached"


class TestBackgroundGitIsOff:
    """#17: no detached git process may outlive the command that started it.

    **Asked of git, not read out of the file.** `seed_home` writes
    `$HOME/.gitconfig`, and whether git reads it depends on the sandbox clearing
    `GIT_CONFIG_GLOBAL` and `XDG_CONFIG_HOME` -- which is the half a test that
    grepped the file it just wrote could not see. `git config --get` under this
    machine's environment is the whole claim: the setting is in force where git
    will look for it.

    Why it matters is in `support.NO_HOUSEKEEPING`. In short: `gc --auto` and
    `maintenance run --auto` are detached by default, and a detached process
    writing into `.git/objects` is what makes a tree non-empty a moment after
    `shutil` scanned it as empty. It has turned CI red twice, both times naming
    the sync property, which had passed.
    """

    @pytest.mark.parametrize(
        ("key", "want"),
        (("gc.auto", "0"), ("gc.autoDetach", "false"), ("maintenance.auto", "false")),
    )
    def test_git_reads_back_every_setting_that_stops_housekeeping(
        self, key: str, want: str, git_box: Repo
    ) -> None:
        got = asked(git_box, key)
        assert got.returncode == 0, got.stderr
        assert got.stdout.strip() == want

    def test_the_probe_can_come_back_empty(self, git_box: Repo) -> None:
        """The precondition. The three cases above, against a `git config` that
        answered *anything*, would pass if `--get` always printed the value
        asked for; this shows it does not, so the three are reading real
        settings rather than an echo."""
        got = asked(git_box, "gc.nosuchsetting")
        assert got.returncode == 1
        assert got.stdout.strip() == ""

    def test_the_identity_still_works_beside_them(self, git_box: Repo) -> None:
        """`seed_home` writes one file, and #17 appended to it. A malformed
        section would take git's identity down with it, and every commit in the
        suite with it -- so this asserts the half that was already there."""
        got = asked(git_box, "user.email")
        assert got.returncode == 0, got.stderr
        assert got.stdout.strip() == f"{support.HOST}@example.invalid"


class TestATreeThatWillNotGo:
    """#17's other half: when cleanup fails, say what survived.

    The failure this exists for is a race nobody has reproduced on demand -- see
    #17, which could not do it on git 2.43 and could not get at the runner's
    2.55. So the *trigger* here is simulated: the box's own `cleanup` is made to
    raise the errno CI actually printed. Everything else is real -- a real tree,
    real files, and the listing read off the real filesystem after the failure.

    That is the honest shape for this claim. What is under test is "if cleanup
    raises, the error names what is left", not "git races teardown"; a fixture
    that waited for a real race would be a test that usually does nothing.

    **The bound method, not `tempfile`'s internals.** `TemporaryDirectory`
    reaches `shutil.rmtree` by a different private route on 3.10, 3.12 and 3.14,
    and this suite runs on all three -- a patch of one of them passes on one leg
    and fails on the others, which is the version trap CLAUDE.md's gotchas
    already collect two instances of. `box.cleanup()` is what `discard` calls,
    and making *that* raise is the precondition stated exactly.
    """

    def test_the_message_names_the_file_that_survived(
        self, stuck: tempfile.TemporaryDirectory[str]
    ) -> None:
        """`tmp_pack_abcdef` is a writer's signature: git wrote it, and no test
        did. That name in the error is the whole point of #17's second half."""
        assert "tmp_pack_abcdef" in str(raised(stuck))

    def test_it_keeps_the_original_error_rather_than_replacing_it(
        self, stuck: tempfile.TemporaryDirectory[str]
    ) -> None:
        """The errno and the wording git's own failure produced are what a
        reader searches for. A wrapper that dropped them would send them looking
        for a different bug."""
        boom = raised(stuck)
        assert "Directory not empty" in str(boom)
        assert boom.__cause__ is REFUSED

    def test_it_says_the_writer_outlived_its_command(
        self, stuck: tempfile.TemporaryDirectory[str]
    ) -> None:
        """The sentence that stops the next reader diagnosing the sync engine,
        which is what #17 says cost the most both times it happened."""
        boom = raised(stuck)
        assert "outliving the command that started it" in str(boom)

    def test_a_long_listing_is_cut_and_says_how_much_it_cut(
        self, stuck: tempfile.TemporaryDirectory[str]
    ) -> None:
        """A whole surviving tree is hundreds of paths, and a message nobody
        reads to the end names nothing.

        The count is written out rather than computed here: the same subtraction
        spelled twice is a test holding a copy of its subject, which cannot fail
        (CLAUDE.md §2). Arriving at it: `stuck` leaves four paths of its own --
        `repo`, `.git`, `objects` and the pack temporary -- so 13 files make 17,
        of which `NAMED_WHEN_STUCK` are named and 5 are not.
        """
        root = Path(stuck.name)
        for number in range(13):
            (root / f"file{number:03d}").write_text("x")
        boom = str(raised(stuck))
        assert "and 5 more" in boom
        # The cut is real: `file012` sorts last and must not have been named.
        assert "file012" not in boom
        assert "file000" in boom

    def test_a_tree_that_goes_quietly_raises_nothing(self) -> None:
        """The ordinary path, which is every other call in the suite. Without
        it, `discard` could raise always and the four tests above would still
        pass -- CLAUDE.md §2's negative assertion with no precondition."""
        box = tempfile.TemporaryDirectory(prefix="tupferl-quiet-")
        root = Path(box.name)
        (root / "a").mkdir()
        (root / "a" / "b").write_text("x")
        support.discard(box)
        assert not root.exists()


class TestTheTwoMachineTemplate:
    """#19's fixture: copies of one tree that must not be able to see each other.

    The saving is real -- 4.3 ms against 120.4 ms per test, and a measured
    median of 19.5 s off the six affected modules run serially -- but it trades
    a fresh build for a shared origin, and the failure that trade can produce is
    the worst kind: two tests quietly sharing a remote, so one sees another's
    commits and the pair pass or fail depending on the order they ran in.
    That is what this class is for.
    """

    def test_two_copies_do_not_share_a_remote(self, copy: Copies) -> None:
        """The contamination test, and it is driven rather than asserted from
        the config: one copy syncs a change, and the other must not see it.

        A URL comparison alone would pass against two paths that differ in text
        and resolve to the same directory.
        """
        first, _, here = copy()
        other_first, other_second, there = copy()
        assert there != here

        first.write(".bashrc", "CHANGED ON THE FIRST COPY\n")
        assert first.call("sync") == 0

        assert other_second.call("init", str(there)) == 0
        assert "CHANGED ON THE FIRST COPY" not in other_second.read(".bashrc")
        assert other_first.read(".bashrc") == support.STARTS_AS

    def test_the_copy_points_at_its_own_remote(self, copy: Copies) -> None:
        """The mechanism behind the test above. Left unrewritten, every copy's
        `origin` is the template's remote."""
        first, _, remote = copy()
        url = support.git(["remote", "get-url", "origin"], cwd=first.repo, env=first.env)
        assert url == str(remote)
        assert not Path(url).is_relative_to(support.template())

    def test_no_stale_fetch_head_survives_the_copy(self, copy: Copies) -> None:
        """It records the URL of the last fetch, which in a copy is the
        template's. Nothing reads it -- `sync` merges `<remote>/<branch>` -- so
        this is a lie removed rather than a bug fixed, and the test says which."""
        first, _, _ = copy()
        assert not (first.repo / ".git" / "FETCH_HEAD").exists()

    def test_nothing_in_a_copy_still_names_the_template(self, copy: Copies) -> None:
        """The general form of the two tests above, so a *third* file that
        learns to hold an absolute path is caught rather than waited for.

        This is how the two were found in the first place: grep the built tree
        for its own root.
        """
        _, _, remote = copy()
        root = str(support.template())
        named = []
        for path in remote.parent.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:  # pragma: no cover - a fifo or a socket, neither present
                continue
            if root in text:
                named.append(str(path.relative_to(remote.parent)))
        assert named == []

    def test_the_template_is_built_once(self) -> None:
        """Per *process*, not per class -- 45 classes take the fixture, so per
        class would be 45 builds. Asked of the cache rather than timed, because
        a timing assertion here would be a flake.

        The count said 40 and was inherited from a tree where `TwoMachinesCase`
        was the thing being counted; B4b deleted that class and re-measured the
        *tests* at 190 without reaching this sentence. 45 classes, 2026-08-31,
        by mapping `--fixtures-per-test`'s `file:line` back through `ast`.
        """
        support.template()
        before = support.template.cache_info()
        support.template()
        support.template()
        after = support.template.cache_info()
        assert after.misses == before.misses
        assert after.hits == before.hits + 2

    def test_a_copy_starts_where_a_built_one_did(self, copy: Copies) -> None:
        """The equivalence the whole change rests on: what the fixture used to
        build by running `init`, `add` and `sync` is what a copy now holds.

        Asserted through the tool rather than by comparing trees -- commit
        hashes and timestamps differ between a build and a copy and always will,
        and none of that is what a test using this fixture depends on.
        """
        first, second, remote = copy()
        assert first.read(".bashrc") == support.STARTS_AS
        status, said = first.say("status")
        assert status == 0, said
        assert "1 file managed, 0 to change, 0 in conflict" in said
        # And the remote really holds it: the second machine can be brought up.
        assert second.call("init", str(remote)) == 0
        assert second.read(".bashrc") == support.STARTS_AS


class TestAPromptIsBoundedRatherThanBlocking:
    """`typing` fails a prompt that asks more often than its keys answer.

    **`FALLBACK` is not a bound.** `conflicts.one_key` sets `VMIN` to 1, so a
    read on a pty whose master is still open waits for ever rather than
    reporting exhaustion -- correct for a real terminal, and it leaves the eight
    `s` keys as the only thing between a prompt asking once too often and a
    suite that hangs.

    That gap had a measured cost. A mutation making `ask` treat *every* key as
    unrecognised eats all nine and blocks; under `tools/mutate.py` it tripped
    the 30s per-test alarm and was filed `BROKE`, which is never `caught` --
    `tupferl/conflicts.py:635` came back that way in three of four ordered
    sweeps and `caught` in none, so the line it appears to guard was guarded by
    nothing. With the bound in place the same mutation is `caught`.

    `PROMPTED` is patched down here rather than waited out: the claim is that a
    bound exists and fires, not what it is set to.
    """

    def test_a_prompt_that_never_settles_fails_instead_of_hanging(self) -> None:
        with (
            mock.patch.object(support, "PROMPTED", 0.5),
            pytest.raises(TimeoutError),
            support.typing("l"),
        ):
            while True:
                conflicts.one_key(sys.stdin)

    def test_the_bounds_beat_the_harness_alarm(self) -> None:
        """The number, not just the mechanism -- and the tests above patch it, so
        nothing else can see it.

        `tools/mutate.py` arms a per-test alarm and files whatever trips it as
        `BROKE`, which is never `caught`. A fixture bound *above* that alarm
        therefore never fires: the two race and the harness always wins, and the
        line under test ends up guarded by nothing while the summary shows the
        row in neither of the two numbers a reader looks at. That is exactly how
        `conflicts.py:635` went unguarded, at `PROMPTED = 60.0` against a 30s
        alarm.

        Asserted against `mutate.EACH_TEST` rather than against a literal, so
        raising the alarm cannot silently re-open the gap.

        **This covers the default alarm and nothing else**, which is the half of
        the guarantee it was originally mistaken for. `--each-test` is a flag,
        and a sweep run with a lower one is not visible from here at all --
        `EACH_TEST` still reads 30 while the alarm in force is 10.
        `TestBoundingAFixtureAgainstTheAlarmActuallyArmed` is that half.
        """
        assert support.PROMPTED < mutate.EACH_TEST, "a whole keyed run"
        assert support.PATIENCE < mutate.EACH_TEST, "a single read"

    def test_a_prompt_that_settles_is_left_alone(self) -> None:
        """The other half, and without it "always raise" passes the test above.
        A bound that fires on a prompt which *did* get its answer would fail
        every keyed test in the suite -- which is exactly what an earlier
        attempt at this did, 11 failures and an error, while the mutation run
        above them reported `caught` on a red baseline and read like a clean
        sweep.
        """
        with mock.patch.object(support, "PROMPTED", 0.5), support.typing("l"):
            assert conflicts.one_key(sys.stdin) == "l"


class TestBoundingAFixtureAgainstTheAlarmActuallyArmed:
    """`bounded`: the fixture's deadline beats whatever `--each-test` armed.

    The class above pins `PROMPTED` and `PATIENCE` below `mutate.EACH_TEST`, and
    that was taken for the whole guarantee. It is not: `EACH_TEST` is a
    *default*, `--each-test` overrides it, and a sweep run with `--each-test 10`
    puts a 20s `PROMPTED` back above the alarm -- the identical hole that left
    `conflicts.py:635` filed `BROKE` in three of four sweeps and `caught` in
    none. The test written to prevent that compares against the constant, so it
    cannot see it. This is the half it cannot see.

    `_run` sets the armed value in the child's environment, so what the fixture
    needs is already there; the only question is whether it reads it.
    """

    def test_an_ordinary_run_is_left_exactly_as_it_was(self) -> None:
        """No harness, no alarm, no change -- and this is most runs of the suite.
        A rule that moved the bounds when nothing was armed would be paying for
        the rare case in every developer's preflight."""
        assert bounded(20.0, None) == 20.0
        assert bounded(5.0, None) == 5.0

    def test_a_tighter_alarm_brings_the_bound_under_it(self) -> None:
        assert bounded(20.0, "10") < 10.0

    @pytest.mark.parametrize("name", ("PROMPTED", "PATIENCE"))
    @pytest.mark.parametrize("armed", (0.5, 1.0, 3.0, 7.5, 10.0, 30.0, 300.0))
    def test_every_alarm_a_sweep_can_arm_is_beaten(self, armed: float, name: str) -> None:
        """The property, rather than one worked example. `--each-test` takes a
        float and the useful range spans three orders of magnitude, so a single
        pair proves the arithmetic and not the guarantee.

        Both constants, because they are separate numbers and only one of them
        was ever wrong -- `PATIENCE` at 5.0 already beat a 10s alarm, so a test
        that checked `PROMPTED` alone would pass against a `bounded` that
        returned its argument for anything under 7.5.
        """
        assert bounded(getattr(support, name), str(armed)) < armed, name

    def test_a_disabled_alarm_leaves_the_bound_alone(self) -> None:
        """`--each-test 0` asks for no alarm, and `verdict.each_test` also
        returns 0 where `SIGALRM` does not exist. Nothing is racing the fixture
        then, and scaling by zero would make every bound fire immediately --
        which turns a run with the alarm *off* into a suite that fails
        everywhere."""
        assert bounded(20.0, "0") == 20.0

    def test_a_value_that_is_not_a_number_leaves_the_bound_alone(self) -> None:
        """Nothing writes this but the harness, so a bad value means something
        else set the name. Erring towards the fixture's own number keeps a
        strange environment from failing every keyed test at once."""
        assert bounded(20.0, "soon") == 20.0
        assert bounded(20.0, "") == 20.0

    def test_the_harness_and_the_fixture_spell_the_name_the_same(self) -> None:
        """Two spellings of one variable, and a typo in either is invisible: the
        harness would set a name nothing reads, `bounded` would find nothing and
        return its argument, and every assertion above would still pass."""
        assert support.ALARM == mutate._ALARM

    def test_the_mutated_tree_marker_is_spelled_the_same_at_both_ends(self) -> None:
        """The same hazard one variable over, and a worse failure. A typo here
        leaves `over_a_mutated_tree` answering `False` inside every probe, the
        two tag assertions run against a mutated copy, and 226 rows go back to
        being credited to a kill nothing behavioural made -- silently, because
        every one of those rows reads `caught` (#110)."""
        assert support.MUTATED == mutate._MUTATED

    def test_nothing_says_the_tree_is_mutated_under_an_ordinary_run(self) -> None:
        """The other half. A helper that answered `True` everywhere would make
        the two assertions it gates dead in the preflight and in CI, which is
        the only place they ever run -- and nothing would say so, because a
        skipped test is not a failure anywhere `--no-skips` is not passed."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(support.MUTATED, None)
            assert support.over_a_mutated_tree() is False

    def test_it_answers_true_where_the_harness_sets_it(self) -> None:
        with mock.patch.dict(os.environ, {support.MUTATED: "1"}):
            assert support.over_a_mutated_tree() is True

    def test_a_driven_bound_follows_the_alarm_that_was_armed(self) -> None:
        """A routed constant really is smaller in a child whose alarm is tighter.

        `bounded` being right is the class above, and that every wait is
        *spelled* through it is `TestEveryWaitOnAChildIsBounded` below. This is
        the third thing neither covers: that a module-level constant, evaluated
        at import in a real child, reads the environment `_run` set.

        Asked of a real import rather than of the source, because what has to be
        true is the value a sweep's child computes. A source check would pass
        against `support.bounded(20.0) if False else 20`.

        Both halves, because "always small" would satisfy the first alone: with
        nothing armed the file's own number has to stand, which is every
        ordinary run of the suite.
        """
        tight, loose = bounds_when_armed("10"), bounds_when_armed(None)
        assert set(tight) == set(DRIVEN), tight
        assert all(seconds < 10.0 for seconds in tight.values()), tight
        assert all(seconds == 20.0 for seconds in loose.values()), loose

    def test_the_sandbox_carries_the_name_through(self) -> None:
        """`support.environment` builds from nothing, so a name not in `CARRIES`
        is absent in every subprocess a test spawns -- which is where the keyed
        fixtures with the longest waits actually run."""
        assert support.ALARM in support.CARRIES

    def test_the_harness_tells_the_child_what_it_armed(self) -> None:
        """The plumbing half, asserted where it happens rather than inferred
        from a sweep. `_run` builds the child's environment; watching the spawn
        and reading its argv and env is the thing that changed, and no alarm has
        to fire to check it.
        """
        seen: dict[str, str] = {}

        def spawn(argv: list[str], **kwargs: Any) -> None:
            seen.update(kwargs["env"])
            raise RuntimeError("far enough")

        # An empty directory rather than `support.ROOT`: `_run` clears bytecode
        # under whatever root it is given, and pointing it at the real tree
        # would have this test delete the `__pycache__` of every shard running
        # beside it.
        with (
            tempfile.TemporaryDirectory() as box,
            mock.patch.object(subprocess, "Popen", spawn),
            pytest.raises(RuntimeError),
        ):
            mutate._run(["tests.test_paths"], Path(box), each=7.5)
        assert seen.get(support.ALARM) == "7.5"


@pytest.mark.usefixtures("_alarm_put_back")
class TestEveryWaitOnAChildIsBounded:
    """No fixture in `tests/` waits on a child process against a bare number.

    **The list this replaced could not see the bugs it was written for.** B5
    routed `test_watch.py`'s and `test_reached.py`'s `BOUND` through `bounded`
    and named the two in a tuple; the same defect was live in four more places,
    including `tests/test_mutate.py`, whose own docstring narrates the mistake
    three times ("that is the third instance of one mistake here") and then
    spells its bound `BOUND = 20`. A hand-written list is a record of what
    somebody remembered.

    So this walks instead, the way `tests/test_errors.py` walks every
    `raise TupferlError`: find every `timeout=` handed to something that waits
    on a child, follow the name to what it was assigned, and insist it reaches
    `support.bounded`. What that buys is not tidiness -- a bound at or above
    the alarm loses the race, the row is filed `BROKE`, and `BROKE` is never
    `caught`, so the line the bound was written to guard is guarded by nothing
    while the summary counts it in neither of the two numbers a reader looks at.
    """

    def test_the_walk_finds_the_waits_there_are(self) -> None:
        """The precondition, and it is the whole reason the test below can fail.

        A resolver that matched nothing -- a renamed helper, a `subprocess`
        alias this does not know -- would report zero unrouted waits and read as
        a clean bill of health. That is CLAUDE.md's zero-iteration trap with the
        loop moved into a walk.
        """
        assert len(list(every_wait())) >= FLOOR

    def test_no_wait_is_left_against_a_bare_number(self) -> None:
        unrouted = [
            f"{name}:{line} timeout={said}" for name, line, said, ok in every_wait() if not ok
        ]
        assert unrouted == []

    def test_the_walk_can_tell_a_bare_number_from_a_routed_one(self) -> None:
        """`routed` against both answers, since the test above is a negative one
        and would hold just as well against a resolver that says yes to
        everything. Written as source rather than driven, because what is under
        test is the reader."""
        tree = ast.parse("import subprocess\nBARE = 20\nsubprocess.run([], timeout=BARE)\n")
        assert [routed(v, tree) for v in waits_on_a_child(tree)] == [False]
        tree = ast.parse(
            "import subprocess\nOK = support.bounded(20.0)\nsubprocess.run([], timeout=OK)\n"
        )
        assert [routed(v, tree) for v in waits_on_a_child(tree)] == [True]

    def test_a_timeout_that_is_the_assertion_is_left_alone(self) -> None:
        """The one shape the rule lets through, pinned so it stays that shape."""
        tree = ast.parse(
            "import pytest, subprocess\n"
            "with pytest.raises(subprocess.TimeoutExpired):\n"
            "    child.wait(timeout=0.5)\n"
        )
        assert list(waits_on_a_child(tree)) == []


class TestABoundGivesTheHarnessItsAlarmBack:
    """`deadline` restores the `ITIMER_REAL` it found, rather than clearing it.

    There is one interval timer per process and `tools/mutate.py` arms it around
    every test, so a bound that zeroed it on the way out left the *rest* of that
    test unwatched by the harness. A hang afterwards then costs `TIMEOUT`'s 300s
    instead of `EACH_TEST`'s 30 -- ten times the lane, and filed under the one
    outcome that names no test.

    It went unnoticed because nothing exercised the interesting shape: a
    `deadline` around a whole test body has nothing after it to leave unwatched.
    A bound inside a *helper* that returns mid-test does, and this PR added two.
    """

    def test_an_alarm_that_was_armed_is_still_armed_afterwards(self) -> None:
        signal.signal(signal.SIGALRM, lambda *a: None)
        signal.setitimer(signal.ITIMER_REAL, 30.0)
        with support.deadline(1.0, "inner"):
            pass
        assert now_armed() > 25.0, "the harness's alarm was cancelled"

    def test_the_time_spent_inside_is_taken_off(self) -> None:
        """Restoring the *original* 30s rather than what is left would give a
        test that had already run 29s a fresh half-minute, which is the bound
        quietly doubling rather than being handed back."""
        signal.signal(signal.SIGALRM, lambda *a: None)
        signal.setitimer(signal.ITIMER_REAL, 30.0)
        with support.deadline(1.0, "inner"):
            time.sleep(0.2)
        assert now_armed() < 29.95, "the spent time was not deducted"

    def test_nothing_armed_stays_nothing_armed(self) -> None:
        """Every ordinary run of the suite. Arming a timer that nobody asked for
        would send `SIGALRM` into whatever handler happened to be installed."""
        signal.setitimer(signal.ITIMER_REAL, 0)
        with support.deadline(1.0, "inner"):
            pass
        assert now_armed() == 0.0

    def test_the_previous_handler_is_back_before_the_alarm_can_fire(self) -> None:
        """Restored in the other order, a re-armed outer alarm could land in
        `ring` and be reported as *this* bound -- a timeout blamed on the
        fixture that had already finished."""
        signal.signal(signal.SIGALRM, lambda *a: None)
        mine = signal.getsignal(signal.SIGALRM)
        signal.setitimer(signal.ITIMER_REAL, 30.0)
        with support.deadline(1.0, "inner"):
            pass
        assert signal.getsignal(signal.SIGALRM) is mine


@pytest.mark.usefixtures("_alarm_put_back")
class TestABoundBuiltForAClassIsReallyArmed:
    """`support.bounds`: `deadline` as the autouse fixture seven classes wanted.

    **A mechanism with no reader is what this file exists to refuse.** Seven
    classes across `test_mutants` and `test_mutate` held four hand-copied lines
    each -- two pairs of them byte-identical, comment included -- and `bounds`
    replaced all seven. A factory that quietly produced a fixture pytest never
    ran would look exactly like one that worked: every one of those classes
    passes either way, because the bound only matters under a mutation that
    hangs, and a hang is filed `BROKE`, which is never `caught`. So it is
    asserted here, from inside a class that uses it, rather than trusted.

    `_alarm_put_back` because these tests really arm the process timer, and this
    is the process `tools/mutate.py` arms its own per-test alarm in.
    """

    #: Far enough from any other bound in this file that the assertion below is
    #: about *this* number rather than about something being armed at all.
    ARMED = 12.5

    _bounded = support.bounds(ARMED, "the class bound never fired")

    def test_a_test_naming_no_fixture_still_gets_the_bound(self) -> None:
        """Autouse is the whole point: the class states the property and no test
        body mentions it, which is the same argument CLAUDE.md makes for putting
        a sandbox mark on the class rather than inferring it per method."""
        assert now_armed() > 0.0, "no alarm was armed for this test"

    def test_it_arms_the_seconds_it_was_given(self) -> None:
        """Not merely *an* alarm. A `bounds` that ignored its argument and armed
        `PATIENCE` would satisfy the test above and quietly give every one of
        the seven classes the same bound, which is the two-symmetric-inputs
        shape one level up."""
        assert self.ARMED - 1.0 < now_armed() <= self.ARMED

    def test_it_is_armed_afresh_for_each_test(self) -> None:
        """Three tests, three arms. A fixture built once at class-definition
        time and entered once would leave this one running on whatever the
        previous test had left -- which under `parametrize` is exactly the
        `subTest` trap B6 converted away from."""
        assert self.ARMED - 1.0 < now_armed() <= self.ARMED
