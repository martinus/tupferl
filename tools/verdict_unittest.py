"""Which kind of failure a `unittest` suite produced -- the retired backend.

**`tools/verdict.py` is the one a sweep uses.** It asks the same question of
pytest reports; this file is reachable only with
``TUPFERL_MUTATE_VERDICT=unittest``, which exists so that a row the two backends
disagree about can be re-run against the classifier that was here before, rather
than argued about. Both are deleted together at the end of the conversion
(`docs/pytest-plan.md`, Phase C). Everything below describes this file and is
unchanged; where the pytest layer had to decide something differently, it says so
at the decision rather than here.

Ported from `martinus/woswoar` (Apache-2.0), where the evidence quoted below
was collected; `woswoar#123` numbers index that repository's issues.

`tools/mutate.py` needs to know whether a *test method noticed* the mutation, or
whether the run merely fell over. Both exit non-zero, and both leave a plausible
``Ran N`` behind, so the distinction has to be drawn where the result objects
still exist rather than reconstructed from what `unittest` printed. That is what
this file is.

**It is read, not imported.** `mutate` reads this source out of its *own* tree
and hands it to ``python -c`` in the sandbox. Two properties fall out, and both
are the point:

- the sandbox's copy of ``tools/`` is never consulted, so a mutation to this file
  cannot decide its own verdict -- which matters as soon as `tools/**.py` is
  itself something a generated table mutates;
- being a real module rather than a string constant, `ruff` and `mypy` see it.
  The previous shape was a 35-line string literal that no checker could reach,
  holding the one piece of genuinely new logic in the change.

``-c`` also puts the working directory on ``sys.path`` where a script path would
not, which is how the sandbox's own test modules are importable at all.

**Classification is by protocol, never by class name.** The obvious spelling --
"is this class defined under ``unittest.``?" -- was written first and was wrong
in the direction that matters. `unittest.case._SubTest` is a `TestCase` whose
module is ``unittest.case``, so an assertion failing inside ``with
self.subTest(...)`` was filed as "the suite broke" rather than "a test noticed",
and with a strict table that aborts the run. This repository uses `subTest` in
more than twenty places. So instead:

- ``addSubTest`` is handed the **owning** test, not the `_SubTest` carrier, so
  overriding it records the right name;
- a `setUpClass` or `setUpModule` failure arrives through ``addError`` as
  `unittest.suite._ErrorHolder`, which is deliberately *not* a `TestCase` -- so
  `isinstance` alone separates "a fixture died" from "a test failed", with no
  private name involved;
- a module that will not import is reported by `TestLoader.errors`, a public
  attribute, and the suite is then not run at all. Running it would surface the
  synthetic `unittest.loader._FailedTest` through ``addError`` -- and that one
  *is* a `TestCase`, so it would read as a test noticing.

Nothing here names a private symbol to make a decision. If a future `unittest`
moves `_FailedTest` or `_ErrorHolder`, `loader.errors` and the `isinstance` still
answer, and the failure mode is a name printed oddly rather than `broke`
silently becoming `caught`.
"""

from __future__ import annotations

import io
import json
import resource
import signal
import sys
import time
import traceback
import unittest
from pathlib import Path
from types import TracebackType
from typing import Any

#: What `unittest` hands a result method for a failure, spelled exactly as
#: typeshed spells it -- `mypy --strict` checks these overrides for Liskov and a
#: near-miss here is an error, not a warning.
ExcInfo = tuple[type[BaseException], BaseException, TracebackType] | tuple[None, None, None]


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
        #   branch's own fix.** Run under `python -m unittest`, three tests in
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
            # test wanted (`TestTheMemoryCapsArithmetic` spawned children that raised
            # soft back to hard to reach a known state). That is now bought with
            # a bounded number instead of an unbounded one, which it should have
            # been: "clear the inherited cap" and "have no cap" are different
            # asks, and only the second can take the machine with it.
            resource.setrlimit(which, (ceiling, ceiling))
        except (OSError, ValueError):  # pragma: no cover - refused
            continue


