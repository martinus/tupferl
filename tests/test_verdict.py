"""`tools/verdict.py`: which kind of failure a suite produced.

**Why it is the first thing tested.** `mutate` reports `caught` when a test
method noticed and `broke` when the run merely fell over, and both exit non-zero
leaving a plausible count of tests behind. Every other number the harness
produces is downstream of that line being drawn correctly, and drawn *wrongly*
it errs towards `caught` -- flattering the tests, which CLAUDE.md §8 names as
the direction every bug in this class has taken.

**Driven the way `mutate` drives it**: the module's source handed to
``python -c`` with a sandbox as the working directory, throwaway test modules
inside it, and the report written outside. Not by importing `verdict` and
calling `collect` in this process -- `cap` sets an address-space rlimit and the
alarm installs a `SIGALRM` handler, so an in-process test would be configuring
the suite that is running it. The four classes at the end that *do* import it
touch neither, and say so.

This file states the same claims `tests/test_verdict_unittest.py` states about
the backend that came before, against pytest. Three of them change, and the
changes are the interesting part:

- a dead fixture is `broke` because of the *phase* pytest reports it in, not
  because of an `isinstance` against a private `unittest` class. That mapping is
  measured rather than documented, so `TestWhatThisAssumesOfPytest` asserts it;
- a broken module is classified identically whether it was named or discovered.
  Under `unittest` those were two different code paths that classified it
  differently, and a fixture written for one proved nothing about the other;
- a failed `subTest` reaches the classifier *before* pytest splits it out, so
  the owner is what gets recorded with no unwrapping. The trap it replaces is
  worse than the one it removes, and has its own test.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

from tests import support

#: The repository root, so a child can import `tools` without a chdir.
ROOT = Path(__file__).resolve().parent.parent

#: The tool's own source, read the way `mutate._probe` reads it -- from this
#: tree, never from the sandbox. A copy under test could otherwise decide its
#: own verdict, which is the property `verdict.py`'s docstring opens with.
SOURCE = (ROOT / "tools" / "verdict.py").read_text(encoding="utf-8")

#: What a probe's environment is, copied from `mutate._run` rather than left to
#: chance. Autoload off is what the probe really runs with, and a plugin that
#: loaded here and not there would make every one of these tests evidence about
#: a configuration no sweep uses.
PROBE_ENV = {"PYTHONDONTWRITEBYTECODE": "1", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}

#: How long a sandbox test sleeps when it is standing in for one that hangs.
#:
#: 8 is comfortably longer than the 0.5s alarm these tests arm and comfortably
#: shorter than `BOUND`, which is itself well under the harness's own alarm. It
#: was 30 once, which reproduced here the exact defect CLAUDE.md records for
#: `tests/test_watch.py`: the harness's 30s alarm fired first, the rows came
#: back `BROKE`, and `BROKE` is never `caught`.
FOREVER = 8

#: What the one timed test sleeps for, so its duration is an interval rather
#: than "not negative". Long enough to clear the clock's noise, short enough
#: that it is not felt.
SLEPT = 0.2

#: Seconds one ``python -c <verdict source>`` run may take before the test calls
#: it hung.
#:
#: Through `support.bounded`, which is the rule CLAUDE.md states: a fixture's own
#: timeout must beat the alarm this run *actually armed*, not the constant the
#: default happens to be. 20 is the number when nothing is armed -- far above the
#: longest honest wait here (a pytest probe over two throwaway modules is under a
#: second; Phase 0 measured pytest's fixed per-probe overhead at 113.6 ms against
#: unittest's 42.5 ms) and two thirds of the 30s default.
BOUND = support.bounded(20.0)


def address_space_caps() -> bool:
    """Whether `RLIMIT_AS` can be set here *and* is applied. Asked by trying.

    Three ways it can be unusable, and this run must tell them apart from a
    working one rather than from each other:

    - `setrlimit` is refused outright (macOS refuses `RLIMIT_AS`);
    - it is accepted and not reflected by `getrlimit`;
    - it is accepted, reflected, and simply not enforced when memory is asked
      for -- which `tools/verdict.py`'s own docstring records CI discovering
      rather than the documentation.

    Asking only "did the probe exit non-zero" is true of a refused `setrlimit`
    as well as of a refused *allocation*, so on macOS it answers "enforced" and
    lets the gated tests through to fail. A probe that cannot tell its own
    failure from the failure it is probing for is the §8 shape in miniature, so
    this one prints a marker and the caller looks for exactly that.

    Asked by trying rather than by reading `sys.platform`: the guarantee is then
    tested wherever it really holds and skipped where it does not.
    """
    probe = (
        "import resource\n"
        "want = 64 << 20\n"
        "hard = resource.getrlimit(resource.RLIMIT_AS)[1]\n"
        "resource.setrlimit(resource.RLIMIT_AS, (want, hard))\n"
        "assert resource.getrlimit(resource.RLIMIT_AS)[0] == want\n"
        "try:\n"
        "    bytearray(256 << 20)\n"
        "except MemoryError:\n"
        "    print('applied')\n"
    )
    try:
        done = subprocess.run(
            [sys.executable, "-B", "-c", probe],
            capture_output=True,
            text=True,
            timeout=support.bounded(30.0),
        )
    except subprocess.SubprocessError:  # pragma: no cover - a machine in trouble
        return False
    return done.stdout.strip() == "applied"


#: Computed once. The probe forks, and gating two classes on it would otherwise
#: pay for that at import *and* at every decorated method.
CAPS = address_space_caps()


@functools.cache
def pytest_needs() -> int:
    """The address space one pytest run occupies here, measured by taking one.

    **A measurement, not a constant, because the constants did not travel.**
    512 MiB and 256 MiB were calibrated against this machine's 278 MiB floor and
    turned four CI legs red on the first push: a runner's interpreter and
    site-packages are leaner, so a cap this machine cannot start under is one
    the runner starts fine under, and an allocation this machine refuses is one
    the runner grants. That is `mutate._FLOOR`'s recorded mistake -- a figure
    measured elsewhere arriving "with the word *here* attached" -- in a new
    place.

    `VmPeak` rather than a binary search for the smallest cap that works: one
    child instead of five, and it is the same quantity `RLIMIT_AS` bounds.
    Measured here at 267 MiB against the search's 278, so it reads slightly
    *under* the true floor -- the safe direction, since the caller adds headroom.

    `/proc` is Linux-only and so is an enforced `RLIMIT_AS`. The one class that
    reads this is gated on `CAPS` and named in the macOS job's `--exclude`, so
    the `0` below is unreachable from any test.

    **Cached and called from the one test that reads it, not bound at import.**
    Measured at 109 ms -- it starts a real pytest -- and it was a module-level
    constant, so every import paid it: `tests/test_mutate.py` alone drove it
    fifteen times through nested `mutate.run` calls, 1.65 s. `CAPS` above has to
    stay at module scope because `skipUnless` reads it when the class is
    defined; this has no such excuse.
    """
    child = (
        "import pytest\n"
        'pytest.main(["-q", "-p", "no:cacheprovider", "test_trivial.py"])\n'
        'print(next(int(line.split()[1]) * 1024 for line in open("/proc/self/status")'
        ' if line.startswith("VmPeak:")))\n'
    )
    body = "import unittest\nclass T(unittest.TestCase):\n    def test_it(self):\n        pass\n"
    with tempfile.TemporaryDirectory(prefix="tupferl-floor-") as box:
        Path(box, "test_trivial.py").write_text(body, encoding="utf-8")
        try:
            done = subprocess.run(
                [sys.executable, "-B", "-c", child],
                cwd=box,
                capture_output=True,
                text=True,
                env={**os.environ, **PROBE_ENV},
                timeout=support.bounded(60.0),
            )
            return int(done.stdout.strip().splitlines()[-1])
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return 0  # pragma: no cover - a platform without /proc


class Probe(unittest.TestCase):
    """A sandbox of throwaway test modules, and one run of the tool over them."""

    def setUp(self) -> None:
        self.fresh()

    def fresh(self) -> None:
        """A new sandbox and a new report path.

        Separate from `setUp` because a test below runs the same broken module
        twice, once named and once discovered, and the second needs a sandbox
        the first has not written to. Calling `setUp` again would work and would
        read as a mistake; this says what it is doing. Each call registers its
        own cleanups, which run at the end of the test as usual.
        """
        box = tempfile.TemporaryDirectory(prefix="tupferl-verdict-test-")
        self.addCleanup(box.cleanup)
        self.sandbox = Path(box.name)
        # Outside the sandbox, for the reason `mutate._run` gives: a report
        # written inside is one `open()` away from being the suite's to write.
        out = tempfile.TemporaryDirectory(prefix="tupferl-verdict-out-")
        self.addCleanup(out.cleanup)
        self.report = Path(out.name) / "verdict.json"

    def module(self, name: str, body: str) -> None:
        (self.sandbox / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")

    def passing(self, *names: str) -> None:
        """One trivially green module per name, where only the count matters."""
        for name in names:
            self.module(
                name,
                """
                import unittest
                class T(unittest.TestCase):
                    def test_it(self):
                        pass
                """,
            )

    def verdict(
        self,
        *names: str,
        failfast: bool = False,
        memory: int = 0,
        each: float = 0.0,
        first: tuple[str, ...] = (),
        walk: bool = False,
    ) -> dict[str, Any]:
        """Run the tool and return the report it wrote.

        The argv is positional and spelled out here rather than built from a
        helper shared with `mutate`, because the point of this file is to hold
        the *other* end of a protocol: when `first` gained its own slot, an
        earlier version of this helper let the selection slide into it and a
        module ran twice with nothing failing. A shared builder cannot notice
        that.

        ``walk`` defaults off, which is a *baseline*'s shape. Most tests here are
        about what one named selection reports, and a walk would run every other
        module in the sandbox inside each of them.
        """
        done = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                SOURCE,
                str(self.report),
                "1" if failfast else "0",
                str(memory),
                str(each),
                json.dumps(list(first)),
                "1" if walk else "0",
                *names,
            ],
            cwd=self.sandbox,
            env={**os.environ, **PROBE_ENV},
            capture_output=True,
            text=True,
            timeout=BOUND,
        )
        assert self.report.is_file(), (
            f"no report was written.\nstdout: {done.stdout}\nstderr: {done.stderr}"
        )
        return dict(json.loads(self.report.read_text(encoding="utf-8")))


class TestATestThatNoticed(Probe):
    """The `caught` half, which everything else is defined against."""

    def test_a_failing_assertion_is_an_answer(self) -> None:
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    self.assertEqual(1, 2)
            """,
        )
        found = self.verdict("test_a")
        assert found["broke"] == []
        assert len(found["noticed"]) == 1
        assert found["ran"] == 1

    def test_the_killer_is_recorded_as_pytest_takes_it_back(self) -> None:
        """The id `mutate` writes into its cache is fed straight back to pytest
        as a selection on a later run, so "it looks like a nodeid" is not the
        claim -- "pytest selects exactly this test with it" is. The round trip
        is the proof, and a format that merely looks right is what it guards
        against.
        """
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    raise AssertionError("no")
            """,
        )
        found = self.verdict("test_a")
        assert found["killers"] == ["test_a.py::T::test_it"]

        again = self.verdict(*found["killers"])
        assert again["ran"] == 1, "the recorded id did not select back"

    def test_an_unexpected_exception_is_also_an_answer(self) -> None:
        """A mutation that makes the code raise is caught, not broken: the test
        body is where it happened, and the body is the test."""
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    raise ValueError("the mutation did this")
            """,
        )
        found = self.verdict("test_a")
        assert found["broke"] == []
        assert len(found["noticed"]) == 1

    def test_a_dead_teardown_belongs_to_the_test_it_ran_after(self) -> None:
        """The half of the phase mapping that must *not* read as `broke`.

        An instance's own `tearDown` is part of that one test, and `unittest`
        reported its failure against a real `TestCase` -- so the backend before
        this one credited it, and equivalence demands this one does too. pytest
        agrees by putting it in the ``call`` phase, which is measured in
        `TestWhatThisAssumesOfPytest` rather than assumed here. Read the other
        way it would be a `broke`, and a mutation only a `tearDown` can see
        would be reported as surviving.
        """
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    pass
                def tearDown(self):
                    raise RuntimeError("the mutation broke the cleanup")
            """,
        )
        found = self.verdict("test_a")
        assert found["broke"] == [], "a test's own tearDown was filed as a broken run"
        assert found["killers"] == ["test_a.py::T::test_it"]


class TestAFixtureThatDied(Probe):
    """`BROKE` is never `caught` -- the single most load-bearing rule here.

    A `setUpClass` or `setUpModule` failure happens *around* the tests rather
    than inside one, so no assertion in it was ever evaluated and crediting it
    would report that the tests noticed a mutation they never reached. pytest
    says which by the phase: ``setup`` and ``teardown`` are the fixture's,
    ``call`` is the test's.

    **Phase is not the whole of it, and the last class below is the correction.**
    A function-scoped fixture also reports in ``setup``, and it is where a
    converted `setUp` goes -- so the same assertion means "a test noticed" on one
    side of a conversion and "the run fell over" on the other, which is not a
    difference anything in the code under test can produce.
    """

    def test_a_dead_setupclass_is_not_an_answer(self) -> None:
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise RuntimeError("the mutation broke the import-time work")
                def test_it(self):
                    pass
            """,
        )
        found = self.verdict("test_a")
        assert found["noticed"] == [], "a dead fixture was credited as a test"
        assert found["killers"] == []
        assert len(found["broke"]) == 1
        assert "setup failed" in found["broke"][0]

    def test_a_dead_setupmodule_is_not_an_answer(self) -> None:
        self.module(
            "test_a",
            """
            import unittest
            def setUpModule():
                raise RuntimeError("died before any test")
            class T(unittest.TestCase):
                def test_it(self):
                    pass
            """,
        )
        found = self.verdict("test_a")
        assert found["noticed"] == []
        assert len(found["broke"]) == 1

    def test_a_dead_teardownclass_is_not_an_answer_either(self) -> None:
        """The far side of the same line, and the one that has no counterpart in
        the backend before this: `unittest` reported it through the same
        `_ErrorHolder` as `setUpClass`, while pytest reports it in a phase of
        its own. A class-scoped teardown ran after every assertion in the class
        had already passed, so it cannot be one of them noticing.
        """
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                @classmethod
                def tearDownClass(cls):
                    raise RuntimeError("died after every test")
                def test_it(self):
                    pass
            """,
        )
        found = self.verdict("test_a")
        assert found["noticed"] == [], "a class-scoped teardown was credited as a test"
        assert len(found["broke"]) == 1
        assert "teardown failed" in found["broke"][0]

    def test_a_module_that_will_not_import_is_not_an_answer(self) -> None:
        """And the suite must not be *run* at all.

        pytest reports it through `pytest_collectreport` before any test starts,
        which is why `ran` is 0. It is the one bucket where the message has to be
        built out of a rendered traceback rather than out of an exception, so the
        content is asserted as well as the count.
        """
        self.module("test_a", "import a_module_that_does_not_exist_xyz\n")
        found = self.verdict("test_a")
        assert found["loaded"]
        assert found["ran"] == 0, "the suite ran despite a collection error"
        assert found["noticed"] == []
        assert len(found["broke"]) == 1
        assert "test_a" in found["broke"][0]
        assert "ModuleNotFoundError" in found["broke"][0]


class TestAPerTestFixtureIsTheTestsOwnSetup(Probe):
    """The correction to the class above, measured rather than reasoned into.

    Cluster B4a converted four modules whose fixtures assert their own
    preconditions -- "the template's `init` failed" and so on. Those assertions
    had been in `unittest`'s instance `setUp`, which pytest runs inside
    ``call``; as fixtures they report in ``setup``. **104 rows of a 1309-row
    sweep moved from `caught` to `BROKE`**, with no test weakened and the
    survivor set identical. A `BROKE` row is never `caught`, so 104 lines of
    `tupferl/` read as guarded by nothing.

    So the line is drawn by *scope*, which `pytest_fixture_setup` carries: one
    test's own setup is that test noticing, and anything wider is not.
    """

    def test_a_function_scoped_fixture_that_raises_is_a_test_noticing(self) -> None:
        self.module(
            "test_a",
            """
            import pytest
            @pytest.fixture
            def box():
                raise AssertionError("the mutation broke the fixture's own check")
            def test_it(box):
                pass
            """,
        )
        found = self.verdict("test_a")
        assert found["broke"] == [], found["broke"]
        assert len(found["noticed"]) == 1, found["noticed"]
        # And the nodeid is one a later run can select, which is what makes it
        # usable as a remembered killer rather than merely a name in a report.
        assert "test_a.py::test_it" in found["killers"][0]

    def test_the_same_assertion_written_as_setup_answers_the_same_way(self) -> None:
        """The pair, and the reason this class exists rather than a comment.

        One assertion, two spellings of where it lives, and the conversion is
        supposed to be behaviour-neutral. Asserted side by side so that a
        classifier which drifts on one of them fails here rather than in a sweep
        six modules later.
        """
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def setUp(self):
                    raise AssertionError("the mutation broke the fixture's own check")
                def test_it(self):
                    pass
            """,
        )
        found = self.verdict("test_a")
        assert found["broke"] == [], found["broke"]
        assert len(found["noticed"]) == 1

    def test_a_later_phase_is_not_covered_by_the_setup_that_was_credited(self) -> None:
        """The scope is a fact about one *phase*, and it must not latch.

        Found by review, measured before the fix: keyed by nodeid alone and read
        at every phase, one test's own fixture failing in ``setup`` credited
        *any* later failure of that test as well -- here a class-scoped fixture
        blowing up after ``yield``, reported as the same nodeid noticing twice.

        The pair in one run is what makes this checkable: the same test has to
        appear in both buckets, once for each phase, which no single rule that
        looks at the nodeid alone can produce.
        """
        self.module(
            "test_a",
            """
            import pytest
            @pytest.fixture
            def narrow():
                raise AssertionError("mine alone")
            @pytest.fixture(scope="class")
            def wide():
                yield
                raise RuntimeError("class teardown fell over")
            class TestX:
                def test_it(self, narrow, wide):
                    pass
            """,
        )
        found = self.verdict("test_a")
        assert found["noticed"] == ["test_a.py::TestX::test_it"]
        assert len(found["broke"]) == 1, found["broke"]
        assert "teardown failed" in found["broke"][0]

    def test_a_module_scoped_fixture_that_raises_is_still_not_an_answer(self) -> None:
        """The other side, and without it the rule above is just "credit every
        setup failure" -- which would credit one broken session fixture as a kill
        for every test in the run.

        Two tests, so the count is what tells the two rules apart: crediting a
        wider fixture would report *both* of them as having noticed something
        neither of them reached.
        """
        self.module(
            "test_a",
            """
            import pytest
            @pytest.fixture(scope="module")
            def shared():
                raise RuntimeError("died before any test")
            def test_one(shared):
                pass
            def test_two(shared):
                pass
            """,
        )
        found = self.verdict("test_a")
        assert found["noticed"] == [], "a module-scoped fixture was credited as a test"
        assert len(found["broke"]) == 2, found["broke"]
        assert "setup failed" in found["broke"][0]


