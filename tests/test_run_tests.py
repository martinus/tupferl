"""The parallel runner, and the one failure mode it exists to refuse.

A serial `python -m unittest` cannot be green because nothing ran. A parallel one
can: a batch that dies before it reports leaves a summary with fewer tests in it
than were discovered, and every remaining test passed. `run_tests` checks

    ids discovered == ids reported

and this file is the proof that the check is wired up, driven against a
throwaway tree containing a test that kills its own process. Asserting on `pack`
and `discover` alone would leave the accounting itself -- the whole reason the
script exists -- untested.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tests import support
from tools import run_tests

#: A test module that runs one test and then takes its process down mid-batch,
#: before the report is written. `os._exit` rather than `sys.exit`: the latter
#: raises `SystemExit`, which unittest catches and turns into an ordinary error,
#: so the batch would still report and there would be nothing to detect.
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


class Tree(unittest.TestCase):
    """A throwaway repository root with its own `tests/` and a copy of the runner.

    A copy rather than a `sys.path` trick: `run_tests` derives its root from
    `__file__` and re-invokes itself as `python -m tools.run_tests`, so the only
    honest way to point it somewhere else is to put it there.
    """

    def setUp(self) -> None:
        box = tempfile.TemporaryDirectory(prefix="tupferl-runner-")
        self.addCleanup(box.cleanup)
        self.tree = Path(box.name)
        (self.tree / "tools").mkdir()
        # Every module, not the three `run_tests` imports today. That list was
        # hand-kept, and it went stale the first time the runner gained an
        # import: `tools/paint.py` was added and all four tests in this file
        # went red with `ImportError: cannot import name 'paint'`, from a tree
        # this fixture had built wrong rather than from anything under test.
        # Copying the directory cannot go stale, and the extra files are inert
        # -- discovery looks in `tests/`.
        for module in Path(run_tests.__file__).parent.glob("*.py"):
            shutil.copy(module, self.tree / "tools" / module.name)
        (self.tree / "tests").mkdir()
        (self.tree / "tests" / "__init__.py").write_text("", encoding="utf-8")

    def add(self, name: str, body: str) -> None:
        (self.tree / "tests" / name).write_text(body, encoding="utf-8")

    def run_it(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "tools.run_tests", *args],
            cwd=self.tree,
            capture_output=True,
            text=True,
            check=False,
        )


class TestTheAccountingCheck(Tree):
    def test_a_healthy_tree_is_green(self) -> None:
        """The precondition. Without it, the failure below could be the fixture
        being broken rather than the death being detected."""
        self.add("test_healthy.py", HEALTHY)
        done = self.run_it("--jobs", "2")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("Ran 2 tests", done.stdout)

    def test_a_batch_that_dies_is_not_green(self) -> None:
        """The whole point: the surviving tests all passed, and the run is red
        anyway, because tests that were discovered never reported."""
        self.add("test_suicide.py", SUICIDE)
        done = self.run_it("--jobs", "2")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("never ran", done.stdout)

    def test_the_missing_test_is_named(self) -> None:
        """A count alone sends the reader to look through the whole suite."""
        self.add("test_suicide.py", SUICIDE)
        done = self.run_it("--jobs", "2")
        self.assertIn("test_takes_the_process_with_it", done.stdout)

    def test_a_module_that_will_not_import_is_reported_as_such(self) -> None:
        """Not as a test that failed: nothing ran because nothing was *there*,
        and printing it as an assertion sends the reader looking for one."""
        self.add("test_broken.py", BROKEN_IMPORT)
        self.add("test_healthy.py", HEALTHY)
        done = self.run_it("--jobs", "2")
        self.assertEqual(1, done.returncode)
        self.assertIn("could not import tests.test_broken", done.stdout)


class TestTheOrderTheSummaryNamesThings(Tree):
    """Two broken modules and two tests that never ran, so ordering is visible.

    Both lists are `sorted`, and with one entry every ordering is the same
    ordering -- the fixture-too-weak shape CLAUDE.md names. A CI log read across
    two runs of the same tree has to be diffable, and a `set`-ordered list moves
    between runs for reasons that have nothing to do with the code.
    """

    def test_broken_modules_are_named_in_a_settled_order(self) -> None:
        self.add("test_zzz_broken.py", BROKEN_IMPORT)
        self.add("test_aaa_broken.py", BROKEN_IMPORT)
        done = self.run_it("--jobs", "2")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        named = [
            line.split("could not import ", 1)[1].split(":", 1)[0]
            for line in done.stdout.splitlines()
            if "could not import" in line
        ]
        self.assertEqual(["tests.test_aaa_broken", "tests.test_zzz_broken"], named)

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

    def test_tests_that_never_ran_are_named_in_a_settled_order(self) -> None:
        """The count line above them is asserted too -- it is the only place the
        *number* appears, and nothing read it before."""
        self.add("test_lost.py", self.LOSES_FIVE)
        done = self.run_it("--jobs", "1")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("5 discovered tests never ran", done.stdout)
        missing = [
            line.split("never ran: ", 1)[1].strip()
            for line in done.stdout.splitlines()
            if "never ran: " in line
        ]
        self.assertEqual(5, len(missing), done.stdout)
        self.assertEqual(sorted(missing), missing, "the names are not in a settled order")

    def test_a_batch_that_died_says_so_before_the_accounting(self) -> None:
        """The `::error::` naming the *cause*. Without it the log carries only
        the consequence -- tests that never ran -- and a reader has to guess
        whether the batch crashed or was never started."""
        self.add("test_lost.py", self.LOSES_FIVE)
        done = self.run_it("--jobs", "1")
        self.assertIn("batch died without reporting", done.stdout)


class TestWhatTheRunSaysAboutItself(Tree):
    """The last line, and the line naming a failing test.

    Both survived the sweep of the change that painted them, and both are the
    shape CLAUDE.md §8 is about: **the exit status is guarded and the words are
    not**, so a run that printed nothing at all and exited 0 passes every other
    test in this file. "Confirm it did the work" needs something to read.
    """

    def test_a_green_run_says_it_is_ok(self) -> None:
        """The summary line is how a person confirms a green run rather than
        inferring it from silence -- and `$?` is not visible in a CI log
        scrollback."""
        self.add("test_healthy.py", HEALTHY)
        done = self.run_it("--jobs", "2")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("OK (0 failures, 0 errors, 0 skipped)", done.stdout)

    def test_a_red_run_says_it_failed_and_counts_it(self) -> None:
        """The other half, without which the assertion above is satisfied by a
        line that says `OK` unconditionally."""
        self.add("test_red.py", FAILING)
        done = self.run_it("--jobs", "2")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("FAILED (1 failures, 0 errors, 0 skipped)", done.stdout)

    def test_a_failing_test_is_named(self) -> None:
        """Which test failed, not how many. A parallel run interleaves 16
        batches, so a count alone sends the reader through every one of them --
        the same argument `test_the_missing_test_is_named` makes for the
        accounting check, on the path a person actually hits."""
        self.add("test_red.py", FAILING)
        done = self.run_it("--jobs", "2")
        self.assertIn("FAIL: tests.test_red.TestRed.test_asserts_something_untrue", done.stdout)

    def test_a_green_run_names_nothing(self) -> None:
        """The precondition for the two above: no `FAIL:` line when nothing
        failed. Without it, a version that printed the whole discovered suite
        under `FAIL:` would satisfy them both."""
        self.add("test_healthy.py", HEALTHY)
        done = self.run_it("--jobs", "2")
        self.assertNotIn("FAIL:", done.stdout)


class TestWhatABatchReports(unittest.TestCase):
    """`run_batch`: the JSON a worker leaves behind, which is all the parent knows.

    **Twelve of its thirteen mutants survived, and nothing called it directly.**
    Every test in this file drives `run_tests` end to end, so `run_batch` ran
    only inside a subprocess -- invisible to coverage, and asserted on only
    through whatever the parent printed afterwards.

    That matters because the accounting check this module exists for --
    `ids discovered == ids reported` -- is fed entirely by these files. A batch
    that under-reports its `ran` list makes the parent announce tests that never
    ran; one that over-reports hides a batch that died.

    Written to a real file by the real function, because the file *is* the
    interface.
    """

    def batch(self, body: str, *names: str) -> dict[str, Any]:
        return self.run_it(body, *names)[0]

    def status(self, body: str, *names: str) -> int:
        """What the worker process exits with, which is the other half of what
        it reports. The parent reads the file; `mutate` and a shell read this.
        """
        return self.run_it(body, *names)[1]

    def run_it(self, body: str, *names: str) -> tuple[dict[str, Any], int]:
        box = Path(tempfile.mkdtemp(prefix="tupferl-batch-"))
        self.addCleanup(shutil.rmtree, box, True)
        (box / "tests_batch").mkdir()
        (box / "tests_batch" / "__init__.py").write_text("", encoding="utf-8")
        (box / "tests_batch" / "test_it.py").write_text(body, encoding="utf-8")
        out = box / "report.json"
        sys.path.insert(0, str(box))
        try:
            with support.quiet():
                code = run_tests.run_batch([f"tests_batch.test_it.{name}" for name in names], out)
        finally:
            sys.path.remove(str(box))
            for name in [m for m in sys.modules if m.startswith("tests_batch")]:
                del sys.modules[name]
        return dict(json.loads(out.read_text(encoding="utf-8"))), code

    def test_a_batch_where_everything_passed_exits_zero(self) -> None:
        """The precondition for every status below: a worker that always
        reported failure would satisfy all of them."""
        self.assertEqual(0, self.status(self.PASSES, "Green"))

    def test_a_batch_with_a_failing_test_does_not_exit_zero(self) -> None:
        self.assertEqual(1, self.status(self.PASSES, "Red"))

    def test_a_skip_is_not_a_failure_here(self) -> None:
        """`--no-skips` is the parent's decision and it needs the ids to make
        it, so a worker that called a skip red would take that choice away."""
        self.assertEqual(0, self.status(self.PASSES, "Skipped"))

    def test_a_module_that_will_not_import_makes_the_batch_red(self) -> None:
        """**The `and not unloadable` half, and it cannot be seen from
        `wasSuccessful()`.** The suite that actually runs has had the unloadable
        module's tests taken out of it, so it passes -- and a batch reporting
        success for a module that never loaded is the green run of nothing this
        script exists to refuse, one level down.
        """
        box = Path(tempfile.mkdtemp(prefix="tupferl-batch-"))
        self.addCleanup(shutil.rmtree, box, True)
        (box / "tests_batch").mkdir()
        (box / "tests_batch" / "__init__.py").write_text("", encoding="utf-8")
        (box / "tests_batch" / "test_it.py").write_text(BROKEN_IMPORT, encoding="utf-8")
        out = box / "report.json"
        sys.path.insert(0, str(box))
        try:
            with support.quiet():
                code = run_tests.run_batch(["tests_batch.test_it"], out)
        finally:
            sys.path.remove(str(box))
            for name in [m for m in sys.modules if m.startswith("tests_batch")]:
                del sys.modules[name]
        written = json.loads(out.read_text(encoding="utf-8"))
        self.assertTrue(written["unloadable"], "the fixture imported after all")
        self.assertEqual([], written["failures"], "nothing *failed*; the module never loaded")
        self.assertEqual(1, code)

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
    )

    def test_every_test_it_ran_is_named(self) -> None:
        """The list the accounting check subtracts from. A batch that reports
        fewer ids than it ran makes the parent announce tests that never ran --
        which is the false alarm, where the missing half is the real one."""
        said = self.batch(self.PASSES, "Green")
        self.assertEqual(
            ["tests_batch.test_it.Green.test_one", "tests_batch.test_it.Green.test_two"],
            sorted(said["ran"]),
        )

    def test_a_failure_and_an_error_are_kept_apart(self) -> None:
        """Two different things: an assertion that did not hold, and code that
        raised on the way. Folding them together loses which one a reader is
        looking at, and `main` prints them under different labels."""
        said = self.batch(self.PASSES, "Red")
        self.assertEqual(["tests_batch.test_it.Red.test_fails"], said["failures"])
        self.assertEqual(["tests_batch.test_it.Red.test_errors"], said["errors"])

    def test_a_skip_carries_its_reason(self) -> None:
        """The reason is not printed by the child at this verbosity, so if the
        report drops it there is nowhere else to get it -- and `--no-skips`
        exists to make a silent skip loud."""
        said = self.batch(self.PASSES, "Skipped")
        self.assertEqual(
            [["tests_batch.test_it.Skipped.test_skipped", "a reason worth carrying"]],
            said["skipped"],
        )

    def test_a_module_that_will_not_import_is_reported_and_not_run(self) -> None:
        """Discovery in the parent sets unloadable modules aside, and this is
        the belt to that brace: a module can import there and not here. The
        classes that *did* load must still run."""
        said = self.batch("import nothing_by_this_name  # noqa: F401\n", "Anything")
        self.assertTrue(said["unloadable"], "a broken import was not reported")
        self.assertEqual([], said["ran"])

    def test_a_green_batch_reports_nothing_wrong(self) -> None:
        """The precondition. Without it every assertion above is satisfied by a
        report that lists everything under every key."""
        said = self.batch(self.PASSES, "Green")
        self.assertEqual([], said["failures"])
        self.assertEqual([], said["errors"])
        self.assertEqual([], said["skipped"])
        self.assertEqual({}, said["unloadable"])


class TestTheWaysARunIsRefusedRatherThanRunEmpty(Tree):
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

    def setUp(self) -> None:
        super().setUp()
        self.add("test_healthy.py", HEALTHY)

    def test_only_that_matches_nothing_is_refused(self) -> None:
        done = self.run_it("--only", "tests.test_nothing_like_this")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("no test class matches", done.stdout)

    def test_only_that_matches_still_runs(self) -> None:
        done = self.run_it("--only", "tests.test_healthy")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("Ran 2 tests", done.stdout)

    def test_exclude_that_matches_nothing_is_refused(self) -> None:
        """One pattern at a time, and each has to match something. Checking only
        that the set shrank would let a renamed class stop being excluded as
        long as some *other* pattern still matched -- and the job that needs
        this passes two, so that is the likely case rather than the exotic one.
        """
        done = self.run_it("--exclude", "tests.test_healthy.TestHealthy", "--exclude", "TestGone")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("TestGone", done.stdout)

    def test_exclude_that_matches_takes_the_class_out(self) -> None:
        """The twin, and it names what it kept: a run that excluded nothing also
        exits 0, so the status alone says nothing here."""
        self.add("test_second.py", HEALTHY.replace("TestHealthy", "TestSecond"))
        done = self.run_it("--exclude", "tests.test_healthy.TestHealthy")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("Ran 2 tests", done.stdout)

    def test_excluding_the_last_class_is_refused_rather_than_run_empty(self) -> None:
        """**Found by writing the tests above, and fixed in this change.**

        Every pattern matched, so the loop above is satisfied -- and the run then
        packed one *empty* batch, spawned a worker with no names, watched
        argparse refuse it, printed `::error::batch died without reporting` and
        **exited 0**. An annotated error on a green job, which is the exact
        failure this whole script exists to refuse.
        """
        done = self.run_it("--exclude", "tests.test_healthy.TestHealthy")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("nothing would run", done.stdout)
        self.assertNotIn("died without reporting", done.stdout)

    def test_a_selection_matching_only_a_broken_module_still_reports_it(self) -> None:
        """The `not unloadable` half of that guard, which nothing else reaches.

        A module that will not import has no classes, so "nothing would run" is
        *true* of it -- and saying that instead sends the reader looking for a
        filter that is wrong when what is wrong is the module. The run is red
        either way, which is why the assertion is on the words: without this,
        dropping `and not unloadable` passes every other test here.
        """
        self.add("test_broken.py", BROKEN_IMPORT)
        done = self.run_it("--only", "tests.test_broken")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("could not import tests.test_broken", done.stdout)
        self.assertNotIn("nothing would run", done.stdout)

    def test_a_malformed_shard_is_refused(self) -> None:
        """`--shard` is one-based: `I` is `1..N`. `0/2` is out of range, and so
        is `3/2`."""
        for spec in ("0/2", "3/2", "one/two", "2"):
            with self.subTest(spec=spec):
                done = self.run_it("--shard", spec)
                self.assertEqual(1, done.returncode, done.stdout + done.stderr)
                self.assertIn("::error::", done.stdout)

    def test_more_shards_than_classes_is_refused(self) -> None:
        """The one that would otherwise be silent. An empty shard reports "Ran 0
        tests" and exits 0 -- the partial run that is green because nothing
        happened -- and it can only come from a matrix that outgrew the suite,
        so every shard would go on being green as classes were removed."""
        done = self.run_it("--shard", "1/9")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("more shards than there are classes", done.stdout)

    def test_a_shard_that_fits_runs_its_share(self) -> None:
        """Without this the class is satisfied by a `main` that refuses every
        `--shard`, which is the flag CI splits the suite with."""
        self.add("test_second.py", HEALTHY.replace("TestHealthy", "TestSecond"))
        done = self.run_it("--shard", "1/2")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("Ran 2 tests", done.stdout)


class TestReadingUnittestsImportFailure(unittest.TestCase):
    """`_unloadable` and `_loaded`, the surgery on `TestLoader.errors`.

    `unittest` hands back a formatted traceback per module whose first line is
    "Failed to import test module: tests.test_x". The module name is the tail of
    that line and the line itself is what a reader needs, so both are cut from
    it -- and every assertion elsewhere in this file matches a *substring* of
    what gets printed, which holds against several wrong cuts.
    """

    def loader(self) -> unittest.TestLoader:
        box = self.box = Path(tempfile.mkdtemp(prefix="tupferl-load-"))
        self.addCleanup(shutil.rmtree, box, True)
        (box / "tests_load").mkdir()
        (box / "tests_load" / "__init__.py").write_text("", encoding="utf-8")
        (box / "tests_load" / "test_broken.py").write_text(BROKEN_IMPORT, encoding="utf-8")
        (box / "tests_load" / "test_fine.py").write_text(HEALTHY, encoding="utf-8")
        loader = unittest.TestLoader()
        sys.path.insert(0, str(box))
        self.addCleanup(sys.path.remove, str(box))
        self.addCleanup(
            lambda: [sys.modules.pop(m) for m in list(sys.modules) if m.startswith("tests_load")]
        )
        self.suite = loader.loadTestsFromNames(["tests_load.test_broken", "tests_load.test_fine"])
        return loader

    def test_the_key_is_the_module_and_the_value_is_the_first_line(self) -> None:
        """Asserted exactly, not by `assertIn`. The key is what the parent
        prints as "could not import <name>" and what `_loaded` matches ids
        against, so a cut that left the whole traceback in the key would name a
        module nobody can act on -- and every `assertIn` elsewhere in this file
        still passes, because the printed line contains the right text
        somewhere inside the wrong one."""
        found = run_tests._unloadable(self.loader())
        self.assertEqual(["test_broken"], list(found))
        self.assertEqual("Failed to import test module: test_broken", found["test_broken"])

    def test_the_two_loaders_name_the_same_module_differently(self) -> None:
        """**Measured, and it is why the assertion above looks wrong.**
        `loadTestsFromNames` -- what `run_batch` uses -- reports the bare module
        name; `discover` -- what `main` uses -- reports the dotted one:

            loadTestsFromNames   'Failed to import test module: test_broken'
            discover             'Failed to import test module: tests_load.test_broken'

        Both spellings therefore flow into `main`'s one `unloadable` dict. They
        do not collide in practice, because `discover` drops a module it could
        not import from `classes` as well, so no batch is ever asked to load it
        -- which is separately why `unloadable.update(report["unloadable"])` is
        all but unreachable. Recorded here because a future `discover` that kept
        such a module would print the same failure twice under two names, and
        nothing else in this file would notice.

        Same family as `test_verdict`'s `TestABrokenModuleTakesTwoDifferentPaths`:
        two loaders, two classifications, and a fixture written for one proves
        nothing about the other.
        """
        loader = self.loader()
        bare = run_tests._unloadable(loader)
        found = unittest.TestLoader()
        found.discover(str(self.box / "tests_load"), top_level_dir=str(self.box))
        dotted = run_tests._unloadable(found)
        self.assertEqual(["test_broken"], list(bare))
        self.assertEqual(["tests_load.test_broken"], list(dotted))

    def test_the_placeholder_tests_of_a_broken_module_are_taken_out(self) -> None:
        """`unittest` puts a synthetic failing test in place of a module it
        could not import. Left in the suite it is reported as a test that
        *failed*, which sends a reader looking for an assertion -- and it is
        counted as having run, so the accounting check is satisfied by it.

        The healthy module beside it is the other half: a filter that dropped
        everything would pass the first assertion on its own.
        """
        loader = self.loader()
        kept = run_tests._loaded(self.suite, run_tests._unloadable(loader))
        ids = [test.id() for test in kept]
        self.assertTrue(any("tests_load.test_fine" in tid for tid in ids), ids)
        self.assertFalse(any("test_broken" in tid for tid in ids), ids)


class TestWhatASkipCosts(Tree):
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

    def setUp(self) -> None:
        super().setUp()
        self.add("test_skipping.py", SKIPPING)

    def test_a_skip_is_green_by_default_and_says_so(self) -> None:
        done = self.run_it("--jobs", "2")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("skipped: ", done.stdout)
        self.assertIn("age is not installed", done.stdout)

    def test_the_same_run_is_red_under_no_skips(self) -> None:
        done = self.run_it("--jobs", "2", "--no-skips")
        self.assertEqual(1, done.returncode, done.stdout + done.stderr)
        self.assertIn("an optional tool is missing", done.stdout)

    def test_a_suite_with_no_skips_is_green_under_the_flag(self) -> None:
        """The other half. Without it the class is satisfied by a `--no-skips`
        that fails every run, which is the flag CI's fullest leg uses."""
        self.add("test_skipping.py", HEALTHY)
        done = self.run_it("--jobs", "2", "--no-skips")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertNotIn("skipped: ", done.stdout)


