"""The parallel runner, and the one failure mode it exists to refuse.

A serial `python -m pytest` cannot be green because nothing ran. A parallel one
can: a batch that dies before it reports leaves a summary with fewer tests in it
than were discovered, and every remaining test passed. `run_tests` checks

    ids discovered == ids reported

and this file is the proof that the check is wired up, driven against a
throwaway tree containing a test that kills its own process. Asserting on `pack`
and `discover` alone would leave the accounting itself -- the whole reason the
script exists -- untested.

Everything that needs a *tree* is driven through a real
`python -m tools.run_tests` in one of its own, `run_batch` included. The rule is
narrower than "never in-process", and stating it precisely matters because one
test here does not obey the wide version: relative nodeids only resolve against
the right working directory, so an in-process run over a throwaway tree would
need an `os.chdir` and would then import from inside it -- which is the mistake
CLAUDE.md records as having made the old verdict layer unmeasurable. A nested
`pytest.main` is merely untidy; the `chdir` is the hazard.

`discover(root=...)` is the seam that makes the one exception safe.
`TestTheWorkflowsExcludesStillNameSomething` collects the **real** tree from the
real working directory, so nothing is chdir'd and nothing is imported that this
process had not already imported.

It does not generalise to a throwaway tree, which is the tempting next step and
does not work: those trees also call their package `tests`, and this process
imported the real one long ago, so a collect over the copy resolves
`tests.test_x` against the original and reports `ModuleNotFoundError`. A second
copy of this tree can only be driven from a second process.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from tests import support
from tools import run_tests
from tools.cpus import usable_cpus

#: How long any `python -m tools.run_tests` started here may take. Every tree in
#: this file holds at most six tests, so a second is generous and twenty is a
#: bound rather than a budget. It has to *beat* `tools/mutate.py`'s per-test
#: alarm: a mutant that leaves the runner blocking is filed `BROKE` otherwise,
#: and `BROKE` is never `caught`, so the line it hangs on is guarded by nothing.
#: Through `support.bounded`, which knows the alarm a sweep actually armed
#: rather than the 30s default `--each-test` can move.
BOUND = support.bounded(20.0)

#: A test module that runs one test and then takes its process down mid-batch,
#: before the report is written. `os._exit` rather than `sys.exit`: the latter
#: raises `SystemExit`, which the runner catches and reports as an ordinary
#: failure, so the batch would still write its report and there would be nothing
#: to detect. pytest catches it in `CallInfo.from_call`, which takes
#: `BaseException`, exactly as `unittest` did before it.
SUICIDE = """
import os
import unittest


class TestOne(unittest.TestCase):
    def test_passes(self):
        self.assertTrue(True)


class TestTwo(unittest.TestCase):
    def test_takes_the_process_with_it(self):
        os._exit(0)
"""

HEALTHY = """
import unittest


class TestHealthy(unittest.TestCase):
    def test_passes(self):
        self.assertTrue(True)

    def test_also_passes(self):
        self.assertEqual(2, 1 + 1)
"""

BROKEN_IMPORT = """
import nothing_by_this_name  # noqa: F401
"""

#: A module with one skipped test in it. **Nothing in this file had one**, so
#: `if skipped:`, the line that prints each skip, `--no-skips` and the argument
#: that defines it were four branches no fixture reached -- and dropping the
#: `--no-skips` argument entirely was invisible, because `args.no_skips` is read
#: only inside the branch nothing entered.
SKIPPING = """
import unittest


class TestSkips(unittest.TestCase):
    @unittest.skip("age is not installed")
    def test_needs_an_optional_tool(self):
        pass

    def test_runs_normally(self):
        self.assertTrue(True)
"""

#: One test that fails on its own assertion, which is different from every other
#: fixture here: `SUICIDE` kills its process and `BROKEN_IMPORT` never loads, so
#: neither reaches the line that *names a failing test*. Nothing in this file
#: had an ordinary red test in it until this existed.
FAILING = """
import unittest


class TestRed(unittest.TestCase):
    def test_asserts_something_untrue(self):
        self.assertEqual(1, 2)
"""


class Tree:
    """A throwaway repository root with its own `tests/` and a copy of the runner.

    A copy rather than a `sys.path` trick: `run_tests` derives its root from
    `__file__` and re-invokes itself as `python -m tools.run_tests`, so the only
    honest way to point it somewhere else is to put it there.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        (path / "tools").mkdir()
        # Every module, not the three `run_tests` imports today. That list was
        # hand-kept, and it went stale the first time the runner gained an
        # import: `tools/paint.py` was added and all four tests in this file
        # went red with `ImportError: cannot import name 'paint'`, from a tree
        # this fixture had built wrong rather than from anything under test.
        # Copying the directory cannot go stale, and the extra files are inert
        # -- discovery looks in `tests/`.
        for module in Path(run_tests.__file__).parent.glob("*.py"):
            shutil.copy(module, path / "tools" / module.name)
        (path / "tests").mkdir()
        (path / "tests" / "__init__.py").write_text("", encoding="utf-8")

    def add(self, name: str, body: str) -> None:
        (self.path / "tests" / name).write_text(body, encoding="utf-8")

    def run_it(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "tools.run_tests", *args],
            cwd=self.path,
            capture_output=True,
            text=True,
            check=False,
            timeout=BOUND,
            # **The opposite of what the real runner does, and for the reason
            # the real runner gives.** `QUIET`'s comment leaves autoload on
            # because that is the developer's own suite and a plugin they
            # installed is theirs to have. These trees hold two hand-written
            # `TestCase`s and no plugin can matter to them -- so autoloading one
            # only makes what this file measures depend on what is installed.
            # Measured on a one-module tree: 0.21 s to 0.12 s per process, and
            # each `run_it` is two to four of them across 33 call sites.
            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )


@pytest.fixture
def tree() -> Iterator[Tree]:
    """One `Tree` per test, removed afterwards however the test ends.

    Through `support.tempdir` rather than pytest's `tmp_path`, which CLAUDE.md
    forbids here: `tmp_path` keeps the last three numbered roots per user, and a
    mutation sweep runs thousands of these as separate processes racing over
    that numbering.
    """
    with support.tempdir(prefix="tupferl-runner-") as box:
        yield Tree(box)