class TestABrokenModuleIsClassifiedTheSameWayTwice(Probe):
    """Named and discovered are one path now, and that is worth a test.

    Under `unittest` they were two: `discover` wrapped everything into
    `TestLoader.errors` while `loadTestsFromNames` wrapped only what derived
    from `Exception`, so a syntax error escaped one and not the other and came
    back `loaded: False` instead of `broke`. Both refused to credit a test,
    which was the only thing that mattered -- but two tests in the old file were
    written with the fixtures exactly backwards and failed, and CLAUDE.md still
    carries the measured table.

    pytest collects the same way whichever it was given, so the two agree. This
    asserts that they do, on the fixture that used to separate them: a
    difference reappearing would be a difference in what a *walk* concludes,
    where every group past the first is discovered rather than named.
    """

    BROKEN = "this is not python at all !!!\n"

    def test_a_syntax_error_is_reported_the_same_way_named_or_found(self) -> None:
        self.module("test_a", self.BROKEN)
        named = self.verdict("test_a")
        self.fresh()
        self.module("test_a", self.BROKEN)
        discovered = self.verdict()
        assert discovered["broke"] == named["broke"]
        assert named["loaded"] and discovered["loaded"]
        assert "SyntaxError" in named["broke"][0]

    def test_neither_route_credits_a_test_with_noticing(self) -> None:
        """The claim that survives whatever pytest does with the two routes, and
        the only one that would corrupt a sweep if it stopped holding."""
        self.module("test_a", self.BROKEN)
        for found in (self.verdict("test_a"), self.verdict()):
            assert found["noticed"] == []
            assert found["killers"] == []
            assert found["ran"] == 0


