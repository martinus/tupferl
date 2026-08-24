"""Prove the mutation harness, against a deliberate one-line bug in real code.

Plan §8 milestone 1 asks for exactly this, and the reason it is a *test* rather
than a note in a pull request is CLAUDE.md §8: a tool that reports "nothing
noticed this" while quietly editing the wrong file flatters the tests, which is
the direction every bug in that class has erred. So the harness is checked in
both directions on this repository's own code:

- a mutation the suite catches must come back `caught`;
- a mutation nothing was asked to look at must come back `survived`.

A tool that could only say one of those would pass a one-sided check.

These tests copy the tree once per mutation and run one test module inside it, so
they are seconds rather than milliseconds. They are worth it once: everything
else in this project's testing story is downstream of the harness being honest.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests import support
from tools import mutants, mutate
from tools.mutants import Mutation, check

#: The deliberate bug: `tupferl/config.py` stops refusing keys it does not know.
#: The same row appears in `tools/mutate.py`'s module docstring as the example,
#: so a change to either line is caught here rather than by a reader noticing
#: that the documentation names code that no longer exists.
UNKNOWN_KEY_GUARD = Mutation(
    "an unknown config key is accepted rather than refused",
    "tupferl/config.py",
    "if key not in KNOWN:",
    "if False:",
    "tests.test_config.TestRejectingAnUnknownKey",
)

#: The same edit, run against tests that cannot see it. Not a claim about the
#: suite -- `tests.test_paths` never parses a config file, so this survives *by
#: construction*, which is what makes it a fixture for "the harness can also say
#: no" rather than a finding about coverage.
UNWATCHED = UNKNOWN_KEY_GUARD._replace(
    label="the same edit, with only unrelated tests to notice it",
    tests="tests.test_paths",
)


class TestTheHarnessAnswersBothWays(unittest.TestCase):
    """The whole loop: copy the tree, apply the edit, run a suite, classify."""

    def test_a_deliberate_bug_is_caught(self) -> None:
        report = mutate.run([UNKNOWN_KEY_GUARD], baseline=True, workers=1, summarise=False)
        self.assertFalse(report.baseline_red, "the untouched tree is not green")
        self.assertEqual(["caught"], [result.verdict.outcome for result in report.results])

    def test_an_unwatched_bug_survives(self) -> None:
        """The other answer. Without this, `test_a_deliberate_bug_is_caught`
        passes just as well against a harness hard-wired to say `caught` -- the
        assertion that passes against its own mutation, from CLAUDE.md §2."""
        report = mutate.run([UNWATCHED], baseline=True, workers=1, summarise=False)
        self.assertFalse(report.baseline_red)
        self.assertEqual(["survived"], [result.verdict.outcome for result in report.results])

    def test_the_working_tree_is_untouched(self) -> None:
        """CLAUDE.md §6: the harness must never edit the tree it is run from.

        Asserted on the file's own bytes, before and after, rather than on
        `git status` -- the guarantee is about this file, and `git status` would
        also be satisfied by an edit that was made and then put back.
        """
        where = Path(UNKNOWN_KEY_GUARD.path)
        before = where.read_bytes()
        mutate.run([UNKNOWN_KEY_GUARD], baseline=False, workers=1, summarise=False)
        self.assertEqual(before, where.read_bytes())


class TestTheDocumentedExampleIsReal(unittest.TestCase):
    def test_the_line_it_names_exists_exactly_once(self) -> None:
        """`check` is what enforces it, and this is the case that keeps the
        docstring in `tools/mutate.py` from naming code nobody has."""
        check(UNKNOWN_KEY_GUARD)

    def test_a_row_naming_absent_code_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            check(UNKNOWN_KEY_GUARD._replace(old="if key not in NOTHING_LIKE_THIS:"))

    def test_a_replacement_that_keeps_the_original_is_refused(self) -> None:
        """An edit that adds without removing leaves the code under test exactly
        as it was, so the run reports an outcome about nothing."""
        with self.assertRaises(SystemExit):
            check(UNKNOWN_KEY_GUARD._replace(new="if key not in KNOWN:  # noqa"))

    def test_an_additive_row_is_allowed_when_it_says_so(self) -> None:
        check(UNKNOWN_KEY_GUARD._replace(new="if key not in KNOWN:  # noqa", additive=True))


class TestGeneratingFromADiff(unittest.TestCase):
    """`--base` reads `git diff` and writes the table itself. Driven against a
    throwaway repository rather than this one, so the answer does not depend on
    what happens to be uncommitted while the suite runs."""

    def setUp(self) -> None:
        box = tempfile.TemporaryDirectory(prefix="tupferl-mutants-")
        self.addCleanup(box.cleanup)
        self.box = Path(box.name)
        # A seeded home beside the tree, not the tree itself: git needs an
        # identity and a written `init.defaultBranch` to commit at all, and
        # `seed_home` is where both are decided for the whole suite.
        home = self.box / "home"
        home.mkdir()
        support.seed_home(home)
        self.env = support.sandbox_env(home)
        self.tree = self.box / "tree"
        self.tree.mkdir()
        support.git(["init", "--initial-branch=main"], self.tree, self.env)
        (self.tree / "tupferl").mkdir()
        self.source = self.tree / "tupferl" / "thing.py"
        self.source.write_text("def size(n: int) -> int:\n    return n + 1\n", encoding="utf-8")
        support.git(["add", "-A"], self.tree, self.env)
        support.git(["commit", "-m", "base"], self.tree, self.env)

    def test_only_the_changed_lines_are_generated_for(self) -> None:
        self.source.write_text(
            "def size(n: int) -> int:\n    return n + 1\n\n\ndef twice(n: int) -> int:\n"
            "    return n * 2\n",
            encoding="utf-8",
        )
        touched = mutants.changed_lines("main", self.tree)
        self.assertEqual({"tupferl/thing.py"}, set(touched))
        # The added lines, not the whole file: a generator that ignored the diff
        # would offer the first function's lines too.
        self.assertNotIn(2, touched["tupferl/thing.py"])
        self.assertIn(6, touched["tupferl/thing.py"])

    def test_tests_are_never_mutated(self) -> None:
        """Breaking a test proves nothing about the fix, and the run would then
        report the assertion it removed."""
        self.assertFalse(mutants.mutable("tests/test_config.py"))
        self.assertTrue(mutants.mutable("tupferl/config.py"))
        self.assertTrue(mutants.mutable("tools/mutate.py"))


if __name__ == "__main__":
    unittest.main()
