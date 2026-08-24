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

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

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


#: A test id that certainly resolves, used where the point is "a real one is
#: kept". This module's own name, so it cannot go stale without this file
#: being edited -- and if it is renamed, the test that depends on it is right
#: here rather than somewhere that would fail mysteriously.
REAL = "tests.test_mutate.TestRememberingWhatCaughtEachMutation.test_a_remembered_test_runs_first"


def row(
    path: str = "tupferl/sync.py",
    old: str = "a",
    new: str = "b",
    label: str = "x:1 in f()",
) -> Mutation:
    """One generated-shaped mutation, with only the fields the cache reads."""
    return Mutation(label, path, old, new, "tests.test_sync", operator="branch")


class TestRememberingWhatCaughtEachMutation(unittest.TestCase):
    """`Killers`: run the test that worked last time, first.

    The claim is about the *selection* the harness builds, which is what
    `failfast` then walks in order -- so that is what these assert on. Driving a
    whole sweep to observe it would take minutes and tell us the same thing.
    """

    def cache(self, known: dict[str, str]) -> mutate.Killers:
        with support.tempdir() as box:
            where = box / "killers.json"
            where.write_text(json.dumps(known), encoding="utf-8")
            return mutate.Killers(where)

    def test_a_remembered_test_runs_first(self) -> None:
        """In front, and the original selection untouched behind it."""
        one = row()
        cached = self.cache({mutate._key(one): REAL})
        (ahead,) = cached.ahead_of([one])
        self.assertEqual(REAL, ahead.first)
        self.assertEqual("tests.test_sync", ahead.tests)

    def test_the_whole_selection_is_kept_behind_it(self) -> None:
        """The safety argument, asserted rather than assumed. Substituting the
        remembered test for the selection would make every stale entry a
        `caught` that nothing verified -- flattering the tests, which is the
        direction every bug in this class errs."""
        one = row()._replace(tests="tests.test_sync tests.test_sync_cli")
        cached = self.cache({mutate._key(one): REAL})
        (ahead,) = cached.ahead_of([one])
        self.assertEqual("tests.test_sync tests.test_sync_cli", ahead.tests)
        self.assertEqual(REAL, ahead.first)

    def test_the_selection_stays_identical_across_rows(self) -> None:
        """`run` shards the baseline check by distinct `tests` string, so a
        killer folded into `tests` gives every row its own shard -- and one
        baseline run of `tupferl/sync.py`'s selection became 42 of them, 27s
        each. That was the whole of a 372s -> 730s regression, and it is
        invisible in every functional assertion above: the verdicts were
        identical. Only the clock saw it."""
        rows = [row(old=f"a{n}") for n in range(5)]
        cached = self.cache({mutate._key(r): REAL for r in rows})
        ahead = cached.ahead_of(rows)
        self.assertEqual(1, len({r.tests for r in ahead}), "the baseline would shard per row")

    def test_a_test_that_no_longer_exists_is_dropped(self) -> None:
        """A renamed test leaves its module in place, so `unittest`'s loader
        records an error rather than raising -- and an error there is classified
        `broke`, which would turn every mutant that remembered it into a
        non-answer. One rename would produce a wall of them."""
        one = row()
        cached = self.cache({mutate._key(one): "tests.test_mutate.NoSuchClass.no_such_test"})
        with support.quiet():
            (ahead,) = cached.ahead_of([one])
        self.assertEqual("", ahead.first)

    def test_a_module_that_no_longer_exists_is_dropped_too(self) -> None:
        one = row()
        cached = self.cache({mutate._key(one): "tests.test_gone.Class.test_x"})
        with support.quiet():
            (ahead,) = cached.ahead_of([one])
        self.assertEqual("", ahead.first)

    def test_a_row_nothing_is_remembered_about_is_left_alone(self) -> None:
        (ahead,) = mutate.Killers(None).ahead_of([row()])
        self.assertEqual("", ahead.first)
        self.assertEqual("tests.test_sync", ahead.tests)


class TestTheKeyIsContentNotPosition(unittest.TestCase):
    """A line number is invalidated by any edit above it -- which is every edit,
    so a position-keyed cache would be empty exactly when it was most wanted."""

    def test_the_same_edit_at_a_different_line_is_the_same_key(self) -> None:
        moved = row(label="tupferl/sync.py:900 in f()")._replace(span=(9000, 9001))
        self.assertEqual(mutate._key(row()), mutate._key(moved))

    def test_a_different_edit_at_the_same_line_is_a_different_key(self) -> None:
        """Otherwise two operators' rows on one line would share an entry, and
        the second would run a test chosen for the first."""
        self.assertNotEqual(mutate._key(row()), mutate._key(row(new="c")))
        self.assertNotEqual(mutate._key(row()), mutate._key(row(path="tupferl/manage.py")))
        self.assertNotEqual(
            mutate._key(row()), mutate._key(row()._replace(operator="return-value"))
        )