class TestASubTestIsARealAnswer(Probe):
    """A `subTest` assertion is a test noticing, and pytest hides that twice.

    First by the count: a failing `subTest` produces an extra report object, so
    a run of 1505 tests emits 1940 reports. Second, and much worse, by the
    outcome: the owning test's *own* report reads ``passed``. This project uses
    `subTest` in 77 tests, so a classifier that read finished reports would
    report a large fraction of its real catches as survivors.
    """

    def test_a_failing_subtest_is_caught(self) -> None:
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    for n in (1, 2):
                        with self.subTest(n=n):
                            self.assertEqual(1, n)
            """,
        )
        found = self.verdict("test_a")
        assert found["broke"] == [], "a subTest assertion was filed as a broken run"
        assert len(found["noticed"]) == 1

    def test_the_owner_is_recorded_and_not_the_carrier(self) -> None:
        """The parameters must not reach the id: pytest hangs them on the
        report's ``context``, and a `first` slot carrying them would select
        nothing. The round trip is the proof."""
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    with self.subTest(n=1):
                        self.fail("no")
            """,
        )
        found = self.verdict("test_a")
        assert found["killers"] == ["test_a.py::T::test_it"]
        assert "[" not in found["killers"][0]
        assert self.verdict(*found["killers"])["ran"] == 1

    def test_a_subtest_failure_is_the_only_thing_that_says_so(self) -> None:
        """The trap, stated as a fixture rather than as a warning.

        The owner passes and the whole *module* is otherwise green, so nothing
        but the subtest's own failure distinguishes this run from a clean one.
        A classifier reading the owner's report -- which is the obvious port of
        `addSubTest` -- would call this "nothing noticed" and report the
        mutation SURVIVED. That is the flattering direction, and it is why the
        classification happens at `pytest_runtest_makereport`, before pytest
        splits the failure out.
        """
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    with self.subTest(n=1):
                        self.fail("only the subtest failed")
                def test_other(self):
                    pass
            """,
        )
        found = self.verdict("test_a")
        assert found["ran"] == 2
        assert found["killers"] == ["test_a.py::T::test_it"]
        assert "only the subtest failed" in found["reasons"][0]


class TestACarrierThatDidNotAssert(Probe):
    """A hung test and a test that ran out of memory both raise *inside* a real
    test, at the ``call`` phase, so by phase they are indistinguishable from that
    test noticing. Filed as answers they credit a test that asserted nothing."""

    def test_a_test_that_runs_past_its_share_is_broken_not_caught(self) -> None:
        self.module(
            "test_a",
            f"""
            import time, unittest
            class T(unittest.TestCase):
                def test_it(self):
                    time.sleep({FOREVER})
            """,
        )
        found = self.verdict("test_a", each=0.5)
        assert found["noticed"] == [], "a hung test was credited with an answer"
        assert found["killers"] == []
        assert len(found["broke"]) == 1
        assert "did not finish" in found["broke"][0]

    def test_the_alarm_cannot_be_swallowed_by_a_test_catching_exception(self) -> None:
        """`Hung` is a `BaseException` for this reason: several tests in this
        project wrap a call in `except Exception` to assert on its message, and
        one of those hanging would swallow the alarm and hang anyway."""
        self.module(
            "test_a",
            f"""
            import time, unittest
            class T(unittest.TestCase):
                def test_it(self):
                    try:
                        time.sleep({FOREVER})
                    except Exception:
                        pass
            """,
        )
        found = self.verdict("test_a", each=0.5)
        assert found["noticed"] == []
        assert len(found["broke"]) == 1
        assert "did not finish" in found["broke"][0]

    def test_a_hung_subtest_is_also_broken_not_caught(self) -> None:
        """`with self.subTest(...)` catches `BaseException`, so the alarm
        arrives as a failed subtest -- which every other test in this file
        treats as a real answer. The carrier check runs before that
        classification for exactly this row."""
        self.module(
            "test_a",
            f"""
            import time, unittest
            class T(unittest.TestCase):
                def test_it(self):
                    with self.subTest(n=1):
                        time.sleep({FOREVER})
            """,
        )
        found = self.verdict("test_a", each=0.5)
        assert found["noticed"] == [], "a hung subTest was credited with an answer"
        assert len(found["broke"]) == 1

    def test_the_alarm_does_not_end_the_run(self) -> None:
        """A hung test costs its own bound and nothing else. If the alarm
        escaped the item, the tests after it would never start and a walk would
        report a survivor because it stopped rather than because nothing
        noticed."""
        self.module(
            "test_a",
            f"""
            import time, unittest
            class T(unittest.TestCase):
                def test_a_hangs(self):
                    time.sleep({FOREVER})
                def test_b_notices(self):
                    self.fail("still reached")
            """,
        )
        found = self.verdict("test_a", each=0.5)
        assert found["ran"] == 2, "the run stopped at the hung test"
        assert found["killers"] == ["test_a.py::T::test_b_notices"]


@unittest.skipUnless(CAPS, "RLIMIT_AS is not usable here")
class TestAnOutOfMemoryTestIsNotAnAnswer(Probe):
    """The `_carrier` arm that needs the cap *enforced* rather than merely set.

    Its own class so that a runner where `RLIMIT_AS` does not work can name it
    to `--exclude` without losing the four tests beside it in
    `TestACarrierThatDidNotAssert`, which need no such thing. `--no-skips` exists
    to catch a suite quietly doing nothing, so a suite that *cannot* run
    somewhere is named in the workflow rather than opting itself out.

    **`main`'s outer `except BaseException` is not tested here, and that is a
    decision rather than an omission.** The only thing that reaches it honestly
    is a cap too small for pytest to reach collection at all -- measured, on
    this machine, between about 190 and 270 MiB -- and that band is a property
    of one interpreter's address space, not of this code. A fixture aimed at it
    passes here and fails on a leaner runner, which is exactly what the first
    version of this class did. The portable half of the same claim is
    `TestWhenTheToolItselfCannotRun.test_a_report_is_always_written`: whatever
    happens, a report exists and says whether it loaded, which is what
    `mutate._run` reads.
    """

    #: Enough for a pytest run to start and not enough for the fixture's 320 MiB,
    #: added to `pytest_needs()` rather than written down. The margin is half the
    #: allocation, so both halves have the same slack: a machine whose floor
    #: the measurement under-reads by less than this still refuses the allocation, and
    #: one it over-reads by less than this still starts.
    HEADROOM = 160 * 1024 * 1024

    def test_a_test_that_exhausts_the_cap_is_broken_not_caught(self) -> None:
        """A `MemoryError` raised inside a test arrives at the ``call`` phase
        looking exactly like an assertion.

        A *bounded* allocation, and the bound is what makes this row catchable
        rather than fatal. `while True` reads better and, with the cap mutated
        away, walks the lane past its whole memory share in about twenty seconds
        -- and a killed session says nothing about any mutation. 40 chunks of
        8 MiB is 320 MiB: it trips a 256 MiB cap after roughly twenty-six of
        them, and when there is no cap it simply ends, leaving `broke` empty and
        this test red. So the mutant that disables `cap` fails here instead of
        taking the run with it.
        """
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    held = []
                    for _ in range(40):
                        held.append(bytearray(8 * 1024 * 1024))
            """,
        )
        found = self.verdict("test_a", memory=pytest_needs() + self.HEADROOM)
        assert found["noticed"] == [], "an out-of-memory test was credited"
        assert len(found["broke"]) == 1
        assert "out of memory" in found["broke"][0]


