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

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