class TestTheAccountingCheck:
    def test_a_healthy_tree_is_green(self, tree: Tree) -> None:
        """The precondition. Without it, the failure below could be the fixture
        being broken rather than the death being detected."""
        tree.add("test_healthy.py", HEALTHY)
        done = tree.run_it("--jobs", "2")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Ran 2 tests" in done.stdout

    def test_a_batch_that_dies_is_not_green(self, tree: Tree) -> None:
        """The whole point: the surviving tests all passed, and the run is red
        anyway, because tests that were discovered never reported."""
        tree.add("test_suicide.py", SUICIDE)
        done = tree.run_it("--jobs", "2")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "never ran" in done.stdout

    def test_the_missing_test_is_named(self, tree: Tree) -> None:
        """A count alone sends the reader to look through the whole suite."""
        tree.add("test_suicide.py", SUICIDE)
        done = tree.run_it("--jobs", "2")
        assert "test_takes_the_process_with_it" in done.stdout

    def test_a_module_that_will_not_import_is_reported_as_such(self, tree: Tree) -> None:
        """Not as a test that failed: nothing ran because nothing was *there*,
        and printing it as an assertion sends the reader looking for one."""
        tree.add("test_broken.py", BROKEN_IMPORT)
        tree.add("test_healthy.py", HEALTHY)
        done = tree.run_it("--jobs", "2")
        assert done.returncode == 1
        assert "could not import tests.test_broken" in done.stdout

    def test_the_cause_reaches_stderr_and_the_node_listing_does_not(self, tree: Tree) -> None:
        """The summary carries one line, so the traceback has to be somewhere --
        and under `unittest` it was nowhere, because `loader.errors` held it and
        only its first line was printed.

        Taken from each failed `CollectReport` rather than from the captured
        `--collect-only` output, and the second assertion is why: ``-q`` prints
        one line per collected test, so dumping that buffer put 1598 node lines
        and 148 KB on stderr around one four-line traceback. Measured, on the
        real suite, before this test existed.
        """
        tree.add("test_broken.py", BROKEN_IMPORT)
        tree.add("test_healthy.py", HEALTHY)
        done = tree.run_it("--jobs", "2")
        assert "ModuleNotFoundError" in done.stderr
        assert "nothing_by_this_name" in done.stderr
        assert "tests/test_healthy.py::" not in done.stderr


class TestTheOrderTheSummaryNamesThings:
    """Two broken modules and two tests that never ran, so ordering is visible.

    Both lists are `sorted`, and with one entry every ordering is the same
    ordering -- the fixture-too-weak shape CLAUDE.md names. A CI log read across
    two runs of the same tree has to be diffable, and a `set`-ordered list moves
    between runs for reasons that have nothing to do with the code.
    """

    def test_broken_modules_are_named_in_a_settled_order(self, tree: Tree) -> None:
        tree.add("test_zzz_broken.py", BROKEN_IMPORT)
        tree.add("test_aaa_broken.py", BROKEN_IMPORT)
        done = tree.run_it("--jobs", "2")
        assert done.returncode == 1, done.stdout + done.stderr
        named = [
            line.split("could not import ", 1)[1].split(":", 1)[0]
            for line in done.stdout.splitlines()
            if "could not import" in line
        ]
        assert named == ["tests.test_aaa_broken", "tests.test_zzz_broken"]

    #: One class whose last test kills the process, so **every** test in it is
    #: unaccounted for. `SUICIDE` cannot do this job: its two tests are two
    #: classes, `pack` puts them in different batches, and only the one that
    #: dies goes missing -- one name, which cannot show an ordering at all.
    #: Five, because `missing` is a *set* difference and Python hashes strings
    #: with a per-run seed: with two names an unsorted list matches the sorted
    #: one about half the time, which is a test that passes at random.
    LOSES_FIVE = (
        "import os\n"
        "import unittest\n"
        "class TestFive(unittest.TestCase):\n"
        "    def test_a(self): pass\n"
        "    def test_b(self): pass\n"
        "    def test_c(self): pass\n"
        "    def test_d(self): pass\n"
        "    def test_e_takes_the_process_with_it(self): os._exit(0)\n"
    )

    def test_tests_that_never_ran_are_named_in_a_settled_order(self, tree: Tree) -> None:
        """The count line above them is asserted too -- it is the only place the
        *number* appears, and nothing read it before."""
        tree.add("test_lost.py", self.LOSES_FIVE)
        done = tree.run_it("--jobs", "1")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "5 discovered tests never ran" in done.stdout
        missing = [
            line.split("never ran: ", 1)[1].strip()
            for line in done.stdout.splitlines()
            if "never ran: " in line
        ]
        assert len(missing) == 5, done.stdout
        assert missing == sorted(missing), "the names are not in a settled order"

    def test_a_batch_that_died_says_so_before_the_accounting(self, tree: Tree) -> None:
        """The `::error::` naming the *cause*. Without it the log carries only
        the consequence -- tests that never ran -- and a reader has to guess
        whether the batch crashed or was never started."""
        tree.add("test_lost.py", self.LOSES_FIVE)
        done = tree.run_it("--jobs", "1")
        assert "batch died without reporting" in done.stdout


class TestWhatTheRunSaysAboutItself:
    """The last line, and the line naming a failing test.

    Both survived the sweep of the change that painted them, and both are the
    shape CLAUDE.md §8 is about: **the exit status is guarded and the words are
    not**, so a run that printed nothing at all and exited 0 passes every other
    test in this file. "Confirm it did the work" needs something to read.
    """

    def test_a_green_run_says_it_is_ok(self, tree: Tree) -> None:
        """The summary line is how a person confirms a green run rather than
        inferring it from silence -- and `$?` is not visible in a CI log
        scrollback."""
        tree.add("test_healthy.py", HEALTHY)
        done = tree.run_it("--jobs", "2")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "OK (0 failures, 0 errors, 0 skipped)" in done.stdout

    def test_a_red_run_says_it_failed_and_counts_it(self, tree: Tree) -> None:
        """The other half, without which the assertion above is satisfied by a
        line that says `OK` unconditionally."""
        tree.add("test_red.py", FAILING)
        done = tree.run_it("--jobs", "2")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "FAILED (1 failures, 0 errors, 0 skipped)" in done.stdout

    def test_a_failing_test_is_named(self, tree: Tree) -> None:
        """Which test failed, not how many. A parallel run interleaves 16
        batches, so a count alone sends the reader through every one of them --
        the same argument `test_the_missing_test_is_named` makes for the
        accounting check, on the path a person actually hits."""
        tree.add("test_red.py", FAILING)
        done = tree.run_it("--jobs", "2")
        assert "FAIL: tests/test_red.py::TestRed::test_asserts_something_untrue" in done.stdout

    def test_a_green_run_names_nothing(self, tree: Tree) -> None:
        """The precondition for the two above: no `FAIL:` line when nothing
        failed. Without it, a version that printed the whole discovered suite
        under `FAIL:` would satisfy them both."""
        tree.add("test_healthy.py", HEALTHY)
        done = tree.run_it("--jobs", "2")
        assert "FAIL:" not in done.stdout


