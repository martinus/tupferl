"""`tools/verdict.py`: which kind of failure a suite produced.

Issue #4 put this module first and called it a port. There is nothing to port --
`martinus/woswoar` has no `tests/test_verdict.py`; its classification is
exercised indirectly through a 117 KB `test_mutate.py`. And `tools/verdict.py`
is the *most* diverged of the ported tools (262 lines there against 410 here,
190 changed), so a port would have been the wrong shape anyway. This is written
against the code that is here.

**Why it is first regardless.** `mutate` reports `caught` when a test method
noticed and `broke` when the run merely fell over, and both exit non-zero
leaving a plausible `Ran N` behind. Every other number the harness produces is
downstream of that line being drawn correctly, and drawn *wrongly* it errs
towards `caught` -- flattering the tests, which CLAUDE.md §8 names as the
direction every bug in this class has taken.

**Driven the way `mutate` drives it**: the module's source handed to `python -c`
with a sandbox as the working directory, throwaway test modules inside it, and
the report written outside. Not by importing `verdict` and calling `collect` in
this process -- `cap` sets an address-space rlimit and the alarm installs a
`SIGALRM` handler, so an in-process test would be configuring the suite that is
running it.
"""

from __future__ import annotations

import json
import resource
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any

#: The repository root, so a child can import `tools` after chdir-free launch.
ROOT = Path(__file__).resolve().parent.parent

#: The tool's own source, read the way `mutate._probe` reads it -- from this
#: tree, never from the sandbox. A copy under test could otherwise decide its
#: own verdict, which is the property `verdict.py`'s docstring opens with.
SOURCE = (Path(__file__).resolve().parent.parent / "tools" / "verdict.py").read_text(
    encoding="utf-8"
)

#: How long a sandbox test sleeps when it is standing in for one that hangs.
#:
#: This was 30, which reproduced in this file the exact defect the same branch
#: fixed in `tests/test_watch.py`. `tools/mutate.py`'s `EACH_TEST` is 30.0, so a
#: mutant that disables the alarm -- dropping the `not` in `each_test`, or the
#: `setitimer` call -- left these three tests running 30.11s each and tripping
#: the harness first. Measured on a copy: 90.5s for the class, and three `BROKE`
#: rows where `caught` was the whole point, since `BROKE` is never `caught`.
#: The docstring here used to claim "fails the suite in seconds", which was
#: wrong by two orders of magnitude.
#:
#: 8 is comfortably longer than the 0.5s alarm these tests arm and comfortably
#: shorter than `BOUND`, which is itself well under `EACH_TEST`.
FOREVER = 8

#: What the one timed test sleeps for, so its duration is an interval rather
#: than "not negative". Long enough to clear the clock's noise, short enough
#: that it is not felt.
SLEPT = 0.2

#: Seconds one `python -c <verdict source>` run may take before the test calls
#: it hung -- the same reasoning as `tests/test_watch.py`'s constant of the same
#: name, and the same two bounds it has to sit between.
BOUND = 20


