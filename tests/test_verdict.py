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
import os
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


def address_space_caps() -> bool:
    """Whether `RLIMIT_AS` can be set here *and* is applied. Asked by trying.

    Three ways it can be unusable, and this run must tell them apart from a
    working one rather than from each other:

    - `setrlimit` is refused outright (macOS refuses `RLIMIT_AS`);
    - it is accepted and not reflected by `getrlimit`;
    - it is accepted, reflected, and simply not enforced when memory is asked
      for -- which `tools/verdict.py`'s own docstring records CI discovering
      rather than the documentation.

    The first draft of this asked only "did the probe exit non-zero", which is
    true of a refused `setrlimit` as well as of a refused *allocation* -- so on
    macOS it answered "enforced" and let five tests through to fail. A probe
    that cannot tell its own failure from the failure it is probing for is the
    §8 shape in miniature, so this one prints a marker and the caller looks for
    exactly that.

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
            [sys.executable, "-B", "-c", probe], capture_output=True, text=True, timeout=30
        )
    except subprocess.SubprocessError:  # pragma: no cover - a machine in trouble
        return False
    return done.stdout.strip() == "applied"


#: Computed once. The probe forks, and gating two classes on it would otherwise
#: pay for that at import *and* at every decorated method.
CAPS = address_space_caps()


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
        walk: bool = False,
    ) -> dict[str, Any]:
        """Run the tool and return the report it wrote.

        The argv layout is `verdict.main`'s, positionally: report, failfast,
        memory cap, per-test seconds, the space-joined `first` selection,
        whether to walk past the selection, then the test names. Spelled out
        here rather than in each test, because a wrong position is the kind of
        mistake that still produces a plausible report.

        ``walk`` defaults off, which is a *baseline*'s shape. Most tests here are
        about what one named selection reports, and a walk would run this
        repository's whole suite inside each of them.
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
                "1" if walk else "0",
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


@unittest.skipUnless(CAPS, "RLIMIT_AS is not usable here")
class TestAnOutOfMemoryTestIsNotAnAnswer(Probe):
    """The `_carrier` arm that needs the cap *enforced* rather than merely set.

    Its own class so that a runner where `RLIMIT_AS` does not work can name
    it to `--exclude` without losing the three tests beside it in
    `TestACarrierThatDidNotAssert`, which need no such thing. `--no-skips`
    exists to catch a suite quietly doing nothing, so a suite that *cannot*
    run somewhere is named in the workflow rather than opting itself out --
    the convention `tests/test_gitrepo.py`'s non-UTF-8 class already set.
    """

    def test_a_test_that_exhausts_the_cap_is_broken_not_caught(self) -> None:
        """`cap` bounds address space, and a `MemoryError` raised inside a test
        arrives at `addError` looking exactly like an assertion.

        The one test here that needs the limit *enforced* rather than merely
        set, so it is the one that is gated -- see `enforced`. Skipped rather
        than dropped: it is the whole argument for `cap` existing, and it holds
        on Linux, which is where the crash that prompted `cap` happened.
        """
        # A *bounded* allocation, and the bound is what makes this row
        # catchable rather than fatal. `while True` reads better and, with the
        # cap mutated away, walks the lane past its whole memory share in about
        # twenty seconds -- measured: the session was killed, and a killed
        # session says nothing about any mutation. 40 chunks of 8 MiB is 320
        # MiB: it trips a 256 MiB cap after roughly twenty-six of them, and
        # when there is no cap it simply ends, leaving `broke` empty and this
        # test red. So the mutant that disables `cap` fails here instead of
        # taking the run with it.
        #
        # 256 MiB and 40 chunks, not 512 and 100. The claim under test is that
        # an exhausted cap is filed as `broke` rather than as a test noticing,
        # and the size of the cap is not part of it -- the smaller pair reaches
        # the same `MemoryError` having zeroed a quarter of the memory. The
        # floor is the interpreter's own address space, which is tens of MiB
        # here, so 256 keeps an order of magnitude over what must still fit.
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
        found = self.verdict("test_a", memory=256 * 1024 * 1024)
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
        # `test_b`, not `test_a`. The prefix has to name something discovery
        # would reach *second*, or prepending and appending give the same
        # failfast answer and the ordering is unobservable -- measured:
        # building the suite as [chosen, first] instead of [first, chosen]
        # leaves the whole selection green when the prefix is `test_a`.
        found = self.verdict(failfast=True, first="test_b.T.test_it")
        self.assertEqual(1, found["ran"])
        self.assertEqual(["test_b.T.test_it"], found["killers"])

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