class TestWhatTheBaselineNeeds(Probe):
    """`reasons` exists for one reader: a red baseline voids every verdict above
    it, and until this was recorded the only thing said about one was the
    failing test's name."""

    def test_the_first_failure_carries_what_it_complained_about(self) -> None:
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    self.assertEqual("expected", "actual")
            """,
        )
        found = self.verdict("test_a")
        assert len(found["reasons"]) == 1
        assert "AssertionError" in found["reasons"][0]
        assert "actual" in found["reasons"][0]

    def test_only_the_first_is_kept(self) -> None:
        """A `failfast` run stops at the first anyway, and a baseline is
        diagnosed from the first failure just as well as from forty."""
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_one(self):
                    self.fail("first")
                def test_two(self):
                    self.fail("second")
            """,
        )
        found = self.verdict("test_a")
        assert len(found["noticed"]) == 2
        assert len(found["reasons"]) == 1
        assert "first" in found["reasons"][0]

    def test_an_error_carries_its_reason_too(self) -> None:
        """The arm that is not an assertion, which renders through a different
        part of pytest and is a second place the recording can fall out of
        step."""
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    raise ValueError("what went wrong")
            """,
        )
        found = self.verdict("test_a")
        assert len(found["reasons"]) == 1
        assert "what went wrong" in found["reasons"][0]

    def test_each_test_is_timed(self) -> None:
        """`mutate.Killers` orders the cheap high-yield tests first from these,
        and the baseline runs are where they mostly come from."""
        self.module(
            "test_a",
            f"""
            import time, unittest
            class T(unittest.TestCase):
                def test_it(self):
                    time.sleep({SLEPT})
            """,
        )
        found = self.verdict("test_a")
        assert list(found["times"]) == ["test_a.py::T::test_it"]
        # An interval, not `>= 0`: a duration is never negative, so that
        # assertion holds against a sum becoming a difference. `Killers` orders
        # the cheap prefix from these numbers, so a wrong one silently
        # mis-orders it.
        assert found["times"]["test_a.py::T::test_it"] > SLEPT / 2
        assert found["times"]["test_a.py::T::test_it"] < SLEPT * 20

    def test_a_subtest_leaves_exactly_one_entry_for_its_owner(self) -> None:
        """**This asserted something it could not see, and CI is what found
        it.** It compared a wall clock against `SLEPT * 2` -- which is precisely
        the value the double-counting it named would produce, so no margin at
        all -- and it turned the macOS leg red the first time three 0.067 s
        sleeps took 0.419 s on a loaded runner. That leg's own wall clock varies
        128 s to 230 s across four consecutive green runs of `main`, so the
        threshold was never going to hold there.

        Worse, it could not have failed for the reason it gave. Measured on
        pytest 9.1.1: a `SubtestReport`'s ``duration`` is **0** -- three subcases
        sleeping 0.067 s each report 0, 0, 0 against the owner's ``call`` report
        of 0.2017 -- so removing `verdict.Watcher`'s ``context`` filter changes
        the number here by nothing at all. `tools/verdict.py` now says the same
        beside the filter, and no test can see that guard removed.

        What is real, and is what this asserts: every subcase reports under its
        *owner's* nodeid, so a test using `subTest` appears in `times` exactly
        once however many cases it runs. `Killers.prefix` divides rows-caught by
        cost, and a second key for the same test would be a row with no cost
        against it. The upper bound is `SLEPT * 20`, the same loose sanity bound
        its sibling above uses, and for the same reason: it catches a number
        that has stopped being a duration, not one that is off by a factor.
        """
        self.module(
            "test_a",
            f"""
            import time, unittest
            class T(unittest.TestCase):
                def test_it(self):
                    for n in range(3):
                        with self.subTest(n=n):
                            time.sleep({SLEPT / 3})
            """,
        )
        found = self.verdict("test_a")
        assert list(found["times"]) == ["test_a.py::T::test_it"]
        assert found["times"]["test_a.py::T::test_it"] > SLEPT / 2
        assert found["times"]["test_a.py::T::test_it"] < SLEPT * 20


class TestWhichTestsGetRun(Probe):
    """`collect`'s selection, where two mistakes each turn "run everything" into
    something much smaller while still reporting a plausible number."""

    def test_no_names_means_the_whole_suite(self) -> None:
        """Handed to pytest as no path arguments at all, so it collects from the
        host project's `testpaths` or its rootdir -- which is the sandbox."""
        self.passing("test_a", "test_b")
        assert self.verdict()["ran"] == 2

    def test_first_does_not_turn_the_whole_suite_into_a_selection(self) -> None:
        """The one that matters. An empty `names` *means* everything; pushing
        `first` onto that list makes it non-empty, so a row that must run the
        whole suite would run the prefix and report it as "everything".
        """
        self.passing("test_a", "test_b", "test_c")
        found = self.verdict(first=("test_a.T.test_it",))
        assert found["ran"] == 4, "the prefix replaced the suite instead of preceding it"

    def test_first_really_runs_before_the_rest(self) -> None:
        """Its whole purpose: a remembered killer is run first so a caught
        mutant is decided in milliseconds. Ordering is observable through
        `failfast`, which stops at the first test that fails."""
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    self.fail("the rest of the suite")
            """,
        )
        self.module(
            "test_b",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    self.fail("the remembered killer")
            """,
        )
        # `test_b`, not `test_a`. The prefix has to name something collection
        # would reach *second*, or running it before and after give the same
        # failfast answer and the ordering is unobservable.
        found = self.verdict(failfast=True, first=("test_b.T.test_it",))
        assert found["ran"] == 1
        assert found["killers"] == ["test_b.py::T::test_it"]

    def test_a_dotted_name_reaches_the_test_pytest_calls_by_another(self) -> None:
        """The translation, driven rather than unit-tested: `mutants.targets_for`
        names modules the way `unittest` loaded them, and pytest addresses files.
        A selection that resolved to nothing would not be an error to pytest --
        it would run zero tests and the row would be filed as holding none.
        """
        self.passing("test_a")
        assert self.verdict("test_a")["ran"] == 1
        assert self.verdict("test_a.T")["ran"] == 1
        assert self.verdict("test_a.T.test_it")["ran"] == 1

    def test_failfast_stops_at_the_first_test_that_noticed(self) -> None:
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_one(self):
                    self.fail("no")
                def test_two(self):
                    self.fail("no")
            """,
        )
        assert self.verdict("test_a", failfast=True)["ran"] == 1
        assert self.verdict("test_a", failfast=False)["ran"] == 2


class TestWhenTheToolItselfCannotRun(Probe):
    """ "Said, not inferred." The caller used to conclude "the suite could not be
    loaded" from an absent report -- which is also what a typo in `verdict.py`
    produces, two very different problems with byte-identical output, in a tool
    whose whole thesis is that those must be told apart."""

    def test_a_module_that_walks_out_at_import_scope_is_reported(self) -> None:
        """`SystemExit` at module scope is not a collection *failure* to pytest
        -- it comes back as an internal error with no report hook fired at all,
        on a stream `mutate` sends to `DEVNULL`. Without the exit-status arm the
        row would read "nothing ran" and be filed as holding no tests, which is
        a different sentence about a tree that is broken.
        """
        self.module("test_a", "raise SystemExit('module scope walked out')\n")
        found = self.verdict("test_a")
        assert found["noticed"] == []
        assert len(found["broke"]) == 1
        assert "INTERNAL_ERROR" in found["broke"][0]

    def test_a_selection_naming_nothing_is_reported_rather_than_run(self) -> None:
        """A stale `first` -- a test that has since been renamed -- makes pytest
        refuse the whole invocation with a usage error and no hook fires. It is
        the shape `mutate._loadable` exists to prevent, and this is what happens
        when one gets past it."""
        self.passing("test_a")
        found = self.verdict("test_a", first=("test_a.py::T::test_gone",))
        assert found["ran"] == 0
        assert len(found["broke"]) == 1
        assert "USAGE_ERROR" in found["broke"][0]

    def test_a_module_holding_no_tests_is_not_an_error(self) -> None:
        """pytest exits 5 for it, and the walk steps over such a module all the
        time. Read as a failure it would end the walk at the first helper-shaped
        test module and report a survivor that nothing had finished looking
        for."""
        self.module("test_a", '"""No tests here."""\n')
        found = self.verdict("test_a")
        assert found["loaded"]
        assert found["broke"] == []
        assert found["ran"] == 0

    def test_a_loaded_report_says_so(self) -> None:
        """The other value of the same flag, so `loaded` is not trivially true
        of every report the caller ever sees."""
        self.passing("test_a")
        assert self.verdict("test_a")["loaded"]

    def test_a_report_is_always_written(self) -> None:
        """The half of `main`'s outer belt that holds on every platform.

        `mutate._run` reads an absent report as "the probe was killed before it
        could write anything", and every scenario above is a different one --
        so a report existing, with a `loaded` in it, is what keeps those apart
        from a killed lane. The belt *firing* needs the address-space cap and is
        therefore in `TestAnOutOfMemoryTestIsNotAnAnswer`, which the runners
        without one exclude.

        `assertIn` on the key rather than on its value, because the two values
        are the two cases the tests above already separate; what is claimed here
        is that the file exists and answers the question at all.
        """
        self.module("test_a", "raise SystemExit('nothing here survives')\n")
        for found in (self.verdict("test_a"), self.verdict()):
            assert "loaded" in found


class TestTheSandboxIsLeftAsItWasFound(Probe):
    """Nothing a probe writes may survive into the next mutation's sandbox.

    A sandbox is reused, and a stale `.pyc` of the same size in the same second
    is read instead of the mutation -- the trap CLAUDE.md records, whose whole
    point is that it leaves no trace to assert on. pytest adds a second one:
    it writes a `.pytest_cache` unless told not to, and rewrites assertions into
    bytecode it would like to cache.
    """

    def test_nothing_is_written_beside_the_tests(self) -> None:
        self.passing("test_a")
        before = {path.name for path in self.sandbox.iterdir()}
        self.verdict("test_a")
        after = {path.name for path in self.sandbox.iterdir()}
        assert after == before, "the probe left something in the sandbox"

    def test_no_bytecode_survives_assertion_rewriting(self) -> None:
        """Rewriting is left on -- it costs below measurement and Phase B's
        pytest-native `assert` statements need it -- so this asserts the two
        settings that keep it from caching: `-B` here and
        `PYTHONDONTWRITEBYTECODE` for everything the suite forks."""
        self.passing("test_a")
        self.verdict("test_a")
        assert list(self.sandbox.rglob("*.pyc")) == []
        assert list(self.sandbox.rglob("__pycache__")) == []


@unittest.skipUnless(CAPS, "RLIMIT_AS is not usable here")
class TestTheMemoryCapsArithmetic(Probe):
    """`cap`'s branches, read back from `getrlimit` rather than by watching a
    runaway allocation die.

    Coverage by *consequence* -- something allocates until the cap stops it --
    can only be slow or fatal, and it is why two mutants of this arithmetic came
    back `BROKE` rather than `caught` in the backend this was ported from: `==`
    becoming `!=` at the `hard` comparison, and `and` becoming `or` at the `soft`
    one, both leave no cap in force, so the memory-eating test is unbounded and
    the harness's alarm speaks first. `BROKE` is never `caught`, so the
    arithmetic was unguarded.

    Reading `getrlimit` back is immediate and exact, and it distinguishes every
    branch. A child process each time, because `setrlimit` is not undoable
    upward once lowered.

    **This class only runs where `RLIMIT_AS` is usable, which today means
    Linux.** macOS refuses to set it at all and reports an unlimited ceiling as
    `sys.maxsize` rather than `RLIM_INFINITY`; CI is what said so, twice. A
    green macOS leg is therefore not evidence that any of this holds -- see
    `address_space_caps`, and CLAUDE.md §2 on labelling a test that can only
    fail on one platform.
    """

    #: Comfortably larger than anything the child allocates, and small enough
    #: to be distinguishable from the unlimited value.
    ASKED = 2 << 30

    #: The "known state" each child starts from: enough room that `cap(ASKED)`
    #: really is a *lowering*, and finite, so that a child of this suite can
    #: never be the unbounded process that took a machine down. Twice `ASKED`,
    #: which is the smallest number that makes the lowering observable.
    ROOMY = 4 << 30

    def limits(self, limit: int, soft: int | None = None) -> int:
        """`RLIMIT_AS`'s soft limit after `cap(limit)`, from a child that starts
        from a known state. The state it started *from* is left on
        `self.started`, because one assertion below is about a difference rather
        than a value.

        There is no `hard` parameter, and that is a finding rather than an
        omission -- see `test_a_higher_finite_soft_limit_is_brought_down`.

        **The child settles its own starting cap first, and that is
        load-bearing.** `verdict.main` calls `cap` before anything is collected,
        so *during a sweep* the process running these tests already holds a
        finite `RLIMIT_AS` -- `mutate.MEMORY` is 4 GiB. Without settling it,
        `limits(0)` reads that back instead of the fixture's own number and the
        test fails on an unmutated tree: every row of a `tools/verdict.py` sweep
        then prints `caught` for a reason that has nothing to do with the
        mutation, and the baseline run voids the lot. Green under a plain suite
        run and red under the harness is the worst shape a test in this file can
        have.

        **It raises to a bounded number, never to `hard`.** `(hard, hard)` was
        the first spelling and it is how a sweep OOM-killed the host: under the
        harness `hard` is whatever `cap` left, and `cap` used to leave it
        `RLIM_INFINITY` -- so "clear the inherited cap" meant "run with no cap",
        and one process reached 51.5 GiB. `cap` lowers `hard` now, so this can no
        longer raise past it even by asking; `ROOMY` is the ask, and `min` keeps
        it legal when the harness's ceiling is lower still.

        Not `RLIM_INFINITY` either, for the reason that spelling was chosen
        over: macOS reports an unlimited ceiling as `sys.maxsize` rather than as
        `RLIM_INFINITY` (which is `-1`), so asking for `-1` against that hard
        limit is "current limit exceeds maximum limit" and the child dies.
        """
        # `RLIM_INFINITY` is `-1` -- a sentinel, not a large number -- so every
        # comparison against it has to be spelled out rather than left to `min`.
        # Written once here because getting it wrong is silent: `min(1 GiB, -1)`
        # is `-1`, which *raises* the limit to unlimited, and the test then reads
        # back a number `cap` chose rather than the one the fixture set.
        setup = (
            "def under(want, hard):\n"
            "    return want if hard == resource.RLIM_INFINITY else min(want, hard)\n"
            "hard = resource.getrlimit(resource.RLIMIT_AS)[1]\n"
            f"resource.setrlimit(resource.RLIMIT_AS, (under({self.ROOMY}, hard), hard))\n"
        )
        if soft is not None:
            setup += f"resource.setrlimit(resource.RLIMIT_AS, (under({soft}, hard), hard))\n"
        code = (
            "import resource, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            f"{setup}"
            "before = resource.getrlimit(resource.RLIMIT_AS)[0]\n"
            "from tools import verdict\n"
            f"verdict.cap({limit})\n"
            "print(before, resource.getrlimit(resource.RLIMIT_AS)[0])\n"
        )
        done = subprocess.run(
            [sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=BOUND
        )
        assert done.returncode == 0, done.stderr
        started, after = (int(word) for word in done.stdout.split())
        self.started = started
        return after

    def test_it_lowers_an_unlimited_process_to_what_was_asked(self) -> None:
        assert self.limits(self.ASKED) == self.ASKED

    def test_zero_is_no_cap(self) -> None:
        """`--memory 0` promises it, and `setrlimit(..., 0)` would make the
        sandbox fail every row for a reason no output would explain.

        Asserted as "the limit is exactly what it was", not as "the limit is
        `RLIM_INFINITY`". The second is a claim about the *fixture's* starting
        state as much as about `cap`, and it only holds if the child raised
        itself to unlimited first, which is the thing that OOM-killed a machine.
        """
        got = self.limits(0)
        assert got == self.started

    def test_it_never_raises_a_ceiling_somebody_else_set(self) -> None:
        """ "A caller who already sandboxed us meant it." The existing soft limit
        is *below* what is asked, so it must survive untouched."""
        already = self.ASKED // 2
        assert self.limits(self.ASKED, soft=already) == already

    def test_a_higher_finite_soft_limit_is_brought_down(self) -> None:
        """The `soft != RLIM_INFINITY and soft <= ceiling` branch. Read with
        `or`, a finite soft limit *above* the ceiling short-circuits the whole
        function and the process keeps the larger allowance.

        `hard == RLIM_INFINITY` read as `!=` is **not** an equivalent mutant, an
        earlier draft's long argument that it was notwithstanding.
        `RLIM_INFINITY` is `-1`, not a large number, so with the comparison
        inverted an infinite `hard` gives `min(limit, -1) == -1` and the process
        is left *uncapped*. That argument assumed infinity sorted above every
        finite limit; the constant is a sentinel.

        **`min(limit, hard)` itself cannot be observed**, and this test stands
        where a second one used to try. The kernel refuses `soft > hard`
        (measured: "current limit exceeds maximum limit"), so whenever `hard` is
        below `limit` the clamp gives `ceiling == hard` -- and `soft <= ceiling`
        then holds by that same invariant, so `cap` returns having touched
        nothing. Dropping the `min` would make it *attempt* a raise of `hard`
        and swallow the refusal, reaching the identical state by a longer road.
        """
        assert self.limits(self.ASKED, soft=self.ASKED * 2) == self.ASKED


class TestTheWalkPastTheSelection(Probe):
    """A mutation's selection is an *ordering*, not a gate: when nothing in it
    notices, the run keeps going through the rest of the suite.

    That is what removed the second pass. Before it, a survivor was re-run
    against the whole suite afterwards -- so it ran its selection and then a
    superset of it, and the narrow run was work thrown away.

    The claims here are the whole of the change, and each fails without it: the
    walk *reaches* a module the selection never named; it *stops* once something
    notices; a baseline does not walk at all; and what it reaches is decided by
    the host project's configuration rather than by a pattern spelled here.
    """

    #: Imported for its side effect, which is the point: a module that has not
    #: been imported cannot have written this. Laziness is not observable from
    #: `ran`, because a module the walk collects but never reaches contributes
    #: no tests to the count either way.
    MARKER = "reached.txt"

    def sandboxed(self, *, selected_notices: bool, beside: str = "test_beside") -> None:
        """Two modules: one selected, one not, and only the second ever fails.

        The second records that it was imported at all, so "the walk stopped"
        and "the walk ran it and it passed" are distinguishable -- they are the
        same `noticed: []` otherwise.
        """
        self.module(
            "test_chosen",
            f"""
            import unittest

            class Chosen(unittest.TestCase):
                def test_it(self):
                    self.assertTrue({not selected_notices})
            """,
        )
        self.module(
            beside,
            f"""
            import pathlib
            import unittest

            pathlib.Path({self.MARKER!r}).write_text("imported", encoding="utf-8")

            class Beside(unittest.TestCase):
                def test_it(self):
                    self.fail("the module the selection never named")
            """,
        )

    def reached(self) -> bool:
        return (self.sandbox / self.MARKER).is_file()

    def test_it_reaches_a_module_the_selection_never_named(self) -> None:
        """The central claim. Without the walk this is `survived` -- which is
        exactly the false survivor the confirmation pass existed to correct."""
        self.sandboxed(selected_notices=False)
        found = self.verdict("test_chosen", failfast=True, walk=True)
        assert found["killers"] == ["test_beside.py::Beside::test_it"], (
            "the walk did not reach past the selection"
        )

    def test_what_it_walks_into_comes_from_the_configuration(self) -> None:
        """The genericity requirement, and the half that fails silently.

        **A pattern this project would never write, and neither of pytest's own
        defaults.** The first version of this test used `beside_test.py`, which
        is pytest's *other* built-in `python_files` entry -- so a `Watcher` with
        the two defaults hardcoded passed it identically, and it distinguished
        nothing. Writing a `pytest.ini` and asserting the walk follows
        `check_*.py` is a test only `config.getini("python_files")` can pass.

        What it guards: a module the walk misses turns a caught row into a
        reported survivor -- the flattering direction -- with nothing anywhere
        going red.
        """
        (self.sandbox / "pytest.ini").write_text(
            "[pytest]\npython_files = check_*.py test_chosen.py\n", encoding="utf-8"
        )
        self.sandboxed(selected_notices=False, beside="check_beside")
        found = self.verdict("test_chosen", failfast=True, walk=True)
        assert found["killers"] == ["check_beside.py::Beside::test_it"]

    def test_it_stops_once_the_selection_itself_notices(self) -> None:
        """The cost half, and the one that makes the walk affordable: a caught
        mutation must pay for its selection and nothing more.

        Asserted on the marker rather than on `ran`. Collecting all 33 modules
        of this repository measures 500.8 ms against 116.0 ms for one, so a walk
        that collected them eagerly would hand back minutes over a sweep -- more
        than deleting the second pass saves. `ran` cannot see that: a collected
        module whose tests never run adds nothing to the count.
        """
        self.sandboxed(selected_notices=True)
        found = self.verdict("test_chosen", failfast=True, walk=True)
        assert found["killers"] == ["test_chosen.py::Chosen::test_it"]
        assert not self.reached(), "a module past the answer was collected anyway"

    def test_it_stops_on_a_notice_even_with_failfast_off(self) -> None:
        """The same claim on the path a hand-written table takes.

        `mutate._run_spec` leaves `failfast` off, so pytest's own `-x` is never
        passed and the outer walk has to notice for itself that the answer is
        already in. Without that, every caught row on the spec path becomes a
        whole-suite run -- the cost this design exists to avoid, reintroduced on
        the one path nothing else here covers.
        """
        self.sandboxed(selected_notices=True)
        found = self.verdict("test_chosen", failfast=False, walk=True)
        assert found["killers"] == ["test_chosen.py::Chosen::test_it"]
        assert not self.reached(), "the walk carried on past its own answer"

    def test_a_baseline_does_not_walk(self) -> None:
        """`walk` is a separate argv slot precisely so this row exists. A
        baseline asks whether *one selection* is green; inferring the walk from
        the selection instead would make every baseline a whole-suite run, and
        the baseline is the check meant to cost nothing.
        """
        self.sandboxed(selected_notices=False)
        found = self.verdict("test_chosen", walk=False)
        assert found["noticed"] == [], "a baseline widened past its selection"
        assert not self.reached(), "a baseline collected a module it was not given"
        assert found["ran"] == 1

    def test_a_red_baseline_is_reported_whole(self) -> None:
        """The other half of the condition that bounds the walk, and the half
        that is not about walking at all.

        `_borrow`'s docstring commits to it: *"Never `failfast`: a red baseline
        is a thing you want the whole of."* The walk stops as soon as anything
        notices, so that stop has to be gated on `walk` -- ungated, a baseline
        over several modules reports the first red one and silently skips the
        rest, which is one shard of a broken tree presented as the whole story.
        """
        self.sandboxed(selected_notices=False)
        found = self.verdict("test_beside", "test_chosen", walk=False)
        assert found["killers"] == ["test_beside.py::Beside::test_it"]
        assert found["ran"] == 2, "a red baseline stopped at its first red module"


class TestWhatThisAssumesOfPytest(Probe):
    """One test per pytest behaviour the classification rests on.

    Every row of the module docstring's table is a *measurement* rather than a
    documented guarantee, and each of them decides a `broke`/`caught` line. A
    pytest release that moved one would otherwise turn `broke` silently into
    `caught`, or hide a whole class of kill -- so this class exists to go red
    loudly at the upgrade instead of flattering the next sweep.

    Driven against pytest directly, with a plugin that records what it is
    handed. Not through `verdict.py`, because a verdict-shaped assertion here
    would pass just as well against a classifier that had learnt to compensate
    for a change, which is the thing being watched for.
    """

    #: Written to a *file*, named by the first argument, and that is the point
    #: rather than a convenience. Anything a plugin prints during a run is eaten
    #: by pytest's own capture -- partially, which is worse than entirely:
    #: measured, 3 of 8 lines survived. The remedy a reader reaches for is `-s`,
    #: and that is the one flag a probe may never pass, because it hands the
    #: suite a real stdin again. A file is outside the whole question, which is
    #: why the report `verdict.py` writes is one too.
    SPY = """
    import json, sys
    import pytest

    seen = []

    class Spy:
        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_makereport(self, item, call):
            report = yield
            seen.append(
                {
                    "nodeid": item.nodeid,
                    "when": call.when,
                    "raised": None if call.excinfo is None else call.excinfo.type.__name__,
                    "outcome": report.outcome,
                    "subtest": getattr(report, "context", None) is not None,
                }
            )
            return report

        def pytest_runtest_logreport(self, report):
            seen.append(
                {
                    "nodeid": report.nodeid,
                    "when": report.when,
                    "raised": None,
                    "outcome": report.outcome,
                    "subtest": getattr(report, "context", None) is not None,
                    "logged": True,
                }
            )

        @pytest.hookimpl(wrapper=True)
        def pytest_fixture_setup(self, fixturedef, request):
            try:
                return (yield)
            except BaseException as raised:
                seen.append(
                    {
                        "nodeid": request.node.nodeid,
                        "when": "fixture",
                        "raised": type(raised).__name__,
                        "outcome": "failed",
                        "subtest": False,
                        "scope": str(fixturedef.scope),
                    }
                )
                raise

    pytest.main(["-q", "-p", "no:cacheprovider", *sys.argv[2:]], plugins=[Spy()])
    with open(sys.argv[1], "w", encoding="utf-8") as out:
        json.dump(seen, out)
    """

    def watched(self, body: str, *args: str) -> list[dict[str, Any]]:
        """What pytest handed a plugin, running ``body`` as ``test_a.py``."""
        self.module("test_a", body)
        seen = self.report.with_name("seen.json")
        done = subprocess.run(
            [sys.executable, "-B", "-c", textwrap.dedent(self.SPY), str(seen), *args],
            cwd=self.sandbox,
            env={**os.environ, **PROBE_ENV},
            capture_output=True,
            text=True,
            timeout=BOUND,
        )
        assert seen.is_file(), (
            f"the spy wrote nothing.\nstdout: {done.stdout}\nstderr: {done.stderr}"
        )
        return [dict(event) for event in json.loads(seen.read_text(encoding="utf-8"))]

    def scopes(self, body: str) -> dict[str, str]:
        """The scope of each fixture that raised, by what it raised."""
        return {
            str(event["raised"]): str(event["scope"])
            for event in self.watched(body)
            if event["when"] == "fixture"
        }

    def phases(self, body: str) -> dict[str, str]:
        """Which phase each failure was reported in, by what it raised."""
        return {
            str(event["raised"]): str(event["when"])
            for event in self.watched(body)
            if event["outcome"] == "failed" and not event.get("logged")
        }

    def test_a_test_and_its_own_teardown_are_the_call_phase(self) -> None:
        """Both are the test's, so both are `caught`. Reported anywhere else
        they would be `broke`, and a mutation only a `tearDown` can see would
        come back a survivor."""
        found = self.phases(
            """
            import unittest
            class T(unittest.TestCase):
                def test_body(self):
                    raise LookupError("the body")
                def test_clean(self):
                    pass
                def tearDown(self):
                    if self._testMethodName == "test_clean":
                        raise ArithmeticError("the cleanup")
            """
        )
        assert found == {"LookupError": "call", "ArithmeticError": "call"}

    def test_class_and_module_fixtures_are_the_other_two_phases(self) -> None:
        """Nothing in them evaluated an assertion, so neither is `caught`. This
        is the whole of what replaced an `isinstance` against
        `unittest.suite._ErrorHolder`."""
        assert self.phases("""
                import unittest
                class T(unittest.TestCase):
                    @classmethod
                    def setUpClass(cls):
                        raise LookupError("before")
                    def test_it(self):
                        pass
                """) == {"LookupError": "setup"}
        self.fresh()
        assert self.phases("""
                import unittest
                class T(unittest.TestCase):
                    @classmethod
                    def tearDownClass(cls):
                        raise ArithmeticError("after")
                    def test_it(self):
                        pass
                """) == {"ArithmeticError": "teardown"}

    def test_a_failed_subtest_reaches_makereport_but_not_its_owners_report(self) -> None:
        """The trap, asserted against pytest itself.

        At `makereport` the failure arrives once, carrying the owner's nodeid --
        which is what makes attribution free. By the time it is *logged* it has
        become a separate object and the owner's own report reads ``passed``, so
        a classifier reading logged reports would answer "nothing noticed" for a
        test the suite demonstrably caught.
        """
        events = self.watched(
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    with self.subTest(n=1):
                        self.fail("inside")
            """
        )
        made = [e for e in events if not e.get("logged") and e["when"] == "call"]
        # Two events, and both name the owner: the subtest's failure, then the
        # owner's own success. Classifying on the failed one is free -- there is
        # no carrier to unwrap and no parametrized id to strip.
        assert [(str(e["nodeid"]), str(e["outcome"])) for e in made] == [
            ("test_a.py::T::test_it", "failed"),
            ("test_a.py::T::test_it", "passed"),
        ], "the subtest failure did not arrive at makereport against its owner"
        # And the same two *logged*, where the failed one has become a separate
        # object -- so "did this test's report fail" answers no.
        logged = [e for e in events if e.get("logged") and e["when"] == "call"]
        assert [(str(e["outcome"]), bool(e["subtest"])) for e in logged] == [
            ("failed", True),
            ("passed", False),
        ], "the owner's own logged report is no longer the passing one"

    def test_a_baseexception_from_a_test_body_is_reported_rather_than_escaping(self) -> None:
        """`Hung` derives from `BaseException` so a test doing `except
        Exception` cannot swallow the alarm. That only helps if pytest reports
        it instead of letting it end the session -- and the test after it has to
        still run, or a hang would look like a walk that finished."""
        events = self.watched(
            """
            import unittest
            class Stop(BaseException):
                pass
            class T(unittest.TestCase):
                def test_a_stops(self):
                    raise Stop("not an Exception")
                def test_b_runs(self):
                    pass
            """
        )
        made = [e for e in events if not e.get("logged")]
        assert ("test_a.py::T::test_a_stops", "call", "Stop") in [
            (str(e["nodeid"]), str(e["when"]), str(e["raised"])) for e in made
        ]
        assert "test_a.py::T::test_b_runs" in {str(e["nodeid"]) for e in made}

    def test_a_skip_is_neither_an_answer_nor_a_break(self) -> None:
        """A skip is not an answer: nothing was checked. It must not be `failed`
        anywhere, or every conditionally-skipped test in the suite would credit
        every mutation it never ran against."""
        events = self.watched(
            """
            import unittest
            class T(unittest.TestCase):
                @unittest.skip("because")
                def test_it(self):
                    self.fail("never reached")
            """
        )
        assert {str(e["outcome"]) for e in events} & {"failed"} == set()
        assert "skipped" in {str(e["outcome"]) for e in events}

    def test_the_scope_of_a_fixture_that_raises_is_reported(self) -> None:
        """The one fact phase cannot carry.

        `verdict.Watcher.pytest_fixture_setup` rests on two things: that pytest
        calls the hook for every fixture and lets a wrapper see the exception,
        and that `FixtureDef.scope` is the string it reads. Neither is a
        documented guarantee, and a release that moved either would send every
        function-scoped fixture failure back to `broke` -- a silent 104-row
        regression rather than a red run.

        Both widths in one run, so a pytest that always answered "function"
        fails here too.
        """
        found = self.scopes(
            """
            import pytest
            @pytest.fixture
            def narrow():
                raise ValueError("mine alone")
            @pytest.fixture(scope="module")
            def wide():
                raise KeyError("everyone's")
            def test_one(narrow):
                pass
            def test_two(wide):
                pass
            """
        )
        assert found == {"ValueError": "function", "KeyError": "module"}