def _exhausted(err: ExcInfo | None) -> bool:
    """Whether this traceback is the memory cap firing rather than an assertion.

    `issubclass` rather than `is`, because the cap can also surface as a
    subclass raised by an extension module; `err[0]` rather than the instance,
    because building the instance is itself an allocation that may not have
    succeeded.
    """
    # survivor: off-by-one -- `err` is `sys.exc_info()`, whose first element is the class. Index 1
    #   is the instance and -1 the traceback, and `issubclass` refuses both with `TypeError` -- so
    #   the mutant raises inside the result object rather than answering, which the docstring above
    #   already argues is why the *class* is what this reads: building the instance is itself an
    #   allocation that may not have succeeded.
    return err is not None and err[0] is not None and issubclass(err[0], MemoryError)


def _carrier(test: object, err: ExcInfo | None, each: float) -> str:
    """Why this error is not an answer, or "" when it is one.

    Both limits in one place, because both are the same mistake waiting to
    happen: they raise *inside* a real `TestCase`, so they arrive at `addError`
    indistinguishable by protocol from that test noticing the mutation, and
    filed as answers they credit a test that asserted nothing. Written out
    twice, a refinement to one copy leaves the other crediting -- and the
    `addSubTest` copy had no test at all.
    """
    # survivor: off-by-one -- same tuple, same argument as `_exhausted`: index 0 is the exception
    #   class, and a `None` there is how `sys.exc_info()` says there is nothing to report.
    if err is None or err[0] is None:
        return ""
    if issubclass(err[0], Hung):
        return f"{test} did not finish within {each:g}s"
    return f"{test} ran out of memory" if _exhausted(err) else ""