@unittest.skipUnless(CAPS, "RLIMIT_AS is not usable here")
class TestTheMemoryCapsArithmetic(Probe):
    """`cap`'s four cases, asserted on the rlimit it sets rather than on a
    runaway allocation dying.

    The existing coverage of `cap` is by *consequence*: something allocates
    until the cap stops it. (woswoar has a `TestAMutantThatEatsMemory` for this;
    an earlier draft of this docstring said *this* project did too, and it does
    not -- nothing in `tests/` mentions `RLIMIT` at all outside this class.)
    Consequence can only be slow or fatal, and it is why two mutants here
    came back `BROKE`
    rather than `caught`: `==` becoming `!=` at the `hard` comparison, and `and`
    becoming `or` at the `soft` one, both leave *no cap in force*, so the
    memory-eating test is unbounded and the harness's alarm speaks first.
    `BROKE` is never `caught`, so the arithmetic was unguarded.

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

    def limits(self, limit: int, soft: int | None = None, hard: int | None = None) -> int:
        """`RLIMIT_AS`'s soft limit after `cap(limit)`, from a child that starts
        from a known state.

        **The child clears any inherited cap first, and that is load-bearing.**
        `verdict.main` calls `cap` before the suite loads, so *during a sweep*
        the process running these tests already holds a finite `RLIMIT_AS` --
        `mutate.MEMORY` is 4 GiB. Without the reset, `limits(0)` reads that back
        instead of `RLIM_INFINITY` and `test_zero_is_no_cap` fails on an
        unmutated tree: every row of a `tools/verdict.py` sweep then prints
        `caught` for a reason that has nothing to do with the mutation, and the
        baseline run voids the lot. Green under a plain `python -m unittest` and
        red under the harness is the worst shape a test in this file can have.

        The raise is permitted because `cap` never lowers `hard` -- it passes
        the existing one straight back to `setrlimit` -- so the ceiling this is
        restoring to is still there.
        """
        # `(hard, hard)`, not `(RLIM_INFINITY, hard)`. macOS reports an
        # unlimited ceiling as `sys.maxsize` rather than as `RLIM_INFINITY`
        # (which is `-1`), so asking for `-1` against that hard limit is
        # "current limit exceeds maximum limit" and the child dies. Raising
        # soft to whatever hard actually says is legal everywhere and clears an
        # inherited cap just as well.
        setup = (
            "hard = resource.getrlimit(resource.RLIMIT_AS)[1]\n"
            "resource.setrlimit(resource.RLIMIT_AS, (hard, hard))\n"
        )
        if soft is not None:
            setup += f"resource.setrlimit(resource.RLIMIT_AS, ({soft}, {hard}))\n"
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

        An earlier draft of this docstring argued at length that `hard ==
        RLIM_INFINITY` read as `!=` is an equivalent mutant. **That was wrong,
        and the sweep had already said so** -- it reports that row `caught`.
        `resource.RLIM_INFINITY` is `-1`, not a large number, so with the
        comparison inverted an infinite `hard` gives `min(limit, -1) == -1` and
        the process is left *uncapped*. The argument assumed infinity sorted
        above every finite limit; the constant is a sentinel, and reading it as
        an ordinary value is how the whole paragraph went wrong.

        The genuinely equivalent one on this pair is `soft <= ceiling` read as
        `soft < ceiling` at the line below, which the sweep does report
        SURVIVED: it changes the answer only when `soft` is exactly `ceiling`,
        and setting a limit to the value it already holds is a no-op either way.
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


class TestTheWalkPastTheSelection(Probe):
    """A mutation's selection is an *ordering*, not a gate: when nothing in it
    notices, the run keeps going through the rest of the suite.

    That is what removed the second pass. Before it, a survivor was re-run
    against the whole suite afterwards -- so it ran its selection and then a
    superset of it, and the narrow run was work thrown away.

    The three claims here are the whole of the change, and each fails without it:
    the walk *reaches* a module the selection never named; it *stops* once
    something notices; and a baseline does not walk at all.
    """

    #: Imported for its side effect, which is the point: a module that has not
    #: been imported cannot have written this. Laziness is not observable from
    #: `ran`, because a module the walk loads but never reaches contributes no
    #: tests to the count either way.
    MARKER = "reached.txt"

    def sandboxed(self, *, selected_notices: bool) -> None:
        """Two modules: one selected, one not, and only the second ever fails.

        `test_beside` records that it was imported at all, so "the walk stopped"
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
            "test_beside",
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
        # `killers`, not `noticed`. The two hold the same test, but `noticed` is
        # the *display* string and its shape changed in 3.11: 3.10 renders
        # `test_it (test_chosen.Chosen)` where later versions render
        # `test_it (test_chosen.Chosen.test_it)`. Asserting on it passed here and
        # turned the `test (3.10)` leg red -- and `verdict.py` says why in the
        # field's own comment: `killers` exists "because `mutate` feeds these
        # straight to a loader and a display format is not an API".
        self.assertEqual(
            ["test_beside.Beside.test_it"],
            found["killers"],
            "the walk did not reach past the selection",
        )

    def test_it_stops_once_the_selection_itself_notices(self) -> None:
        """The cost half, and the one that makes the walk affordable: a caught
        mutation must pay for its selection and nothing more.

        Asserted on the marker rather than on `ran`. Loading all 29 modules of
        this repository measures 621ms against 0-1ms for one, so a walk that
        loaded them eagerly would hand back ~2 min over a 194-row sweep -- more
        than deleting the second pass saves. `ran` cannot see that: an imported
        module whose tests never run adds nothing to the count.
        """
        self.sandboxed(selected_notices=True)
        found = self.verdict("test_chosen", failfast=True, walk=True)
        self.assertEqual(["test_chosen.Chosen.test_it"], found["killers"])
        self.assertFalse(self.reached(), "a module past the answer was imported anyway")

    def test_it_stops_on_a_notice_even_with_failfast_off(self) -> None:
        """The same claim on the path a hand-written table takes.

        `mutate._run_spec` leaves `failfast` off, so `shouldStop` is never set
        and the outer walk has to notice for itself that the answer is already
        in. Without that, every caught row on the spec path becomes a
        whole-suite run -- the cost this design exists to avoid, reintroduced
        on the one path nothing else here covers.
        """
        self.sandboxed(selected_notices=True)
        found = self.verdict("test_chosen", failfast=False, walk=True)
        self.assertEqual(["test_chosen.Chosen.test_it"], found["killers"])
        self.assertFalse(self.reached(), "the walk carried on past its own answer")

    def test_a_baseline_does_not_walk(self) -> None:
        """`walk` is a separate argv slot precisely so this row exists. A
        baseline asks whether *one selection* is green; inferring the walk from
        the selection instead would make every baseline a whole-suite run, and
        the baseline is the check meant to cost nothing.
        """
        self.sandboxed(selected_notices=False)
        found = self.verdict("test_chosen", walk=False)
        self.assertEqual([], found["noticed"], "a baseline widened past its selection")
        self.assertFalse(self.reached(), "a baseline imported a module it was not given")
        self.assertEqual(1, found["ran"])

    def test_a_red_baseline_is_reported_whole(self) -> None:
        """The other half of the condition that bounds the walk, and the half
        that is not about walking at all.

        `_borrow`'s docstring commits to it: *"Never `failfast`: a red baseline
        is a thing you want the whole of."* The walk stops as soon as anything
        notices, so that stop has to be gated on `walk` -- ungated, a baseline
        over several modules reports the first red one and silently skips the
        rest, which is one shard of a broken tree presented as the whole story.

        Reachable only from here today, because `baseline_shards` returns a
        single `WHOLE_SUITE` shard and an empty selection never enters that loop.
        Written anyway: the gate is a claim about what a baseline means, and the
        alternative is a line no fixture can tell from its own deletion.
        """
        self.sandboxed(selected_notices=False)
        found = self.verdict("test_beside", "test_chosen", walk=False)
        self.assertEqual(["test_beside.Beside.test_it"], found["killers"])
        self.assertEqual(2, found["ran"], "a red baseline stopped at its first red module")


