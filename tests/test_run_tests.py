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
        box = Path(tempfile.mkdtemp(prefix="tupferl-batch-"))
        self.addCleanup(shutil.rmtree, box, True)
        (box / "tests_batch").mkdir()
        (box / "tests_batch" / "__init__.py").write_text("", encoding="utf-8")
        (box / "tests_batch" / "test_it.py").write_text(body, encoding="utf-8")
        out = box / "report.json"
        sys.path.insert(0, str(box))
        try:
            with support.quiet():
                run_tests.run_batch([f"tests_batch.test_it.{name}" for name in names], out)
        finally:
            sys.path.remove(str(box))
            for name in [m for m in sys.modules if m.startswith("tests_batch")]:
                del sys.modules[name]
        return dict(json.loads(out.read_text(encoding="utf-8")))

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


class TestPacking(unittest.TestCase):
    """`pack` decides both splits -- classes over processes, and shards over
    machines -- so it is worth its own tests without a suite to run."""

    def test_the_heaviest_class_is_scheduled_first(self) -> None:
        """Largest-first onto the lightest batch. The weights are 5, 3 and 2 so
        that a wrong rule produces a visibly different answer: two batches, and
        the only balanced split is [5] and [3, 2]."""
        classes = {"a": ["x"] * 5, "b": ["x"] * 3, "c": ["x"] * 2}
        batches = run_tests.pack(classes, 2)
        self.assertEqual([["a"], ["b", "c"]], [sorted(batch) for batch in batches])

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

    def test_out_of_range_and_malformed_specs_are_refused(self) -> None:
        for spec in ("0/3", "4/3", "1/0", "one/three", "3", ""):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                run_tests.shard_of(spec)


class TestSelection(unittest.TestCase):
    def test_only_is_anchored_at_a_dot(self) -> None:
        """So `--only tests.test_sync` does not also drag in a later
        `tests.test_sync_chunks` that nobody chose."""
        self.assertTrue(run_tests.selects("tests.test_sync.TestX", "tests.test_sync"))
        self.assertTrue(run_tests.selects("tests.test_sync", "tests.test_sync"))
        self.assertFalse(run_tests.selects("tests.test_sync_chunks.TestX", "tests.test_sync"))


if __name__ == "__main__":
    unittest.main()