class TestHowManyWorkersAndBatchesARunUses:
    """The two derived counts, read out of the line the run prints about itself.

    Seven mutants survived here -- `usable_cpus() * 2` and `pack(classes, jobs *
    2)` in every arithmetic variation, plus `--jobs`' default. Nothing in the
    file asserted either number, because every test passes `--jobs 2` and then
    reads the *verdict*, which both counts are deliberately invisible to: the
    run is correct at any parallelism, and only slower or less overlapped.

    That is what makes them worth pinning rather than shrugging at. Both numbers
    are measured -- `run_tests` records "jobs=8 beats jobs=4 by ~9%, and jobs=16
    regresses" for the first, and the second exists so a long batch is overlapped
    rather than deciding the wall clock alone -- and a measured constant with no
    test is one an edit silently reverts.

    Read from the printed line rather than by importing `main`: `run_it` drives
    a real subprocess in a tree of its own, which is this file's whole shape.
    """

    #: Two classes, written across three modules below, because the batch count
    #: needs six: `pack` cannot make more batches than there are classes, so
    #: against the two this file's other trees hold, `jobs * 2`, `jobs * 3` and
    #: `jobs * 1` all come back as 2 and the assertion pins nothing. The names
    #: need not vary -- discovery keys a class by its module.
    MANY = (
        "import unittest\n\n\nclass T(unittest.TestCase):\n"
        "    def test_it(self) -> None:\n        self.assertTrue(True)\n"
        "\n\nclass U(unittest.TestCase):\n"
        "    def test_it(self) -> None:\n        self.assertTrue(True)\n"
    )

    def test_the_batch_count_is_twice_the_worker_count(self, tree: Tree) -> None:
        """More batches than workers, so a batch that runs long is overlapped by
        the others rather than deciding the wall clock on its own. Six classes
        and two workers, so four batches is a number only `jobs * 2` gives."""
        for n in range(3):
            tree.add(f"test_many{n}.py", self.MANY)
        done = tree.run_it("--jobs", "2")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "in 4 batches on 2 workers" in done.stdout

    def test_the_default_worker_count_is_twice_the_usable_cpus(self, tree: Tree) -> None:
        """The work is subprocess wait rather than CPU, which is why it is a
        multiple at all. Asserted against `usable_cpus()` rather than a literal:
        the number differs per machine and per CI leg, and a literal here would
        be a test that passes on one runner."""
        tree.add("test_healthy.py", HEALTHY)
        done = tree.run_it()
        assert done.returncode == 0, done.stdout + done.stderr
        assert f"on {usable_cpus() * 2} workers" in done.stdout

    def test_an_explicit_job_count_wins(self, tree: Tree) -> None:
        """`args.jobs or ...`, which needs the default to be falsy. A default of
        1 makes every run single-batched and says nothing -- the run is still
        correct, just serial, which is the failure no verdict can show."""
        tree.add("test_healthy.py", HEALTHY)
        assert "on 3 workers" in tree.run_it("--jobs", "3").stdout


class Batch(NamedTuple):
    """One worker run, from the outside: its report file and its process.

    The process is kept whole rather than unpacked into renamed fields. Every
    other test in this file reads `done.stdout` / `done.stderr` / `.returncode`
    off a `CompletedProcess`, and a second vocabulary for the same two streams
    is one a reader has to come back here to decode.
    """

    written: dict[str, Any]
    done: subprocess.CompletedProcess[str]


def worker(tree: Tree, body: str, *scopes: str) -> Batch:
    """Run `scopes` of a module written from `body`, as a real worker does.

    A missing report file comes back as an empty dict rather than raising,
    because "the worker wrote no report" is a thing two tests here assert
    about and an `==` on it reads better than an exception.
    """
    tree.add("test_it.py", body)
    out = tree.path / "report.json"
    done = tree.run_it(
        "--worker", *(f"tests/test_it.py::{scope}" for scope in scopes), "--out", str(out)
    )
    written = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    return Batch(written, done)