class TestPacking(unittest.TestCase):
    """`pack` decides both splits -- classes over processes, and shards over
    machines -- so it is worth its own tests without a suite to run."""

    def test_the_heaviest_class_is_scheduled_first(self) -> None:
        """Largest-first onto the lightest batch. The weights are 5, 3 and 2 so
        that a wrong rule produces a visibly different answer: two batches, and
        the only balanced split is [5] and [3, 2].

        **Inserted lightest-first, and that is the fixture.** Written `a, b, c`
        -- which is already the order the sort produces -- dropping the `sorted`
        entirely gives the same answer, so the test passed against a `pack` that
        did not sort at all. Measured: that mutant survived until this line was
        reordered.
        """
        classes = {"c": ["x"] * 2, "b": ["x"] * 3, "a": ["x"] * 5}
        batches = run_tests.pack(classes, 2)
        self.assertEqual([["a"], ["b", "c"]], [sorted(batch) for batch in batches])

    def test_nothing_to_pack_is_still_one_batch(self) -> None:
        """The `max(1, ...)` floor, and the only input for which `pack` returns
        a batch with nothing in it. `main` refuses an empty selection before it
        gets here -- see `test_excluding_the_last_class_is_refused_rather_than_
        run_empty` for what happens when it does not -- so this pins the shape
        the caller is entitled to rather than a case it should ever produce."""
        self.assertEqual([[]], run_tests.pack({}, 3))

    def test_no_batch_is_empty(self) -> None:
        """An empty batch starts an interpreter to run nothing."""
        batches = run_tests.pack({"a": ["one"]}, 8)
        self.assertEqual([["a"]], batches)

    def test_every_class_is_placed_exactly_once(self) -> None:
        classes = {f"c{n}": ["x"] * (n % 4 + 1) for n in range(20)}
        placed = [name for batch in run_tests.pack(classes, 6) for name in batch]
        self.assertEqual(sorted(classes), sorted(placed))
        self.assertEqual(len(placed), len(set(placed)))