class Probe(unittest.TestCase):
    """A sandbox of throwaway test modules, and one run of the tool over them."""

    def setUp(self) -> None:
        self.fresh()

    def fresh(self) -> None:
        """A new sandbox and a new report path.

        Separate from `setUp` because two tests below run the same broken module
        twice, once named and once discovered, and the second needs a sandbox
        the first has not written to. Calling `setUp` again would work and would
        read as a mistake; this says what it is doing. Each call registers its
        own cleanups, which run at the end of the test as usual.
        """
        box = tempfile.TemporaryDirectory(prefix="tupferl-verdict-test-")
        self.addCleanup(box.cleanup)
        self.sandbox = Path(box.name)
        # Outside the sandbox, for the reason `mutate.probe` gives: a report
        # written inside is one `open()` away from being the suite's to write.
        out = tempfile.TemporaryDirectory(prefix="tupferl-verdict-out-")
        self.addCleanup(out.cleanup)
        self.report = Path(out.name) / "verdict.json"

    def module(self, name: str, body: str) -> None:
        (self.sandbox / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")

    def verdict(
        self,
        *names: str,
        failfast: bool = False,
        memory: int = 0,
        each: float = 0.0,
        first: str = "",
    ) -> dict[str, Any]:
        """Run the tool and return the report it wrote.

        The argv layout is `verdict.main`'s, positionally: report, failfast,
        memory cap, per-test seconds, the space-joined `first` selection, then
        the test names. Spelled out here rather than in each test, because a
        wrong position is the kind of mistake that still produces a plausible
        report.
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
                first,
                *names,
            ],
            cwd=self.sandbox,
            capture_output=True,
            text=True,
            timeout=BOUND,
        )
        self.assertTrue(
            self.report.is_file(),
            f"no report was written.\nstdout: {done.stdout}\nstderr: {done.stderr}",
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
        self.assertEqual([], found["broke"])
        self.assertEqual(1, len(found["noticed"]))
        self.assertEqual(1, found["ran"])

    def test_the_killer_is_recorded_as_unittest_takes_it_back(self) -> None:
        """`noticed` is for a human and `killers` is fed straight to a loader.

        The display string is `test_it (test_a.T.test_it)`, which no loader
        accepts. Asserting the id can be *loaded* rather than just matching a
        shape is the point -- a format that merely looks right is what this
        field exists to avoid.
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
        self.assertEqual(["test_a.T.test_it"], found["killers"])

        again = self.verdict(*found["killers"])
        self.assertEqual(1, again["ran"], "the recorded id did not load back")

    def test_an_unexpected_exception_is_also_an_answer(self) -> None:
        """`addError` on a real `TestCase`, which is a test noticing just as
        much as `addFailure` -- a mutation that makes the code raise is caught,
        not broken."""
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
        self.assertEqual([], found["broke"])
        self.assertEqual(1, len(found["noticed"]))


class TestAFixtureThatDied(Probe):
    """`BROKE` is never `caught` -- the single most load-bearing rule here.

    A `setUpClass` failure arrives through `addError` carrying a
    `unittest.suite._ErrorHolder`, which is deliberately *not* a `TestCase`. No
    assertion in it was ever evaluated, so crediting it would report that the
    tests noticed a mutation they never reached.
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
        self.assertEqual([], found["noticed"], "a dead fixture was credited as a test")
        self.assertEqual([], found["killers"])
        self.assertEqual(1, len(found["broke"]))

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
        self.assertEqual([], found["noticed"])
        self.assertEqual(1, len(found["broke"]))

    def test_a_module_that_will_not_import_is_not_an_answer(self) -> None:
        """And the suite must not be *run* at all.

        `loadTestsFromNames` hands back a synthetic `unittest.loader._FailedTest`
        for an unimportable module, and that one **is** a `TestCase` -- so
        running it would surface through `addError` and read as a test noticing.
        `loader.errors` is checked first, which is why `ran` is 0.

        A missing import rather than a syntax error, and that is not
        interchangeable -- see `TestABrokenModuleTakesTwoDifferentPaths`. The
        first draft of this test used a syntax error and failed, because that
        one never reaches `loader.errors` at all.
        """
        self.module("test_a", "import a_module_that_does_not_exist_xyz\n")
        found = self.verdict("test_a")
        self.assertTrue(found["loaded"])
        self.assertEqual(0, found["ran"], "the suite ran despite a load error")
        self.assertEqual([], found["noticed"])
        self.assertEqual(1, len(found["broke"]))
        self.assertIn("test_a", found["broke"][0])


class TestABrokenModuleTakesTwoDifferentPaths(Probe):
    """The same broken module is classified differently by name and by
    discovery, and a fixture written for one proves nothing about the other.

    `tools/mutate.py`'s docstring already records the shape -- "the only fixture
    guarding it used a *syntax* error, which `unittest.loader` does not wrap and
    which therefore takes a different path entirely, so the check passed while
    the case it was named for went unasked". Measured here for both paths:

    | module | named | discovered |
    |---|---|---|
    | `import missing_xyz` | `loader.errors` | `loader.errors` |
    | a syntax error | escapes to `main` | `loader.errors` |
    | `raise SystemExit(...)` | escapes to `main` | `loader.errors` |

    `discover` wraps everything; `loadTestsFromNames` wraps only what derives
    from `Exception`. **What matters is the invariant across all six cells: no
    test is ever credited.** That is asserted below rather than left implied,
    because it is the only thing a caller needs to be true.
    """

    #: The three ways a module can fail to give up its tests, and whether a
    #: *named* load still reaches `loader.errors` for it. Discovery reaches it
    #: for all three, so that column is not stored.
    #:
    #: This is the docstring's table as data. Storing the expected value rather
    #: than branching on the observed one is the difference between a test that
    #: pins six cells and a test that agrees with whatever happened -- the first
    #: draft did the latter, and would have passed against a `verdict.py` where
    #: *every* case escaped to `main`.
    BROKEN = (
        ("a missing import", "import a_module_that_does_not_exist_xyz\n", True),
        ("a syntax error", "this is not python at all !!!\n", False),
        ("a module that exits", "raise SystemExit('gone')\n", False),
    )

    def test_no_broken_module_is_ever_credited_as_a_test_noticing(self) -> None:
        """The invariant first, unconditionally, then the cell.

        `.get(..., [])` rather than `[...]`: a report that did not load carries
        no `noticed` key at all, and the point of asserting it anyway is that
        this line holds for all six cells rather than for the four that happen
        to have the key.
        """
        for what, body, loads_when_named in self.BROKEN:
            for named in (True, False):
                with self.subTest(what=what, named=named):
                    self.fresh()
                    self.module("test_a", body)
                    found = self.verdict("test_a") if named else self.verdict()

                    self.assertEqual([], found.get("noticed", []), "a broken module was credited")
                    self.assertEqual([], found.get("killers", []))
                    self.assertEqual(0, found.get("ran", 0))

                    self.assertEqual(
                        loads_when_named if named else True,
                        found["loaded"],
                        f"{what} took the other path",
                    )
                    if found["loaded"]:
                        self.assertTrue(found["broke"])
                    else:
                        # The tool said so rather than leaving an absent file,
                        # which is the distinction `main`'s handler exists for.
                        self.assertIn("why", found)

    def test_discovery_wraps_what_a_named_load_lets_through(self) -> None:
        """The asymmetry itself, so the table above cannot quietly stop being
        true. A syntax error is the cell that differs."""
        self.module("test_a", "this is not python at all !!!\n")
        self.assertFalse(self.verdict("test_a")["loaded"], "a named syntax error was wrapped")

        self.fresh()
        self.module("test_a", "this is not python at all !!!\n")
        found = self.verdict()
        self.assertTrue(found["loaded"], "a discovered syntax error escaped")
        self.assertEqual(0, found["ran"])


class TestASubTestIsARealAnswer(Probe):
    """`unittest.case._SubTest` is a `TestCase` whose module is `unittest.case`.

    The obvious classification -- "is this class defined under `unittest.`?" --
    files a `subTest` assertion as "the suite broke", and with a strict table
    that aborts the run. This project uses `subTest` in many places, so the
    whole sweep would have been wrong.
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
        self.assertEqual([], found["broke"], "a subTest assertion was filed as a broken run")
        self.assertEqual(1, len(found["noticed"]))

    def test_the_owner_is_recorded_and_not_the_carrier(self) -> None:
        """A `_SubTest`'s `id()` carries the parameters in brackets, and
        `unittest` cannot load that back. Recording the owning test is what
        keeps the id usable, and the round trip is the proof."""
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
        self.assertEqual(["test_a.T.test_it"], found["killers"])
        self.assertNotIn("[", found["killers"][0])
        self.assertEqual(1, self.verdict(*found["killers"])["ran"])


class TestACarrierThatDidNotAssert(Probe):
    """A hung test and a test that ran out of memory both raise *inside* a real
    `TestCase`, so by protocol they are indistinguishable from that test
    noticing. Filed as answers they credit a test that asserted nothing."""

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
        self.assertEqual([], found["noticed"], "a hung test was credited with an answer")
        self.assertEqual([], found["killers"])
        self.assertEqual(1, len(found["broke"]))
        self.assertIn("did not finish", found["broke"][0])

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
        self.assertEqual([], found["noticed"])
        self.assertEqual(1, len(found["broke"]))

    def test_a_hung_subtest_is_also_broken_not_caught(self) -> None:
        """The `addSubTest` path has its own copy of the carrier check, and the
        docstring records that this copy once had no test at all."""
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
        self.assertEqual([], found["noticed"], "a hung subTest was credited with an answer")
        self.assertEqual(1, len(found["broke"]))

    def test_a_test_that_exhausts_the_cap_is_broken_not_caught(self) -> None:
        """`cap` bounds address space, and a `MemoryError` raised inside a test
        arrives at `addError` looking exactly like an assertion."""
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    held = []
                    while True:
                        held.append(bytearray(8 * 1024 * 1024))
            """,
        )
        found = self.verdict("test_a", memory=512 * 1024 * 1024)
        self.assertEqual([], found["noticed"], "an out-of-memory test was credited")
        self.assertEqual(1, len(found["broke"]))
        self.assertIn("out of memory", found["broke"][0])


class TestWhatTheBaselineNeeds(Probe):
    """`reasons` exists for one reader: a red baseline voids every verdict above
    it, and until this was recorded the only thing said about one was the
    failing test's name."""

    def test_the_first_failure_carries_its_traceback(self) -> None:
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
        self.assertEqual(1, len(found["reasons"]))
        self.assertIn("AssertionError", found["reasons"][0])
        self.assertIn("actual", found["reasons"][0])

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
        self.assertEqual(2, len(found["noticed"]))
        self.assertEqual(1, len(found["reasons"]))
        self.assertIn("first", found["reasons"][0])

    def test_an_error_carries_a_traceback_too(self) -> None:
        """`addError`'s copy of the recording, which is a second place that can
        fall out of step with `addFailure`'s."""
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
        self.assertEqual(1, len(found["reasons"]))
        self.assertIn("what went wrong", found["reasons"][0])

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
        self.assertEqual(["test_a.T.test_it"], list(found["times"]))
        # An interval, not `>= 0`: a duration is never negative, so that
        # assertion held against `stopTest`'s subtraction becoming an addition
        # -- a real generated mutant, verified to leave this whole file green.
        # `Killers` orders the cheap prefix from these numbers, so a wrong one
        # silently mis-orders it.
        self.assertGreater(found["times"]["test_a.T.test_it"], SLEPT / 2)
        self.assertLess(found["times"]["test_a.T.test_it"], SLEPT * 20)


class TestWhichTestsGetRun(Probe):
    """`collect`'s selection, where two mistakes each turn "run everything" into
    something much smaller while still reporting a plausible number."""

    def test_no_names_means_the_whole_suite(self) -> None:
        """And by discovery, not `loadTestsFromNames(["tests"])`, which imports
        the package, finds nothing, and comes back green having run zero."""
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    pass
            """,
        )
        self.module(
            "test_b",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    pass
            """,
        )
        self.assertEqual(2, self.verdict()["ran"])

    def test_first_does_not_turn_the_whole_suite_into_a_selection(self) -> None:
        """The one that matters. An empty `names` *means* everything; pushing
        `first` onto that list makes it non-empty, so `confirm` -- which widens
        a survivor to the whole suite while the cheap prefix is still attached
        -- would run the prefix alone and report it as "the whole suite".
        """
        for name in ("test_a", "test_b", "test_c"):
            self.module(
                name,
                """
                import unittest
                class T(unittest.TestCase):
                    def test_it(self):
                        pass
                """,
            )
        found = self.verdict(first="test_a.T.test_it")
        self.assertEqual(4, found["ran"], "the prefix replaced the suite instead of preceding it")

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
                    self.fail("the remembered killer")
            """,
        )
        self.module(
            "test_b",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    self.fail("the rest of the suite")
            """,
        )
        found = self.verdict(failfast=True, first="test_a.T.test_it")
        self.assertEqual(1, found["ran"])
        self.assertEqual(["test_a.T.test_it"], found["killers"])

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
        self.assertEqual(1, self.verdict("test_a", failfast=True)["ran"])
        self.assertEqual(2, self.verdict("test_a", failfast=False)["ran"])


