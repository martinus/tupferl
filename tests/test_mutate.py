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
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests import support
from tools import mutants, mutate, verdict
from tools.mutants import Mutation, check

#: Seconds a driven probe may take before a test calls it hung. Above the ~2s
#: an honest `collect(2)` spends and well below `tools/mutate.py`'s `EACH_TEST`
#: of 30 -- see `TestAHungTestIsBoundedAndNotCredited.collect` for what happens
#: when it is not, and `tests/test_watch.py` for the same constant and the same
#: two bounds it has to sit between.
BOUND = 20

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


class TestWhatASandboxDoesNotCopy(unittest.TestCase):
    """#32: the sandbox copy must not race a directory something else is writing.

    `_sandboxes` copies the working tree per lane. `.hypothesis` is created and
    removed *by Hypothesis while the suite runs*, and `tools/run_tests.py` shards
    across eight workers with this module starting the harness inside one of
    them -- so `copytree` scanned `.hypothesis/tmp` and it was gone before the
    copy. CI went red on PR #31, whose diff touched no file in `tools/`.

    Driven against a real `_sandboxes` rather than by reading `_SKIP`: the
    constant is the mechanism, and a test that asserted its *contents* would
    pass against a `copytree` that had stopped passing it.
    """

    #: Every name `_SKIP` exists to keep out, and one that must survive.
    #: `sweeps` and `.hypothesis` are #32's; the rest were already there and are
    #: here so that dropping one is a failure rather than a silence.
    KEPT_OUT = (".git", "__pycache__", ".mypy_cache", ".ruff_cache", ".hypothesis", "sweeps")
    KEPT = "tupferl"

    def sandbox(self, tree: Path) -> Path:
        """One lane's copy of `tree`, through the real `_sandboxes`."""
        with (
            mock.patch.object(Path, "cwd", return_value=tree),
            mutate._sandboxes(1) as available,
        ):
            borrowed = available.get()
            copy = Path(str(borrowed))
            # Read while it is still borrowed: the context manager removes
            # the whole thing on the way out.
            return Path(shutil.copytree(copy, tree.parent / "seen"))

    def test_none_of_them_reaches_a_lane(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tupferl-skip-") as box:
            tree = Path(box) / "tree"
            (tree / self.KEPT).mkdir(parents=True)
            (tree / self.KEPT / "__init__.py").write_text("", encoding="utf-8")
            for name in self.KEPT_OUT:
                (tree / name).mkdir()
                (tree / name / "inside").write_text("x", encoding="utf-8")

            copy = self.sandbox(tree)
            for name in self.KEPT_OUT:
                with self.subTest(name=name):
                    self.assertFalse((copy / name).exists(), f"{name} was copied")
            self.assertTrue((copy / self.KEPT / "__init__.py").is_file(), "the tree was not copied")

    def test_a_nested_one_is_kept_out_too(self) -> None:
        """`shutil.ignore_patterns` matches the base name at any depth. A
        pattern that only applied at the root would leave this copied, and
        nothing would notice until the next red leg."""
        with tempfile.TemporaryDirectory(prefix="tupferl-skip-") as box:
            tree = Path(box) / "tree"
            deep = tree / self.KEPT / "somewhere" / ".hypothesis"
            deep.mkdir(parents=True)
            (deep / "tmp").write_text("x", encoding="utf-8")

            copy = self.sandbox(tree)
            self.assertTrue((copy / self.KEPT / "somewhere").is_dir(), "the tree was not copied")
            self.assertFalse((copy / self.KEPT / "somewhere" / ".hypothesis").exists())


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
            cache.learn(mutate.Report([mutate.Result(one, mutate.Verdict(outcome, "", killer))]))
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

    def collect(
        self,
        each: float,
        wait: float = BOUND,
        first: str = "",
        names: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        """Drive the real probe, in a real subprocess, on a real fifo.

        The argv is positional and shared with `mutate._run`: report, failfast,
        memory, per-test seconds, the prefix, then the selection. Spelling it out
        here is what made a protocol change visible -- when `first` gained its
        own slot, this helper's selection slid into it and the module ran twice.

        **`wait` was 60, which is twice `EACH_TEST`.** The two callers that use
        the default arm a 2s per-test alarm, so an honest run here takes about
        two seconds -- but a mutant that disables that alarm leaves the fifo read
        blocking, and at 60 the harness's own 30s alarm fired first. Measured:
        seven mutants of `verdict.py`'s alarm (`each_test`, `startTest`'s
        `setitimer`, `build`'s `made.each`) came back `BROKE`, and `BROKE` is
        never `caught` -- so the lines this class exists to guard were unguarded
        by the very bound written to guard them.

        That is the third instance of one mistake here, after
        `tests/test_watch.py`'s `ran` (30, equal to the alarm) and its two inline
        `subprocess.run` calls (60). Each was written deliberately to bound a
        hang, and each picked a number without checking it against the
        harness's.
        """
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
                    mutate._probe(),
                    str(report),
                    "0",
                    "0",
                    str(each),
                    first,
                    *(("tests.test_hang",) if names is None else names),
                ],
                cwd=box,
                env={**os.environ, "HANGDIR": str(box), "PYTHONPATH": str(box)},
                capture_output=True,
                text=True,
                timeout=wait,
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
        the hang, which is the behaviour before this existed.

        Three seconds, not sixty. The first version waited a minute for the
        subprocess timeout and so *was* a sixty-second test: it added that to
        every suite run, and once `EACH_TEST` existed the alarm killed it and
        broke two unrelated mutation rows, which is how it was noticed. A test
        that hangs on purpose has to be the cheapest possible version of itself.
        """
        with self.assertRaises(subprocess.TimeoutExpired):
            self.collect(0, wait=3)

    def test_zero_arms_nothing(self) -> None:
        """The same claim without a subprocess at all, because the one above can
        only ever say "it did not finish in three seconds"."""
        self.assertEqual(0.0, verdict.each_test(0))
        self.assertEqual(2.0, verdict.each_test(2))


class TestTheCheapPrefix(unittest.TestCase):
    """`Killers.prefix`: cheap tests that between them catch a lot, first.

    The remembered killer (above) helps a row that has been seen. This helps
    every row, including one seen for the first time -- which is exactly the case
    a per-row cache cannot serve.

    Greedy on rows-newly-caught-per-second, which is the 4-approximation for
    Min-Sum Set Cover and the best any polynomial algorithm gets unless P=NP.
    """

    def cache(self, known: dict[str, str], cost: dict[str, float]) -> mutate.Killers:
        made = mutate.Killers(None)
        made.known, made.cost = known, cost
        return made

    def test_a_cheap_test_that_catches_a_lot_comes_first(self) -> None:
        """The ordering is by *rate*, not by either half alone: `slow` catches
        the most and `rare` is the cheapest, and neither is the answer."""
        cache = self.cache(
            {f"k{n}": "tests.m.C.slow" for n in range(50)}
            | {f"c{n}": "tests.m.C.quick" for n in range(20)}
            | {"r0": "tests.m.C.rare"},
            {"tests.m.C.slow": 0.40, "tests.m.C.quick": 0.02, "tests.m.C.rare": 0.001},
        )
        self.assertEqual(["tests.m.C.quick", "tests.m.C.rare", "tests.m.C.slow"], cache.prefix())

    def test_it_stops_at_the_budget(self) -> None:
        """Every row pays this up front, so it is bounded in seconds rather than
        in tests -- tests do not all cost the same."""
        cache = self.cache(
            {f"k{n}": f"tests.m.C.t{n}" for n in range(20)},
            {f"tests.m.C.t{n}": 0.2 for n in range(20)},
        )
        chosen = cache.prefix()
        self.assertLessEqual(sum(0.2 for _ in chosen), mutate.PREFIX)
        self.assertLess(len(chosen), 20)

    def test_a_test_with_no_measured_cost_is_not_guessed_at(self) -> None:
        """A killer recorded before costs existed has no denominator, and
        inventing one would put it anywhere at all in the order."""
        cache = self.cache({"k": "tests.m.C.unmeasured"}, {})
        self.assertEqual([], cache.prefix())

    def test_nothing_is_covered_twice(self) -> None:
        """Greedy credits a test only with rows nothing before it caught, or the
        second-best test rides on the first one's coverage and the prefix fills
        with duplicates."""
        cache = self.cache(
            {"a": "tests.m.C.one", "b": "tests.m.C.one", "c": "tests.m.C.two"},
            {"tests.m.C.one": 0.01, "tests.m.C.two": 0.02},
        )
        self.assertEqual(["tests.m.C.one", "tests.m.C.two"], cache.prefix())


class TestWhichRowsGetThePrefix(unittest.TestCase):
    def rows(self, tests: str = "tests.test_sync") -> list[Mutation]:
        return [row()._replace(tests=tests)]

    def cache(self) -> mutate.Killers:
        made = mutate.Killers(None)
        made.cost = {REAL: 0.001}
        return made

    def test_a_row_with_a_remembered_killer_does_not_pay_for_it(self) -> None:
        """Exact beats general: that test is known to catch *this* row, so the
        prefix would only be work in front of the answer."""
        cache = self.cache()
        one = self.rows("tests.test_mutate")[0]
        cache.known = {mutate._key(one): REAL}
        (ahead,) = cache.ahead_of([one])
        self.assertEqual(REAL, ahead.first)

    def test_a_row_with_nothing_remembered_gets_the_prefix(self) -> None:
        cache = self.cache()
        cache.known = {"someone-else": REAL}
        with support.quiet():
            (ahead,) = cache.ahead_of(self.rows("tests.test_mutate"))
        self.assertEqual(REAL, ahead.first)

    def test_the_prefix_is_cut_to_what_the_row_can_reach(self) -> None:
        """A test in a module that does not import the mutated file cannot see
        the mutation, so running it would be pure cost."""
        cache = self.cache()
        cache.known = {"someone-else": REAL}
        with support.quiet():
            (ahead,) = cache.ahead_of(self.rows("tests.test_paths"))
        self.assertEqual("", ahead.first)


class TestTheCacheLearnsFromARealRun(unittest.TestCase):
    """The plumbing, not the algorithm.

    `TestTheCheapPrefix` sets `cost` by hand, so every one of its assertions
    passed while the harness was recording **zero** costs -- `sweep` re-wrapped
    the report without `times` and the prefix quietly ordered nothing. A test
    that builds its own inputs cannot see a data path that never delivers them,
    which is CLAUDE.md §8's pass nobody can explain.
    """

    def test_a_run_measures_the_tests_it_ran(self) -> None:
        found = mutate.run([UNWATCHED], baseline=True, workers=1, summarise=False)
        times = found.times or {}
        self.assertTrue(times, "the run recorded no test timings at all")
        # `tests.test_paths` is UNWATCHED's whole selection, so its tests are
        # exactly what should have been measured.
        self.assertTrue(
            any(name.startswith("tests.test_paths.") for name in times), sorted(times)[:5]
        )
        self.assertTrue(all(seconds >= 0 for seconds in times.values()))

    def test_they_reach_the_cache(self) -> None:
        found = mutate.run([UNWATCHED], baseline=True, workers=1, summarise=False)
        cache = mutate.Killers(None)
        cache.learn(found)
        self.assertEqual(found.times or {}, cache.cost)


class TestThePrefixReachesTheExpensiveRows(unittest.TestCase):
    """Which rows the prefix is cut to, and the two it used to be cut *out* of.

    The first version compared module names -- `test.rsplit(".", 2)[0] in
    reachable` -- which dropped the prefix in the two places it was worth most.
    Both are here because neither is visible in a `tupferl/` sweep: every file
    there has an importer, so every row names modules and matches. It is the
    `tools/` sweeps, and any new file nothing imports yet, that hit them.
    """

    def cache(self) -> mutate.Killers:
        made = mutate.Killers(None)
        made.cost = {REAL: 0.001}
        made.known = {"a-row-that-is-not-this-one": REAL}
        return made

    def ahead(self, tests: str) -> Mutation:
        with support.quiet():
            (ahead,) = self.cache().ahead_of([row()._replace(tests=tests)])
        return ahead

    def test_a_row_that_runs_everything_gets_the_whole_prefix(self) -> None:
        """`WHOLE_SUITE` is the empty string -- what a file nothing imports gets,
        so its rows run the entire suite at ~51s each. Cutting the prefix to
        "modules named in an empty selection" left them with nothing, which is
        the most expensive row in the table paying the most for the omission.

        Safe only because `first` reaches the probe as its own argument. Merged
        into the selection it would make an empty list non-empty, and "run
        everything" would become "run the prefix" -- see `verdict.collect`.
        """
        self.assertEqual(REAL, self.ahead(mutate.WHOLE_SUITE).first)

    def test_a_selection_naming_a_class_still_matches_its_tests(self) -> None:
        """`tests.test_mutate.TestX` selects `tests.test_mutate.TestX.test_y`.
        Comparing module names made this never match, so any row selected at
        class granularity silently lost the prefix."""
        klass = REAL.rsplit(".", 1)[0]
        self.assertEqual(REAL, self.ahead(klass).first)

    def test_a_row_that_cannot_reach_it_still_does_not_pay(self) -> None:
        """The guard the two above must not break: a test in a module that does
        not import the mutated file cannot see the mutation."""
        self.assertEqual("", self.ahead("tests.test_paths").first)


class TestConfirmationReallyRunsTheWholeSuite(unittest.TestCase):
    """CLAUDE.md promises every survivor is re-run against the whole suite
    before it is reported, and `Report.widened` is the flag that claims it.

    Two things could quietly make that false, and both are one character wide.
    `WHOLE_SUITE` is the *empty* selection -- `verdict.collect` falls through to
    `discover` only when the list is empty -- so anything in front of it turns
    "everything" into "only this". The rows `confirm` builds are exactly the
    shape that triggers it: a survivor's selection widened while its remembered
    test is still attached.
    """

    def test_a_widened_row_carries_no_remembered_test(self) -> None:
        survivor = mutate.Result(
            row()._replace(first="tests.test_sync.TestTheReport.test_it"),
            mutate.Verdict("survived"),
        )
        widened = survivor.mutation._replace(tests=mutate.WHOLE_SUITE, first="")
        self.assertEqual("", widened.first)
        self.assertEqual("", widened.tests)

    def test_an_empty_selection_behind_a_prefix_still_discovers(self) -> None:
        """The protocol half: an empty selection plus a prefix must run the
        prefix *and* everything, not the prefix instead of everything.

        Driven in the probe's own two-test tree rather than against this
        repository, which would run the whole suite inside a test of it.
        """
        found = TestAHungTestIsBoundedAndNotCredited.collect(
            self,  # type: ignore[arg-type]
            # Armed, because discovery reaches that tree's deliberately hanging
            # test. With the alarm off this test hung for its whole subprocess
            # timeout -- proving the discovery worked, at thirty seconds a run.
            each=2,
            wait=30,
            first="tests.test_hang.TestOne.test_is_fine",
            names=(),
        )
        # Two tests in that module, and the prefix names one of them -- so it
        # runs twice, once in front and once as discovery reaches it. Anything
        # less than three means the empty selection stopped discovering.
        self.assertEqual(3, found["ran"])


class TestARowActuallyRunsWithItsPrefix(unittest.TestCase):
    """The gap that let the `WHOLE_SUITE` defect through.

    Every other test here asserts on what `ahead_of` *returns*. None drove
    `_attempt`, so nothing noticed that the argv it built turned "discover
    everything" into "run these three" -- the review found it by reading. These
    drive a real mutation with a real `first`.
    """

    def test_a_prefix_that_catches_it_still_reports_caught(self) -> None:
        one = UNKNOWN_KEY_GUARD._replace(
            first="tests.test_config.TestRejectingAnUnknownKey.test_a_typo_is_an_error_rather_than_silence"
        )
        found = mutate.run([one], baseline=False, workers=1, summarise=False)
        self.assertEqual(["caught"], [r.verdict.outcome for r in found.results])

    def test_a_prefix_that_misses_falls_through_to_the_selection(self) -> None:
        """The safety argument, driven rather than asserted on a data structure:
        a prefix that cannot see the mutation must cost one test, not the
        answer."""
        one = UNKNOWN_KEY_GUARD._replace(first="tests.test_paths.TestWhereTheRepositoryGoes")
        found = mutate.run([one], baseline=False, workers=1, summarise=False)
        self.assertEqual(["caught"], [r.verdict.outcome for r in found.results])

    # The `WHOLE_SUITE` case is *not* driven through `mutate.run` here. It would
    # discover and run this entire suite inside a mutation sandbox -- ~50s, for a
    # claim `TestConfirmationReallyRunsTheWholeSuite` already proves against the
    # probe's own two-test tree in milliseconds. Adding a minute to every run to
    # re-state something is the mistake `test_zero_disables_it` already made
    # once today.


class TestASpecFileGetsTheFlagsItWasGiven(unittest.TestCase):
    """A `MUTATIONS` table honours the command line it was run with.

    It did not. The dispatch was `run(mutations)` -- no arguments -- so
    `argparse` accepted `--workers`, `--memory`, `--timeout`, `--each-test`,
    `--no-baseline`, `--no-confirm` and `--json`, and every one of them was
    dropped on the floor. Asking for one lane got two. Asking for a report got no
    file, which reads as the run having failed to write one rather than as the
    flag never having been consulted, and cost an hour of this author's time
    diagnosing the wrong thing.

    Each test below fails against `run(mutations)`, because that call cannot
    carry the value being asserted.
    """

    #: A real row against a real file, because the `--json` test below drives the
    #: actual run rather than a stub: `check` refuses a path that is not there,
    #: and a report of nothing would not tell us the flag was honoured.
    #: `tests.test_merge` is eleven tests in 0.09s.
    #:
    #: One that is *caught*, deliberately. With a surviving row, a mutant that
    #: forces confirmation on sends the `--json` test into a whole-suite re-run
    #: and past the harness's 30s per-test alarm -- reported `BROKE`, which is no
    #: verdict at all. Nothing here asserts on the survivor count, so the cheaper
    #: row costs the tests nothing and gives the sweep two answers back.
    TABLE = (
        "from tools.mutants import Mutation\n"
        "MUTATIONS = [\n"
        "    Mutation(\n"
        '        label="probe",\n'
        '        path="tupferl/merge.py",\n'
        '        old="WHOLE_FILE = 1",\n'
        '        new="WHOLE_FILE = 2",\n'
        '        tests="tests.test_merge",\n'
        "    )\n"
        "]\n"
    )

    def spec(self, box: Path) -> Path:
        where = box / "spec.py"
        where.write_text(self.TABLE, encoding="utf-8")
        return where

    def asked(self, *flags: str) -> dict[str, Any]:
        """Run a spec file with `flags` and return the kwargs `run` received.

        `run` is replaced rather than driven, because what is under test is the
        wiring between the parser and the call -- not what a mutation does, which
        every other class here covers and which would cost a suite run per flag.
        """
        seen: dict[str, Any] = {}

        def watch(table: Any, *args: Any, **kwargs: Any) -> mutate.Report:
            seen.update(kwargs)
            seen["positional"] = args
            return mutate.Report([])

        with tempfile.TemporaryDirectory(prefix="tupferl-spec-") as name:
            box = Path(name)
            with mock.patch.object(mutate, "run", watch):
                mutate.main([str(self.spec(box)), "--no-confirm", *flags])
        return seen

    def test_workers_reaches_the_run(self) -> None:
        self.assertEqual(1, self.asked("--workers", "1")["workers"])

    def test_memory_reaches_the_run(self) -> None:
        self.assertEqual(0, self.asked("--memory", "0")["memory"])

    def test_the_timeout_reaches_the_run(self) -> None:
        self.assertEqual(7.0, self.asked("--timeout", "7")["timeout"])

    def test_the_per_test_alarm_reaches_the_run(self) -> None:
        self.assertEqual(3.0, self.asked("--each-test", "3")["each"])

    def test_no_baseline_reaches_the_run(self) -> None:
        self.assertFalse(self.asked("--no-baseline")["baseline"])

    def test_the_baseline_is_on_by_default(self) -> None:
        """The other half: a wiring that hard-coded `False` would pass the test
        above and quietly stop checking the untouched tree."""
        self.assertTrue(self.asked()["baseline"])

    def test_json_is_written_and_marked_done(self) -> None:
        """Not through the `run` stub -- this one drives the real thing, because
        the report and its `.done` marker are what a watcher reads and a stub
        cannot produce them."""
        with tempfile.TemporaryDirectory(prefix="tupferl-spec-") as name:
            box = Path(name)
            report = box / "out.json"
            mutate.main(
                [str(self.spec(box)), "--no-baseline", "--no-confirm", "--json", str(report)]
            )
            self.assertTrue(report.is_file(), "--json wrote nothing")
            self.assertIn("results", json.loads(report.read_text(encoding="utf-8")))
            self.assertTrue(
                report.with_suffix(".json.done").is_file(), "the run left no done marker"
            )


class TestWhatASpecFileExitsWith(unittest.TestCase):
    """The exit status, and whether survivors get confirmed.

    Eight mutants survived the first sweep of `_run_spec`, every one of them
    here: the tests above stub `run` to read its kwargs, which cannot see what
    the function *returns* or which branch it takes afterwards. A stub that
    proves the wiring says nothing about the answer.
    """

    def spec(self, box: Path, old: str, new: str) -> Path:
        where = box / "spec.py"
        where.write_text(
            "from tools.mutants import Mutation\n"
            "MUTATIONS = [\n"
            "    Mutation(\n"
            '        label="probe",\n'
            f'        path="tupferl/merge.py",\n'
            f"        old={old!r},\n"
            f"        new={new!r},\n"
            '        tests="tests.test_merge",\n'
            "    )\n"
            "]\n",
            encoding="utf-8",
        )
        return where

    def status(self, old: str, new: str, *flags: str) -> int:
        with tempfile.TemporaryDirectory(prefix="tupferl-exit-") as name, support.quiet():
            return mutate.main([str(self.spec(Path(name), old, new)), "--no-baseline", *flags])

    #: A mutation `tests.test_merge` notices, and one it does not. Both were
    #: measured rather than assumed: `PROBE` shrinking to 1 survives, which is
    #: itself a fair finding about that constant and not this test's business.
    CAUGHT = ("WHOLE_FILE = 1", "WHOLE_FILE = 2")
    SURVIVES = ("PROBE = 8000", "PROBE = 1")

    def test_a_caught_table_exits_zero(self) -> None:
        self.assertEqual(0, self.status(*self.CAUGHT, "--no-confirm"))

    def test_a_surviving_table_exits_one(self) -> None:
        """The other half. Without it, "always returns 0" passes the test above
        and a spec file full of decoration reports success.

        `--no-confirm` is stubbed out rather than trusted: a mutant that forces
        confirmation on would otherwise send this into a whole-suite re-run and
        past the harness's 30s alarm, which is `BROKE` -- no verdict for the
        line under test. What this asserts is the exit status, and confirmation
        of a survivor cannot change it.
        """
        with mock.patch.object(mutate, "confirm", lambda report, *a, **k: report):
            self.assertEqual(1, self.status(*self.SURVIVES, "--no-confirm"))

    def test_survivors_are_confirmed_against_the_whole_suite_by_default(self) -> None:
        """CLAUDE.md's promise about a survivor before it is reported. This path
        never kept it, so `--no-confirm` turned off something that was not
        happening."""
        seen: list[bool] = []
        real = mutate.confirm

        def watch(report: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(True)
            return real(report, *args, **kwargs)

        with mock.patch.object(mutate, "confirm", watch):
            self.status(*self.CAUGHT)
        self.assertEqual([True], seen, "survivors were reported without being confirmed")

    def test_confirmation_is_told_what_the_run_was_told(self) -> None:
        """Its `baseline` comes from `--no-baseline` like the run's does. Nothing
        looked at what `confirm` was handed, so a wiring that passed the flag
        through un-negated -- checking a baseline the caller asked to skip --
        went unnoticed."""
        seen: dict[str, Any] = {}

        def watch(report: Any, *args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return report

        with mock.patch.object(mutate, "confirm", watch):
            self.status(*self.CAUGHT)
        self.assertFalse(seen["baseline"], "confirmation re-checked a skipped baseline")

    def test_no_confirm_really_turns_it_off(self) -> None:
        """The precondition for the test above: if `confirm` ran either way, the
        assertion there would hold against a wiring that ignored the flag."""
        seen: list[bool] = []

        def watch(report: Any, *args: Any, **kwargs: Any) -> Any:
            seen.append(True)
            return report

        with mock.patch.object(mutate, "confirm", watch):
            self.status(*self.CAUGHT, "--no-confirm")
        self.assertEqual([], seen, "--no-confirm confirmed anyway")


class TestASpecFileWithNothingInIt(unittest.TestCase):
    """A script that defines no table and never calls `verify` is a mistake, and
    saying so is the only useful thing left to do.

    Guarded because the branch that reaches it is one `if mutations:` away from
    handing `None` to a function that expects a table -- which the sweep found
    unguarded, and which would report the mistake as a traceback from inside the
    harness rather than as the sentence that says what shape a spec file takes.
    """

    def test_it_says_what_a_spec_file_should_look_like(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tupferl-empty-") as name:
            where = Path(name) / "spec.py"
            where.write_text("x = 1\n", encoding="utf-8")
            with self.assertRaises(SystemExit) as raised:
                mutate.main([str(where)])
        self.assertIn("MUTATIONS", str(raised.exception))


class TestWhoOwnsTheMachine(unittest.TestCase):
    """`_budget` halves a shared machine and does not halve a dedicated one.

    The halving was unconditional, and on a machine with nobody else on it that
    is waste rather than thrift: 16 GiB and four cores gave a budget of 8037 MiB,
    which `_share` turned into **three** lanes where the cores wanted eight.
    Measured on one interleaved pair of the same 17-mutant table: 283.7s at three
    lanes against 154.7s at seven.
    """

    #: Bigger than `_SPARE` and than `_FLOOR`, so "leave a gibibyte" and "never
    #: go under the floor" are both visible rather than clipping each other.
    VISIBLE = 16 << 30

    def budget(self, **environment: str) -> int:
        seen = mock.patch.object(mutate, "_visible_memory", lambda: self.VISIBLE)
        with seen, mock.patch.dict(os.environ, environment, clear=True):
            return mutate._budget()

    def test_a_shared_machine_keeps_half_for_the_person_using_it(self) -> None:
        self.assertEqual(self.VISIBLE // 2, self.budget())

    def test_a_ci_runner_is_not_shared(self) -> None:
        """Nobody is waiting for their editor on a CI runner, and every CI
        system sets this.

        The gibibyte is written out rather than taken from `mutate._SPARE`:
        against the constant this assertion changes with the code it checks and
        holds for any value of it, which is CLAUDE.md §2's copy-of-the-code by
        name. The sweep found it -- both mutations of `_SPARE` survived.
        """
        self.assertEqual(self.VISIBLE - (1 << 30), self.budget(CI="true"))

    def test_a_cgroup_limit_means_the_share_is_already_carved_out(self) -> None:
        """Halving a cgroup limit double-counts the same reservation: the
        container has no other half to leave, because nobody else is in it."""
        with mock.patch.object(mutate, "_confined", lambda: 1 << 30):
            self.assertEqual(self.VISIBLE - (1 << 30), self.budget())

    def test_being_told_beats_both(self) -> None:
        asked = 12 << 30
        self.assertEqual(asked, self.budget(**{mutate._TOTAL: str(asked)}))
        self.assertEqual(asked, self.budget(CI="true", **{mutate._TOTAL: str(asked)}))

    def test_nonsense_in_the_variable_is_ignored_rather_than_obeyed(self) -> None:
        for said in ("", "0", "-1", "lots"):
            with self.subTest(said=said):
                self.assertEqual(self.VISIBLE // 2, self.budget(**{mutate._TOTAL: said}))

    def test_a_tiny_dedicated_machine_never_drops_under_the_floor(self) -> None:
        """Otherwise subtracting `_SPARE` hands it less than one lane's ceiling
        and it gets *fewer* lanes than the shared rule would have given -- the
        opposite of the point."""
        tiny = mock.patch.object(mutate, "_visible_memory", lambda: (1 << 30) + (1 << 20))
        with tiny, mock.patch.dict(os.environ, {"CI": "true"}, clear=True):
            self.assertEqual(mutate._FLOOR, mutate._budget())

    def test_the_run_says_which_rule_it_used(self) -> None:
        """A lane count nobody can account for is what sent this author reading
        `_share` in the first place."""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn("shared", mutate._why())
        with mock.patch.dict(os.environ, {"CI": "true"}, clear=True):
            self.assertIn("dedicated", mutate._why())
        with mock.patch.dict(os.environ, {mutate._TOTAL: "123"}, clear=True):
            # `mutate._TOTAL`, not the literal. This asserted `"--budget"` and
            # passed for a release, naming a flag the parser has never had --
            # so someone reading the printed line and typing it got
            # "unrecognized arguments". A test that pins prose has to pin it
            # against the thing it describes, or it guards the wrong name just
            # as firmly as the right one.
            self.assertEqual(mutate._TOTAL, mutate._why())


class TestReadingACgroupLimit(unittest.TestCase):
    """`_confined` tells a real limit from the two ways of saying "no limit"."""

    HOST = 16 << 30

    def confined(self, written: str) -> int:
        def reads(where: str, **kwargs: object) -> str:
            del where, kwargs
            return written

        # Both patched by name rather than through `mutate.<attr>`: the module
        # imports `os` and `Path`, it does not re-export them, and mypy is right
        # to say so.
        host = mock.patch(
            "os.sysconf", lambda name: {"SC_PAGE_SIZE": 1, "SC_PHYS_PAGES": self.HOST}[name]
        )
        with mock.patch("pathlib.Path.read_text", reads), host:
            return mutate._confined()

    def test_a_real_limit_below_the_host_total_counts(self) -> None:
        self.assertEqual(2 << 30, self.confined(str(2 << 30)))

    def test_cgroup_v2_writes_max_for_no_limit(self) -> None:
        self.assertEqual(0, self.confined("max"))

    def test_cgroup_v1_writes_a_sentinel_near_two_to_the_sixty_three(self) -> None:
        """Which is what this machine's `memory.limit_in_bytes` actually holds:
        9223372036854771712. Read as a limit it would look like the largest
        dedicated machine ever built."""
        self.assertEqual(0, self.confined("9223372036854771712"))


class TestWhenTheMachineCannotSayHowBigItIs(unittest.TestCase):
    """`_confined`'s answers when the question cannot be asked.

    Every one of these lines survived the first sweep of the budget change: the
    tests above mock `_confined` wholesale, which proves what `_budget` does with
    its answer and nothing about how the answer is reached.
    """

    def test_no_host_total_means_no_limit_can_be_judged(self) -> None:
        """`sysconf` is not POSIX everywhere. Without a host total there is
        nothing to compare a cgroup file against, and calling any number a limit
        would be guessing -- a v1 sentinel would read as a machine with eight
        exabytes."""
        with mock.patch("os.sysconf", side_effect=OSError):
            self.assertEqual(0, mutate._confined())

    def test_no_cgroup_file_means_the_host_bounds_us(self) -> None:
        with mock.patch("pathlib.Path.read_text", side_effect=OSError):
            self.assertEqual(0, mutate._confined())

    def test_a_shared_machine_is_reported_as_the_empty_string(self) -> None:
        """`dedicated` returns a *reason*, and "no reason" has to be falsy for
        `_budget` to read it. `None` would work there and break the line that
        prints it."""
        alone = mock.patch.object(mutate, "_confined", lambda: 0)
        with mock.patch.dict(os.environ, {}, clear=True), alone:
            self.assertEqual("", mutate.dedicated())

    def test_a_budget_of_one_byte_is_still_a_budget(self) -> None:
        """The boundary on `int(said) > 0`. A fixture using a comfortable number
        cannot tell that from `> 1`."""
        with mock.patch.dict(os.environ, {mutate._TOTAL: "1"}, clear=True):
            self.assertEqual(1, mutate._budget())


class TestTheRunAccountsForItsLanes(unittest.TestCase):
    """The line is printed when the machine cut the run down, and not when it
    did not.

    Both halves: a run that always explains itself is noise, and one that never
    does is what sent this author reading `_share` to find where three lanes on
    a four-core box came from.
    """

    #: One real row, because `run` returns before any of this on an empty table.
    #: `_attempt` is stubbed, so the row is never applied and its content only
    #: has to survive `check`.
    ROW = Mutation(
        label="probe",
        path="tupferl/merge.py",
        old="PROBE = 8000",
        new="PROBE = 1",
        tests="tests.test_merge",
    )

    def lines(self, wanted: int, given: mutate.Share) -> str:
        answered = mock.patch.object(
            mutate, "_attempt", lambda *a, **k: mutate.Verdict("caught", "probe")
        )
        with (
            mock.patch.object(mutate, "_share", lambda *a, **k: given),
            mock.patch.object(mutate, "usable_cpus", lambda: wanted),
            answered,
            support.quiet() as spill,
        ):
            mutate.run([self.ROW], baseline=False, summarise=False)
        return spill.getvalue()

    #: What `run` asks for here: one row and one shard, so `len(table) + shards`
    #: caps it at two whatever the cores say.
    WANTED = 2

    def test_it_says_so_when_the_machine_cut_the_lanes_down(self) -> None:
        said = self.lines(8, mutate.Share(self.WANTED - 1, mutate.MEMORY))
        self.assertIn("1 lane(s)", said)
        self.assertIn("see tools.mutate._share", said)

    def test_it_stays_quiet_when_nothing_was_taken_away(self) -> None:
        """The other half. Without it, "always print" passes the test above and
        every run carries a line about a limit that did not bind."""
        self.assertNotIn("lane(s)", self.lines(8, mutate.Share(self.WANTED, mutate.MEMORY)))