class Hung(BaseException):
    """A test that ran past its share of the clock.

    `BaseException`, not `Exception`, so that a test doing `except Exception` --
    which several here do, to assert on a message -- cannot swallow the alarm and
    hang anyway. `unittest` catches `BaseException` around a test, so it is still
    reported rather than escaping the runner.
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


class Verdicts(unittest.TextTestResult):
    """Keeps the tests that asserted apart from the carriers that did not."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: Real test methods that failed. This is what `caught` means.
        self.noticed: list[str] = []
        #: The same tests, as `python -m unittest` takes them back --
        #: `module.Class.method`, where `noticed` holds `method (module.Class.
        #: method)` for a human. Recorded rather than parsed back out of the
        #: display string, because `mutate` feeds these straight to a loader and
        #: a display format is not an API.
        self.killers: list[str] = []
        #: Fixtures that died before any assertion ran. Not an answer.
        self.broke: list[str] = []
        #: The formatted traceback of the first test that noticed.
        #:
        #: A mutation run does not want this -- `caught` is the whole answer and
        #: 200 tracebacks is noise. The *baseline* does: a red baseline voids
        #: every verdict above it, and until this was recorded the only thing
        #: said about one was the failing test's name. Diagnosing it meant
        #: reproducing the shard by hand, and five reproductions of a red
        #: baseline all came back green because the sixth thing that differed
        #: was never guessed. See `mutate.run`'s baseline branch, which is the
        #: one reader.
        self.reasons: list[str] = []
        #: Seconds one test may take. 0 disables the alarm, which is what a
        #: platform without `SIGALRM` gets.
        self.each: float = 0.0
        #: What each test that ran cost, by id. `mutate.Killers` accumulates
        #: these so it can order the cheap high-yield tests first; the *baseline*
        #: runs are where they mostly come from, since those alone run a whole
        #: selection to the end without `failfast` cutting it short.
        self.times: dict[str, float] = {}
        # survivor: drop-assign -- equivalent: `startTest` assigns it before any test body runs, and
        #   `stopTest` is the only reader. The line declares the attribute so the class is complete
        #   without a test having started, which is what a `Verdicts` handed to `_unloadable` is.
        self._began = 0.0

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self._began = time.perf_counter()
        # survivor: branch -- `each` is 0 exactly where `SIGALRM` does not exist -- Windows, and any
        #   non-main thread -- and this suite runs on neither. Taking the branch there would raise
        #   inside `startTest`; skipping it where the alarm exists loses the per-test bound, which
        #   is `verdict.each_test`'s whole job and is asserted by
        #   `TestAHungTestIsBoundedAndNotCredited` through the alarm rather than through this line.
        if self.each:
            signal.setitimer(signal.ITIMER_REAL, self.each)

    def stopTest(self, test: unittest.TestCase) -> None:
        self.times[test.id()] = time.perf_counter() - self._began
        # Cancelled here rather than only on the next `startTest`, or a fast test
        # would be charged for the timer its predecessor set and the alarm would
        # land in whatever ran next.
        # survivor: branch -- `each` is 0 exactly where `SIGALRM` does not exist -- Windows, and any
        #   non-main thread -- and this suite runs on neither. Taking the branch there would raise
        #   inside `startTest`; skipping it where the alarm exists loses the per-test bound, which
        #   is `verdict.each_test`'s whole job and is asserted by
        #   `TestAHungTestIsBoundedAndNotCredited` through the alarm rather than through this line.
        if self.each:
            # survivor: drop-call, off-by-one -- cancels the alarm the previous test armed. Dropping
            #   it charges a fast test for its predecessor's timer, so the bound fires in whatever
            #   ran next -- which is a misattributed `BROKE` rather than a wrong verdict, and the
            #   comment above says so. Asserting it means watching *which* test a timeout is blamed
            #   on, and that is a race by construction.
            signal.setitimer(signal.ITIMER_REAL, 0)
        # survivor: drop-call -- `TestResult.stopTest` clears the per-test bookkeeping the base
        #   class keeps. Nothing this module reads comes from it, so the mutant answers identically
        #   -- and a fixture for it would be asserting on `unittest`'s internals rather than on any
        #   claim of ours.
        super().stopTest(test)

    def _answered(self, test: unittest.TestCase, err: ExcInfo | None = None) -> None:
        """Record one test that noticed, every way round."""
        self.noticed.append(str(test))
        self.killers.append(test.id())
        if err is not None and not self.reasons:
            # The first only. A `failfast` run stops here anyway, and a baseline
            # -- which never uses `failfast` -- is diagnosed from the first
            # failure just as well as from forty.
            # `traceback.format_exception`, not `TestResult._exc_info_to_string`:
            # the latter is private, untyped in typeshed, and the version that
            # differs between releases -- three reasons the one lint that only
            # fails in CI would have found here.
            self.reasons.append("".join(traceback.format_exception(*err)))

    def addFailure(self, test: unittest.TestCase, err: ExcInfo) -> None:
        super().addFailure(test, err)
        self._answered(test, err)

    def addError(self, test: unittest.TestCase, err: ExcInfo) -> None:
        # survivor: drop-call -- the base class's own error list, which nothing here reads:
        #   `noticed`, `broke` and `killers` are this module's answer and are appended above.
        #   Delegating keeps `wasSuccessful()` honest for a caller that asks it, and
        #   `verdict.collect` does not.
        super().addError(test, err)
        # The two limits, classified by type rather than by protocol, because by
        # protocol they are a test noticing. See `_carrier`.
        if reason := _carrier(test, err, self.each):
            self.broke.append(reason)
            return
        # `_ErrorHolder` -- a dead `setUpClass`, `setUpModule` or `tearDown` --
        # is not a `TestCase`, and that is the whole check. It carries a
        # traceback for something that happened *around* the tests, so no
        # assertion in it was ever evaluated.
        # Kept as a ternary over the *list*, rather than an `if`/`else` around
        # two statements. `test` is annotated `TestCase` because that is what
        # typeshed says `addError` takes, so mypy proves an `else:` branch
        # unreachable and `warn_unreachable` rejects it -- while at runtime the
        # branch is exactly the case this check exists for. `target is` is a
        # fact about a list mypy cannot argue with.
        target = self.noticed if isinstance(test, unittest.TestCase) else self.broke
        target.append(str(test))
        if target is self.noticed:
            self.killers.append(test.id())
            # survivor: branch -- the first traceback is the one kept, and the comment above says
            #   why: a run without `failfast` is diagnosed from the first failure as well as from
            #   forty. Keeping them all is a longer report, not a different verdict.
            if not self.reasons:
                self.reasons.append("".join(traceback.format_exception(*err)))

    def addSubTest(
        self, test: unittest.TestCase, subtest: unittest.TestCase, err: ExcInfo | None
    ) -> None:
        # survivor: drop-call -- same as `addError`'s: the base class keeps its own list and this
        #   module keeps the one it reports from.
        super().addSubTest(test, subtest, err)
        if err is not None:
            # `test` is the owning case; `subtest` is the `_SubTest` carrier that
            # the base class files into `failures`. Recording the owner is what
            # keeps a `subTest` assertion a real answer.
            if reason := _carrier(test, err, self.each):
                self.broke.append(reason)
            else:
                # The *owner*, not the `_SubTest` carrier: its `id()` carries the
                # parameters in brackets and `unittest` cannot load that back.
                self._answered(test)