class TestWhenTheToolItselfCannotRun(Probe):
    """ "Said, not inferred." The caller used to conclude "the suite could not be
    loaded" from an absent report -- which is also what a typo in `verdict.py`
    produces, two very different problems with byte-identical output, in a tool
    whose whole thesis is that those must be told apart."""

    def test_a_report_is_written_even_when_collect_raises(self) -> None:
        """A syntax error in a *named* module escapes `loader.errors` entirely
        and reaches `main`'s `except BaseException`. Named rather than
        discovered, and a syntax error rather than a missing import: both halves
        matter, and both were wrong in this test's first draft. See
        `TestABrokenModuleTakesTwoDifferentPaths` for the measurement."""
        self.module("test_a", "this is not python at all !!!\n")
        found = self.verdict("test_a")
        self.assertFalse(found["loaded"])
        self.assertIn("SyntaxError", found["why"])

    def test_a_module_that_walks_out_at_import_scope_is_also_reported(self) -> None:
        """`SystemExit` is a `BaseException`, so `loadTestsFromNames`' wrapping
        -- which catches `Exception` -- does not see it either. `main` catches
        `BaseException` for exactly this."""
        self.module("test_a", "raise SystemExit('module scope walked out')\n")
        found = self.verdict("test_a")
        self.assertFalse(found["loaded"])
        self.assertIn("module scope walked out", found["why"])

    def test_a_loaded_report_says_so(self) -> None:
        """The other value of the same flag, so `loaded` is not trivially true
        of every report the caller ever sees."""
        self.module(
            "test_a",
            """
            import unittest
            class T(unittest.TestCase):
                def test_it(self):
                    pass
            """,
        )
        self.assertTrue(self.verdict("test_a")["loaded"])


