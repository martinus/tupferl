"""Which kind of failure a suite produced -- the question an exit status cannot answer.

`tools/mutate.py` needs to know whether a *test method noticed* the mutation, or
whether the run merely fell over. Both exit non-zero, and both leave a plausible
count of tests behind, so the distinction has to be drawn where the exception
objects still exist rather than reconstructed from what pytest printed. That is
what this file is.

This is the pytest backend, and it is what a sweep uses.
`tools/verdict_unittest.py` is the classifier that was here before, kept until
`docs/pytest-plan.md`'s Phase C and reachable with
``TUPFERL_MUTATE_VERDICT=unittest`` so that a row the two disagree about can be
re-run against the old one rather than argued about. `cap` and `each_test` are
duplicated between the two files rather than shared, because the isolation
property below forbids either from importing anything of ours.

**It is read, not imported.** `mutate` reads this source out of its *own* tree
and hands it to ``python -c`` in the sandbox. Two properties fall out, and both
are the point:

- the sandbox's copy of ``tools/`` is never consulted, so a mutation to this file
  cannot decide its own verdict -- which matters as soon as `tools/**.py` is
  itself something a generated table mutates;
- being a real module rather than a string constant, `ruff` and `mypy` see it.

``-c`` also puts the working directory on ``sys.path`` where a script path would
not, and it is the directory pytest then takes as its ``rootdir``.

**Classification is by phase, never by class name.** Measured against pytest
9.1.1 on both supported interpreters, driving `unittest.TestCase`s:

| what died | phase | bucket |
|---|---|---|
| the test body, by assertion or by any exception | ``call`` | `noticed` |
| a case inside ``self.subTest(...)`` | ``call`` | `noticed`, against the **owner** |
| the instance's own ``tearDown`` | ``call`` | `noticed` |
| ``setUpClass`` / ``setUpModule`` | ``setup`` | `broke` |
| ``tearDownClass`` / ``tearDownModule`` | ``teardown`` | `broke` |
| a module that will not import | no phase; `pytest_collectreport` | `broke` |

So the fixture/test line that `verdict_unittest` had to draw with an
`isinstance` against `unittest.suite._ErrorHolder` is drawn here by the phase
pytest already reports, and needs no special case at all. That mapping is a
*measurement*, not a documented guarantee, which is why
`tests/test_verdict.py`'s `TestWhatThisAssumesOfPytest` asserts each row of it:
a pytest that moved one would otherwise turn `broke` silently into `caught`.

**The subtest trap, and it is the reason to classify in
`pytest_runtest_makereport` rather than in `pytest_runtest_logreport`.** When a
case inside ``self.subTest(...)`` fails, pytest 9 emits a failed
`_pytest.subtests.SubtestReport` *and* the owning test's own report says
``passed``. Reading finished reports and asking "did this test fail" therefore
answers no for a test the suite demonstrably caught -- flattering the tests,
which is the direction every bug in this class has erred. At `makereport` there
is no such split: every failure, subtest or not, arrives once with ``item``
being the owner and ``call.excinfo`` holding what was raised.

**`-s` is forbidden, and not as a style rule.** pytest replaces `sys.stdin` with
something whose `isatty()` is false, which is what stops the suite prompting;
``-s`` undoes exactly that, and `tupferl sync` asks `sys.stdin.isatty()` to
decide whether anyone is there to answer a conflict. A probe that prompts does
not fail, it blocks. The corollary is that anything this plugin `print`s is
eaten by pytest's own capture -- partially, which is worse than entirely:
measured, 3 of 8 lines survived. Diagnose from the report file or a file
descriptor duplicated before `pytest.main` is entered, never by reaching for
``-s``.
"""

from __future__ import annotations

import json
import resource
import signal
import sys
import traceback
from collections.abc import Generator, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

#: Exit statuses that mean the run happened and its reports are the answer.
#: `NO_TESTS_COLLECTED` belongs here rather than with the failures: an empty
#: module is something the *walk* steps over, and a selection that holds no
#: tests is reported by `mutate._run` from ``ran`` being zero, which is where
#: that judgement has always lived.
ANSWERED = frozenset(
    {
        int(pytest.ExitCode.OK),
        int(pytest.ExitCode.TESTS_FAILED),
        int(pytest.ExitCode.NO_TESTS_COLLECTED),
    }
)


