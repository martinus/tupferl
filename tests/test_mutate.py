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

import argparse
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

    #: `walk=False` throughout this class, and it is not a shortcut. What these
    #: assert is the *classification* -- caught, survived, and the tree left
    #: alone -- which `tests/test_verdict.py` covers the walk of separately. With
    #: it on, `UNWATCHED` is a survivor by construction, so it runs the whole
    #: suite before it can be called one: three of these tests went from seconds
    #: to about two minutes each, and the baseline from one small shard to
    #: another whole-suite run. That is the design working as intended on a
    #: sweep and pure cost inside the harness's own tests.
    WALK = False

    def test_a_deliberate_bug_is_caught(self) -> None:
        report = mutate.run(
            [UNKNOWN_KEY_GUARD], baseline=True, workers=1, summarise=False, walk=self.WALK
        )
        self.assertFalse(report.baseline_red, "the untouched tree is not green")
        self.assertEqual(["caught"], [result.verdict.outcome for result in report.results])

    def test_an_unwatched_bug_survives(self) -> None:
        """The other answer. Without this, `test_a_deliberate_bug_is_caught`
        passes just as well against a harness hard-wired to say `caught` -- the
        assertion that passes against its own mutation, from CLAUDE.md §2."""
        report = mutate.run([UNWATCHED], baseline=True, workers=1, summarise=False, walk=self.WALK)
        self.assertFalse(report.baseline_red)
        self.assertEqual(["survived"], [result.verdict.outcome for result in report.results])
        self.assertFalse(report.widened, "a report that did not walk claimed it had")

    def test_the_walk_catches_what_the_selection_missed(self) -> None:
        """The walk, end to end through the real harness, on the fixture built to
        need it.

        `UNWATCHED` is `UNKNOWN_KEY_GUARD`'s edit pointed at `tests.test_paths`,
        which never parses a config file -- so it survives its *selection* by
        construction. With the walk on it does not survive the run: the walk goes
        on past that selection and reaches
        `tests.test_config.TestRejectingAnUnknownKey`, which does see the edit.
        That is the whole change in one row, and it is why the pair above sets
        `walk=False`: with it on there is no survivor there to assert.

        It also disproves a reading of the recorded sweeps I had believed. "The
        killer was inside its own selection in 1,516 of 1,516 caught rows" cannot
        mean the tail never catches anything, because those runs never *ran* the
        tail for a caught row -- a killer outside the selection had no way to be
        recorded. What that figure actually shows is narrower: the confirmation
        pass never corrected a survivor in them.
        """
        report = mutate.run([UNWATCHED], baseline=True, workers=1, summarise=False)
        self.assertFalse(report.baseline_red, "the untouched tree is not green")
        self.assertEqual(["caught"], [result.verdict.outcome for result in report.results])
        killer = report.results[0].verdict.killer
        self.assertTrue(
            killer.startswith("tests.test_config."),
            f"caught, but not by the module the walk had to reach: {killer}",
        )
        self.assertTrue(report.widened)

    def test_the_working_tree_is_untouched(self) -> None:
        """CLAUDE.md §6: the harness must never edit the tree it is run from.

        Asserted on the file's own bytes, before and after, rather than on
        `git status` -- the guarantee is about this file, and `git status` would
        also be satisfied by an edit that was made and then put back.
        """
        where = Path(UNKNOWN_KEY_GUARD.path)
        before = where.read_bytes()
        mutate.run([UNKNOWN_KEY_GUARD], baseline=False, workers=1, summarise=False, walk=self.WALK)
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
                    # A baseline's shape: these tests are about what one named
                    # selection reports, and a walk would run the sandbox's
                    # other modules under each of them.
                    "0",
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
        found = mutate.run([UNWATCHED], baseline=True, workers=1, summarise=False, walk=False)
        times = found.times or {}
        self.assertTrue(times, "the run recorded no test timings at all")
        # `tests.test_paths` is UNWATCHED's whole selection, so its tests are
        # exactly what should have been measured.
        self.assertTrue(
            any(name.startswith("tests.test_paths.") for name in times), sorted(times)[:5]
        )
        self.assertTrue(all(seconds >= 0 for seconds in times.values()))

    def test_they_reach_the_cache(self) -> None:
        found = mutate.run([UNWATCHED], baseline=True, workers=1, summarise=False, walk=False)
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