def each_test(seconds: float) -> float:
    """Arm the per-test alarm, and say what was actually armed.

    0 where `SIGALRM` does not exist -- Windows, and any non-main thread. Plan §2
    puts Windows out of scope for v1 and `collect` runs in the main thread, so
    this is a guard rather than a supported path. The returned value is what the
    `Verdicts` instances are given, so a run with no alarm armed reports every
    test at `0s` in its `broke` messages rather than quoting a bound that was
    never in force.
    """
    if not seconds or not hasattr(signal, "SIGALRM"):
        return 0.0
    try:
        signal.signal(signal.SIGALRM, _ring)
    except ValueError:
        return 0.0
    return seconds


def every_module(names: list[str]) -> list[str]:
    """Every test module beside ``names``, found where ``names`` themselves live.

    Globbed from the sandbox rather than handed in, because the caller's row
    names the *selection* and this is precisely the part it does not name.

    The directory comes from the selection rather than being spelled `tests`
    here. Two reasons, and the second is why it is not a nicety: the suite this
    tool runs lives wherever the caller's modules do, and a constant would make
    the walk unreachable from `tests/test_verdict_unittest.py`, whose sandboxes are flat
    throwaway modules. A guard nothing can drive is the shape CLAUDE.md §2 is
    about.
    """
    found: list[str] = []
    # survivor: order -- the ordering is reversed -- equivalent: this sorts the *packages* the walk
    #   visits, and the function ends `return sorted(set(found))` -- so the order the names are
    #   appended in is not observable. The sort on line 346 *is* guarded, by
    #   `test_what_it_returns_is_sorted`.
    for package in sorted({name.rpartition(".")[0] for name in names}):
        root = Path(package.replace(".", "/")) if package else Path()
        prefix = f"{package}." if package else ""
        found += [f"{prefix}{beside.stem}" for beside in root.glob("test_*.py")]
    # survivor: order -- `sorted` over a *set*, which CLAUDE.md records as only probabilistically
    #   guarded: a set iterates in hash order, randomised per run, so the mutant agrees with the
    #   original whenever that order happens to match. Sizing a fixture for the odds would be
    #   pinning the hash seed rather than the sort.
    return sorted(set(found))


