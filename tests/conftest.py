"""Fixtures more than one test module needs.

**Deferred until a cluster genuinely shared one, on purpose.** B1's plan entry
said to create this file "initially near-empty"; it did not, because after
converting eight modules no *fixture* in them was shared and an empty
`conftest.py` makes a claim -- shared fixtures live here -- that nothing backs.
B3 is the cluster that backs it: five modules want the same throwaway `$HOME`,
and six more do in B4a and B4b (`test_diff`, `test_manage`, `test_status`,
`test_sync`, `test_sync_cli`, `test_sync_commits`).

**A fixture goes here only when a second module wants it.** The alternative is
the shape every large suite ends up regretting, where `conftest.py` is a
grab-bag nobody can delete from because no one call site owns anything in it.
Where a fixture has exactly one user it stays in that module -- `test_paths`'
`only`, `test_config`'s `box` and `test_merge`'s `merged_under` are all still
where B1 left them.

**What a sandbox *is* lives in `tests/support.py`, not here.** `support.sandbox`
is the definition and this file is the pytest adapter over it, holding no setup
of its own. There was a `unittest` adapter beside it -- `support.SandboxCase`,
with `MachineCase` and `TwoMachinesCase` -- from B3 until B4b converted the last
`TestCase` user and deleted all three.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Iterator
from contextlib import ExitStack

import pytest

from tests import support


@pytest.fixture(autouse=True)
def _every_test_puts_the_environment_back() -> Iterator[None]:
    """Fail the test that leaves `os.environ` changed, rather than the next nine.

    **Written because B3 produced exactly this and it was diagnosed backwards.**
    A test that used the sandbox only for its side effect -- the patched
    environment -- named no fixture when it was converted, so it got none and set
    `PATH=/nonexistent` in the real one. What went red was nine *later* tests
    that could not find git, in three other classes.

    This is not a replacement for the sandbox: it catches a **leak**, not a
    read, and a test that quietly *read* the developer's `$HOME` still passes.
    That half stays a convention, enforced by the `usefixtures` marks on every
    class that needs a sandbox.

    Nor is a session-wide replacement of `$HOME` the deeper fix it looks like.
    It would turn a loud failure -- nine tests broken, and the developer's own
    dotfiles at risk -- into a quiet one, where every test shares one `$HOME`
    and passes anyway. That is the green run CLAUDE.md §8 is about.

    One dict copy per test; measured below measurement against a 1828-test run.

    `PYTEST_CURRENT_TEST` is excluded because **pytest writes it itself** -- the
    value carries the phase, so it reads `... (setup)` when this fixture starts
    and `... (teardown)` when it finishes. Found on this guard's first run,
    where it failed 1711 of 1828 tests: a whole-environment comparison cannot
    pass, and a guard that always fires is no guard. It is the only exclusion,
    and it is named rather than pattern-matched so that a second one has to be
    argued for.
    """

    def ours() -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}

    before = ours()
    yield
    assert ours() == before, "this test changed os.environ and did not put it back"


@pytest.fixture(autouse=True)
def _every_test_leaves_the_alarm_no_louder_than_it_found_it() -> Iterator[None]:
    """Fail the test that arms `ITIMER_REAL` and walks away, rather than the
    unrelated one running thirty seconds later.

    **Written because that is exactly what happened (#115).**
    `test_support.TestABoundGivesTheHarnessItsAlarmBack` arms a real 30s timer
    and installs a real handler in each of its four tests, and was missing the
    `_alarm_put_back` mark its sibling class has -- so the module finished with
    the timer reading `(29.988, 0.0)`. Thirty seconds later `SIGALRM` fired into
    whatever was running, which was a test in another module with no idea an
    alarm existed. It took a `getitimer` after `pytest.main` to see it at all:
    from the failure it read as shared state with no owner, because *which* test
    died depended only on what was executing at T+30s. `tests/test_mutate.py`
    alone was green, `tests/test_support.py` alone was green, and the pair was
    red at a different test each run.

    This is the alarm's version of `_every_test_puts_the_environment_back`
    above, and it is the same argument: the failure lands on the test that
    caused it instead of on the nine that follow.

    **It asserts the timer never gets *louder*, not that it is unchanged**, and
    the asymmetry is deliberate. An alarm that legitimately *fires* during a
    test leaves the timer at zero, and a guard demanding equality would then add
    a second, spurious failure to a test that has already reported the real one.
    Every direction that matters is still covered: arming where nothing was
    armed, and arming longer over shorter, both raise the number.

    The handler is compared by identity, which is the half that caught the
    lambda `test_the_previous_handler_is_back_before_the_alarm_can_fire` left
    installed.

    Autouse, so it is set up before the `_alarm_put_back` a class may request
    and therefore torn down after it -- the restoring fixture's work is what
    this reads, not the state it was hiding.
    """
    handler = signal.getsignal(signal.SIGALRM)
    armed, _ = signal.getitimer(signal.ITIMER_REAL)
    yield
    left, _ = signal.getitimer(signal.ITIMER_REAL)
    assert left <= armed, (
        f"this test left ITIMER_REAL at {left:.3f}s where it found {armed:.3f}s; "
        "arm it through support.deadline, or ask for the _alarm_put_back fixture"
    )
    assert signal.getsignal(signal.SIGALRM) is handler, (
        "this test left a SIGALRM handler installed; the next test to run under "
        "an alarm would report the timeout against whatever this one installed"
    )


@pytest.fixture
def sandbox() -> Iterator[support.Sandbox]:
    """A throwaway `$HOME` with `os.environ` pointed inside it, for one test.

    Function-scoped, and that is not the default being accepted quietly: the
    fixture *patches `os.environ`*, so a module- or session-scoped one would
    leak one test's `HOME` into the next and make an ordering bug look like a
    fixture bug. It costs a `mkdir` and a seeded `~/.gitconfig`.
    """
    with support.sandbox() as made:
        yield made


@pytest.fixture
def machine() -> Iterator[support.Machine]:
    """A sandboxed `$HOME` with a bare remote beside it, and the CLI pointed there.

    Function-scoped for `sandbox`'s reason, which it inherits by composition,
    plus one of its own: `init` and every command after it write into a real git
    repository, so a shared one would carry a test's commits into the next.
    """
    with support.machine() as made:
        yield made


@pytest.fixture
def two_machines() -> Iterator[support.TwoMachines]:
    """Two `$HOME`s and the bare remote they share, `.bashrc` already synced.

    A `copytree` of a template built once per process -- 4.3 ms against the
    120.4 ms a real `init`/`add`/`sync` costs (#19), which is what makes this
    affordable per test for the 190 tests that take it.

    It does **not** patch `os.environ`: each machine carries its own environment
    and applies it per command, which is how two hostnames coexist in one
    process. So a test that takes this and then reads `os.environ` is reading the
    developer's, and wants `sandbox` as well or instead.
    """
    with support.two_machines() as made:
        yield made


@pytest.fixture
def boxes() -> Iterator[support.Boxes]:
    """Throwaway directories that live until the end of the test.

    Here rather than in one module because a second wants it: `test_mutate`
    hands them to fifteen tree-building helpers, and `test_verdict`'s `Probe`
    needs a *second* box while the first is still there. What one **is** is
    `support.Boxes`, per this file's rule -- this is the adapter.
    """
    with ExitStack() as stack:
        yield support.Boxes(stack)


@pytest.fixture
def _alarm_put_back() -> Iterator[None]:
    """Whatever `ITIMER_REAL` and `SIGALRM` were, back afterwards.

    Two modules arm a real timer and install a real handler --
    `test_support`'s `TestABoundGivesTheHarnessItsAlarmBack` and
    `TestABoundBuiltForAClassIsReallyArmed`, and `test_verdict`'s
    `TestWhenTheAlarmIsArmedAtAll` -- and this process is also the one
    `tools/mutate.py` arms its per-test alarm in.

    **The timer as well as the handler, and that is the half a second copy
    got wrong.** B6 wrote a handler-only version in `test_verdict` before this
    was shared. Restoring the handler and leaving a timer armed hands the next
    test an alarm nobody asked for, into whichever handler is then installed.

    **It restores the timer rather than zeroing it, which is the other half.**
    Clearing was the first spelling, and its own docstring conceded that
    "leaving the timer *cleared* is the fault `support.deadline` has its own
    four tests about, one level down" -- committing that fault here instead. It
    matters under a probe: `tools/mutate.py` arms `ITIMER_REAL` around every
    test, so a fixture that zeroed it left the *rest* of that test unwatched by
    the harness, and a hang afterwards costs `TIMEOUT`'s 300s rather than
    `EACH_TEST`'s 30 -- filed under the one outcome that names no test. Same
    rule as `deadline`: whatever was left, less the time spent in here, and
    never zero, because `setitimer(0)` means *disarm*.
    """
    handler = signal.getsignal(signal.SIGALRM)
    outer, _ = signal.getitimer(signal.ITIMER_REAL)
    began = time.monotonic()
    try:
        yield
    finally:
        # Off and the handler back *before* re-arming, or the restored alarm
        # could fire into whatever this test installed -- `deadline`'s ordering,
        # and for its reason.
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, handler)
        if outer:
            signal.setitimer(signal.ITIMER_REAL, max(outer - (time.monotonic() - began), 1e-6))