class TestEverySurvivorHasRunTheWholeSuite(unittest.TestCase):
    """CLAUDE.md promises every survivor has been run against the whole suite
    before it is reported, and `Report.widened` is the flag that claims it.

    It used to be earned by a second pass over the survivors. It is structural
    now: every row walks outward past its selection until something notices, so
    a row *called* a survivor has run everything by construction. What is left
    to guard is that `run` says so, and that the one shape which could quietly
    make "everything" mean "only this" still does not -- `WHOLE_SUITE` is the
    *empty* selection, and `verdict.collect` falls through to `discover` only
    when the list is empty, so anything pushed in front of it truncates the run.
    """

    def test_a_report_claims_the_guarantee_it_now_keeps(self) -> None:
        """`run` sets it, rather than a later pass earning it. Without this the
        flag reverts to its old default and `tools/reached.py` prints its caveat
        about survivors nobody widened -- on a report where they were.

        `run` itself is real; only `_attempt` is stubbed. What is asserted is
        `run`'s own report construction, and driving it for real would mean
        finding a mutation caught inside a small module -- which is a fact about
        the suite, not about this line, and the first attempt at it walked the
        whole suite for two minutes to assert one boolean.
        """
        caught = Mutation(
            "x:1 in f()",
            "tests/profiles.py",
            '{"mutation": (3, 4)}',
            '{"mutation": (0, 0)}',
            "tests.test_profiles",
            operator="branch",
        )
        caught_by = mutate.Verdict("caught", "t")
        with (
            mock.patch.object(mutate, "_attempt", lambda *a, **k: caught_by),
            support.quiet(),
        ):
            report = mutate.run([caught], baseline=False, workers=1)
        self.assertTrue(report.widened, "a walked report did not claim the guarantee")

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
    `--no-baseline` and `--json`, and every one of them was
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
                mutate.main([str(self.spec(box)), *flags])
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
            mutate.main([str(self.spec(box)), "--no-baseline", "--json", str(report)])
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
        self.assertEqual(0, self.status(*self.CAUGHT))

    def test_a_surviving_table_exits_one(self) -> None:
        """The other half. Without it, "always returns 0" passes the test above
        and a spec file full of decoration reports success.

        **`_attempt` is stubbed, and that is a change worth naming.** This used
        to drive the real mutation with `--no-confirm`, which is what kept a
        survivor from paying for a whole-suite re-run. There is no such flag
        now: a survivor walks the rest of the suite inside its own row, by
        design, so the unstubbed spelling runs this repository's entire suite --
        about two minutes -- to assert one exit status. Everything from
        `argparse` to the return value is still real; only the verdict is
        supplied, and the verdict is not what this asserts.
        """
        survived = mutate.Verdict("survived")
        with tempfile.TemporaryDirectory(prefix="tupferl-exit-") as name, support.quiet():
            spec = self.spec(Path(name), *self.SURVIVES)
            with mock.patch.object(mutate, "_attempt", lambda *a, **k: survived):
                self.assertEqual(1, mutate.main([str(spec), "--no-baseline"]))