class TestWhatTheCacheLearns(unittest.TestCase):
    def learned(self, outcome: mutate.Outcome, killer: str) -> dict[str, str]:
        one = row()
        with support.tempdir() as box:
            cache = mutate.Killers(box / "killers.json")
            cache.known = {mutate._key(one): "tests.test_old.C.t"}
            cache.learn([mutate.Result(one, mutate.Verdict(outcome, "", killer))])
            return cache.known

    def test_a_catch_is_remembered(self) -> None:
        self.assertEqual({mutate._key(row()): REAL}, self.learned("caught", REAL))

    def test_a_survivor_forgets_whatever_used_to_catch_it(self) -> None:
        """Keeping it would put a test that cannot help at the front of every
        future run of this row, for ever."""
        self.assertEqual({}, self.learned("survived", ""))

    def test_a_run_that_asked_nothing_changes_nothing(self) -> None:
        """`broke` and `timeout` are not answers, so they are not evidence that
        the remembered test stopped working."""
        outcomes: tuple[mutate.Outcome, ...] = ("broke", "timeout")
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                self.assertEqual(
                    {mutate._key(row()): "tests.test_old.C.t"}, self.learned(outcome, "")
                )


class TestTheKillerIsRecordedAtAll(unittest.TestCase):
    """The cache is worth nothing if `Verdict.killer` is empty, and it comes
    from `tools/verdict.py` through a JSON report -- so this drives the real
    thing rather than asserting on a field."""

    def test_a_caught_mutation_names_the_test_in_a_form_unittest_takes_back(self) -> None:
        found = mutate.run([UNKNOWN_KEY_GUARD], baseline=False, workers=1, summarise=False)
        (result,) = found.results
        self.assertEqual("caught", result.verdict.outcome)
        self.assertTrue(result.verdict.killer, "nothing recorded the killing test")
        # The claim: it loads. `str(test)` -- "method (dotted.id)" -- does not.
        self.assertEqual({result.verdict.killer}, mutate._loadable([result.verdict.killer]))


#: A test module that hangs on a blocking read, and one that does not. Written
#: to a throwaway directory rather than kept in `tests/`, because `run_tests`
#: discovers everything here and a permanently-hanging test in the tree is the
#: exact failure this guards against.
HANGS = """
import os, unittest
from pathlib import Path


class TestOne(unittest.TestCase):
    def test_hangs_on_a_fifo(self):
        where = Path(os.environ["HANGDIR"]) / "pipe"
        if not where.exists():
            os.mkfifo(where)
        where.read_bytes()
        self.fail("unreachable")

    def test_is_fine(self):
        self.assertTrue(True)
"""


class TestAHungTestIsBoundedAndNotCredited(unittest.TestCase):
    """A per-test alarm, and the classification that makes it safe.

    `tools/mutate.py`'s `TIMEOUT` bounds a whole *run* at 300s and cannot say
    which test hung. This bounds a *test*, in seconds, and names it.

    The dangerous part is not the timer, it is where the result is filed. The
    alarm raises inside a real `TestCase`, so it reaches `addError` carrying a
    genuine test -- indistinguishable by protocol from that test having noticed
    the mutation. Filed as an answer it would report `caught`, crediting a test
    that asserted nothing. 300s of wasted lane is visible; a false `caught` is
    not.
    """

    def collect(self, each: float) -> dict[str, Any]:
        """Drive the real probe, in a real subprocess, on a real fifo."""
        with support.tempdir() as box:
            (box / "tests").mkdir()
            (box / "tests" / "__init__.py").write_text("", encoding="utf-8")
            (box / "tests" / "test_hang.py").write_text(HANGS, encoding="utf-8")
            report = box / "verdict.json"
            done = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    Path("tools/verdict.py").read_text(encoding="utf-8"),
                    str(report),
                    "0",
                    "0",
                    str(each),
                    "tests.test_hang",
                ],
                cwd=box,
                env={**os.environ, "HANGDIR": str(box), "PYTHONPATH": str(box)},
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertTrue(report.is_file(), done.stderr[-800:])
            return dict(json.loads(report.read_text(encoding="utf-8")))

    def test_a_hung_test_is_interrupted_rather_than_waited_out(self) -> None:
        """A blocking `read()` on a fifo, which is the shape `tupferl/copies.py`
        hangs in when its not-a-regular-file guard is mutated away. PEP 475
        retries a syscall interrupted by a signal, so this only works because the
        handler *raises* rather than setting a flag."""
        found = self.collect(2)
        self.assertEqual(2, found["ran"], "the run did not get past the hung test")

    def test_it_is_never_counted_as_the_test_noticing(self) -> None:
        """The whole safety argument. `noticed` is what `caught` is made of."""
        found = self.collect(2)
        self.assertEqual([], found["noticed"])
        broke = [str(line) for line in found["broke"]]
        self.assertEqual(1, len(broke), broke)
        self.assertIn("test_hangs_on_a_fifo", broke[0])
        self.assertIn("did not finish", broke[0])

    def test_zero_disables_it(self) -> None:
        """So a platform without `SIGALRM`, or someone debugging a genuinely slow
        test, can turn it off -- and then the whole-run `TIMEOUT` is what bounds
        the hang, which is the behaviour before this existed."""
        with self.assertRaises(subprocess.TimeoutExpired):
            self.collect(0)