def _reached(names: list[str], walk: bool) -> list[list[str]]:
    """The groups to run, in order, one load at a time.

    Each entry is loaded only when the walk reaches it. That laziness is the
    whole of the design: importing all 29 modules costs 621ms against 0-1ms for
    one, so loading the ordered list up front would hand back two minutes over a
    194-row sweep -- more than the second pass this replaced ever cost.
    """
    # No `if not names` guard, though an empty selection must reach `discover`
    # below and not a named load -- the two classify a module that will not
    # import differently, everything into `TestLoader.errors` where a named load
    # wraps only what derives from `Exception` (`TestABrokenModuleTakesTwoDiffer
    # entPaths` holds the measured table). It gets there anyway: `every_module`
    # asks the selection which package to look in, and an empty selection names
    # none, so both arms below return `[]` and the caller falls through. A guard
    # was written here first and no fixture could tell it from its absence.
    # survivor: branch, drop-not -- unanswered rather than equivalent, and this is the honest state.
    #   The walk is reached only by tests that drive a *nested* `mutate.run`, so a broken walk makes
    #   that inner run hang or exit rather than fail -- and the harness files either as `BROKE`,
    #   which is never `caught`. Two attempts are recorded: #73 gave the tests a deadline so a hang
    #   fails, and #74 passed `strict=False` so an inapplicable row comes back in the report instead
    #   of raising `SystemExit` out of the test. Both were verified against a chosen selection and
    #   both still come back unanswered under the *generated* one, which is the lesson CLAUDE.md
    #   states twice: the killer a sweep names is one route to a line, not all of them. Re-open with
    #   the generated selection in hand, not a chosen one.
    if not walk:
        # A baseline. It asks whether *this selection* is green, and widening it
        # would make every baseline a whole-suite run.
        # survivor: return-value -- unanswered rather than equivalent, and this is the honest state.
        #   The walk is reached only by tests that drive a *nested* `mutate.run`, so a broken walk
        #   makes that inner run hang or exit rather than fail -- and the harness files either as
        #   `BROKE`, which is never `caught`. Two attempts are recorded: #73 gave the tests a
        #   deadline so a hang fails, and #74 passed `strict=False` so an inapplicable row comes
        #   back in the report instead of raising `SystemExit` out of the test. Both were verified
        #   against a chosen selection and both still come back unanswered under the *generated*
        #   one, which is the lesson CLAUDE.md states twice: the killer a sweep names is one route
        #   to a line, not all of them. Re-open with the generated selection in hand, not a chosen
        #   one.
        return [[name] for name in names]
    chosen = set(names)
    # survivor: return-value -- unanswered rather than equivalent, and this is the honest state. The
    #   walk is reached only by tests that drive a *nested* `mutate.run`, so a broken walk makes
    #   that inner run hang or exit rather than fail -- and the harness files either as `BROKE`,
    #   which is never `caught`. Two attempts are recorded: #73 gave the tests a deadline so a hang
    #   fails, and #74 passed `strict=False` so an inapplicable row comes back in the report instead
    #   of raising `SystemExit` out of the test. Both were verified against a chosen selection and
    #   both still come back unanswered under the *generated* one, which is the lesson CLAUDE.md
    #   states twice: the killer a sweep names is one route to a line, not all of them. Re-open with
    #   the generated selection in hand, not a chosen one.
    return [[name] for name in names] + [[m] for m in every_module(names) if m not in chosen]