class TestTellingAnAnswerFromACarrier(unittest.TestCase):
    """`_carrier`, which decides whether a test gets *credited* with noticing.

    Both limits raise inside a real test at the ``call`` phase, so by phase they
    are indistinguishable from that test asserting something -- and filed as
    answers they credit a test that asserted nothing, which is a mutation
    reported as caught by a test that never looked at it. That is the one error
    this whole tool cannot afford.

    Driven directly rather than through a probe: the module docstring's rule is
    that nothing here may arm the alarm or set the rlimit in the process running
    the suite, and this is a subclass check and a string format.
    """

    class Held:
        """The two fields of `pytest.ExceptionInfo` that `_carrier` reads.

        A stand-in rather than a real one, because building a real
        `ExceptionInfo` needs a real raise inside a real `sys.exc_info`, and
        `_carrier`'s claim is about which *class* it is handed.
        """

        def __init__(self, kind: type[BaseException]) -> None:
            self.type = kind

    def carrier(self, kind: type[BaseException] | None, each: float = 30.0) -> str:
        from tools import verdict

        held = None if kind is None else self.Held(kind)
        return verdict._carrier("t.py::T::test_it", held, each)  # type: ignore[arg-type]

    def test_an_ordinary_failure_is_an_answer(self) -> None:
        assert self.carrier(AssertionError) == ""

    def test_no_error_at_all_is_an_answer(self) -> None:
        assert self.carrier(None) == ""

    def test_the_alarm_is_a_carrier_and_quotes_the_bound(self) -> None:
        """The bound is in the message because the number is not a constant:
        `--each-test` moves it, and a `broke` line naming a limit that was never
        in force is worse than one naming none."""
        from tools import verdict

        said = self.carrier(verdict.Hung, each=2.5)
        assert "did not finish within 2.5s" in said
        assert "t.py::T::test_it" in said

    def test_the_cap_is_a_carrier(self) -> None:
        assert "ran out of memory" in self.carrier(MemoryError)

    def test_a_subclass_of_the_cap_counts_too(self) -> None:
        """`issubclass` rather than `is`: the cap can surface as a subclass
        raised by an extension module, and one that read `is` would credit the
        test."""

        class Worse(MemoryError):
            pass

        assert "ran out of memory" in self.carrier(Worse)

    def test_the_alarm_is_checked_before_the_cap(self) -> None:
        """`Hung` is a `BaseException` and `MemoryError` an `Exception`, so no
        class is both -- but the order is what makes the two messages
        distinguishable, and a run that reported a hang as an exhausted cap
        would send a reader looking at `--memory`."""
        from tools import verdict

        assert "did not finish" in self.carrier(verdict.Hung)