def cap(limit: int) -> None:
    """Bound this process's address space, so a runaway mutant dies alone.

    A mutation does not have to loop forever to hold a lane -- it can loop
    forever *while appending*, and then the timeout is the wrong instrument
    because the machine is gone before it fires. Measured, on the mutant that
    prompted this: `line_starts` reduced to `at -= ...` never advances (a
    negative index wraps in Python rather than raising, so the `while` condition
    stays true), and three lanes reached 15.5 GB, 7.6 GB and 6.4 GB of resident
    memory 73 seconds in. The host OOM-killed the session twice before the 300 s
    timeout had any chance to speak.

    `RLIMIT_AS` rather than a cgroup, and the reasons are not interchangeable:

    - it is set *here*, in the child, so it needs no `preexec_fn`. `run` drives
      its lanes from a `ThreadPoolExecutor`, and `preexec_fn` is documented as
      unsafe in the presence of threads -- the one mechanism that must not
      itself be a source of rare, undebuggable failures;
    - `systemd-run --scope -p MemoryMax=` is Linux-only, and CI runs a macOS
      job. A guard that silently does not exist on one platform is worse than
      the guard being visible everywhere;
    - no root, no delegated cgroup, nothing to configure before the tool works.

    Two honest limits. These bound *address space* and the data segment, not
    resident memory, so the number is necessarily looser than the RSS it
    protects; and a platform may enforce neither, in which case this silently
    does nothing. That is not hypothetical -- macOS ignores `RLIMIT_AS`, and CI
    is what said so. `tests.test_mutate` therefore asks whether the limit is
    enforced *here* by trying it, rather than deciding from `sys.platform`, so
    the guarantee is tested wherever it actually holds and skipped where it does
    not. Neither weakens the Linux case, which is where the crash happened.

    `resource` is imported unguarded: it is Unix-only, and so is everything
    else here -- the product is a shell hook for bash and zsh, and CI runs
    Linux and macOS. A `try: import` around it only bought an unreachable
    branch that `mypy` correctly refused.

    The limit is inherited by the `git`, `age` and `bash` the suite really
    forks. That is intended -- they are equally capable of running away -- and
    it is why `mutate.MEMORY`, which supplies `limit`, is a measured margin over
    an observed whole-suite peak rather than a round number.

    **It bounds one process, and inheritance is not a total.** Each of those
    children gets its own allowance of this size rather than a share of one, so
    a mutation that spawns processes is unbounded here however small `limit` is:
    the crash that prompted `mutate._Lanes` was 4,340 of them holding 26 GB
    between them, none within two orders of magnitude of this ceiling. Nothing
    in this function can see that, which is why the guard against it is a
    sampler one level up rather than another rlimit here.
    """
    # survivor: off-by-one -- `0` becomes `1` -- cosmetic, same as the `-1` mutation on this line.
    if limit <= 0:
        # 0 is "no cap", as `--memory 0` promises and as `--limit 0` beside it
        # already means. Guarded here as well as at the CLI because `cap` is
        # reachable from a spec file, and `setrlimit(..., 0)` would make the
        # sandbox fail every row for a reason no output would explain.
        return
    # Both, because neither is enforced everywhere and they cost the same to
    # ask for. Linux honours `RLIMIT_AS`; macOS largely does not, and CI proved
    # it rather than the docs -- the first version of this passed on Linux and
    # failed the macOS job with the runaway allocation simply succeeding.
    # `RLIMIT_DATA` covers anonymous `mmap` on Linux since 4.7 and is the one
    # macOS is likelier to apply, so asking for both is how this stays a guard
    # on more than one platform without branching on a platform name.
    for which in (resource.RLIMIT_AS, resource.RLIMIT_DATA):
        try:
            soft, hard = resource.getrlimit(which)
        except (OSError, ValueError):  # pragma: no cover - platform without it
            continue
        # Never raise an existing ceiling: a caller who already sandboxed us
        # meant it.
        # survivor: negate -- **caught outside the harness and equivalent inside it, because of this
        #   branch's own fix.** Run under a plain pytest, three tests in
        #   `TestTheMemoryCapsArithmetic` fail -- measured. Run inside a sweep they cannot: since
        #   `cap` began lowering the *hard* half, a probe's children inherit a finite ceiling, so
        #   `hard == RLIM_INFINITY` is false either way and the two spellings agree. Closing the
        #   escape hatch that let a descendant run unbounded cost this row its observability under
        #   the tool, which is a trade worth naming rather than a gap.
        ceiling = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
        # survivor: boundary -- equivalent: `<=` against `<` differs only when `soft` is exactly
        #   `ceiling`, and setting a limit to the value it already holds is a no-op in either
        #   reading.
        if soft != resource.RLIM_INFINITY and soft <= ceiling:
            continue
        try:
            # **The hard limit comes down too, and that is the guard rather than
            # tidiness.** A soft limit alone can be raised back to `hard` by any
            # descendant, and something in this tree really did: a whole-tree
            # sweep OOM-killed the host with a single process at 51.5 GiB
            # resident and 61 GiB of address space -- impossible under the 4 GiB
            # ceiling every probe is given, and therefore proof that some child
            # was running with no `RLIMIT_AS` at all. Lowering `hard` makes the
            # cap survive every `fork` and `exec` below this point, because
            # raising a hard limit needs a privilege none of this has.
            #
            # It costs the ability to *undo* the cap further down, which one
            # test wanted (`TestTheMemoryCapsArithmetic` spawned children that
            # raised soft back to hard to reach a known state). That is now
            # bought with a bounded number instead of an unbounded one, which it
            # should have been: "clear the inherited cap" and "have no cap" are
            # different asks, and only the second can take the machine with it.
            resource.setrlimit(which, (ceiling, ceiling))
        except (OSError, ValueError):  # pragma: no cover - refused
            continue