def collect(
    names: list[str],
    failfast: bool,
    each: float = 0.0,
    first: list[str] | None = None,
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
    the first red test whatever it was about. That is woswoar#268, and it is why
    `mutate.run` baselines `WHOLE_SUITE` for a walking table.
    """
    loader = unittest.TestLoader()
    groups = _reached(names, walk)
    # survivor: branch, drop-not -- unanswered rather than equivalent, and this is the honest state.
    #   The walk is reached only by tests that drive a *nested* `mutate.run`, so a broken walk makes
    #   that inner run hang or exit rather than fail -- and the harness files either as `BROKE`,
    #   which is never `caught`. Two attempts are recorded: #73 gave the tests a deadline so a hang
    #   fails, and #74 passed `strict=False` so an inapplicable row comes back in the report instead
    #   of raising `SystemExit` out of the test. Both were verified against a chosen selection and
    #   both still come back unanswered under the *generated* one, which is the lesson CLAUDE.md
    #   states twice: the killer a sweep names is one route to a line, not all of them. Re-open with
    #   the generated selection in hand, not a chosen one.
    if not groups:
        chosen = loader.discover(".", pattern="test_*.py", top_level_dir=".")
        # `first` in its own argument rather than pushed onto `names`, and that is
        # not tidiness. An empty `names` *means* the whole suite and the branch
        # above is how; prepending to the list makes it non-empty, so "run
        # everything" quietly becomes "run only these three".
        groups, prepared = (
            [],
            (unittest.TestSuite([loader.loadTestsFromNames(first), chosen]) if first else chosen),
        )
        if loader.errors:
            return _unloadable(loader)
        ready: list[unittest.TestSuite] = [prepared]
    else:
        ready = []
    armed = each_test(each)

    def build(*args: Any, **kwargs: Any) -> Verdicts:
        made = Verdicts(*args, **kwargs)
        made.each = armed
        return made

    # survivor: off-by-one -- the third argument is `unittest`'s own verbosity, and the stream is a
    #   `StringIO` nobody reads -- `collect` reports through `Verdicts`, never through what the
    #   runner printed.
    result = build(io.StringIO(), False, 0)
    result.failfast = failfast
    broke: list[str] = []
    # survivor: branch -- the `if` is never taken -- the `first` prefix -- the remembered killer
    #   tried ahead of a row. Skipping it costs a longer run and cannot change a verdict: the same
    #   tests run afterwards as part of the selection. That is the whole design of the cache, and
    #   `Killers`/`Learned` have their own tests for what goes into it.
    if first and groups:
        head = unittest.TestLoader()
        # survivor: drop-call -- the call to `ready.append(...)` never happens -- same as
        #   `verdict_unittest.py:425`: the prefix is an ordering optimisation, and dropping it
        #   changes when a killer is found rather than whether.
        ready.append(head.loadTestsFromNames(first))
        # survivor: branch -- the `if` is never taken -- a prefix naming a test that will not
        #   import. Reachable only from a killers cache written by an older tree, which `Killers`
        #   already validates once per run -- so by the time `collect` sees it, it has been checked.
        if head.errors:
            # survivor: return-value -- returns `None` instead of `_unloadable(head)` -- same branch
            #   as `verdict_unittest.py:428`, and the caller reads an absent report as `broke`
            #   either way.
            return _unloadable(head)
    # survivor: drop-call -- the call to `result.startTestRun(...)` never happens --
    #   `TextTestResult.startTestRun` is a hook with no body in the stdlib, and `Verdicts` does not
    #   override it. Nothing in the process has anything to do at that point.
    result.startTestRun()
    try:
        for suite in ready:
            suite(result)
            # survivor: branch -- the `failfast` early exit inside one group. Losing it runs the
            #   remaining tests of a group whose verdict is already decided -- slower, never
            #   different, because `noticed` is already non-empty and that is what the outcome
            #   reads.
            if result.shouldStop:
                break
        else:
            for group in groups:
                # `result.noticed` as well as `shouldStop`, and only when
                # walking. `failfast` is off for a hand-written table -- a red
                # baseline is a thing you want the whole of -- so `shouldStop`
                # alone would let a row that has *already* been caught carry on
                # through the rest of the suite, turning every caught row on
                # that path into a whole-suite run. Walking outward asks "has
                # anything noticed yet", which is answered the moment one test
                # has, whatever the caller wants to see of the rest.
                #
                # Not hoisted into the baseline's arm: there `failfast` being
                # off is the request, and stopping at the first red module would
                # report one shard of a broken tree as the whole story.
                # survivor: branch, connector -- unanswered rather than equivalent, and this is the
                #   honest state. The walk is reached only by tests that drive a *nested*
                #   `mutate.run`, so a broken walk makes that inner run hang or exit rather than
                #   fail -- and the harness files either as `BROKE`, which is never `caught`. Two
                #   attempts are recorded: #73 gave the tests a deadline so a hang fails, and #74
                #   passed `strict=False` so an inapplicable row comes back in the report instead of
                #   raising `SystemExit` out of the test. Both were verified against a chosen
                #   selection and both still come back unanswered under the *generated* one, which
                #   is the lesson CLAUDE.md states twice: the killer a sweep names is one route to a
                #   line, not all of them. Re-open with the generated selection in hand, not a
                #   chosen one.
                if result.shouldStop or (walk and result.noticed):
                    break
                # A fresh loader per group: `TestLoader.errors` accumulates, so a
                # shared one would re-report an earlier group's failure against
                # every later module.
                step = unittest.TestLoader()
                found = step.loadTestsFromNames(group)
                if step.errors:
                    broke = [str(error).splitlines()[0] for error in step.errors]
                    break
                found(result)
    finally:
        # survivor: drop-call -- the call to `result.stopTestRun(...)` never happens -- the matching
        #   hook, equally empty. Both are called because the protocol says to, not because either
        #   does anything here.
        result.stopTestRun()
    return {
        "loaded": True,
        "ran": result.testsRun,
        "noticed": result.noticed,
        "killers": result.killers,
        "reasons": result.reasons,
        "times": result.times,
        "broke": result.broke + broke,
    }


def _unloadable(loader: unittest.TestLoader) -> dict[str, Any]:
    """A module that would not import, reported before anything runs.

    Public API, and checked before running: the suite `loadTestsFromNames`
    returns for an unimportable module holds a synthetic `_FailedTest` which
    *is* a `TestCase`, so running it would report a test noticing.
    """
    return {
        "loaded": True,
        "ran": 0,
        "noticed": [],
        "killers": [],
        "reasons": [],
        "times": {},
        # `str()` because typeshed types `errors` as exception classes while
        # `unittest` actually appends formatted tracebacks; the first line is
        # the "Failed to import test module: x" that says which.
        "broke": [str(error).splitlines()[0] for error in loader.errors],
    }


def main(argv: list[str]) -> None:
    # survivor: off-by-one -- unanswerable: `argv` indices into `_probe`'s own command line,
    #   which `_run` builds three frames away in the same repository -- so the two are always
    #   the same revision and a shifted index is a protocol break rather than an input. It shows
    #   up as every row coming back `BROKE` at once, which no single fixture would diagnose
    #   better than the first sweep does.
    #
    #   `cap(int(argv[2]))` below repeats the operator with a pointer back here, because a tag
    #   guards one statement. Only those two of the four `argv` reads carry an `off-by-one` row
    #   at all -- the others index inside a nested call or a slice -- and a tag on a statement
    #   with no such row excuses nothing, which is what
    #   `test_mutants.TestEveryTagGuardsARowThatExists` refuses.
    report, failfast = argv[0], argv[1] == "1"
    # `walk` in its own slot rather than inferred from the selection: a baseline
    # and a mutation can carry the *same* selection and must be run differently
    # -- the baseline asks whether that selection is green, the mutation asks
    # what in the whole suite notices. Inferring it from `names` cannot tell them
    # apart, and getting it wrong turns every baseline into a whole-suite run.
    # survivor: negate -- `argv[5] == "1"` against `!=` flips whether the walk runs, and every test
    #   that could see it drives a nested harness -- the same family as the walk rows above, and
    #   unanswered for the same reason.
    # JSON, the same slot `tools/verdict.py` reads, because `mutate._run` builds
    # one command line for whichever layer it was told to use. Splitting on
    # spaces here is what this file did when it was the only backend, and it
    # made `TUPFERL_MUTATE_VERDICT=unittest` report every row `broke`: the empty
    # prefix arrives as the two characters `[]`, which is a module name to a
    # loader. Nothing went red, because the tests below drove this file with the
    # argv it used to be given rather than the one it is now given.
    first, walk, names = list(json.loads(argv[4])), argv[5] == "1", argv[6:]
    # Before the suite loads, not after: `discover` imports every test module,
    # and a mutation to something imported at module scope can run away there.
    # survivor: off-by-one -- unanswerable, for the reason on the first `argv` line above.
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