class TestWhatABatchReports:
    """`run_batch`: the JSON a worker leaves behind, which is all the parent knows.

    **Twelve of its thirteen mutants survived, and nothing called it directly.**
    Every test in this file drove `run_tests` end to end, so `run_batch` ran only
    inside a subprocess -- invisible to coverage, and asserted on only through
    whatever the parent printed afterwards.

    That matters because the accounting check this module exists for --
    `ids discovered == ids reported` -- is fed entirely by these files. A batch
    that under-reports its `ran` list makes the parent announce tests that never
    ran; one that over-reports hides a batch that died.

    Still through a subprocess, and now on purpose rather than by accident. The
    worker calls `pytest.main`, so calling `run_batch` in this process would
    start a pytest session inside the pytest session running this test -- and it
    would have to `os.chdir` into the throwaway tree first for the relative
    nodeids to resolve, then import from inside that chdir, which CLAUDE.md
    names as the mistake that made the old verdict layer unmeasurable. Driving
    the real `--worker` arm costs a spawn and answers about the thing that
    ships, including the stream it writes on.
    """

    #: One module holding every shape a batch has to classify. `BadSetup` and
    #: `Subtests` are new here: the first is the only way to reach a
    #: *setup*-phase failure, which is what `errors` means now, and the second
    #: is the only fixture that can show a failed `subTest` is not lost.
    #:
    #: Two of `Subtests`' three subcases fail, deliberately. Both carry the
    #: owning test's nodeid, so one failing subcase would leave `_settled`
    #: unexercised and a summary that named the same test twice would pass.
    PASSES = (
        "import unittest\n"
        "class Green(unittest.TestCase):\n"
        "    def test_one(self): pass\n"
        "    def test_two(self): pass\n"
        "class Red(unittest.TestCase):\n"
        "    def test_fails(self): self.assertEqual(1, 2)\n"
        "    def test_errors(self): raise RuntimeError('boom')\n"
        "class Skipped(unittest.TestCase):\n"
        "    @unittest.skip('a reason worth carrying')\n"
        "    def test_skipped(self): pass\n"
        "class BadSetup(unittest.TestCase):\n"
        "    @classmethod\n"
        "    def setUpClass(cls): raise RuntimeError('the fixture blew up')\n"
        "    def test_needs_the_fixture(self): pass\n"
        "class Subtests(unittest.TestCase):\n"
        "    def test_two_subcases_fail(self):\n"
        "        for n in (1, 2, 3):\n"
        "            with self.subTest(n=n):\n"
        "                self.assertEqual(1, n)\n"
        "class Expected(unittest.TestCase):\n"
        "    @unittest.expectedFailure\n"
        "    def test_expected_to_fail(self): self.assertEqual(1, 2)\n"
        "class Unexpected(unittest.TestCase):\n"
        "    @unittest.expectedFailure\n"
        "    def test_expected_to_fail_but_passes(self): self.assertEqual(1, 1)\n"
    )

    def test_the_worker_says_how_many_tests_it_ran(self, tree: Tree) -> None:
        """Its own count, in its own log. The parent counts from the `ran` list
        in the JSON, so a worker that printed nothing at all would satisfy every
        other assertion in this class and leave a CI log with no evidence in it
        that the batch did anything."""
        assert "2 passed" in worker(tree, self.PASSES, "Green").done.stderr

    def test_a_traceback_reaches_the_workers_own_stderr(self, tree: Tree) -> None:
        """Where it interleaves with the tests' output and a human reads it in
        context. The summary carries only ids."""
        said = worker(tree, self.PASSES, "Red").done.stderr
        assert "RuntimeError: boom" in said
        assert "test_fails" in said

    def test_the_worker_writes_nothing_to_stdout(self, tree: Tree) -> None:
        """**The parent's stdout is the summary channel**, and a hundred-odd
        batches' progress interleaved into it would bury the four lines a reader
        is looking for. That is what `redirect_stdout` in `run_batch` buys, and
        without this assertion dropping it changes nothing any other test here
        can see -- the traceback is still *somewhere*, and a substring test on
        the combined output would still hold.
        """
        got = worker(tree, self.PASSES, "Red")
        assert got.done.stdout == ""
        assert "RuntimeError: boom" in got.done.stderr

    def test_a_batch_where_everything_passed_exits_zero(self, tree: Tree) -> None:
        """The precondition for every status below: a worker that always
        reported failure would satisfy all of them."""
        assert worker(tree, self.PASSES, "Green").done.returncode == 0

    def test_a_batch_with_a_failing_test_does_not_exit_zero(self, tree: Tree) -> None:
        assert worker(tree, self.PASSES, "Red").done.returncode == 1

    def test_a_skip_is_not_a_failure_here(self, tree: Tree) -> None:
        """`--no-skips` is the parent's decision and it needs the ids to make
        it, so a worker that called a skip red would take that choice away."""
        assert worker(tree, self.PASSES, "Skipped").done.returncode == 0

    def test_a_module_that_will_not_import_makes_the_batch_red(self, tree: Tree) -> None:
        """**The `and not unloadable` half, and it cannot be seen from the
        failures.** The tests that actually ran all passed -- there were none --
        and a batch reporting success for a module that never loaded is the
        green run of nothing this script exists to refuse, one level down.
        """
        got = worker(tree, BROKEN_IMPORT, "Anything")
        assert got.written["unloadable"], "the fixture imported after all"
        assert got.written["failures"] == [], "nothing *failed*; the module never loaded"
        assert got.done.returncode == 1
        # The whole traceback, once, where a human reads it in context -- the
        # summary carries only the line that says what went wrong.
        assert "ModuleNotFoundError" in got.done.stderr
        assert "nothing_by_this_name" in got.done.stderr

    def test_every_test_it_ran_is_named(self, tree: Tree) -> None:
        """The list the accounting check subtracts from. A batch that reports
        fewer ids than it ran makes the parent announce tests that never ran --
        which is the false alarm, where the missing half is the real one."""
        said = worker(tree, self.PASSES, "Green").written
        assert sorted(said["ran"]) == [
            "tests/test_it.py::Green::test_one",
            "tests/test_it.py::Green::test_two",
        ]

    def test_a_test_whose_fixture_died_is_still_named_as_having_started(self, tree: Tree) -> None:
        """**The half that moved with the runner, and it is an improvement worth
        pinning.** `unittest` reported a failing `setUpClass` as one synthetic
        `setUpClass (module.Class)` id and never started the tests under it, so
        each of them surfaced in the parent as "never ran" -- red for the wrong
        reason, under a name nobody can act on. pytest starts every test in the
        class and files a `setup` error against its real nodeid, so the
        accounting check is satisfied by ids a reader can run.
        """
        said = worker(tree, self.PASSES, "BadSetup").written
        assert said["ran"] == ["tests/test_it.py::BadSetup::test_needs_the_fixture"]

    def test_a_failure_and_a_broken_fixture_are_kept_apart(self, tree: Tree) -> None:
        """Two different things, and `main` prints them under different labels
        so a reader knows whether to look at the test or at what set it up.

        **The line between them moved with the runner.** `unittest` split by
        exception type -- `AssertionError` against everything else -- so
        `test_errors` raising `RuntimeError` was an "error" while asserting
        1 == 2 was a "failure", though both are the test body saying no. The
        split is by *phase* now: both of `Red`'s tests are failures, and what
        `errors` holds is the class whose fixture never let its test run.
        """
        said = worker(tree, self.PASSES, "Red", "BadSetup").written
        assert sorted(said["failures"]) == [
            "tests/test_it.py::Red::test_errors",
            "tests/test_it.py::Red::test_fails",
        ]
        assert said["errors"] == ["tests/test_it.py::BadSetup::test_needs_the_fixture"]

    def test_a_failing_subtest_is_reported_once_and_not_lost(self, tree: Tree) -> None:
        """**A batch whose only failure is a subcase would otherwise be green.**

        On pytest 9 a failed `subTest` arrives as a `SubtestReport` and the
        owning test's own `call` report reads `passed` -- the behaviour
        pyproject.toml's version floor is there for. A recorder that skipped
        every report carrying a `context`, which is the obvious way to avoid
        counting a subcase twice, would file no failure at all: the JSON would
        say the batch passed, `run_batch` would exit 0, and only pytest's own
        exit status would disagree with it.

        Named *once* for two failing subcases, which is the other half: both
        reports carry the owning test's nodeid.
        """
        got = worker(tree, self.PASSES, "Subtests")
        assert got.written["failures"] == ["tests/test_it.py::Subtests::test_two_subcases_fail"]
        assert got.done.returncode == 1

    def test_an_expected_failure_is_neither_a_pass_nor_a_skip(self, tree: Tree) -> None:
        """**Nothing in this suite uses xfail yet, which is why the rule needs a
        test rather than only a docstring**: an unexercised branch is one an
        edit reverts silently, and the first real use would then inherit
        whatever it had drifted to.

        `unittest.expectedFailure` is the stdlib spelling and pytest reports it
        as a `skipped` report carrying `wasxfail` -- so without the guard it
        lands in `skipped`, and a leg running `--no-skips` goes red over a test
        that behaved exactly as it was declared to.
        """
        got = worker(tree, self.PASSES, "Expected")
        assert got.written["ran"] == ["tests/test_it.py::Expected::test_expected_to_fail"]
        assert got.written["skipped"] == [], "an expected failure is not a skip"
        assert got.written["failures"] == [], "an expected failure is not a failure"
        assert got.done.returncode == 0

    def test_an_expected_failure_that_passes_is_a_failure(self, tree: Tree) -> None:
        """The other half of the same rule, and the reason it is safe to leave
        the first one as quietly as it is: a test declared to fail and passing
        is a claim that has stopped being true, and `unittest.expectedFailure`
        is strict, so pytest files it as an ordinary `call` failure. Without
        this the class is satisfied by a recorder that ignores xfail entirely.
        """
        got = worker(tree, self.PASSES, "Unexpected")
        assert got.written["failures"] == [
            "tests/test_it.py::Unexpected::test_expected_to_fail_but_passes"
        ]
        assert got.done.returncode == 1

    def test_a_skip_carries_its_reason(self, tree: Tree) -> None:
        """The reason is what `--no-skips` exists to make loud, and pytest
        prefixes it with `Skipped: ` on the way through -- so a summary that
        passed the rendering along verbatim would put that marker on every skip
        line in the log."""
        said = worker(tree, self.PASSES, "Skipped").written
        assert said["skipped"] == [
            ["tests/test_it.py::Skipped::test_skipped", "a reason worth carrying"]
        ]

    def test_a_module_that_will_not_import_is_reported_and_not_run(self, tree: Tree) -> None:
        """Discovery in the parent sets unloadable modules aside, and this is
        the belt to that brace: a module can import there and not here. What is
        named is the *module*, dotted, so `--only` and `--exclude` reach it."""
        said = worker(tree, BROKEN_IMPORT, "Anything").written
        assert list(said["unloadable"]) == ["tests.test_it"]
        assert said["ran"] == []

    def test_a_green_batch_reports_nothing_wrong(self, tree: Tree) -> None:
        """The precondition. Without it every assertion above is satisfied by a
        report that lists everything under every key."""
        said = worker(tree, self.PASSES, "Green").written
        assert said["failures"] == []
        assert said["errors"] == []
        assert said["skipped"] == []
        assert said["unloadable"] == {}

    def test_a_worker_that_cannot_be_believed_writes_no_report(self, tree: Tree) -> None:
        """**Exit statuses 3 and 4 fire no hook at all**, so a batch naming a
        scope the tree does not have looks, through the hooks, exactly like a
        batch that legitimately held no tests: `ran` empty, nothing failed,
        exit 0, and the parent then reports nothing wrong.

        `run_batch` reads the status and declines to write a report it cannot
        stand behind, which puts the batch on the parent's "died without
        reporting" path -- where every one of its ids comes back as never-run.
        """
        tree.add("test_it.py", self.PASSES)
        out = tree.path / "report.json"
        done = tree.run_it("--worker", "tests/test_it.py::NoSuchClass", "--out", str(out))
        assert done.returncode == 1, done.stdout + done.stderr
        assert not out.exists(), "a report was written for a run that never happened"
        assert "USAGE_ERROR" in done.stderr