class Hung(BaseException):
    """A test that ran past its share of the clock.

    `BaseException`, not `Exception`, so that a test doing `except Exception` --
    which several here do, to assert on a message -- cannot swallow the alarm and
    hang anyway. pytest reports whatever a test raises, `BaseException` included,
    so it is still classified rather than escaping the run.
    """


def _ring(signum: int, frame: object) -> None:
    """Raise, rather than set a flag.

    The whole mechanism turns on this. PEP 475 makes Python *retry* a syscall
    interrupted by a signal, so a handler that recorded the alarm and returned
    would be swallowed by exactly the blocking `read()` a hung test sits in.
    Raising propagates instead of resuming. Measured against a fifo read and a
    `subprocess.run`, both interrupted at 0.5s.
    """
    raise Hung("this test ran past the per-test limit")


def each_test(seconds: float) -> float:
    """Arm the per-test alarm, and say what was actually armed.

    0 where `SIGALRM` does not exist -- Windows, and any non-main thread. Plan §2
    puts Windows out of scope for v1 and `collect` runs in the main thread, so
    this is a guard rather than a supported path. The returned value is what the
    `Watcher` is given, so a run with no alarm armed reports every test at `0s`
    in its `broke` messages rather than quoting a bound that was never in force.
    """
    if not seconds or not hasattr(signal, "SIGALRM"):
        return 0.0
    try:
        signal.signal(signal.SIGALRM, _ring)
    except ValueError:
        return 0.0
    return seconds


def _carrier(nodeid: str, excinfo: pytest.ExceptionInfo[BaseException] | None, each: float) -> str:
    """Why this failure is not an answer, or "" when it is one.

    Both limits in one place, because both are the same mistake waiting to
    happen: they raise *inside* a real test, at the ``call`` phase, so by phase
    alone they are indistinguishable from that test noticing the mutation, and
    filed as answers they credit a test that asserted nothing.

    The *class* is read rather than the instance, and `issubclass` rather than
    `is`, for the reason the cap makes plain: building an exception instance is
    itself an allocation that may not have succeeded, and either limit can
    surface as a subclass raised by an extension module.

    **Not flattened across exception groups, and that was checked rather than
    assumed.** pytest does report a group -- a dead `tearDownClass` and a dead
    `tearDownModule` arrive as one `ExceptionGroup` -- but only at the
    ``teardown`` phase, which is `broke` whatever is inside it. No group has
    been observed at ``call``, and a guard for one could not be driven by any
    fixture, which is the shape CLAUDE.md §2 is about.
    """
    if excinfo is None:
        return ""
    if issubclass(excinfo.type, Hung):
        return f"{nodeid} did not finish within {each:g}s"
    return f"{nodeid} ran out of memory" if issubclass(excinfo.type, MemoryError) else ""