class TestWhereTheWalkLooks(unittest.TestCase):
    """`every_module` follows the selection's own package rather than a constant.

    A hardcoded `tests` would be right for this repository and unreachable from
    the flat sandboxes above, so the guard would be one no fixture could drive.

    **Imported here, unlike everything else in this file, and the module
    docstring's reason is why that is allowed rather than an exception to it.**
    What must not run in this process is `collect`: `cap` sets an address-space
    rlimit and the alarm installs a `SIGALRM` handler, so driving it here would
    configure the suite that is running it. `every_module` is a `glob` and a
    string join, and touches neither. The import stays inside the methods so
    that remains true of importing this file as well.
    """

    @staticmethod
    def every(names: list[str]) -> list[str]:
        from tools import verdict

        return verdict.every_module(names)

    def test_it_follows_the_package_the_selection_lives_in(self) -> None:
        found = self.every(["tests.test_sync"])
        self.assertIn("tests.test_verdict", found)
        self.assertNotIn("test_verdict", found, "the package prefix was dropped")

    def test_what_it_returns_is_sorted(self) -> None:
        """The order is the walk's order, and it has to be *stable*: a bare
        `set` iterates by hash, so two runs of the same sweep would try the
        modules in different orders and a row's recorded `killer` would move.

        Eight modules, not two. With two, `list(set(...))` frequently comes out
        sorted by luck and the assertion holds against its own mutation -- which
        is what happened: the sweep could only report this line as `BROKE`,
        because unsorted order made an unrelated test in `tests/test_mutate.py`
        reach its module last and trip the 30s per-test alarm. A `BROKE` is
        never `caught`, so the line read as unguarded. Asserted here instead of
        left to a timing accident.
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
            here = Path.cwd()
            os.chdir(box)
            try:
                found = self.every(["test_alpha"])
            finally:
                os.chdir(here)
        self.assertEqual(sorted(found), found, "the walk order is not stable")
        self.assertEqual(8, len(found))

    def test_a_flat_selection_looks_beside_itself(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tupferl-walk-") as name:
            box = Path(name)
            (box / "test_one.py").write_text("", encoding="utf-8")
            (box / "test_two.py").write_text("", encoding="utf-8")
            (box / "helper.py").write_text("", encoding="utf-8")
            here = Path.cwd()
            os.chdir(box)
            try:
                # `helper.py` is the half that can fail quietly: a glob of `*.py`
                # rather than `test_*.py` walks into support modules, and a
                # loader handed one reports it as broke.
                self.assertEqual(["test_one", "test_two"], self.every(["test_one"]))
            finally:
                os.chdir(here)


if __name__ == "__main__":
    unittest.main()