class TestAPytestNativeModule:
    """The claim Phase A2 exists to make true, driven rather than asserted.

    **Nothing here drove one until this class, and that is exactly why the bug
    below survived the phase that introduced it.** Every other fixture in this
    file is a `unittest.TestCase`, so no batch was ever handed a module *scope*
    -- a test outside any class is what packs under the file -- and the one
    shape that breaks is a file holding both.

    Measured, before the dispatch was changed: a file of only functions is
    green, a file of only classes is green, and one file with both reports
    `::error::1 tests ran more than once` and exits 1. Handing a worker the
    scope name `tests/test_native.py` tells pytest to run the whole file, so
    the class in it runs in that batch *and* in its own.

    Phase B converts 33 modules to this shape. Its first mixed one would have
    found it, at the cost of a session spent suspecting the conversion.

    **Driven end to end, and there is no in-process shortcut for it.** Calling
    `discover(tree.path)` to assert the two scopes directly looks safe -- the
    root is a parameter, so nothing is chdir'd -- and it is not: the throwaway
    tree's package is also called `tests`, which this process imported long ago,
    so pytest resolves `tests.test_native` against the *real* one and reports
    `ModuleNotFoundError`. `discover(root=...)` is a seam for pointing at a real
    tree, which is what `TestTheWorkflowsExcludesStillNameSomething` does; it is
    not one for pointing at a second copy of this one.
    """

    #: Deliberately all three shapes at once, and pytest-native throughout:
    #: a bare function, a `parametrize` whose ids carry `[...]`, and a plain
    #: class that is not a `TestCase`. Five tests in two scopes.
    NATIVE = (
        "import pytest\n"
        "\n"
        "\n"
        "def test_a_plain_function():\n"
        "    assert True\n"
        "\n"
        "\n"
        '@pytest.mark.parametrize("n, doubled", [(1, 2), (2, 4), (3, 6)])\n'
        "def test_a_parametrized_case(n, doubled):\n"
        "    assert n * 2 == doubled\n"
        "\n"
        "\n"
        "class TestBesideThem:\n"
        "    def test_in_a_class(self):\n"
        "        assert True\n"
    )

    @pytest.fixture(autouse=True)
    def _written(self, tree: Tree) -> None:
        tree.add("test_native.py", self.NATIVE)

    def test_every_test_runs_exactly_once(self, tree: Tree) -> None:
        """The count is the assertion, not the exit status alone: five tests in
        two scopes, and the class's one must not be run by both."""
        done = tree.run_it("--jobs", "2")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Ran 5 tests" in done.stdout
        assert "ran more than once" not in done.stdout
        assert "never ran" not in done.stdout

    def test_only_reaches_it_by_its_module_name(self, tree: Tree) -> None:
        """`--only` speaks dotted names and a module scope is a bare file, so
        this is the one selector shape `dotted` has to get right for a
        pytest-native module -- and it takes the class in the same file with it,
        because `selects` is anchored at a dot."""
        done = tree.run_it("--only", "tests.test_native")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Ran 5 tests" in done.stdout

    def test_a_failing_parametrized_case_is_named_with_its_parameters(self, tree: Tree) -> None:
        """Which case failed, not which function. A parametrized id is also the
        first thing in this tree that can contain a space, which is why
        `--worker` takes a list and nothing joins these into a string."""
        tree.add(
            "test_native.py",
            "import pytest\n"
            "\n"
            "\n"
            '@pytest.mark.parametrize("word", ["fine", "not fine"])\n'
            "def test_each_word(word):\n"
            '    assert word == "fine"\n',
        )
        done = tree.run_it("--jobs", "2")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "FAIL: tests/test_native.py::test_each_word[not fine]" in done.stdout