class TestWhenTheAlarmIsArmedAtAll(unittest.TestCase):
    """`each_test`: what it arms, and what it says it armed.

    The return value is what the `Watcher` is given, so a run with no alarm
    armed must report `0` rather than quoting a bound that was never in force.
    """

    def setUp(self) -> None:
        import signal

        self.signal = signal
        before = signal.getsignal(signal.SIGALRM)
        self.addCleanup(signal.signal, signal.SIGALRM, before)

    def handler(self) -> object:
        return self.signal.getsignal(self.signal.SIGALRM)

    def test_zero_arms_nothing(self) -> None:
        from tools import verdict

        before = self.handler()
        assert verdict.each_test(0) == 0.0
        assert self.handler() is before, "a handler was installed for no alarm"

    def test_a_bound_arms_the_handler_that_raises(self) -> None:
        """PEP 475 retries a syscall interrupted by a signal, so a handler that
        recorded the alarm and returned would be swallowed by exactly the
        blocking read a hung test sits in. Raising propagates instead."""
        from tools import verdict

        assert verdict.each_test(2.5) == 2.5
        assert self.handler() is verdict._ring
        with self.assertRaises(verdict.Hung):
            verdict._ring(self.signal.SIGALRM, None)

    def test_a_platform_without_the_alarm_arms_nothing(self) -> None:
        """Windows, which plan §2 puts out of scope for v1 -- so this is a guard
        rather than a supported path, and the guard is what keeps it a `0` in
        the report instead of an `AttributeError` inside the probe."""
        from unittest import mock

        from tools import verdict

        with mock.patch.object(verdict, "signal") as absent:
            del absent.SIGALRM
            assert verdict.each_test(5) == 0.0