def _stated(longrepr: object) -> str:
    """The one line of a rendered failure worth putting in a summary table.

    A collection failure renders as a small traceback whose last line is the
    exception, prefixed ``E   `` the way it is printed. The *first* line is the
    file and line number, which is what a reader would reach for and is the less
    useful half -- "test_x.py:1: in <module>" says nothing about what went wrong.

    Only collection failures come through here. A failure with a `CallInfo`
    behind it is described by `_said` instead, which asks the exception rather
    than the rendering: at the ``teardown`` phase pytest's rendering is a
    forty-line `ExceptionGroup` traceback whose last line is ``+-------``.
    """
    spoken = [line for line in str(longrepr).splitlines() if line.strip()]
    if not spoken:
        return "collection failed and said nothing"
    # The marker is stripped only when it really is one -- `removeprefix("E")`
    # turns a line beginning "Errno" into one beginning "rrno", quietly, in the
    # one message a reader has to act on.
    head, _, rest = spoken[-1].partition(" ")
    return (rest if head == "E" else spoken[-1]).strip()


def _said(call: pytest.CallInfo[None]) -> str:
    """What a phase failure was, in one line.

    `exconly` is the exception as Python spells it -- ``RuntimeError: setUpClass
    blew up`` -- rather than the traceback that led to it, and it is the first
    line of that in case the message is a paragraph.

    A failed report with no exception recorded says so rather than falling back
    to `_stated`, which is documented as taking a *collection* rendering and
    would be handed a forty-line `ExceptionGroup` here. No fixture produces this
    and something still has to be said.
    """
    if call.excinfo is None:  # pragma: no cover - a failure with nothing raised
        return "the phase failed with nothing raised"
    return call.excinfo.exconly().splitlines()[0].strip()


def as_path(name: str) -> str:
    """A dotted selection name as the node pytest addresses it by.

    `mutants.targets_for` names modules the way `unittest` loads them --
    ``tests.test_sync`` -- and this is the one place that translation happens.
    A name that is already a path or already a nodeid is handed back untouched,
    so a caller may pass either.

    The longest existing prefix wins, so ``tests.test_sync.TestX.test_y``
    becomes ``tests/test_sync.py::TestX::test_y`` and not a directory called
    ``TestX``. When nothing matches, the plain module reading is handed back and
    pytest refuses it -- which is reported as `broke`, exactly as an unloadable
    module always was, rather than quietly selecting nothing.
    """
    if name.endswith(".py") or "::" in name:
        return name
    parts = name.split(".")
    for cut in range(len(parts), 0, -1):
        found = Path(*parts[:cut]).with_suffix(".py")
        if found.is_file():
            return "::".join([found.as_posix(), *parts[cut:]])
    return Path(*parts).with_suffix(".py").as_posix()