class TestTheWaysARunIsRefusedRatherThanRunEmpty:
    """Every filter that could select nothing, and the refusal it earns instead.

    These are the guards §8 is about. A selector that matches nothing reports
    "Ran 0 tests" and exits 0 -- indistinguishable in a CI log from a suite that
    passed -- and each of them is reachable from a typo in a workflow file that
    nobody would ever see fail. `main` refuses instead, and until now nothing
    asserted that: the *exit status* of the happy path was covered and none of
    these branches was.

    Each refusal has its accepting twin in the same class, because a `main` that
    refused every selector would satisfy the refusals on their own and make the
    tool unusable in exactly the jobs these flags exist for.
    """

    @pytest.fixture(autouse=True)
    def _written(self, tree: Tree) -> None:
        tree.add("test_healthy.py", HEALTHY)

    def test_only_that_matches_nothing_is_refused(self, tree: Tree) -> None:
        done = tree.run_it("--only", "tests.test_nothing_like_this")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "no test scope matches" in done.stdout

    def test_only_that_matches_still_runs(self, tree: Tree) -> None:
        done = tree.run_it("--only", "tests.test_healthy")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Ran 2 tests" in done.stdout

    def test_exclude_that_matches_nothing_is_refused(self, tree: Tree) -> None:
        """One pattern at a time, and each has to match something. Checking only
        that the set shrank would let a renamed class stop being excluded as
        long as some *other* pattern still matched -- and the job that needs
        this passes two, so that is the likely case rather than the exotic one.
        """
        done = tree.run_it("--exclude", "tests.test_healthy.TestHealthy", "--exclude", "TestGone")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "TestGone" in done.stdout

    def test_exclude_that_matches_takes_the_class_out(self, tree: Tree) -> None:
        """The twin, and it names what it kept: a run that excluded nothing also
        exits 0, so the status alone says nothing here."""
        tree.add("test_second.py", HEALTHY.replace("TestHealthy", "TestSecond"))
        done = tree.run_it("--exclude", "tests.test_healthy.TestHealthy")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Ran 2 tests" in done.stdout

    def test_excluding_the_last_class_is_refused_rather_than_run_empty(self, tree: Tree) -> None:
        """**Found by writing the tests above, and fixed in this change.**

        Every pattern matched, so the loop above is satisfied -- and the run then
        packed one *empty* batch, spawned a worker with no names, watched
        argparse refuse it, printed `::error::batch died without reporting` and
        **exited 0**. An annotated error on a green job, which is the exact
        failure this whole script exists to refuse.
        """
        done = tree.run_it("--exclude", "tests.test_healthy.TestHealthy")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "nothing would run" in done.stdout
        assert "died without reporting" not in done.stdout

    def test_a_selection_matching_only_a_broken_module_still_reports_it(self, tree: Tree) -> None:
        """The `not unloadable` half of that guard, which nothing else reaches.

        A module that will not import has no classes, so "nothing would run" is
        *true* of it -- and saying that instead sends the reader looking for a
        filter that is wrong when what is wrong is the module. The run is red
        either way, which is why the assertion is on the words: without this,
        dropping `and not unloadable` passes every other test here.
        """
        tree.add("test_broken.py", BROKEN_IMPORT)
        done = tree.run_it("--only", "tests.test_broken")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "could not import tests.test_broken" in done.stdout
        assert "nothing would run" not in done.stdout
        # And nothing about a batch dying, which is what this printed on both
        # runners until `main` stopped packing a batch for an empty selection:
        # an `::error::` about a crash that never happened, directly above the
        # import failure the reader is meant to act on.
        assert "died without reporting" not in done.stdout
        assert "in 0 batches" in done.stdout

    def test_a_malformed_shard_is_refused(self, tree: Tree) -> None:
        """**One spec, not four.** What only the CLI can show is the wiring --
        `shard_of` raising, `main` catching it, `::error::` printed, exit 1 --
        and one spec shows all of it. The parse table itself belongs where it
        costs nothing: `TestShardSpecs` runs six specs in-process, and each
        subprocess here is a fresh interpreter discovering a tree (measured:
        ~64 ms) to re-prove what a function call already proved."""
        done = tree.run_it("--shard", "one/two")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "--shard wants I/N" in done.stdout

    def test_an_empty_selection_is_blamed_on_the_filter_that_emptied_it(self, tree: Tree) -> None:
        """The refusal has to come *before* the shard check, or the shard check
        answers first about a selection it did not empty.

        Measured before the guard moved: `--exclude` removing the last class
        together with `--shard 1/2` reported "`--shard 1/2` wants more shards
        than there are classes" -- true, and it sends the reader to the matrix
        when the exclude list is what did it. The whole point of hoisting the
        check out of the individual filters is that the answer must not depend
        on which one ran.
        """
        done = tree.run_it("--exclude", "tests.test_healthy.TestHealthy", "--shard", "1/2")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "nothing would run" in done.stdout
        assert "more shards than there are test scopes" not in done.stdout

    def test_more_shards_than_scopes_is_refused(self, tree: Tree) -> None:
        """The one that would otherwise be silent. An empty shard reports "Ran 0
        tests" and exits 0 -- the partial run that is green because nothing
        happened -- and it can only come from a matrix that outgrew the suite,
        so every shard would go on being green as classes were removed."""
        done = tree.run_it("--shard", "1/9")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "more shards than there are test scopes" in done.stdout

    def test_a_collect_that_cannot_be_believed_is_refused_rather_than_read(
        self, tree: Tree
    ) -> None:
        """**Statuses 3 and 4 fire no hook at all**, so through the hooks alone
        a collect that blew up half way is indistinguishable from one that went
        fine. Measured on this exact fixture: a `conftest.py` hook that raises
        collects **one item, reports no failure, and exits INTERNAL_ERROR** --
        so a `discover` reading only the hooks hands back one scope, the run
        goes green having run one test, and the tests the collect never reached
        are never missed. That is the partial green run this script exists to
        refuse, moved one level up into discovery itself.

        The traceback is asserted separately from the summary line, because it
        is the only place the *cause* exists: pytest writes `INTERNALERROR>` to
        its own stdout, which discovery captures, and the captured buffer is
        dumped only on this path -- an ordinary broken module gets its own
        four-line traceback instead of 1600 node lines.
        """
        tree.add("test_second.py", HEALTHY.replace("TestHealthy", "TestSecond"))
        (tree.path / "conftest.py").write_text(
            "def pytest_collection_modifyitems(items):\n"
            "    raise RuntimeError('a plugin blew up')\n",
            encoding="utf-8",
        )
        done = tree.run_it("--jobs", "2")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "INTERNAL_ERROR" in done.stdout
        assert "OK (" not in done.stdout
        assert "INTERNALERROR" in done.stderr
        assert "a plugin blew up" in done.stderr

    def test_a_broken_module_does_not_excuse_a_collect_that_blew_up(self, tree: Tree) -> None:
        """**A hole, found by review, and this fixture is what proves it was
        one.** Measured with the guard reverted: `exit 0`, `OK (0 failures, 0
        errors, 0 skipped)` -- over a collect that raised.

        `_unexplained` used to treat *anything* in `unloadable` as accounting
        for *any* exit status. So a broken module excused an `INTERNAL_ERROR`
        raised by something else entirely, discovery handed its scope map back
        as though nothing had happened, and `--only` on an unrelated module then
        filtered the single piece of red evidence away.

        **The `> 3` is the fixture, not a magic number.** It is what makes the
        parent and the batch see different things: the parent collects four
        tests and the hook fires, the batch collects the two of
        `tests.test_healthy` and it does not -- so the batch is genuinely,
        honestly green and nothing downstream can notice. A hook that raised
        unconditionally would take the worker down too, and the accounting check
        would catch it for the wrong reason, which reads exactly like the right
        one and is how this stayed invisible.

        The two statuses are not interchangeable, and that is what the split
        rests on: only a *batch* names scopes, so only a batch can reach the
        `USAGE_ERROR` a broken module legitimately explains. Discovery names the
        root and nothing else, so for it `INTERRUPTED` is the whole list.
        """
        tree.add("test_second.py", HEALTHY.replace("TestHealthy", "TestSecond"))
        tree.add("test_broken.py", BROKEN_IMPORT)
        (tree.path / "conftest.py").write_text(
            "def pytest_collection_modifyitems(items):\n"
            "    if len(items) > 3:\n"
            "        raise RuntimeError('a plugin blew up')\n",
            encoding="utf-8",
        )
        done = tree.run_it("--only", "tests.test_healthy")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "INTERNAL_ERROR" in done.stdout
        assert "OK (" not in done.stdout

    def test_a_shard_that_fits_runs_its_share(self, tree: Tree) -> None:
        """Without this the class is satisfied by a `main` that refuses every
        `--shard`, which is the flag CI splits the suite with."""
        tree.add("test_second.py", HEALTHY.replace("TestHealthy", "TestSecond"))
        done = tree.run_it("--shard", "1/2")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "Ran 2 tests" in done.stdout

    def test_the_shards_together_run_every_test(self, tree: Tree) -> None:
        """The property the `macos` matrix rests on, and the one no single
        shard can show.

        `--shard 1/4` is green whatever the other three did, so a split that
        dropped a scope -- or handed one to two shards and another to none --
        would be four green legs over three quarters of the suite. That is
        exactly the run that is green because nothing happened, arriving
        through the flag added to spread the work out.

        `pack` already has `test_every_scope_is_placed_exactly_once`, and this
        is not that test again: that one asks the function, this one asks four
        real `main` invocations, which is what CI runs. The counts are summed
        rather than the ids unioned because "Ran N tests" is what a shard
        prints; a duplicated scope would have to be paid for by a dropped one
        to keep the sum, and `pack`'s own test is what rules that out.
        """
        for number in range(2, 5):
            tree.add(f"test_{number}.py", HEALTHY.replace("TestHealthy", f"Test{number}"))
        whole = tree.run_it()
        assert whole.returncode == 0, whole.stdout + whole.stderr
        assert "Ran 8 tests" in whole.stdout

        counted = 0
        for shard in range(1, 5):
            done = tree.run_it("--shard", f"{shard}/4")
            assert done.returncode == 0, done.stdout + done.stderr
            found = re.search(r"Ran (\d+) tests", done.stdout)
            assert found is not None, done.stdout
            ran = int(found.group(1))
            assert ran > 0, f"shard {shard}/4 ran nothing:\n{done.stdout}"
            counted += ran
        assert counted == 8