class TestWhichKillersNothingBaselined(unittest.TestCase):
    """`_unbaselined`: the hole the walk opens, and the only thing that closes it.

    A row nothing in its selection notices keeps going through the rest of the
    suite, so it can be caught by a test no baseline shard ran untouched. On a
    tree already red that claim is free -- `failfast` stops at the first red test
    whatever it was about -- which is the false `caught` of woswoar#268.

    Checking these tests rather than the whole suite is what keeps it affordable.
    The whole-suite baseline was tried and measured: six minutes of preflight, a
    second harness started inside every sandbox, and finally
    `BASELINE NOT GREEN (timeout)`, which voids every verdict above it.
    """

    def caught(self, killer: str, tests: str = "tests.test_paths") -> mutate.Result:
        return mutate.Result(row()._replace(tests=tests), mutate.Verdict("caught", "d", killer))

    def test_a_killer_its_shard_covered_needs_nothing(self) -> None:
        found = self.caught("tests.test_paths.TestA.test_b")
        self.assertEqual([], mutate._unbaselined([found], ["tests.test_paths"]))

    def test_a_killer_no_shard_covered_is_returned(self) -> None:
        """The one the walk produces. `UNWATCHED` is exactly this shape in the
        real harness: selected on `tests.test_paths`, caught in `test_config`."""
        found = self.caught("tests.test_config.TestRejectingAnUnknownKey.test_it")
        self.assertEqual(
            ["tests.test_config.TestRejectingAnUnknownKey.test_it"],
            mutate._unbaselined([found], ["tests.test_paths"]),
        )

    def test_the_whole_suite_shard_covers_everything(self) -> None:
        """`WHOLE_SUITE` is the *empty* selection and means "run the lot", so a
        shard list holding one covers every test. Read as a plain string it
        matches nothing instead, and every killer would be re-checked."""
        found = self.caught("tests.test_config.TestRejectingAnUnknownKey.test_it")
        self.assertEqual([], mutate._unbaselined([found], [mutate.WHOLE_SUITE]))

    def test_only_caught_rows_are_asked_about(self) -> None:
        """A survivor has no killer to stand behind, and a `broke` row's is not
        an answer. Without the outcome check a stale `killer` on either would
        send the run off to baseline a test that decided nothing."""
        survivor = mutate.Result(row(), mutate.Verdict("survived"))
        broke = mutate.Result(row(), mutate.Verdict("broke", "d", "tests.test_config.T.t"))
        self.assertEqual([], mutate._unbaselined([survivor, broke], ["tests.test_paths"]))

    def test_a_class_shard_still_covers_its_own_tests(self) -> None:
        """`run_tests.selects` rather than comparing module names: a shard naming
        a class never matched at all when this was spelled by hand, and every row
        it caught was sent for re-baselining."""
        found = self.caught("tests.test_config.TestRejectingAnUnknownKey.test_it")
        covered = ["tests.test_config.TestRejectingAnUnknownKey"]
        self.assertEqual([], mutate._unbaselined([found], covered))

    def test_one_whole_suite_shard_among_several_covers_everything(self) -> None:
        """`any`, not `all`. With a single shard the two agree, which is why the
        test above cannot tell them apart: `all` over one element *is* `any` over
        it. Given a `WHOLE_SUITE` shard beside a narrow one -- the shape every
        table with a file nothing imports produces -- `all` is False, the guard
        does not fire, and every killer is sent off to be re-baselined against a
        run that already covered it.
        """
        found = self.caught("tests.test_config.TestRejectingAnUnknownKey.test_it")
        mixed = [mutate.WHOLE_SUITE, "tests.test_paths"]
        self.assertEqual([], mutate._unbaselined([found], mixed))

    def test_a_killer_covered_by_one_shard_of_several_needs_nothing(self) -> None:
        """The second `any`, and the same trap. A killer is baselined if *some*
        shard ran it, not if every shard did -- and a table always has several.
        Read as `all`, a killer in `test_config` is called uncovered because
        `test_paths` did not also run it, so every caught row in a multi-shard
        sweep would drag the run into a re-baseline it does not need.
        """
        found = self.caught("tests.test_config.TestRejectingAnUnknownKey.test_it")
        several = ["tests.test_paths", "tests.test_config"]
        self.assertEqual([], mutate._unbaselined([found], several))

    def test_a_sibling_module_is_not_covered_by_a_prefix_of_its_name(self) -> None:
        """The dot `run_tests.selects` anchors on, and the reason a bare
        `startswith` is wrong rather than merely loose.

        `tests.test_sync` is a real shard and `tests.test_sync_cli` is a real
        module beside it. Read as a prefix, the first covers the second, so a
        killer in `test_sync_cli` would be called baselined by a shard that never
        ran it -- the false `caught` this function exists to prevent, arriving
        through the check meant to catch it. The class test above cannot see this:
        both spellings agree there.
        """
        found = self.caught("tests.test_sync_cli.TestTheRemoteLine.test_it")
        self.assertEqual(
            ["tests.test_sync_cli.TestTheRemoteLine.test_it"],
            mutate._unbaselined([found], ["tests.test_sync"]),
        )