class TestShardSpecs(unittest.TestCase):
    def test_a_valid_spec_is_zero_based(self) -> None:
        self.assertEqual((0, 3), run_tests.shard_of("1/3"))
        self.assertEqual((2, 3), run_tests.shard_of("3/3"))

    def test_one_shard_of_one_is_the_ordinary_case_and_is_valid(self) -> None:
        """`--shard 1/1` is what a matrix of one leg passes, and every other
        test here uses N=3 -- so a bound that rejected N=1 passed all of them.
        Both `count < 1` becoming `count < 2` and `<` becoming `<=` refuse
        exactly this spec and nothing else in the file."""
        self.assertEqual((0, 1), run_tests.shard_of("1/1"))

    def test_out_of_range_and_malformed_specs_are_refused(self) -> None:
        for spec in ("0/3", "4/3", "1/0", "one/three", "3", ""):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                run_tests.shard_of(spec)

    def test_a_malformed_spec_is_refused_in_words_a_caller_can_print(self) -> None:
        """**The type is not the assertion.** Without the shape guard,
        `int("one")` raises `ValueError` too -- so `assertRaises(ValueError)`
        above holds with the guard deleted, and what reaches the user changes
        from "--shard wants I/N" to "invalid literal for int() with base 10".
        `main` prints this text straight out, so the words are the contract.
        """
        for spec in ("one/three", "3", "", "/", "1/"):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError) as raised:
                    run_tests.shard_of(spec)
                self.assertIn("--shard wants I/N", str(raised.exception))

    def test_an_out_of_range_spec_says_what_the_range_is(self) -> None:
        """The other message, and the other branch. A caller told only that
        something is wrong with `4/3` has to read this file to find out what."""
        with self.assertRaises(ValueError) as raised:
            run_tests.shard_of("4/3")
        self.assertIn("out of range", str(raised.exception))
        self.assertIn("1..N", str(raised.exception))


class TestSelection(unittest.TestCase):
    def test_only_is_anchored_at_a_dot(self) -> None:
        """So `--only tests.test_sync` does not also drag in a later
        `tests.test_sync_chunks` that nobody chose."""
        self.assertTrue(run_tests.selects("tests.test_sync.TestX", "tests.test_sync"))
        self.assertTrue(run_tests.selects("tests.test_sync", "tests.test_sync"))
        self.assertFalse(run_tests.selects("tests.test_sync_chunks.TestX", "tests.test_sync"))


if __name__ == "__main__":
    unittest.main()