class Watcher:
    """One process's worth of pytest runs, and what they concluded.

    The same instance is handed to every `pytest.main` call a walk makes, so
    everything it holds accumulates across groups the way one `unittest` result
    object used to.
    """

    def __init__(self, each: float) -> None:
        #: Seconds one test may take. 0 disables the alarm, which is what a
        #: platform without `SIGALRM` gets.
        self.each = each
        #: Tests that failed at the ``call`` phase, having asserted something.
        #: This is what `caught` means. Under pytest a nodeid is both the
        #: display form and the id a later run feeds back, so `mutate`'s two
        #: keys -- `noticed` and `killers` -- are filled from this one list;
        #: under `unittest` they genuinely differed and had to be kept apart.
        self.noticed: list[str] = []
        #: Fixtures and imports that died before or after any assertion ran, and
        #: the two limits below. Not answers.
        self.broke: list[str] = []
        #: The rendered failure of the first test that noticed.
        #:
        #: A mutation run does not want this -- `caught` is the whole answer and
        #: 200 tracebacks is noise. The *baseline* does: a red baseline voids
        #: every verdict above it, and until this was recorded the only thing
        #: said about one was the failing test's name. See `mutate.run`'s
        #: baseline branch, which is the one reader.
        self.reasons: list[str] = []
        #: What each test cost, by nodeid, summed over its three phases.
        #: `mutate.Killers` accumulates these so it can order the cheap
        #: high-yield tests first.
        self.times: dict[str, float] = {}
        #: How many tests started, counting a test named in `first` again when
        #: the selection reaches it -- so this is starts rather than distinct
        #: tests, which is what `unittest`'s `testsRun` counted and what
        #: `tests.test_mutate` reads to tell "the prefix *and* everything" from
        #: "the prefix instead of everything". `mutate._run` reads only whether
        #: it is zero.
        self.ran = 0
        #: Whether the run fell over rather than reporting: a module that would
        #: not import, or a selection pytest refused. No later group can change
        #: that answer, so the walk ends here.
        self.stopped = False
        #: `python_files`, out of the host project's own configuration rather
        #: than assumed. Empty until the first `pytest.main` call configures.
        self.patterns: list[str] = []
        #: Where that configuration says the tree starts.
        self.root = Path()

    # -- what pytest tells us -------------------------------------------------

    def pytest_configure(self, config: pytest.Config) -> None:
        """Take the walk's vocabulary from the host project, not from here.

        A tool that globbed ``test_*.py`` would be right about tupferl and wrong
        about a project that spells its tests `*_test.py` or keeps them
        somewhere else -- and wrong *quietly*, because a module the walk never
        reaches turns a caught row into a reported survivor.
        """
        self.root = Path(str(config.rootpath))
        self.patterns = [str(pattern) for pattern in config.getini("python_files")]

    def pytest_runtest_logstart(self, nodeid: str, location: object) -> None:
        """Counted here rather than from the reports, because this fires for a
        test whose *setup* dies too -- which `unittest` also counted as a test
        that started, and which is a `broke` that must not read as "nothing
        ran"."""
        self.ran += 1

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(
        self, item: pytest.Item, nextitem: pytest.Item | None
    ) -> Generator[None, object, object]:
        """Arm the alarm around one whole test, and always cancel it.

        Cancelled in a `finally` rather than only on the next arm, or a fast
        test would be charged for the timer its predecessor set and the alarm
        would land in whatever ran next -- a misattributed `broke` rather than a
        wrong verdict, but one nothing could diagnose.
        """
        if self.each:
            signal.setitimer(signal.ITIMER_REAL, self.each)
        try:
            return (yield)
        finally:
            if self.each:
                signal.setitimer(signal.ITIMER_REAL, 0)

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo[None]
    ) -> Generator[None, pytest.TestReport, pytest.TestReport]:
        """Classify here, where the exception object still exists.

        See the module docstring for why this and not `pytest_runtest_logreport`:
        by the time a report is logged, a failed `subTest` has become a separate
        object and the owning test's own report reads ``passed``.
        """
        report = yield
        if report.outcome == "failed":
            self.answered(item.nodeid, call, report)
        return report

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Charge each test for its three phases, and nothing twice.

        A subtest's report is skipped rather than added, and it is recognised
        by carrying a ``context`` -- what pytest hangs the subcase's parameters
        on -- rather than by its class name.

        **Measured on pytest 9.1.1, and it is weaker than it reads: a
        `SubtestReport`'s ``duration`` is 0.** Three subcases sleeping 0.067 s
        each report 0, 0, 0 against the owner's ``call`` report of 0.2017 -- the
        machinery does not time subcases -- so adding them would change this
        number by exactly nothing, and no fixture can make it change. The filter
        stays as a guard against a pytest that starts timing them, which is a
        future that has not arrived; that also means nothing can test it, and
        `tests/test_verdict.py` says so where the test that pretended to used
        to be. Deciding whether it earns its line is Phase C's, with the rest of
        the documentation settling.

        ``setup`` starts the sum again rather than adding to it, so a test named
        in `first` and then reached again by the selection is charged for one
        run instead of two. That is what the layer before this did, where one
        assignment per test meant the last run won -- and it matters because
        `Killers.prefix` divides rows-caught by cost, so a remembered killer
        that looked twice as dear as it is would drop out of the cheap prefix.
        """
        if getattr(report, "context", None) is not None:
            return
        if report.when == "setup":
            self.times[report.nodeid] = report.duration
        else:
            self.times[report.nodeid] = self.times.get(report.nodeid, 0.0) + report.duration

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """A module that will not import, reported before anything of it runs.

        The parameter must be named ``report``: pluggy validates hookimpl
        argument *names* against the hook specification and raises
        `PluginValidationError` at registration for anything else.
        """
        if report.outcome == "failed":
            self.stopped = True
            self.broke.append(f"{report.nodeid or 'collection'}: {_stated(report.longrepr)}")

    # -- what we make of it ---------------------------------------------------

    def answered(self, nodeid: str, call: pytest.CallInfo[None], report: pytest.TestReport) -> None:
        """Record one failure, in whichever of the two buckets it belongs to."""
        # The two limits first, and by exception type rather than by phase,
        # because by phase they are a test noticing. See `_carrier`.
        if reason := _carrier(nodeid, call.excinfo, self.each):
            self.broke.append(reason)
            return
        if call.when != "call":
            # `setUpClass`, `setUpModule` and their teardowns. Nothing in them
            # evaluated an assertion, so crediting the mutation to them would
            # be crediting a test that never ran.
            self.broke.append(f"{nodeid}: {call.when} failed -- {_said(call)}")
            return
        self.noticed.append(nodeid)
        if not self.reasons:
            # The first only. A `failfast` run stops here anyway, and a baseline
            # -- which never uses `failfast` -- is diagnosed from the first
            # failure just as well as from forty.
            self.reasons.append(str(report.longrepr))

    def beside(self, names: Sequence[str]) -> list[str]:
        """Every test file next to the selection's own, and not in it.

        Globbed from the sandbox rather than handed in, because the caller's row
        names the *selection* and this is precisely the part it does not name.

        The directories come from the selection rather than being spelled
        ``tests`` here. Two reasons, and the second is why it is not a nicety:
        the suite this tool runs lives wherever the caller's modules do, and a
        constant would make the walk unreachable from `tests/test_verdict.py`,
        whose sandboxes are flat throwaway modules.

        **Not recursive, and so `norecursedirs` is deliberately not consulted.**
        It looks beside the selection and no deeper, which is what the
        `unittest` backend did and what the acceptance sweep compares against.
        A recursive enumeration would need that setting to earn its keep: a
        naive `rglob` of the default patterns over this very tree finds 71
        files, 38 of them inside `.venv`, i.e. it would walk pytest's own test
        suite. Anyone widening this owes that filter in the same change.
        """
        chosen = {as_path(name) for name in names}
        found: set[str] = set()
        for folder in sorted({str(Path(path).parent) for path in chosen}):
            for pattern in self.patterns:
                found |= {
                    hit.relative_to(self.root).as_posix()
                    for hit in (self.root / folder).glob(pattern)
                }
        # `sorted` rather than any set order: the walk order decides which test
        # a row records as its killer, and a killer that moves between runs of
        # the same sweep is a cache that never warms.
        return sorted(found - chosen)

    def over(self, group: Sequence[str], failfast: bool) -> None:
        """One `pytest.main`, plus whatever its exit status says that no hook did.

        The flags are the ones Phase 0 measured. ``-p no:cacheprovider`` is what
        keeps a `.pytest_cache` out of the sandbox; ``-x`` is `failfast`,
        spelled with pytest's own flag rather than by setting
        `session.shouldstop`, and it stops on a `broke` as well as on a
        `noticed` exactly as `unittest`'s did. Assertion rewriting is left on:
        it costs below measurement here and Phase B's pytest-native `assert`
        statements need it.
        """
        said = len(self.broke)
        argv = ["-q", "-p", "no:cacheprovider", *(["-x"] if failfast else []), *group]
        code = int(pytest.main(argv, plugins=[self]))
        if code in ANSWERED:
            return
        # Said, not inferred. A usage error -- a nodeid in `first` that no
        # longer exists, a module the selection names and the tree does not --
        # is announced on pytest's own stdout and through no hook at all, and
        # `mutate` sends that stream to `DEVNULL`. Without this the report would
        # say "nothing ran" and the row would be filed as holding no tests.
        self.stopped = True
        if len(self.broke) == said:
            over = " ".join(group) or "everything"
            self.broke.append(f"pytest exited {_named(code)} over {over}")


def _named(code: int) -> str:
    """An exit status as pytest spells it, or as a number when it is not one."""
    try:
        return pytest.ExitCode(code).name
    except ValueError:  # pragma: no cover - pytest exiting outside its own enum
        return str(code)


def _groups(
    names: Sequence[str], first: Sequence[str], walk: bool, watcher: Watcher
) -> Iterator[list[str]]:
    """The `pytest.main` calls to make, in order, one at a time.

    A generator rather than a list, and that is load-bearing twice over. The
    walk's members are only knowable *after* the first call has configured --
    `beside` reads the host project's `python_files` out of it -- and each group
    beyond the selection is a module whose import nobody has paid for yet:
    importing all 33 of this tree's test modules costs 250ms against 116ms for
    one, so building the whole list up front would charge every caught row for
    modules the walk never reaches.

    `first` gets its own group rather than being pushed onto `names`, and that
    is not tidiness. An empty `names` *means* the whole suite; prepending to the
    list makes it non-empty, so "run everything" quietly becomes "run only
    these three".
    """
    if first:
        yield [as_path(name) for name in first]
    if names:
        yield from ([as_path(name)] for name in names)
    else:
        # No paths at all: pytest collects from `testpaths` or its rootdir,
        # which is the sandbox. This is `mutate.WHOLE_SUITE`.
        yield []
    if walk and names:
        yield from ([path] for path in watcher.beside(names))


def collect(
    names: Sequence[str],
    failfast: bool,
    each: float = 0.0,
    first: Sequence[str] = (),
    walk: bool = False,
) -> dict[str, Any]:
    """What the suite said, walking outward until something notices.

    The order is `first`, then `names`, then every other test module. A mutation
    its selection catches therefore costs exactly what the selection costs -- the
    head of the walk *is* the selection, measured at 100% of 1,516 caught rows
    across five sweeps -- and one nothing catches has, by the time it is called a
    survivor, already run the whole suite once.

    That is what removes the second pass. Selection stops being a gate and
    becomes an ordering, so a miss is slow rather than wrong, and there is
    nothing left to confirm afterwards.

    **The caller must have baselined the whole suite, not just the selection.**
    A row that walks outward can be caught by a module its selection never named,
    and on a tree that is already red that claim is free -- `failfast` stops at
    the first red test whatever it was about. That is why `mutate.run` baselines
    `WHOLE_SUITE` for a walking table.
    """
    watcher = Watcher(each_test(each))
    for group in _groups(names, first, walk, watcher):
        watcher.over(group, failfast)
        # Three ways to stop, and they are not the same question. `stopped` is
        # "no later group can change this answer". `failed` under `failfast` is
        # the caller's request. `noticed` under `walk` is the walk's own
        # question -- "has anything noticed yet" -- which is answered the moment
        # one test has, and which a hand table must not be subjected to: there
        # `failfast` being off is the request, and stopping at the first red
        # module would report one shard of a broken tree as the whole story.
        answered = watcher.noticed or watcher.broke
        if watcher.stopped or (failfast and answered) or (walk and watcher.noticed):
            break
    return {
        "loaded": True,
        "ran": watcher.ran,
        "noticed": watcher.noticed,
        "killers": watcher.noticed,
        "reasons": watcher.reasons,
        "times": watcher.times,
        "broke": watcher.broke,
    }


def main(argv: list[str]) -> None:
    report, failfast = argv[0], argv[1] == "1"
    # `first` is JSON rather than a space-joined slot: a pytest nodeid can
    # contain spaces once anything is parametrized, and splitting on them would
    # shred one name into several that select nothing.
    #
    # `walk` in its own slot rather than inferred from the selection: a baseline
    # and a mutation can carry the *same* selection and must be run differently
    # -- the baseline asks whether that selection is green, the mutation asks
    # what in the whole suite notices. Inferring it from `names` cannot tell them
    # apart, and getting it wrong turns every baseline into a whole-suite run.
    first, walk, names = list(json.loads(argv[4])), argv[5] == "1", argv[6:]
    # Before the suite loads, not after: collection imports every test module,
    # and a mutation to something imported at module scope can run away there.
    cap(int(argv[2]))
    try:
        written = collect(names, failfast, float(argv[3]), first, walk)
    except BaseException:
        # Said, not inferred. The caller used to conclude "the suite could not be
        # loaded" from an absent file, which is also what a typo in this file
        # produces -- two very different problems with byte-identical output, in
        # a tool whose whole thesis is that those must be told apart.
        # `limit=4` keeps the tail readable; `format_exc` is itself an
        # allocation, so a `MemoryError` here may leave nothing at all -- which
        # the caller already reads as `broke` from the absent report.
        written = {"loaded": False, "why": traceback.format_exc(limit=4)}
    with open(report, "w", encoding="utf-8") as out:
        json.dump(written, out)


if __name__ == "__main__":
    main(sys.argv[1:])