class TestARowCaughtByAnUnbaselinedTest(unittest.TestCase):
    """What `run` *does* once `_unbaselined` finds one, which is the whole point
    of finding it.

    The guard against woswoar#268: a row the walk carried past its selection can
    be caught by a test the baseline never ran, and if that test fails on the
    untouched tree the `caught` is free -- `failfast` stops at the first red
    test whatever it was about. Both real survivors of one sweep came back
    credited to a shell-hook test that had never heard of the file under
    mutation.

    Every mutation of this block survived the first sweep of it -- the check
    fires only when a caught row's killer is unbaselined *and* that test is red
    untouched, and nothing in the suite arranged both. These do, by supplying the
    two verdicts and driving the real `run` between them.
    """

    KILLER = "tests.test_config.TestRejectingAnUnknownKey.test_it"

    def report(self, loose_outcome: mutate.Outcome) -> mutate.Report:
        """One row, caught by a test its selection never named, with the extra
        shard answering ``loose_outcome``."""
        # A real, unique edit: `check` refuses text that is not, and it runs
        # before anything here is reached. Nothing applies it -- `_attempt` is
        # supplied below -- but the row still has to be one `run` accepts.
        one = Mutation(
            "x:1 in f()",
            "tests/profiles.py",
            '{"mutation": (3, 4)}',
            '{"mutation": (0, 0)}',
            "tests.test_paths",
            operator="branch",
        )
        caught = mutate.Verdict("caught", "d", self.KILLER)

        def answered(available: Any, tests: Any, *rest: Any) -> mutate.Verdict:
            """Green for the baseline's own shards, `loose_outcome` for the extra
            one. Answering every call the same way was the first spelling and it
            made the *baseline* red, so the run never reached the check under
            test -- a stub that cannot tell the two callers apart tests neither.
            """
            if self.KILLER in list(tests):
                return mutate.Verdict(loose_outcome, "the untouched tree says so")
            return mutate.Verdict("survived")

        with (
            mock.patch.object(mutate, "_attempt", lambda *a, **k: caught),
            mock.patch.object(mutate, "_borrow", answered),
            support.quiet() as spill,
        ):
            found = mutate.run([one], baseline=True, workers=1, summarise=False)
        # Kept, not discarded. `quiet` hands back what was written for exactly
        # this reason: a test that silences output it never asserts on is one
        # that would pass if the output stopped happening -- and both lines this
        # block prints survived their own deletion in the first sweep of it.
        self.said = spill.getvalue()
        return found

    def test_it_says_that_it_is_checking(self) -> None:
        """The announcement, which is the only sign a run gives that the walk
        reached past what was baselined. Deleting it survived the sweep."""
        self.report("survived")
        self.assertIn("caught a row without being baselined", self.said)
        self.assertIn("1 test(s)", self.said)

    def test_it_says_when_the_check_came_back_red(self) -> None:
        """The other line, and the more important one: a row silently demoted to
        `broke` with nothing said about why reads as the harness malfunctioning
        rather than as the guarantee working."""
        self.report("caught")
        self.assertIn("NOT GREEN", self.said)
        self.assertIn("reported broke", self.said)

    def test_a_green_check_leaves_the_verdict_alone(self) -> None:
        """The common case, and the one that must stay cheap: the test was not
        baselined, it is green anyway, the row is caught and stays caught."""
        found = self.report("survived")
        self.assertEqual(["caught"], [r.verdict.outcome for r in found.results])
        self.assertEqual(self.KILLER, found.results[0].verdict.killer)

    def test_a_red_check_refuses_the_verdict(self) -> None:
        """The failure this exists for. Caught by a test that also fails
        untouched is not an answer, and reporting it as one is the false
        `caught` this module is built to make impossible."""
        found = self.report("caught")
        self.assertEqual(["broke"], [r.verdict.outcome for r in found.results])
        self.assertIn("also fails untouched", found.results[0].verdict.detail)

    def test_the_rest_of_the_run_is_not_voided(self) -> None:
        """Only the rows that test caught. Every other verdict rests on a shard
        that *was* green, and throwing those away would discard answers this
        found nothing wrong with -- which is what setting `baseline_red` would
        do."""
        found = self.report("caught")
        self.assertFalse(found.baseline_red, "one loose test voided the whole run")