class TestWhereTheWalkLooks(unittest.TestCase):
    """`Watcher.beside`: every test file next to the selection's own.

    Driven in this process, which the module docstring allows for exactly the
    functions that touch neither the alarm nor the rlimit: this is a `glob` and
    a sort. The import stays inside the methods so that remains true of
    importing this file as well -- and it is issued *before* any `chdir`, since
    a probe runs as ``python -c`` where `sys.path[0]` is `''` and resolves
    against the current directory at each import rather than at startup.
    """

    def watcher(self, root: Path, patterns: list[str] | None = None) -> Any:
        from tools import verdict

        made = verdict.Watcher(0.0)
        made.root = root
        made.patterns = ["test_*.py", "*_test.py"] if patterns is None else patterns
        return made

    def test_it_looks_in_the_directory_the_selection_lives_in(self) -> None:
        found = self.watcher(ROOT).beside(["tests.test_sync"])
        assert "tests/test_verdict.py" in found
        assert "test_verdict.py" not in found, "the directory was dropped"
        assert "tests/test_sync.py" not in found, "the selection walked into itself"

    def test_it_takes_the_patterns_it_is_given(self) -> None:
        """The genericity claim at the unit it is decided in. A project spelling
        its tests `*_test.py` gets those and no others; one spelling them the
        way this project does gets those."""
        with tempfile.TemporaryDirectory(prefix="tupferl-patterns-") as name:
            box = Path(name)
            for stem in ("test_one", "two_test", "helper"):
                (box / f"{stem}.py").write_text("", encoding="utf-8")
            made = self.watcher(box, ["*_test.py"])
            assert made.beside(["test_one"]) == ["two_test.py"]
            assert self.watcher(box, ["test_*.py"]).beside(["two_test"]) == ["test_one.py"]

    def test_a_module_that_is_not_a_test_is_never_walked_into(self) -> None:
        """`helper.py` is the half that can fail quietly: a glob of `*.py`
        rather than the configured patterns walks into support modules, and a
        selection handed one reports it as holding no tests."""
        with tempfile.TemporaryDirectory(prefix="tupferl-walk-") as name:
            box = Path(name)
            for stem in ("test_one", "test_two", "helper"):
                (box / f"{stem}.py").write_text("", encoding="utf-8")
            assert self.watcher(box).beside(["test_one"]) == ["test_two.py"]

    def test_what_it_returns_is_sorted(self) -> None:
        """The order is the walk's order, and it has to be *stable*: a bare set
        iterates by hash, so two runs of the same sweep would try the modules in
        different orders and a row's recorded `killer` would move.

        Eight modules, not two. With two, `list(set(...))` frequently comes out
        sorted by luck and the assertion holds against its own mutation -- one
        in two against one in 40320.
        """
        with tempfile.TemporaryDirectory(prefix="tupferl-order-") as name:
            box = Path(name)
            for stem in (
                "test_zulu",
                "test_alpha",
                "test_mike",
                "test_bravo",
                "test_yankee",
                "test_charlie",
                "test_x_ray",
                "test_delta",
            ):
                (box / f"{stem}.py").write_text("", encoding="utf-8")
            found = self.watcher(box).beside(["test_alpha"])
        assert found == sorted(found), "the walk order is not stable"
        assert len(found) == 7, "the selection was not subtracted, or a file was missed"