class TestReadingACollectionFailure:
    """`_stated`: a rendered collection failure cut down to the summary line.

    pytest renders one as a small traceback. The *first* line is the file and
    line number, which is what a reader would reach for and is the less useful
    half; the last is the exception, prefixed `E   ` the way it is printed.

    Under `unittest` this line read "Failed to import test module: tests.test_x"
    -- the module named twice and the cause not at all -- so a reader who wanted
    to know *why* had nowhere to go. Every other assertion in this file matches
    a substring of what gets printed, which holds against several wrong cuts.
    """

    RENDERED = (
        "ImportError while importing test module '/tmp/x/tests/test_broken.py'.\n"
        "Hint: make sure your test modules/packages have valid Python names.\n"
        "Traceback:\n"
        "tests/test_broken.py:1: in <module>\n"
        "    import nothing_by_this_name  # noqa: F401\n"
        "E   ModuleNotFoundError: No module named 'nothing_by_this_name'\n"
    )

    def test_the_cause_is_kept_rather_than_the_file_and_line(self) -> None:
        """Asserted exactly, not by `in`: this whole string is what the parent
        prints after "could not import <module>: ", so a cut that left the
        traceback in would put five lines on one summary line and every
        substring assertion elsewhere in this file would still pass."""
        assert (
            run_tests._stated(self.RENDERED)
            == "ModuleNotFoundError: No module named 'nothing_by_this_name'"
        )

    def test_a_line_that_merely_starts_with_an_e_keeps_it(self) -> None:
        """`removeprefix("E")` is the obvious spelling and it turns a line
        beginning "Errno" into one beginning "rrno" -- quietly, in the one
        message a reader has to act on. The marker is a word, so it is matched
        as one.

        **Three lines, and it took two sweeps to get there.** With one line,
        `spoken[-1]` and `spoken[0]` are the same line; with two, `spoken[-1]`
        and `spoken[1]` are. Each time the fixture grew, the sweep reported the
        *next* index mutation surviving -- the symmetric fixture CLAUDE.md §2
        names, twice in a row, one subscript apart. Three lines is the first
        length at which no other index gives this answer.
        """
        assert (
            run_tests._stated(
                "tests/test_x.py:1: in <module>\n    raise OSError(2)\nErrno 2: no such file"
            )
            == "Errno 2: no such file"
        )

    def test_a_skip_that_did_not_render_as_a_tuple_is_handed_back_whole(self) -> None:
        """`_why`'s guard, driven directly because nothing pytest produces
        reaches it -- and an untested defensive branch is one an edit reverts
        silently. All three of its mutations were reported surviving.

        **The three-character string is the fixture, not a filler.** `isinstance`
        and `len(...) == 3` are joined with `and`; swap it for `or` and a
        *non-tuple* of length three takes the branch, so `longrepr[2]` hands
        back one character of it. The longer string beside it cannot see that,
        because both connectives answer False for it.
        """
        assert run_tests._why("no reason recorded") == "no reason recorded"
        assert run_tests._why("bad") == "bad"

    def test_a_rendering_with_nothing_in_it_still_says_something(self) -> None:
        """`::error::could not import tests.test_x: ` with nothing after the
        colon reads like a bug in this tool rather than like a broken module."""
        assert run_tests._stated("\n  \n") == "collection failed and said nothing"


class TestTheTwoSpellingsOfAScope:
    """`dotted`: the one place a nodeid meets the dotted name a flag is written in.

    `--only` and `--exclude` were written before pytest and ci.yml passes six of
    them, so the translation is load-bearing rather than cosmetic: a scope that
    came out wrong would make an `--exclude` match nothing, and `main` refuses a
    pattern that matches nothing -- which turns the macOS leg red rather than
    silently running a suite that cannot run there. That is the failure this can
    have, and it is the loud one.
    """

    def test_a_class_node_becomes_a_module_and_a_class(self) -> None:
        assert run_tests.dotted("tests/test_sync.py::TestX") == "tests.test_sync.TestX"

    def test_a_module_node_loses_its_suffix_and_nothing_else(self) -> None:
        """The scope a plain function packs under, which is what Phase B will
        produce -- and a trailing dot from an empty `::` half would stop
        `--only tests.test_sync` matching it."""
        assert run_tests.dotted("tests/test_sync.py") == "tests.test_sync"

    def test_every_segment_of_a_test_node_survives(self) -> None:
        assert (
            run_tests.dotted("tests/test_sync.py::TestX::test_y") == "tests.test_sync.TestX.test_y"
        )

    def test_a_nested_directory_becomes_nested_dots(self) -> None:
        """A single `replace` rather than `Path.parts`, so this is what pins
        that every separator is translated and not just the last."""
        assert run_tests.dotted("a/b/c/test_x.py") == "a.b.c.test_x"


#: Every `--exclude` ci.yml's macOS leg passes, read once at import so
#: `parametrize` below has the cases at collection time. A computed list is the
#: shape §2's zero-iteration trap takes under pytest -- an empty one collects no
#: cases at all -- so `test_there_are_excludes_to_check` asserts the count.
EXCLUDES = [
    line.split("--exclude", 1)[1].strip()
    for line in (run_tests.ROOT / ".github" / "workflows" / "ci.yml")
    .read_text(encoding="utf-8")
    .splitlines()
    if "--exclude" in line and not line.lstrip().startswith("#")
]