class TestABatchSweepEndToEnd(unittest.TestCase):
    """`--batch` driven all the way through `main`, in a repository of its own.

    Nothing drove this path at all (#40). `TestASweepRecordsAsItGoes` below
    stubs `_run_generated`, so it reaches the mid-run write and nothing around
    it: the nine mutants the #38 sweep left alive in `sweep` and `main` are all
    lines no test executed.

    That matters because this is the *resume* machinery, and it exists for
    crashes. A real sweep is minutes to hours; `sweep` records per file so a
    crash costs one file rather than the afternoon, and re-running with the same
    `--json` skips what is already recorded. One defect has already slipped
    through the gap: #38 introduced a `NameError` in `finished` -- it read
    `report`, which is not bound until `_run_generated` *returns*, while
    `finished` is called by it -- and that was caught by reading the diff, not by
    this suite.

    **A repository of its own, not this one.** `generated` diffs the working
    tree against a ref and mutates what changed, and `_sandboxes` copies
    `Path.cwd()`; pointed here that is a real sweep of tupferl, which is the
    tens of minutes #40 says not to spend. Three mutants over four tests runs in
    seconds and exercises the same code.
    """

    #: `n` has exactly three mutable points on its `if`, and the fixture kills
    #: all three -- `test_boundary` is what kills `>` becoming `>=`, which
    #: survives against 5 and 1 alone because the two differ only at 2. Without
    #: it the run exits 1 and every assertion here would be about a failure.
    MODULE = "def n(x):\n    if x > 2:\n        return 'big'\n    return 'small'\n"
    CHANGED = "def n(x):\n    if x > 3:\n        return 'big'\n    return 'small'\n"
    SUITE = (
        "import unittest\n"
        "from tupferl import tiny\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_big(self): self.assertEqual('big', tiny.n(9))\n"
        "    def test_small(self): self.assertEqual('small', tiny.n(1))\n"
        "    def test_boundary(self): self.assertEqual('small', tiny.n(3))\n"
    )

    #: A **second** file, and it is what makes the two writes distinguishable.
    #: With one file, dropping the per-file write leaves the end-of-sweep write
    #: to produce the same report, and dropping the end-of-sweep write leaves
    #: the per-file one -- so each mutant survives behind the other, and both
    #: did until this existed.
    OTHER = "def s(x):\n    return x * 2\n"
    CHANGED_OTHER = "def s(x):\n    return x * 3\n"
    OTHER_SUITE = (
        "import unittest\n"
        "from tupferl import other\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_doubles(self): self.assertEqual(8, other.s(4))\n"
        "    def test_zero(self): self.assertEqual(0, other.s(0))\n"
    )

    def repository(self) -> Path:
        """A committed base, then one changed line for `--base HEAD` to find."""
        box = Path(tempfile.mkdtemp(prefix="tupferl-batch-"))
        self.addCleanup(shutil.rmtree, box, True)
        for package in ("tupferl", "tests"):
            (box / package).mkdir()
            (box / package / "__init__.py").write_text("", encoding="utf-8")
        (box / "tupferl" / "tiny.py").write_text(self.MODULE, encoding="utf-8")
        (box / "tests" / "test_tiny.py").write_text(self.SUITE, encoding="utf-8")
        (box / "tupferl" / "other.py").write_text(self.OTHER, encoding="utf-8")
        (box / "tests" / "test_other.py").write_text(self.OTHER_SUITE, encoding="utf-8")

        def git(*argv: str) -> None:
            subprocess.run(("git", *argv), cwd=box, check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "batch@example.invalid")
        git("config", "user.name", "batch")
        git("add", "-A")
        git("commit", "-qm", "base")
        (box / "tupferl" / "tiny.py").write_text(self.CHANGED, encoding="utf-8")
        (box / "tupferl" / "other.py").write_text(self.CHANGED_OTHER, encoding="utf-8")
        return box

    def sweep(self, box: Path, report: Path) -> tuple[int, str]:
        """One real `--batch` run, from `argparse` to the done marker.

        `_persist` is *wrapped*, not replaced: it still writes, and `self.wrote`
        records how many rows each call was handed. That sequence is the only
        thing separating the mid-run write from the final one, and both mutants
        hid behind the other until it existed.
        """
        self.wrote: list[int] = []
        real = mutate._persist

        def watched(found: mutate.Report, where: Path) -> None:
            self.wrote.append(len(found.results))
            real(found, where)

        here = Path.cwd()
        os.chdir(box)
        try:
            with mock.patch.object(mutate, "_persist", watched), support.quiet() as spill:
                code = mutate.main(
                    [
                        "--base",
                        "HEAD",
                        "--batch",
                        "--json",
                        str(report),
                        "--no-baseline",
                        "--workers",
                        "1",
                        "--no-killers",
                    ]
                )
            return code, spill.getvalue()
        finally:
            os.chdir(here)

    def test_a_batch_run_writes_its_report_and_marks_itself_done(self) -> None:
        """The whole path: `generated`, `sweep`, the per-file write, the final
        write, and the marker `tools/watch.py --done` waits on."""
        box = self.repository()
        report = box / "r.json"
        code, said = self.sweep(box, report)
        self.assertEqual(0, code, said)
        written = json.loads(report.read_text(encoding="utf-8"))
        outcomes = [row["outcome"] for row in written["results"]]
        self.assertEqual(["caught"] * len(outcomes), outcomes, said)
        self.assertEqual(
            {"tupferl/tiny.py", "tupferl/other.py"},
            {row["path"] for row in written["results"]},
            "the batch did not cover both files",
        )

        self.assertTrue(written["widened"], "a swept report dropped the guarantee")
        self.assertTrue(report.with_suffix(".json.done").is_file(), "no done marker")

    def test_it_writes_after_each_file_and_again_at_the_end(self) -> None:
        """The point of recording per file: a crash costs one file, not the run.

        Asserted on the *sequence* of writes, not their number. The last two are
        the same size -- the final rewrite of a finished sweep -- so a count
        alone cannot show that the earlier, smaller write ever happened, and
        that earlier write is exactly what a crash leaves behind.
        """
        box = self.repository()
        code, said = self.sweep(box, box / "r.json")
        self.assertEqual(0, code, said)
        self.assertGreaterEqual(len(self.wrote), 3, f"writes: {self.wrote}\n{said}")
        self.assertLess(self.wrote[0], self.wrote[-1], f"nothing was written mid-run: {self.wrote}")
        self.assertEqual(self.wrote[-1], max(self.wrote), "the last write was not the complete one")

    def test_the_pidfile_is_cleared_when_the_run_is_over(self) -> None:
        """A stale pid is the false liveness `watch.py` refuses to answer with,
        so the file naming a process that no longer exists must not outlive it."""
        box = self.repository()
        report = box / "r.json"
        self.sweep(box, report)
        self.assertFalse(mutate._pidfile(report).is_file(), "the run left its pidfile behind")

    def test_a_red_baseline_reaches_the_report(self) -> None:
        """The one thing the end-of-sweep write carries that the per-file writes
        cannot.

        After the last file finishes, the mid-run write has already recorded
        every row -- so on a green run `sweep`'s own final `_persist` rewrites
        the same rows and deleting it changes nothing. What it adds is
        `baseline_red`, which is only known once the baseline shard has answered.
        Without this test that write is equivalent, and its mutant survived.

        `tools/reached.py` reads the flag back and refuses to explain a report
        that carries it, because a red baseline makes every row in it meaningless
        -- so a report that lost the flag is one that invites conclusions from
        verdicts that mean nothing.
        """
        box = self.repository()
        # Red on the untouched tree, which is what a baseline is for. The rows
        # still run; their verdicts are what `baseline_red` invalidates.
        (box / "tests" / "test_red.py").write_text(
            "import unittest\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_it(self): self.fail('red on the untouched tree')\n",
            encoding="utf-8",
        )
        report = box / "r.json"
        here = Path.cwd()
        os.chdir(box)
        try:
            with support.quiet() as spill:
                mutate.main(["--base", "HEAD", "--batch", "--json", str(report), "--no-killers"])
        finally:
            os.chdir(here)
        written = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(
            written["baseline_red"],
            f"a red baseline never reached the report\n{spill.getvalue()}",
        )

    def test_a_second_run_skips_the_file_already_recorded(self) -> None:
        """Resume, which is the reason any of this records per file.

        The second run reaches `if not by_file` with everything already done and
        returns without touching a sandbox -- so it says it is skipping, runs no
        mutant, and still reports the recorded rows rather than an empty sweep.
        Both halves matter: returning early with `[]` would report a clean sweep
        of nothing, which is the flattering direction.
        """
        box = self.repository()
        report = box / "r.json"
        self.sweep(box, report)
        code, said = self.sweep(box, report)
        self.assertIn("already recorded, skipping", said)
        self.assertNotIn("in one pool", said, "the second run swept anyway")
        self.assertEqual(0, code, said)
        written = json.loads(report.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(written["results"]), 4, "the resume lost rows")