class TestHowANameBecomesANode(unittest.TestCase):
    """`as_path`: the one place a dotted selection becomes something pytest can
    address.

    It fails in the direction that is hardest to see. A name pytest cannot
    resolve is refused with a usage error, which the exit-status arm reports as
    `broke` -- loud. A name that resolves to the *wrong* node runs the wrong
    tests and reports a plausible verdict about them.
    """

    def as_path(self, name: str) -> str:
        from tools import verdict

        return verdict.as_path(name)

    def test_a_module_becomes_its_file(self) -> None:
        assert self.as_path("tests.test_sync") == "tests/test_sync.py"

    def test_a_class_and_a_method_become_node_parts(self) -> None:
        """The longest *existing* prefix wins, so the class is a node inside the
        file rather than a directory called `TestX`. Split at the first dot it
        would name `tests/test_sync/TestTheDecisionTable.py`, which does not
        exist -- and pytest would refuse the whole run."""
        assert (
            self.as_path("tests.test_sync.TestTheDecisionTable")
            == "tests/test_sync.py::TestTheDecisionTable"
        )

    def test_a_nodeid_is_handed_back_untouched(self) -> None:
        """`first` arrives already in this form, out of a killers cache written
        by an earlier run, and translating it twice would turn every `::` into
        a directory."""
        for already in ("tests/test_sync.py", "tests/test_sync.py::TestX::test_y"):
            assert self.as_path(already) == already

    def test_a_name_that_resolves_to_nothing_is_still_a_module(self) -> None:
        """Refused by pytest and reported as `broke`, which is what an
        unloadable module always was. Selecting nothing quietly would be filed
        as "the targets held no tests", which is a different sentence about a
        tree that is fine."""
        assert self.as_path("tests.test_nothing_of_the_sort") == "tests/test_nothing_of_the_sort.py"


class TestTheSummaryLineOfACollectionFailure(unittest.TestCase):
    """`_stated`, and the fact that `tools/run_tests.py` holds a second copy.

    The copy is deliberate and both docstrings argue for it: this module is read
    as *source text* into a sandbox and may import nothing from `tools`, so a
    shared helper would drag the whole package in behind it. What the decision
    costs is that nothing kept the two honest, and until this class nothing did
    -- `tests/test_run_tests.py` tests its copy and no test anywhere named this
    one, so the `Errno`-is-not-`E` trap was guarded in one file and guarded by
    nothing in the other. That trap matters here: this line is what a `broke`
    row carries into a sweep's summary, which is the only text a reader gets
    for a mutant no test could answer.

    **The assertion is that the two agree**, not that this one is right. A
    divergence is the failure the duplication risks, and it is invisible from
    either side alone.

    Imported inside the methods, as `TestHowANameBecomesANode` above does: the
    subject of this file is a probe that runs as a subprocess, and a
    module-scope import would read as though that had changed.
    """

    #: What pytest renders for a module whose import raised. The first line is
    #: the file, which is the half a reader would reach for and the less useful
    #: one; the last is the exception, prefixed the way pytest prints it.
    RENDERED = (
        "ImportError while importing test module '/tmp/x/tests/test_broken.py'.\n"
        "Hint: make sure your test modules/packages have valid Python names.\n"
        "Traceback:\n"
        "tests/test_broken.py:1: in <module>\n"
        "    import nothing_by_this_name  # noqa: F401\n"
        "E   ModuleNotFoundError: No module named 'nothing_by_this_name'\n"
    )

    def both(self, rendered: str) -> tuple[str, str]:
        from tools import run_tests, verdict

        return verdict._stated(rendered), run_tests._stated(rendered)

    def test_the_cause_is_kept_rather_than_the_file_and_line(self) -> None:
        said, twin = self.both(self.RENDERED)
        assert said == "ModuleNotFoundError: No module named 'nothing_by_this_name'"
        assert twin == said, "the two copies of `_stated` have diverged"

    def test_a_line_that_merely_starts_with_an_e_keeps_it(self) -> None:
        """`removeprefix("E")` turns a line beginning "Errno" into one beginning
        "rrno", quietly, in the one message a reader has to act on.

        Three lines, so that neither `spoken[0]` nor `spoken[1]` is the answer
        -- `tests/test_run_tests.py` records the two sweeps it took to learn
        that, and the same fixture has to be used here or this copy is guarded
        more weakly than the one it is checked against."""
        said, twin = self.both(
            "tests/test_x.py:1: in <module>\n    raise OSError(2)\nErrno 2: no such file"
        )
        assert said == "Errno 2: no such file"
        assert twin == said, "the two copies of `_stated` have diverged"

    def test_a_rendering_with_nothing_in_it_still_says_something(self) -> None:
        said, twin = self.both("\n  \n")
        assert said == "collection failed and said nothing"
        assert twin == said, "the two copies of `_stated` have diverged"


if __name__ == "__main__":
    unittest.main()