class TestTheMemoryCapsArithmetic(Probe):
    """`cap`'s four cases, asserted on the rlimit it sets rather than on a
    runaway allocation dying.

    The existing coverage of `cap` is by *consequence* -- `test_mutate.py`'s
    `TestAMutantThatEatsMemory` allocates until something stops it. That can
    only be slow or fatal, and it is why two mutants here came back `BROKE`
    rather than `caught`: `==` becoming `!=` at the `hard` comparison, and `and`
    becoming `or` at the `soft` one, both leave *no cap in force*, so the
    memory-eating test is unbounded and the harness's alarm speaks first.
    `BROKE` is never `caught`, so the arithmetic was unguarded.

    Reading `getrlimit` back is immediate and exact, and it distinguishes every
    branch. A child process each time, because `setrlimit` is not undoable
    upward once lowered.
    """

    #: Comfortably larger than anything the child allocates, and small enough
    #: to be distinguishable from the unlimited value.
    ASKED = 2 << 30

    def limits(self, limit: int, soft: int | None = None, hard: int | None = None) -> int:
        """`RLIMIT_AS`'s soft limit after `cap(limit)`, from a child that
        optionally starts with one already in force."""
        setup = ""
        if soft is not None:
            setup = f"resource.setrlimit(resource.RLIMIT_AS, ({soft}, {hard}))\n"
        code = (
            "import resource, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            f"{setup}"
            "from tools import verdict\n"
            f"verdict.cap({limit})\n"
            "print(resource.getrlimit(resource.RLIMIT_AS)[0])\n"
        )
        done = subprocess.run(
            [sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=BOUND
        )
        self.assertEqual(0, done.returncode, done.stderr)
        return int(done.stdout.strip())

    def test_it_lowers_an_unlimited_process_to_what_was_asked(self) -> None:
        self.assertEqual(self.ASKED, self.limits(self.ASKED))

    def test_zero_is_no_cap(self) -> None:
        """`--memory 0` promises it, and `setrlimit(..., 0)` would make the
        sandbox fail every row for a reason no output would explain."""
        self.assertEqual(resource.RLIM_INFINITY, self.limits(0))

    def test_it_never_raises_a_ceiling_somebody_else_set(self) -> None:
        """ "A caller who already sandboxed us meant it." The existing soft limit
        is *below* what is asked, so it must survive untouched."""
        already = self.ASKED // 2
        self.assertEqual(
            already, self.limits(self.ASKED, soft=already, hard=resource.RLIM_INFINITY)
        )

    def test_it_lowers_a_finite_ceiling_rather_than_leaving_it(self) -> None:
        """`min(limit, hard)`. A process already bounded *above* what is asked
        must still come down to what is asked.

        This is the reachable half of that line. The other half -- `hard ==
        RLIM_INFINITY` read as `!=` -- is an **equivalent mutant**, and the
        first draft of this test tried to kill it with a fixture POSIX does not
        permit (`soft` above `hard`, which `setrlimit` refuses outright). Worked
        through: `soft <= hard` always, so when `hard` is infinite both spellings
        give `limit`; when `hard` is finite and `limit <= hard` both give
        `limit`; and when `limit > hard` the original clamps to `hard` and then
        skips because `soft <= ceiling`, while the mutant asks for `limit`, is
        refused with `ValueError`, and is swallowed by the `except`. Every
        reachable state ends with the same rlimit, so no honest fixture can tell
        them apart and none is invented here.
        """
        hard = self.ASKED * 2
        self.assertEqual(self.ASKED, self.limits(self.ASKED, soft=hard, hard=hard))

    def test_a_higher_finite_soft_limit_is_brought_down(self) -> None:
        """The `soft != RLIM_INFINITY and soft <= ceiling` branch. Read with
        `or`, a finite soft limit *above* the ceiling short-circuits the whole
        function and the process keeps the larger allowance."""
        self.assertEqual(
            self.ASKED, self.limits(self.ASKED, soft=self.ASKED * 2, hard=resource.RLIM_INFINITY)
        )


if __name__ == "__main__":
    unittest.main()