class TestASweepRecordsAsItGoes(unittest.TestCase):
    """`sweep` persists after every file, and that write happens *inside* the
    run it is reporting on.

    Which makes it the one place where reading the run's own report is a
    `NameError` rather than a wrong value: `finished` is called by
    `_run_generated` while `report = _run_generated(...)` is still evaluating,
    so the name it would bind is not bound yet. Written after exactly that was
    introduced here and caught by inspection rather than by this suite -- no
    test drove `--batch` at all, so the mid-run write had no guard.

    `_run_generated` is stubbed so the callback fires without a real sweep;
    what is under test is the bookkeeping around it, not the mutating.
    """

    def sweep(self, box: Path) -> mutate.Report:
        one = row(path="tests/profiles.py", old='{"mutation": (3, 4)}', new='{"mutation": (0, 0)}')
        answered = mutate.Result(one, mutate.Verdict("caught", "t"))
        midway: list[dict[str, Any]] = []

        # `landed=` by its real name, not `**kwargs`. Written as `landed_cb`
        # first: `sweep` calls `_run_generated(rows, args, landed=finished)`, so
        # the callback fell into `**kw` and never fired, and both tests below
        # passed against the very `NameError` they exist to catch. The assert
        # after the call is what makes that impossible to repeat.
        fired: list[bool] = []

        def straight_through(rows: Any, args: Any, landed: Any = None) -> Any:
            assert landed is not None, "sweep stopped passing a callback"
            fired.append(True)
            landed(answered)
            # Read *here*, before returning. The write `landed` triggers is
            # overwritten by the one after the run, so a test that reads the file
            # afterwards is asserting on different bytes -- which is why the
            # mid-run flag survived its own deletion when this read at the end.
            # These are the bytes a sweep killed mid-run leaves behind, and the
            # ones `tools/reached.py` would explain.
            midway.append(json.loads(args.json.read_text(encoding="utf-8")))
            return mutate.Report([answered], widened=True)

        args = argparse.Namespace(
            json=box / "out.json",
            workers=1,
            timeout=60.0,
            memory=0,
            each_test=1.0,
            no_baseline=True,
            batch=1,
            all=False,
            limit=None,
        )
        with mock.patch.object(mutate, "_run_generated", straight_through), support.quiet():
            done = mutate.sweep([one], args)
        self.assertEqual([True], fired, "the mid-run callback never ran")
        self.midway = midway[0]
        return done

    def test_the_mid_run_write_does_not_reach_for_the_run_s_own_report(self) -> None:
        with tempfile.TemporaryDirectory(prefix="tupferl-sweep-") as name:
            box = Path(name)
            report = self.sweep(box)
            self.assertTrue((box / "out.json").is_file(), "the sweep recorded nothing")
            self.assertTrue(report.widened, "a swept report dropped the guarantee")

    def test_what_it_wrote_claims_the_guarantee(self) -> None:
        """The durable half, asserted on both writes.

        `tools/reached.py` reads this file back and prints a caveat about
        survivors nobody widened, so a `false` here is a claim about the rows
        that outlives the run -- and the mid-run file is the one a sweep killed
        by the machine leaves behind, which is the case the per-file recording
        exists for.
        """
        with tempfile.TemporaryDirectory(prefix="tupferl-sweep-") as name:
            box = Path(name)
            self.sweep(box)
            self.assertTrue(self.midway["widened"], f"the mid-run write: {self.midway}")
            written = json.loads((box / "out.json").read_text(encoding="utf-8"))
            self.assertTrue(written["widened"], written)


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