class TestTheWorkflowsExcludesStillNameSomething:
    """Every `--exclude` in ci.yml, against the scopes this tree really has.

    The macOS leg names six classes it cannot run, and `main` refuses a pattern
    that matches nothing -- so a class renamed without touching the workflow
    turns that leg red. Red is the right answer, and *finding out on the runner*
    is not: this is the same check, here, where it costs one collect.

    It reads the workflow rather than a copy of the list, because a copy is the
    thing that goes stale. Driving `discover` rather than grepping for `class`
    is what makes it a test of the translation as well as of the names.
    """

    def test_there_are_excludes_to_check(self) -> None:
        """The companion to the parametrize below, and not a formality: a
        `wanted` that came back empty would parametrize over nothing, every case
        would pass by not existing, and the whole class would go green on a
        workflow file it had failed to read."""
        assert len(EXCLUDES) == 6, EXCLUDES

    @pytest.mark.parametrize("pattern", EXCLUDES)
    def test_every_exclude_names_at_least_one_scope(self, pattern: str) -> None:
        named = {run_tests.dotted(scope) for scope in run_tests.discover().scopes}
        matched = [name for name in named if run_tests.selects(name, pattern)]
        assert len(matched) == 1, f"{pattern} matches {matched}"


class TestWhatASkipCosts:
    """A skipped test is green by default and red under `--no-skips`.

    The flag exists for the CI legs that install every optional tool: there, a
    skip means the tool is missing rather than absent by design, and a run that
    quietly skipped half the suite is the partial green run this script refuses
    everywhere else.

    Nothing here had a skipped test before, so the branch, both its prints, and
    the argument that defines the flag were unreachable together -- and the last
    of those is only readable *from inside* the branch, which is why deleting
    the `add_argument` call survived.
    """

    @pytest.fixture(autouse=True)
    def _written(self, tree: Tree) -> None:
        tree.add("test_skipping.py", SKIPPING)

    def test_a_skip_is_green_by_default_and_says_so(self, tree: Tree) -> None:
        done = tree.run_it("--jobs", "2")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "skipped: " in done.stdout
        assert "age is not installed" in done.stdout

    def test_the_same_run_is_red_under_no_skips(self, tree: Tree) -> None:
        done = tree.run_it("--jobs", "2", "--no-skips")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "an optional tool is missing" in done.stdout

    def test_a_suite_with_no_skips_is_green_under_the_flag(self, tree: Tree) -> None:
        """The other half. Without it the class is satisfied by a `--no-skips`
        that fails every run, which is the flag CI's fullest leg uses."""
        tree.add("test_skipping.py", HEALTHY)
        done = tree.run_it("--jobs", "2", "--no-skips")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "skipped: " not in done.stdout


class TestPacking:
    """`pack` decides both splits -- scopes over processes, and shards over
    machines -- so it is worth its own tests without a suite to run."""

    def test_the_heaviest_scope_is_scheduled_first(self) -> None:
        """Largest-first onto the lightest batch. The weights are 5, 3 and 2 so
        that a wrong rule produces a visibly different answer: two batches, and
        the only balanced split is [5] and [3, 2].

        **Inserted lightest-first, and that is the fixture.** Written `a, b, c`
        -- which is already the order the sort produces -- dropping the `sorted`
        entirely gives the same answer, so the test passed against a `pack` that
        did not sort at all. Measured: that mutant survived until this line was
        reordered.
        """
        scopes = {"c": ["x"] * 2, "b": ["x"] * 3, "a": ["x"] * 5}
        batches = run_tests.pack(scopes, 2)
        assert [sorted(batch) for batch in batches] == [["a"], ["b", "c"]]

    def test_nothing_to_pack_needs_no_batches(self) -> None:
        """No scopes, no batches -- and `[[]]`, which this used to pin, was a
        wrong answer its only caller had to undo.

        A batch with nothing in it starts an interpreter, hands argparse no
        names, and is reported as `::error::batch died without reporting`. The
        selection that reaches here is real -- a `--only` matching nothing but a
        module that will not import -- so the case matters; see
        `test_a_selection_matching_only_a_broken_module_still_reports_it`."""
        assert run_tests.pack({}, 3) == []

    def test_no_batch_is_empty(self) -> None:
        """An empty batch starts an interpreter to run nothing."""
        batches = run_tests.pack({"a": ["one"]}, 8)
        assert batches == [["a"]]

    def test_every_scope_is_placed_exactly_once(self) -> None:
        scopes = {f"c{n}": ["x"] * (n % 4 + 1) for n in range(20)}
        placed = [name for batch in run_tests.pack(scopes, 6) for name in batch]
        assert sorted(placed) == sorted(scopes)
        assert len(set(placed)) == len(placed)


class TestShardSpecs:
    def test_a_valid_spec_is_zero_based(self) -> None:
        assert run_tests.shard_of("1/3") == (0, 3)
        assert run_tests.shard_of("3/3") == (2, 3)

    def test_one_shard_of_one_is_the_ordinary_case_and_is_valid(self) -> None:
        """`--shard 1/1` is what a matrix of one leg passes, and every other
        test here uses N=3 -- so a bound that rejected N=1 passed all of them.
        Both `count < 1` becoming `count < 2` and `<` becoming `<=` refuse
        exactly this spec and nothing else in the file."""
        assert run_tests.shard_of("1/1") == (0, 1)

    @pytest.mark.parametrize("spec", ["0/3", "4/3", "1/0", "one/three", "3", ""])
    def test_out_of_range_and_malformed_specs_are_refused(self, spec: str) -> None:
        with pytest.raises(ValueError):
            run_tests.shard_of(spec)

    @pytest.mark.parametrize("spec", ["one/three", "3", "", "/", "1/"])
    def test_a_malformed_spec_is_refused_in_words_a_caller_can_print(self, spec: str) -> None:
        """**The type is not the assertion.** Without the shape guard,
        `int("one")` raises `ValueError` too -- so `pytest.raises(ValueError)`
        above holds with the guard deleted, and what reaches the user changes
        from "--shard wants I/N" to "invalid literal for int() with base 10".
        `main` prints this text straight out, so the words are the contract.
        """
        with pytest.raises(ValueError) as raised:
            run_tests.shard_of(spec)
        assert "--shard wants I/N" in str(raised.value)

    def test_an_out_of_range_spec_says_what_the_range_is(self) -> None:
        """The other message, and the other branch. A caller told only that
        something is wrong with `4/3` has to read this file to find out what."""
        with pytest.raises(ValueError) as raised:
            run_tests.shard_of("4/3")
        assert "out of range" in str(raised.value)
        assert "1..N" in str(raised.value)


class TestSelection:
    def test_only_is_anchored_at_a_dot(self) -> None:
        """So `--only tests.test_sync` does not also drag in a later
        `tests.test_sync_chunks` that nobody chose."""
        assert run_tests.selects("tests.test_sync.TestX", "tests.test_sync")
        assert run_tests.selects("tests.test_sync", "tests.test_sync")
        assert not run_tests.selects("tests.test_sync_chunks.TestX", "tests.test_sync")
