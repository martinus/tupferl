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
import contextlib
import itertools
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import typing
import unittest
from collections.abc import Container, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

from tests import support
from tools import mutants, mutate, paint, reached, verdict
from tools.mutants import Mutation, check

#: Seconds a driven probe may take before a test calls it hung. Well above the
#: ~0.5s an honest `collect(ALARM)` spends and well below `tools/mutate.py`'s
#: `EACH_TEST` of 30 -- see `TestAHungTestIsBoundedAndNotCredited.collect` for
#: what happens when it is not, and `tests/test_watch.py` for the same constant
#: and the same two bounds it has to sit between.
#:
#: Left at 20 when `ALARM` dropped from 2 to 0.5. The number that matters is the
#: gap to 30, not the gap to the honest wait: this bound exists to fail a
#: *hung* probe before the harness's own alarm does, and shrinking it in step
#: would buy nothing and narrow the margin that stops a slow runner reading as
#: a hang.
BOUND = 20

#: Seconds of per-test alarm the hung-test class arms.
#:
#: **0.5, not 2.** The test it arms against blocks on a fifo read and does
#: nothing else, so the alarm is pure waiting: two tests here paid two seconds
#: each to learn something 0.5s proves identically. Measured on this container:
#: the class went from 8.4s to 5.0s, against a serial suite of ~133s under the
#: mutation profile.
#:
#: Sub-second is not novel -- `tests/test_verdict.py` already drives the same
#: probe at `each=0.5`. The floor is the child's own startup (~60ms measured),
#: and the alarm is armed per *test* rather than per process, so the interpreter
#: is up long before it can fire.
ALARM = 0.5

#: How long a test may wait for a *nested* harness run before calling it a
#: failure. Its own bound rather than `support.PATIENCE`, because the subject is
#: a whole `mutate.run` and not one call: measured at 0.66s honestly, so this is
#: eighteen times the honest wait, and it goes through `support.bounded` so it
#: stays under whatever `--each-test` the outer sweep armed rather than under the
#: default alone. The margin is wide on purpose -- under a 39-lane sweep this
#: runs inside a memory-capped sandbox against 38 other lanes.
NESTED = support.bounded(12.0)

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
    """The whole loop: copy the tree, apply the edit, run a suite, classify.

    Every test here runs under `NESTED`, because every one of them drives a real
    `mutate.run` and a broken walk does not stop. Six mutants of
    `verdict._reached` and `verdict.collect` came back `BROKE` on the whole-tree
    sweep -- never `caught`, so the widening this class exists to prove was
    proved by nothing.

    Armed in `setUp` rather than around the one call, which was the first
    attempt and left two of the six still `BROKE`: `if not walk:` inverted hangs
    `test_a_deliberate_bug_is_caught` and `test_an_unwatched_bug_survives`, which
    pass `walk=False` and are not the test the bound was written on. A bound
    around one call covers that call and reads as though it covered the class --
    the same mistake `TestLineEndingsThatAreNotNewline` records one file over.
    """

    #: `walk=False` throughout this class, and it is not a shortcut. What these
    #: assert is the *classification* -- caught, survived, and the tree left
    #: alone -- which `tests/test_verdict.py` covers the walk of separately. With
    #: it on, `UNWATCHED` is a survivor by construction, so it runs the whole
    #: suite before it can be called one: three of these tests went from seconds
    #: to about two minutes each, and the baseline from one small shard to
    #: another whole-suite run. That is the design working as intended on a
    #: sweep and pure cost inside the harness's own tests.
    WALK = False

    def setUp(self) -> None:
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(support.deadline(NESTED, "a nested harness run never finished"))

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

    def test_a_remembered_test_is_marked_as_exact(self) -> None:
        """The flag `_attempt` reads to decide whether this goes in front of the
        learned front or behind it. Without it every row takes the `else` arm
        and the ordering silently reverts -- nothing fails, the sweep is just
        slower, which is the failure mode this whole area keeps producing.

        Measured: dropping `exact=True` here survived every other test of the
        ordering, because those construct the flag by hand.
        """
        one = row()
        (ahead,) = self.cache({mutate._key(one): REAL}).ahead_of([one])
        self.assertTrue(ahead.exact, "a remembered killer was not marked exact")

    def test_the_cheap_prefix_is_not_marked_as_exact(self) -> None:
        """The other half. The prefix is what a row with *no* remembered killer
        falls back on -- tests that catch a lot per second across the table,
        which is a claim about the suite and not about this row -- so it must
        not claim the precedence an exact killer has earned."""
        with support.quiet():
            (ahead,) = mutate.Killers(None).ahead_of([row()])
        self.assertFalse(ahead.exact, "the general prefix claimed to be exact")

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
        the default arm an `ALARM`-second per-test alarm, so an honest run here
        takes about half a second -- but a mutant that disables that alarm leaves
        the fifo read blocking, and at 60 the harness's own 30s alarm fired
        first. Measured:
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
        found = self.collect(ALARM)
        self.assertEqual(2, found["ran"], "the run did not get past the hung test")

    def test_it_is_never_counted_as_the_test_noticing(self) -> None:
        """The whole safety argument. `noticed` is what `caught` is made of."""
        found = self.collect(ALARM)
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
    #: What the killer complained about. A real `why` is a traceback; the only
    #: property asserted on is that these bytes reach the reader, so a
    #: recognisable marker beats a plausible-looking stack.
    TRACEBACK = "Traceback (most recent call last):\n  AssertionError: dJk9 marker"

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
        caught = mutate.Verdict("caught", "d", self.KILLER, why=self.TRACEBACK)

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

    def test_the_killers_traceback_is_printed_when_the_check_is_red(self) -> None:
        """The diagnosable case, and the easier of the two: the extra shard also
        says what went wrong, so this is corroboration rather than the only
        evidence."""
        self.report("caught")
        self.assertIn("dJk9 marker", self.said)

    def test_the_killers_traceback_is_printed_when_the_check_is_green(self) -> None:
        """The case this was written for, and the one with nothing else to read.

        A killer that passes untouched and still caught rows is not corrected by
        anything -- `run` leaves the verdict alone, by design -- so without the
        traceback the run says only that a test the baseline never saw claimed a
        catch, and stops. That is exactly the state the shuffled-walk experiment
        left behind: 24 rows credited to one green test, reproducible in no
        setting smaller than a 32-lane sweep, with six attempts failing to
        narrow it. Printing on the green path is the whole point of the change;
        printing only on the red one would have told that run nothing.
        """
        self.report("survived")
        self.assertIn("dJk9 marker", self.said)
        self.assertIn(f"{self.KILLER} caught 1 row(s)", self.said)


class TestWhatAnUnbaselinedKillerIsMadeToSay(unittest.TestCase):
    """`_loose_evidence`: the traceback behind a killer nothing baselined.

    Driven directly rather than through `run`, because the shapes worth pinning
    are the degenerate ones -- several rows to one killer, a row with no `why`,
    a name matching nothing -- and arranging each of those through a real run
    would take five stubbed sweeps to say what four calls say.
    `TestARowCaughtByAnUnbaselinedTest` covers the wiring.
    """

    def caught(self, killer: str, label: str, why: str = "boom") -> mutate.Result:
        return mutate.Result(row(label=label), mutate.Verdict("caught", "d", killer, why=why))

    def test_it_names_the_killer_the_count_and_the_first_row(self) -> None:
        """All three, because none of them is derivable from the others by a
        reader: the name says which test, the count says how much of the run
        rests on it, and the label says where to look first."""
        rows = [self.caught("t.A.test_x", f"tupferl/sync.py:{n} in f()") for n in (7, 8, 9)]
        head, _ = mutate._loose_evidence(rows, ["t.A.test_x"])
        self.assertIn("t.A.test_x", head)
        self.assertIn("caught 3 row(s)", head)
        self.assertIn("tupferl/sync.py:7 in f()", head)

    def test_it_prints_one_traceback_and_not_one_per_row(self) -> None:
        """24 copies of one traceback is the noise `Verdict.why`'s docstring
        refuses, and the count above already carries the rest."""
        rows = [self.caught("t.A.test_x", f"tupferl/sync.py:{n} in f()") for n in (7, 8, 9)]
        said = mutate._loose_evidence(rows, ["t.A.test_x"])
        self.assertEqual(2, len(said))
        self.assertEqual(1, "\n".join(said).count("boom"))

    def test_a_row_with_no_traceback_says_so(self) -> None:
        """A `caught` verdict takes `why` from `verdict.py`'s `reasons`, and that
        list can come back empty -- so this is a state the tool really reaches
        rather than a defensive arm. An empty indented block reads as "the test
        failed for no reason", which is a different claim and a wrong one."""
        said = mutate._loose_evidence(
            [self.caught("t.A.test_x", "x:1 in f()", why="")], ["t.A.test_x"]
        )
        self.assertIn("no traceback recorded", "\n".join(said))
        self.assertNotIn("boom", "\n".join(said))

    def test_a_killer_matching_no_row_is_skipped_rather_than_guessed_at(self) -> None:
        """Unreachable from `run`, which passes the names `_unbaselined` derived
        from these same rows. Pinned anyway because the alternative -- a header
        with nothing under it -- would attribute the *next* killer's traceback to
        this one, which is the exact misreading the block exists to prevent."""
        rows = [self.caught("t.A.test_x", "x:1 in f()")]
        self.assertEqual([], mutate._loose_evidence(rows, ["t.B.test_gone"]))

    def test_each_killer_gets_its_own_entry(self) -> None:
        """Two loose killers is two separate questions. Folding them into one
        block would attribute one test's traceback to the other's rows."""
        rows = [self.caught("t.A.test_x", "x:1 in f()"), self.caught("t.B.test_y", "x:2 in g()")]
        said = mutate._loose_evidence(rows, ["t.A.test_x", "t.B.test_y"])
        self.assertEqual(4, len(said))
        self.assertIn("t.A.test_x", said[0])
        self.assertIn("t.B.test_y", said[2])


class TestMovingTheKillerToTheFront(unittest.TestCase):
    """`Learned`: whatever caught the last row goes first on the next.

    The only ordering mechanism in the file that learns *during* a run.
    `Killers.known` is keyed on the mutation's text, so it misses by
    construction on `--base main`, whose rows are new lines; `Killers.prefix()`
    is computed once before the table starts. Neither looks at the fact that
    consecutive rows sit in the same function, which is what this is for --
    measured at 27-42% same killing test as the previous row, against 1-3% by
    chance.
    """

    def row(self, tests: str = "tests.test_sync", first: str = "") -> Mutation:
        return row()._replace(tests=tests, first=first)

    def test_the_last_killer_comes_first(self) -> None:
        learned = mutate.Learned()
        learned.saw("tests.test_sync.TestTheDecisionTable.test_it")
        self.assertEqual("tests.test_sync.TestTheDecisionTable.test_it", learned.ahead(self.row()))

    def test_the_newest_wins(self) -> None:
        """Move-to-*front*, not append. Without the reordering this is a queue,
        and a queue hands back the oldest killer first -- which is the one the
        walk has moved furthest away from."""
        learned = mutate.Learned()
        for name in ("a", "b", "c"):
            learned.saw(f"tests.test_sync.T.test_{name}")
        self.assertEqual(
            ["tests.test_sync.T.test_c", "tests.test_sync.T.test_b", "tests.test_sync.T.test_a"],
            learned.ahead(self.row()).split(),
        )

    def test_seeing_one_again_moves_it_rather_than_repeating_it(self) -> None:
        """The move half of move-to-front. Appending a duplicate would spend a
        slot on a test already in the list and push a real one off the end."""
        learned = mutate.Learned()
        for name in ("a", "b", "a"):
            learned.saw(f"tests.test_sync.T.test_{name}")
        self.assertEqual(
            ["tests.test_sync.T.test_a", "tests.test_sync.T.test_b"],
            learned.ahead(self.row()).split(),
        )

    def test_it_is_bounded(self) -> None:
        """Or it grows to the size of the suite, and every row pays for the whole
        of it before reaching its own selection -- a second `prefix()` with no
        budget."""
        learned = mutate.Learned(keep=3)
        for index in range(10):
            learned.saw(f"tests.test_sync.T.test_{index}")
        self.assertEqual(3, len(learned.ahead(self.row()).split()))

    def test_a_test_the_row_cannot_reach_is_not_offered(self) -> None:
        """The guard `Killers.ahead_of` gives: a test in a module that does not
        import the mutated file cannot see the mutation, so running it first is
        pure cost."""
        learned = mutate.Learned()
        learned.saw("tests.test_paths.T.test_it")
        self.assertEqual("", learned.ahead(self.row(tests="tests.test_sync")))

    def test_a_whole_suite_row_reaches_everything(self) -> None:
        """`WHOLE_SUITE` is the *empty* selection and means "run the lot", so
        every learned test is reachable from it. Read as a plain string it
        matches nothing and the row is offered none of them."""
        learned = mutate.Learned()
        learned.saw("tests.test_paths.T.test_it")
        self.assertEqual(
            "tests.test_paths.T.test_it",
            learned.ahead(self.row(tests=mutate.WHOLE_SUITE)),
        )

    def test_what_the_row_already_remembers_is_not_repeated(self) -> None:
        """`first` runs in order, so naming a test twice costs a run and buys
        nothing."""
        learned = mutate.Learned()
        learned.saw("tests.test_sync.T.test_it")
        self.assertEqual("", learned.ahead(self.row(first="tests.test_sync.T.test_it")))

    def test_an_empty_killer_is_not_learned(self) -> None:
        """A survivor and a `broke` row both carry `killer=""`.

        Asserted on `recent`, not on `ahead`. An empty name is filtered by the
        reachability check on its way out, and `" ".join` swallows it besides --
        so through `ahead` this guard is invisible and its mutant survived. What
        it actually costs is a *slot*: `keep` is eight, and a stored blank is one
        fewer real test in front of every later row.
        """
        learned = mutate.Learned()
        learned.saw("")
        self.assertEqual([], learned.recent, "an empty killer took a slot")


class TestWhatAnOutcomeMeans(unittest.TestCase):
    """`MEANING`: one row per outcome, instead of four spellings that must agree.

    What an outcome implies was written out in `Verdict.answered`,
    `Report.clean`, the headline map and `reached.Row.answered`. Adding a fifth
    outcome means visiting all four, and only the headline fails loudly -- it is
    a `[]` lookup and raises `KeyError` *mid-sweep*, discarding every answer
    already paid for. The other three fall through to a **wrong answer,
    silently**, and nothing anywhere says so.

    Both of those happened while #33 added one.
    """

    def test_meaning_covers_exactly_the_outcomes(self) -> None:
        """The enforcement `mypy` cannot give a dict.

        Set equality, both directions, **derived from the type**: a missing key
        crashes a run, an extra one is a typo that would never fire, and listing
        the outcomes here instead of deriving them is how this guard rots on the
        next one.
        """
        self.assertEqual(
            set(typing.get_args(mutate.Outcome)),
            set(mutate.MEANING),
            "MEANING and Outcome have drifted apart",
        )

    def test_only_a_real_verdict_counts_as_answered(self) -> None:
        """`caught` and `survived` say something about the tests. `broke` and
        `timeout` are the run failing to put the question, and folding either
        into an answer is the false `caught` this module exists to prevent."""
        answered = {name for name, what in mutate.MEANING.items() if what.answered}
        self.assertEqual({"caught", "survived"}, answered)

    def test_only_a_caught_row_leaves_a_sweep_clean(self) -> None:
        """A survivor is the finding; a broken or timed-out row is a question
        never put. Neither may report the table as done -- it would claim the
        table was complete while it was smaller than it looked."""
        clean = {name for name, what in mutate.MEANING.items() if what.clean}
        self.assertEqual({"caught"}, clean)

    def test_a_non_answer_is_not_evidence_that_a_line_ran(self) -> None:
        """What `tools/reached.py` reads it for: it crosses survivors with
        coverage, and a row that never got to ask is not evidence its line was
        executed."""
        usable = {name for name, what in mutate.MEANING.items() if what.usable}
        self.assertEqual({"caught", "survived"}, usable)

    def test_the_readers_go_through_the_table(self) -> None:
        """The point of the table is that nothing keeps its own copy. Asserted on
        the real properties rather than on `MEANING` alone, or the table could be
        right while every reader ignored it."""
        for outcome, what in mutate.MEANING.items():
            verdict = mutate.Verdict(outcome, "d")  # type: ignore[arg-type]
            self.assertEqual(what.answered, verdict.answered, outcome)
            self.assertEqual(
                what.clean, mutate.Report([mutate.Result(row(), verdict)]).clean, outcome
            )

    #: An outcome that does not exist, standing in for the *next* one.
    #:
    #: The four tests above pass just as well against readers that kept their own
    #: `in ("caught", "survived")` -- of course they do: those copies agree with
    #: the table *today*, which is exactly why replacing them was safe. They
    #: diverge only when an outcome is added, and that is the whole thing this
    #: change is for. So the fixture adds one. Measured: without it, reverting
    #: any of the three readers to its own tuple survives; with it, all three go
    #: red.
    INVENTED = "from-the-future"

    def imagined(self) -> Any:
        """`MEANING` with a fifth outcome in it, whose flags no hardcoded copy
        could possibly guess: answered *and* clean *and* usable."""
        return mock.patch.dict(
            mutate.MEANING,
            {self.INVENTED: mutate.Meaning("FUTURE", answered=True, clean=True, usable=True)},
        )

    def test_answered_follows_a_new_outcome(self) -> None:
        verdict = mutate.Verdict(self.INVENTED, "d")  # type: ignore[arg-type]
        with self.imagined():
            self.assertTrue(verdict.answered, "`answered` is not reading the table")

    def test_clean_follows_a_new_outcome(self) -> None:
        verdict = mutate.Verdict(self.INVENTED, "d")  # type: ignore[arg-type]
        with self.imagined():
            self.assertTrue(
                mutate.Report([mutate.Result(row(), verdict)]).clean,
                "`clean` is not reading the table",
            )

    def test_reached_follows_a_new_outcome(self) -> None:
        with self.imagined():
            self.assertTrue(
                reached.Row("l", "p.py", 1, self.INVENTED).answered,
                "`reached` is not reading the table",
            )

    def test_every_outcome_says_what_colour_it_is(self) -> None:
        """The fifth column, and the one a reader uses *before* reading a word.
        An outcome with no colour is an outcome that looks like every other line
        in a screen of nine hundred."""
        for outcome, what in mutate.MEANING.items():
            with self.subTest(outcome=outcome):
                self.assertRegex(what.colour, r"^\x1b\[[0-9;]+m$")

    def test_the_two_real_verdicts_do_not_share_one(self) -> None:
        """`caught` and `SURVIVED` are the good news and the finding, and the
        whole value of the channel is telling them apart at a glance."""
        self.assertNotEqual(mutate.MEANING["caught"].colour, mutate.MEANING["survived"].colour)

    def test_an_outcome_that_forgets_to_say_claims_nothing(self) -> None:
        """The default, chosen the way `reached.Row.answered` chooses its own:
        an outcome this build has never heard of gets the colour that makes no
        claim, rather than the one that says everything is fine."""
        invented = mutate.Meaning("FUTURE", answered=True, clean=True, usable=True)
        self.assertEqual(paint.QUIET, invented.colour)

    #: A colour nothing in the tree uses, so finding it in a line proves the
    #: line asked the table rather than agreeing with it by coincidence. Every
    #: real code would be indistinguishable from the literal it replaced.
    MAGENTA = "\x1b[35m"

    def repainted(self) -> Any:
        """`MEANING` with `survived` in a colour no reader would choose."""
        recoloured = mutate.MEANING["survived"]._replace(colour=self.MAGENTA)
        return mock.patch.dict(mutate.MEANING, {"survived": recoloured})

    def test_the_survivor_paragraph_reads_the_table(self) -> None:
        """`_summarise` is the part a pull request quotes, and the only reason it
        is red is that the table says so. Spelling `paint.BAD` there is a second
        copy that agrees today -- which is exactly what #45 removed from the
        other four readers."""
        found = [mutate.Result(row(), mutate.Verdict("survived", "d"))]
        with self.repainted(), support.quiet(terminal=True) as said:
            mutate._summarise(found)
        self.assertIn(self.MAGENTA, said.getvalue(), "the survivor list keeps its own colour")

    def test_reached_reads_the_table_for_colour_too(self) -> None:
        """The fifth reader, in the other module. It already imports `MEANING`
        for `answered`; the colour comes from the same row."""
        survivor = reached.Row("l", "p.py", 1, "survived")
        with self.repainted(), support.quiet(terminal=True) as said:
            reached._summarise([survivor], reached.Split([survivor], []), 0)
        self.assertIn(self.MAGENTA, said.getvalue(), "reached keeps its own colour")

    def test_reached_reads_the_same_table(self) -> None:
        """The fourth copy, in another module. It imports `MEANING` rather than
        spelling the outcomes again, and an outcome this build does not know is
        read conservatively -- a report from a newer `mutate` is not evidence."""
        for outcome, what in mutate.MEANING.items():
            seen = reached.Row("l", "p.py", 1, outcome)
            self.assertEqual(what.usable, seen.answered, outcome)
        self.assertFalse(reached.Row("l", "p.py", 1, "from-the-future").answered)


class TestTheParagraphAPullRequestQuotes(unittest.TestCase):
    """`_summarise`, asserted line by line rather than "something was printed".

    This is the one paragraph a sweep exists to produce: a run whose survivor
    list does not appear reads as a clean run, and nothing downstream would say
    otherwise -- `main`'s exit status comes from `Report.clean`, which is
    computed from the same results and would still be 1, so the *number* is
    right while the page a reader acts on is blank.

    It was guarded by two tests that could not see that. Both asserted a colour
    appeared somewhere in the output, and the colour appears twice -- in the
    heading and again on every label -- so dropping either print left the other
    to satisfy them. Measured: `the call to print(...) never happens` survived
    on four of this function's six lines. The fix is to assert what each line
    *says*, which is also what a reader would miss.
    """

    def summarised(self, results: list[mutate.Result]) -> str:
        with support.quiet() as said:
            mutate._summarise(results)
        return said.getvalue()

    def survivor(self, label: str = "tupferl/sync.py:1 in f()") -> mutate.Result:
        return mutate.Result(row(label=label), mutate.Verdict("survived", "d"))

    def test_the_count_and_the_labels_are_both_there(self) -> None:
        """Two lines, two claims. The count is what goes in the PR title and the
        labels are what someone opens a file to look at, and each has been
        printed by a version of this function that dropped the other."""
        said = self.summarised([self.survivor("a.py:1 in f()"), self.survivor("b.py:2 in g()")])
        self.assertIn("2 survived", said)
        self.assertIn("a.py:1 in f()", said)
        self.assertIn("b.py:2 in g()", said)

    def test_the_selection_is_named_beside_each_label(self) -> None:
        """Which tests the row ran against. Without it a survivor cannot be
        told from a row that was pointed at the wrong suite -- which is the
        error `tools/README.md` says points the expensive way, at rewriting a
        test that was never weak."""
        self.assertIn("tests.test_sync", self.summarised([self.survivor()]))

    def test_the_instruction_to_suspect_the_fixture_is_printed(self) -> None:
        """The one line of the paragraph that says what to *do*, and the only
        one that is not derived from the results -- so it is also the one a
        `print`-dropping mutation removes with no other symptom."""
        self.assertIn("Suspect the fixture", self.summarised([self.survivor()]))

    def test_a_clean_run_says_none_of_it(self) -> None:
        """The precondition. Without it every assertion above is equally
        satisfied by a function that prints the paragraph unconditionally, and
        `if survivors:` becoming `if True:` would survive all four."""
        said = self.summarised([mutate.Result(row(), mutate.Verdict("caught", ""))])
        self.assertEqual("", said)

    def test_rows_that_asked_nothing_get_their_own_paragraph(self) -> None:
        """Counted separately and never as survivors -- the error this module
        exists to prevent, one level up. The count, the label and the reason
        are three separate prints and each is a line a reader needs."""
        broke = mutate.Result(row(label="c.py:3 in h()"), mutate.Verdict("broke", "no such name"))
        said = self.summarised([broke])
        self.assertIn("1 asked nothing", said)
        self.assertIn("c.py:3 in h()", said)
        self.assertIn("no such name", said)

    def test_an_unanswered_row_is_not_counted_as_a_survivor(self) -> None:
        """Both paragraphs from one table, so the two counts are visibly about
        different rows. A `broke` row folded into the survivor count is the
        false finding; a survivor folded into the other is the false clean."""
        said = self.summarised(
            [self.survivor(), mutate.Result(row(), mutate.Verdict("timeout", "30s"))]
        )
        self.assertIn("1 survived", said)
        self.assertIn("1 asked nothing", said)

    def test_a_recorded_row_that_asked_nothing_is_counted_not_listed(self) -> None:
        """The same terms a recorded survivor gets, and the reason the record
        widened: a `broke` row somebody wrote a reason for should stop being one
        of the rows the sweep asks them to read. Listed, it is noise; counted,
        the number going up is still visible.
        """
        broke = mutate.Result(row(label="c.py:3 in h()"), mutate.Verdict("broke", "a fork bomb"))
        key = mutate._key(broke.mutation)
        with support.quiet() as said:
            mutate._summarise([broke], {key: mutate.Accepted("cannot be answered", 1)})
        self.assertNotIn("asked nothing", said.getvalue())
        self.assertIn("1 survivor(s) already recorded", said.getvalue())

    def test_an_unrecorded_row_that_asked_nothing_is_still_listed(self) -> None:
        """The precondition. Without it the assertion above is satisfied by a
        `_summarise` that prints nothing at all once a record is passed."""
        broke = mutate.Result(row(label="c.py:3 in h()"), mutate.Verdict("broke", "a fork bomb"))
        with support.quiet() as said:
            mutate._summarise([broke], {})
        self.assertIn("1 asked nothing", said.getvalue())
        self.assertIn("c.py:3 in h()", said.getvalue())


class GeneratedTable(unittest.TestCase):
    """A repository with a real diff in it, and one way to build a table from it.

    **Not a `Test...` class and holding no tests of its own.** Subclassing one
    that *does* makes every test in it run again under the subclass's name --
    `tests/test_gitrepo.py`'s `ConflictedIndex` says the same thing, and this
    file did it anyway: the class below inherited six tests, one of them a
    `git init` with two commits and a full `generated()` run, and its docstring
    said it "inherits nothing else".
    """

    def repository(self) -> Path:
        """Two mutable files whose changed lines differ in number, committed and
        then changed, so `generated` has a real diff to read.

        `wee.py` sorts *after* `many.py`, so path order and size order disagree
        -- without that the fixture cannot tell a sorted table from an
        unsorted one, which is the shape that made an earlier attempt here
        useless (`--limit 40` gave every file two rows, so every ordering
        looked identical).
        """
        box = Path(tempfile.mkdtemp(prefix="tupferl-order-"))
        self.addCleanup(shutil.rmtree, box, True)
        (box / "tupferl").mkdir()
        (box / "tupferl" / "__init__.py").write_text("", encoding="utf-8")
        many = "\n".join(f"def f{n}(x):\n    return x + {n}\n" for n in range(6))
        (box / "tupferl" / "many.py").write_text(many, encoding="utf-8")
        (box / "tupferl" / "wee.py").write_text("def g(x):\n    return x + 1\n", encoding="utf-8")

        def git(*argv: str) -> None:
            subprocess.run(("git", *argv), cwd=box, check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "order@example.invalid")
        git("config", "user.name", "order")
        git("add", "-A")
        git("commit", "-qm", "base")
        (box / "tupferl" / "many.py").write_text(many.replace("+", "-"), encoding="utf-8")
        (box / "tupferl" / "wee.py").write_text("def g(x):\n    return x - 1\n", encoding="utf-8")
        return box

    def table(self, box: Path, **over: Any) -> tuple[list[Mutation], str]:
        """`generated` over `box`, with what it printed.

        `os.chdir` because `generated` reads `Path.cwd()` -- it is a tool that
        runs from the repository root and says so -- and the `finally` because
        a test that left the process somewhere else would break every test
        after it in ways that look like their own fault.
        """
        args = argparse.Namespace(
            **{
                "all": False,
                "base": "HEAD",
                "only": [],
                "limit": 0,
                "operator": [],
                "skip_operator": [],
                **over,
            }
        )
        here = Path.cwd()
        os.chdir(box)
        try:
            with support.quiet() as said:
                return mutate.generated(args), said.getvalue()
        finally:
            os.chdir(here)


class TestWhichFileASweepReachesFirst(GeneratedTable):
    """`by_size`: smallest file first, and every file's rows kept together.

    Two paths built their table differently. `sweep` sorted by size; the plain
    `--base` path did not, because `generated` assembles with `for path in
    sorted(touched)` and a `ThreadPoolExecutor` runs futures in submission
    order -- so alphabetical order became execution order by accident, and
    nobody chose it.

    Measured on this tree before the fix, the order files were reached in:
    `cpus.py` (3 rows), `mutants.py` (514), `mutate.py` (738), `paint.py` (19).
    A run stopped early answered three rows and then ground through the two
    largest files. After: 3, 19, 22, 29, 31, 38, 52, 77 -- seven whole files in
    the first 194 rows.
    """

    def rows(self, sizes: dict[str, int]) -> list[Mutation]:
        """A table with `sizes[path]` rows in each file, built in *path* order
        so that the sort has something to undo."""
        return [
            Mutation(f"{path}:{n} x", path, "a", "b", "tests.test_sync")
            for path in sorted(sizes)
            for n in range(sizes[path])
        ]

    def test_the_smallest_file_comes_first(self) -> None:
        """Ascending by row count, not by name. `a.py` is both alphabetically
        first and the largest, so a table that came back in path order would
        look identical to one that was never sorted."""
        grouped = mutate.by_size(self.rows({"a.py": 5, "b.py": 1, "c.py": 3}))
        self.assertEqual(["b.py", "c.py", "a.py"], list(grouped))

    def test_every_row_survives_the_ordering(self) -> None:
        """A sort that drops rows would still pass the assertion above. The
        table is a work list before it is an order."""
        table = self.rows({"a.py": 5, "b.py": 1, "c.py": 3})
        self.assertEqual(
            sorted(row.label for row in table),
            sorted(row.label for rows in mutate.by_size(table).values() for row in rows),
        )

    def test_a_file_keeps_its_rows_together_and_in_order(self) -> None:
        """Contiguity is load-bearing twice: `sweep.finished` counts a file down
        to zero by relying on it, and `Learned`'s move-to-front rests on
        consecutive rows sitting in the same function. Interleaving them is a
        measured dead end -- see CLAUDE.md."""
        grouped = mutate.by_size(self.rows({"a.py": 3, "b.py": 1}))
        self.assertEqual(["a.py:0 x", "a.py:1 x", "a.py:2 x"], [r.label for r in grouped["a.py"]])

    def test_files_of_the_same_size_keep_a_stable_order(self) -> None:
        """Nothing about size separates them, so the answer must not depend on
        dictionary iteration luck -- a table that reorders equal files between
        runs would make two sweeps of one tree incomparable."""
        even = {"c.py": 2, "a.py": 2, "b.py": 2}
        self.assertEqual(["a.py", "b.py", "c.py"], list(mutate.by_size(self.rows(even))))

    def test_an_empty_table_is_not_an_error(self) -> None:
        """`--base main` on a diff that touches no mutable file. `generated`
        passes whatever it has."""
        self.assertEqual({}, mutate.by_size([]))

    def test_the_generated_table_itself_comes_back_smallest_first(self) -> None:
        """The fix, rather than the rule it uses.

        Every assertion above drives `by_size` directly, so a `generated` that
        never called it would satisfy all of them -- and that is exactly the
        state this issue describes: the rule existed in `sweep` and the plain
        path did not use it.
        """
        table, _ = self.table(self.repository())
        reached = [path for path, _ in itertools.groupby(row.path for row in table)]
        self.assertEqual(
            ["tupferl/wee.py", "tupferl/many.py"],
            reached,
            "the smaller file did not come first",
        )
        self.assertEqual(2, len(reached), "a file's rows were split rather than kept together")


class TestWhatTheGeneratedTableSaysBeforeItRuns(GeneratedTable):
    """`generated`'s filtering and its four printed lines.

    It shares the repository fixture with the ordering test above through
    `GeneratedTable`, which holds no tests of its own -- see that class for what
    went wrong when this one subclassed the test class instead.

    Twenty-two mutations of this function survived a sweep, and the printed
    lines were most of them -- a table built from the wrong files still has rows
    in it, and a cap applied silently reads as "everything was covered".
    """

    def test_only_keeps_every_pattern_that_matches_rather_than_the_overlap(self) -> None:
        """**Two patterns, and that is the fixture.** With one, `any` and `all`
        agree -- so a filter that required a path to match *every* pattern
        passed, and `--only a --only b` (which the tool's own help offers as
        repeatable) would have produced an empty table and the refusal below.
        """
        box = self.repository()
        table, _ = self.table(box, only=["wee", "many"])
        self.assertEqual({"tupferl/many.py", "tupferl/wee.py"}, {row.path for row in table})

    def test_only_drops_what_it_does_not_name(self) -> None:
        """The other half: a filter that kept everything passes the test above."""
        box = self.repository()
        table, _ = self.table(box, only=["wee"])
        self.assertEqual({"tupferl/wee.py"}, {row.path for row in table})

    def test_a_selection_matching_nothing_refuses_rather_than_runs_empty(self) -> None:
        """An empty table is a sweep that reports every row caught, because
        there are none -- the green run of nothing again, one tool along."""
        box = self.repository()
        with self.assertRaises(SystemExit) as raised:
            self.table(box, only=["no-such-file"])
        self.assertIn("nothing mutable changed", str(raised.exception))

    def test_it_says_how_many_files_lines_and_mutants(self) -> None:
        """The header a reader uses to tell a table of three rows from one of
        three hundred before waiting for either."""
        box = self.repository()
        table, said = self.table(box)
        self.assertIn("2 file(s)", said)
        self.assertIn(f"-> {len(table)} mutants", said)

    def imported(self) -> Path:
        """The repository above, plus a test module that imports one of the two.

        Without it every file takes the whole-suite fallback, so `targets_for(
        ...) or WHOLE_SUITE` and the notice beside it are unobservable: the
        fallback is what the fixture produces either way. This is the shape
        CLAUDE.md calls two symmetric inputs -- both files answer the same, so
        which branch ran is not visible.
        """
        box = self.repository()
        (box / "tests").mkdir()
        (box / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (box / "tests" / "test_wee.py").write_text(
            "from tupferl import wee  # noqa: F401\n", encoding="utf-8"
        )
        return box

    def test_a_row_runs_the_tests_of_whatever_imports_its_file(self) -> None:
        """`targets_for(...) or WHOLE_SUITE`. Read as `and`, every row that has
        a real target gets the empty selection instead -- which *is* the
        whole-suite fallback, so the sweep still finishes and takes many times
        as long for no extra signal."""
        table, _ = self.table(self.imported())
        by_path = {row.path: row.tests for row in table}
        self.assertEqual("tests.test_wee", by_path["tupferl/wee.py"])
        self.assertEqual(mutate.WHOLE_SUITE, by_path["tupferl/many.py"])

    def test_the_notice_is_only_for_the_file_nothing_imports(self) -> None:
        """The other half of the branch. Printed for every file, the line stops
        distinguishing the slow rows from the ordinary ones -- and it exists
        only to make that difference visible before the wait."""
        _, said = self.table(self.imported())
        self.assertIn("nothing imports tupferl/many.py", said)
        self.assertNotIn("nothing imports tupferl/wee.py", said)

    def test_a_file_nothing_imports_says_its_rows_run_the_whole_suite(self) -> None:
        """`targets_for` finds no importer, so the rows fall back to the whole
        suite -- which is much slower and is a fact about the *tree*, not about
        the change. Silence here reads as a normal table.

        The fixture is the repository as it stands: nothing imports either
        module, so both rows take the fallback.
        """
        box = self.repository()
        table, said = self.table(box)
        self.assertIn("nothing imports tupferl/wee.py", said)
        self.assertTrue(
            all(row.tests == mutate.WHOLE_SUITE for row in table),
            "the fixture no longer takes the fallback, so the notice proves nothing",
        )

    def test_a_capped_table_says_what_it_dropped_and_from_where(self) -> None:
        """A silent cap reads as "everything was covered", and the count looks
        right either way. Per file, because which file lost its rows is what
        decides whether the cap mattered."""
        box = self.repository()
        whole, _ = self.table(box)
        table, said = self.table(box, limit=2)
        self.assertEqual(2, len(table))
        self.assertIn("--limit 2", said)
        self.assertIn(f"{len(whole) - 2} not run", said)
        # **The counts, not just the names.** `share[path] = share.get(path, 0)
        # + 1` is six mutations' worth of arithmetic, and a report naming the
        # right files with the wrong numbers reads exactly like a right one.
        #
        # The expectation is derived from the *total*, not from a copy of how
        # `cap` spreads the limit across files -- which is a rule of its own,
        # with its own tests, and modelling it here would be a test that agrees
        # with the code because it repeats it. Whatever the split, the parts
        # have to add up to what was dropped and be listed in a settled order.
        listed = said.split("not run (", 1)[1].split(").", 1)[0]
        pairs = [entry.rsplit(" ", 1) for entry in listed.split(", ")]
        self.assertEqual(2, len(pairs), "one file cannot show the order of the list")
        self.assertEqual(sorted(path for path, _ in pairs), [path for path, _ in pairs])
        self.assertEqual(len(whole) - 2, sum(int(count) for _, count in pairs))

    def test_an_uncapped_table_says_nothing_about_a_cap(self) -> None:
        """The other half. A line that always appears is one nobody reads."""
        box = self.repository()
        _, said = self.table(box)
        self.assertNotIn("not run", said)


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
    #: `a` and `b` are the same expression twice, and they are here for #46.
    #: Two rows of one file then share an `old` and a `new` and differ only in
    #: their `span` -- so a resume key that drops the span reads the second as
    #: already answered. Without them `(path, new)` is as unique as
    #: `(path, span, new)` and the two spellings are indistinguishable;
    #: measured on the real tree, `_key` collapses 556 of 3103 rows.
    #: **Not dead locals.** The first spelling of this made them unused, and
    #: both rows then honestly survived -- two survivors, exit 1, and the eleven
    #: tests here that assert a clean run went red. `test_four` is what catches
    #: them: at `x == 4` the sum is 24 against a threshold of 18, so `a` losing
    #: its multiply drops it to 13 and the answer flips. `test_boundary` still
    #: catches `>` becoming `>=`, which needs a case landing exactly on 18.
    MODULE = "def n(x):\n    if x > 2:\n        return 'big'\n    return 'small'\n"
    CHANGED = (
        "def n(x):\n    a = x * 3\n    b = x * 3\n"
        "    if a + b > 18:\n        return 'big'\n    return 'small'\n"
    )
    SUITE = (
        "import unittest\n"
        "from tupferl import tiny\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_big(self): self.assertEqual('big', tiny.n(9))\n"
        "    def test_small(self): self.assertEqual('small', tiny.n(1))\n"
        "    def test_boundary(self): self.assertEqual('small', tiny.n(3))\n"
        "    def test_four(self): self.assertEqual('big', tiny.n(4))\n"
    )

    #: A **second** file, and it is what makes the two writes distinguishable.
    #: With one file, dropping the per-file write leaves the end-of-sweep write
    #: to produce the same report, and dropping the end-of-sweep write leaves
    #: the per-file one -- so each mutant survives behind the other, and both
    #: did until this existed.
    #: `x + x`, not `x * 3`. The suite below asserts `s(4) == 8`, so the old
    #: spelling made `test_doubles` **fail on the changed tree** -- every
    #: `zeta.py` mutant was then "caught" by a test that was already red, and
    #: the sweep's own guard said so on every run ("caught by a test that also
    #: fails untouched") with nothing asserting on it. `--no-baseline` is what
    #: let it sit: these tests are about writes and row counts, so a meaningless
    #: verdict cost nothing until #46's resume test began comparing row sets.
    #: A red fixture reporting rows as caught is this project's oldest trap.
    OTHER = "def s(x):\n    return x * 2\n"
    CHANGED_OTHER = "def s(x):\n    return x + x\n"
    OTHER_SUITE = (
        "import unittest\n"
        "from tupferl import zeta\n\n"
        "class T(unittest.TestCase):\n"
        "    def test_doubles(self): self.assertEqual(8, zeta.s(4))\n"
        "    def test_zero(self): self.assertEqual(0, zeta.s(0))\n"
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
        (box / "tupferl" / "zeta.py").write_text(self.OTHER, encoding="utf-8")
        (box / "tests" / "test_zeta.py").write_text(self.OTHER_SUITE, encoding="utf-8")

        def git(*argv: str) -> None:
            subprocess.run(("git", *argv), cwd=box, check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "batch@example.invalid")
        git("config", "user.name", "batch")
        git("add", "-A")
        git("commit", "-qm", "base")
        (box / "tupferl" / "tiny.py").write_text(self.CHANGED, encoding="utf-8")
        (box / "tupferl" / "zeta.py").write_text(self.CHANGED_OTHER, encoding="utf-8")
        return box

    def sweep(
        self,
        box: Path,
        report: Path,
        terminal: bool = False,
        extra: Sequence[str] = (),
        # `--all` and `--base` are mutually exclusive by `parser.error`, so a
        # whole-tree run replaces this rather than adding to `extra`.
        scope: Sequence[str] = ("--base", "HEAD"),
    ) -> tuple[int, str]:
        """One real `--batch` run, from `argparse` to the done marker.

        `_persist` is *wrapped*, not replaced: it still writes, and `self.wrote`
        records how many rows each call was handed. That sequence is the only
        thing separating the mid-run write from the final one, and both mutants
        hid behind the other until it existed.
        """
        self.wrote: list[int] = []
        real = mutate._persist

        def watched(found: mutate.Report, where: Path, announce: bool = True) -> None:
            self.wrote.append(len(found.results))
            real(found, where, announce)

        here = Path.cwd()
        os.chdir(box)
        try:
            with mock.patch.object(mutate, "_persist", watched), support.quiet(terminal) as spill:
                code = mutate.main(
                    [
                        *scope,
                        "--batch",
                        "--json",
                        str(report),
                        "--no-baseline",
                        "--workers",
                        "1",
                        "--no-killers",
                        *extra,
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
            {"tupferl/tiny.py", "tupferl/zeta.py"},
            {row["path"] for row in written["results"]},
            "the batch did not cover both files",
        )

        self.assertTrue(written["widened"], "a swept report dropped the guarantee")
        self.assertTrue(report.with_suffix(".json.done").is_file(), "no done marker")

    def test_a_redirected_sweep_carries_no_escape_codes(self) -> None:
        """The guarantee `tools/watch.py` rests on, driven through a whole run.

        A sweep is started detached with `> sweep.log`, and the watcher counts
        rows out of that file with `--match 'caught|SURVIVED'`. One escape
        sequence inside the word and the pattern matches nothing -- so a healthy
        run reads as a stalled one, which is the wrong answer that looks like a
        real finding.

        The row is asserted with its spacing, not just its word: this is the
        line another program parses, and `L0 caught    ` is what it parses. The
        counter and the lane sit in front of it now, so the pattern is anchored
        on the lane tag rather than on the start of the line -- `--match` is a
        substring search, and it is the *word* that must survive intact.
        """
        box = self.repository()
        code, said = self.sweep(box, box / "r.json")
        self.assertEqual(0, code, said)
        self.assertNotIn("\x1b", said, "a captured run was painted")
        self.assertIn("L0 caught    tupferl/tiny.py:", said)

    def test_the_same_sweep_on_a_terminal_is_coloured(self) -> None:
        """The half that stops the test above from being satisfied by a tool
        that never colours anything -- which is every version of this code
        before the colour was added, and would be every version after it broke.

        Same fixture, same run, one stream. The only difference is `isatty`.
        """
        box = self.repository()
        code, said = self.sweep(box, box / "r.json", terminal=True)
        self.assertEqual(0, code, said)
        self.assertIn(f"{paint.GOOD}caught", said, "a terminal run was not painted")

    def test_the_colour_does_not_move_the_column(self) -> None:
        """Pad first, paint second. `f"{painted:9}"` counts the escape bytes as
        columns, so the coloured row would sit five characters left of a plain
        one -- invisible in a screenshot of a green run, and a ragged table for
        anyone reading nine hundred rows.

        Asserted by stripping the codes back off and insisting the line is the
        one a log file gets, character for character. The counter and the lane
        tag are painted too, so they are part of what must survive stripping --
        three painted fields on one line is three chances to pad the wrong side
        of the escape.
        """
        box = self.repository()
        _, coloured = self.sweep(box, box / "r.json", terminal=True)
        bare = re.sub(r"\x1b\[[0-9;]*m", "", coloured)
        self.assertIn("L0 caught    tupferl/tiny.py:", bare)
        # The counter keeps its width too: `[1/8]`, not `[1/8] ` shifted by the
        # escapes that were around it.
        self.assertRegex(bare, r"\[\d+/8\] L0 caught    tupferl/tiny\.py:")

    def test_a_capped_run_says_what_it_did_not_run(self) -> None:
        """The one print in `generated` whose absence changes a decision.

        A cap that drops rows silently reads as "everything was covered", and
        the counts underneath look right either way -- they are counts of what
        ran. CLAUDE.md is explicit about this shape, and the code carries a
        comment saying so; neither is a test. Measured before this: both prints
        and both halves of the `if` survived.
        """
        box = self.repository()
        code, said = self.sweep(box, box / "r.json", extra=["--limit", "2"])
        self.assertEqual(0, code, said)
        self.assertIn("--limit 2", said)
        self.assertIn("not run", said)
        # The file names, not just a number: a reader deciding whether the cap
        # mattered needs to know *which* file went unswept.
        self.assertIn("tupferl/tiny.py", said)
        self.assertIn("Counts below are out of what ran", said)

    def test_an_uncapped_run_says_none_of_it(self) -> None:
        """The precondition, without which the assertions above are equally
        satisfied by a run that prints the warning unconditionally."""
        box = self.repository()
        _, said = self.sweep(box, box / "r.json")
        self.assertNotIn("not run", said)

    def test_it_writes_after_every_row_and_again_at_the_end(self) -> None:
        """The point of recording per row: a crash costs one row, not one file.

        **This assertion is inverted from what it was**, and deliberately. It
        used to insist every write landed on a file *boundary*, because resume
        keyed on the path and a mid-file write left a report claiming a file was
        done when it was not. #46 moved both halves together -- resume keys on
        the row now -- so a mid-file write is the feature rather than the hazard,
        and the old assertion would forbid exactly what the change is for.

        Asserted on the *sequence* of writes, not their number. The last two are
        the same size -- the final rewrite of a finished sweep -- so a count
        alone cannot show that the earlier, smaller writes ever happened, and
        those earlier writes are exactly what a crash leaves behind.
        """
        box = self.repository()
        code, said = self.sweep(box, box / "r.json")
        self.assertEqual(0, code, said)
        self.assertGreaterEqual(len(self.wrote), 3, f"writes: {self.wrote}\n{said}")
        self.assertLess(self.wrote[0], self.wrote[-1], f"nothing was written mid-run: {self.wrote}")
        self.assertEqual(self.wrote[-1], max(self.wrote), "the last write was not the complete one")

        # **A write after every row**, which is the claim "recorded per row"
        # makes and the three assertions above do not: they hold just as well
        # for the per-file version this replaced. Counted against the report's
        # own size rather than against a constant, so the fixture growing a
        # file does not turn this into a test of the number 5.
        written = json.loads((box / "r.json").read_text(encoding="utf-8"))
        rows = len(written["results"])
        self.assertEqual(
            list(range(1, rows + 1)),
            self.wrote[:rows],
            f"a row landed without a write; {rows} rows, writes {self.wrote}",
        )

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
        #
        # **Appended to a module a shard actually runs**, not written to a new
        # `tests/test_red.py`. `baseline_shards` is one shard per distinct
        # selection, and every row here selects its own module -- so a third
        # file is in no shard, is never a killer (the real ones fail first), and
        # is therefore completely inert. Measured: with it deleted this test
        # still passed, because the fixture's `zeta.py` suite was itself red
        # and *that* was setting the flag. The precondition was never
        # established, which is CLAUDE.md §2's shape exactly; fixing `zeta.py`
        # in this change is what exposed it.
        red = box / "tests" / "test_tiny.py"
        red.write_text(
            red.read_text(encoding="utf-8")
            + "    def test_red(self): self.fail('red on the untouched tree')\n",
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

    def test_a_real_sweep_feeds_the_killer_forward(self) -> None:
        """`Learned` on the real path, which the unit tests above cannot show.

        They drive the class directly; this drives `main`, so it is the only
        thing that proves the lane consults it at all and that a verdict landing
        updates it. Both halves are asserted: something *was* fed forward, and
        the verdicts are unchanged -- an ordering that altered an answer would
        be the one failure this whole file exists to prevent.
        """
        fed: list[str] = []
        real = mutate.Learned.ahead

        def watched(inner: mutate.Learned, row: Mutation) -> str:
            got = real(inner, row)
            fed.append(got)
            return got

        box = self.repository()
        report = box / "r.json"
        with mock.patch.object(mutate.Learned, "ahead", watched):
            code, said = self.sweep(box, report)
        self.assertEqual(0, code, said)
        self.assertTrue(any(fed), f"nothing was ever fed forward: {fed}")
        outcomes = [
            row["outcome"] for row in json.loads(report.read_text(encoding="utf-8"))["results"]
        ]
        self.assertEqual(["caught"] * len(outcomes), outcomes, "the ordering changed an answer")

    def truncated(self, box: Path, report: Path, keep: int) -> list[str]:
        """Run a real sweep, then cut the report to its first `keep` rows.

        What a crash mid-file leaves behind, built from a real report rather
        than by hand: a report written by this version of `_persist`, holding
        whatever fields it actually writes. A fixture assembled by hand would
        pass just as well if `_persist` stopped recording `span`, which is the
        one field the resume key needs.

        The caller has already swept, so this only cuts -- re-sweeping here
        would answer the rows it is about to remove a second time.
        """
        written = json.loads(report.read_text(encoding="utf-8"))
        written["results"] = written["results"][:keep]
        report.write_text(json.dumps(written), encoding="utf-8")
        return [row["label"] for row in written["results"]]

    def test_a_crash_mid_file_costs_one_row_and_not_the_file(self) -> None:
        """#46, and the whole of it. Both halves have to be here.

        The cut lands *inside* a file, which is what the old resume could not
        express: keyed by path, it read that file as done and silently dropped
        every row of it that had never run -- a report claiming answers it never
        had. On the real tree that was up to 673 rows, 23% of the table.

        So the second run must do two things at once, and each is a way for the
        fix to be wrong in a different direction: re-run exactly the rows the
        cut removed (or the report claims answers it never had), and re-run
        *only* those (or resume records nothing and the whole mechanism is
        decoration).
        """
        box = self.repository()
        report = box / "r.json"
        self.sweep(box, report)
        whole = json.loads(report.read_text(encoding="utf-8"))
        every = [row["label"] for row in whole["results"]]
        # **The cut lands between two rows a key without the span cannot tell
        # apart**, which is two requirements in one. It is inside a file, so the
        # old path-keyed resume would call that file done and drop the rest --
        # the defect. And it separates a colliding pair, so a resume keyed on
        # `(path, new)` reads the second as already answered and drops it too:
        # without that, the span is unobservable and dropping it from the key
        # passes this test. Measured on the real tree, `_key` collapses 556 of
        # 3103 rows; here `MODULE`'s repeated `x * 3` is the same shape, small.
        #
        # Derived from the report rather than hard-coded, because `by_size` puts
        # the smallest file first: an index picked by hand landed in `zeta.py`
        # and missed the pair entirely, and the mutant survived.
        first: dict[tuple[str, str], int] = {}
        cut = None
        for at, record in enumerate(whole["results"]):
            twin = (record["path"], record["new"])
            if twin in first:
                cut = at
                break
            first[twin] = at
        self.assertIsNotNone(cut, f"the fixture has no colliding pair; see MODULE\n{every}")
        keep = typing.cast(int, cut)

        kept = self.truncated(box, report, keep)
        self.wrote = []
        code, said = self.sweep(box, report)
        self.assertEqual(0, code, said)

        again = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(every),
            sorted(row["label"] for row in again["results"]),
            f"the resumed report is not the whole table\n{said}",
        )
        # `self.wrote[0]` is the size of the first write of the second run: the
        # rows carried over plus the one just answered. Anything larger means
        # rows were carried that this run did not do and did not keep.
        self.assertEqual(
            len(every) - len(kept),
            max(self.wrote) - len(kept),
            f"the second run did not re-run exactly the missing rows\n{said}",
        )
        # Summed across the printed lines, because the count is per file and
        # the cut leaves two files partly recorded. Asserting on one line would
        # pass for a run that reported the other as untouched.
        #
        # `-?\d+`, not `\d+`. Without the sign this reads "-2" as 2, and the
        # counter's `+ 1` becoming `- 1` then prints -2 and -1 where 2 and 1
        # belong and still sums to 3. The mutation survived on exactly that.
        self.assertEqual(
            len(kept),
            sum(int(n) for n in re.findall(r"(-?\d+) row\(s\) already recorded", said)),
            f"the skip lines do not account for every recorded row\n{said}",
        )

    def test_the_per_row_writes_are_silent_and_the_last_one_is_not(self) -> None:
        """`announce`, which nothing asserted: both its mutants survived.

        Since #46 a whole-tree run writes 3124 times, and "wrote N row(s)" after
        every one is the loudest thing in the log while saying the same thing
        each time. Both halves, because either alone is satisfied by a
        `_persist` that never prints or always does.
        """
        box = self.repository()
        report = box / "r.json"
        code, said = self.sweep(box, report)
        self.assertEqual(0, code, said)
        rows = len(json.loads(report.read_text(encoding="utf-8"))["results"])
        wrote = said.count("wrote ")
        self.assertLess(wrote, rows, f"a line per row reached the log\n{said}")
        self.assertGreaterEqual(wrote, 1, f"no write was ever announced\n{said}")

    def test_the_skipped_files_are_listed_in_order(self) -> None:
        """`sorted`, and **the fixture is what makes it observable.**

        `by_size` emits the smaller file first, so the counts go into the dict
        in size order and a dict keeps what it was given. The second module was
        called `other.py`, which is both the smaller file *and* the
        alphabetically first one -- the two orders agreed, and `sorted`
        becoming `list` was invisible. It is `zeta.py` now: smaller, so it is
        counted first, and last alphabetically, so the two orders disagree.

        That rename is the whole test. Asserting `sorted(listed) == listed`
        against the old fixture was a tautology, which is CLAUDE.md §2's "two
        symmetric inputs" wearing a different hat.
        """
        box = self.repository()
        report = box / "r.json"
        self.sweep(box, report)
        # One row short of the whole table: every file but the last is fully
        # recorded and the last is partly, so both are listed.
        written = json.loads(report.read_text(encoding="utf-8"))
        written["results"] = written["results"][:-1]
        report.write_text(json.dumps(written), encoding="utf-8")

        self.wrote = []
        _, said = self.sweep(box, report)
        # The precondition, read off the table rather than off the output: the
        # counts are inserted in the order rows appear, so `sorted` only does
        # work when that order is not already alphabetical. Asserted, because
        # this is exactly what was silently untrue before the rename.
        appeared = list(dict.fromkeys(row["path"] for row in written["results"]))
        self.assertNotEqual(
            sorted(appeared), appeared, "the fixture no longer distinguishes the two orders"
        )

        listed = re.findall(r"(\S+): -?\d+ row\(s\) already recorded", said)
        self.assertEqual(2, len(listed), f"both files should be listed\n{said}")
        self.assertEqual(sorted(listed), listed, f"the skip lines are not in order\n{said}")

    def test_a_batch_sweep_without_a_report_runs_and_writes_nothing(self) -> None:
        """`if args.json:` around the per-row write, which every other test here
        satisfies by always passing `--json`.

        Found by the sweep: the guard becoming `if True:` survived, because no
        test drives a batch run without one -- and there it would call
        `_persist(..., None)` and die in `with_name` on a `NoneType`. The rows
        still have to run and the verdicts still have to be reported; the only
        thing absent is the file.
        """
        box = self.repository()
        here = Path.cwd()
        os.chdir(box)
        try:
            with support.quiet() as spill:
                code = mutate.main(
                    ["--base", "HEAD", "--batch", "--no-baseline", "--workers", "1", "--no-killers"]
                )
        finally:
            os.chdir(here)
        said = spill.getvalue()
        self.assertEqual(0, code, said)
        self.assertIn("caught", said, f"no row was reported\n{said}")
        self.assertEqual([], sorted(box.glob("*.json")), "a run with no --json wrote one anyway")

    def test_a_crash_during_a_write_leaves_the_previous_report(self) -> None:
        """`_persist` renames into place, so the report is never half-written.

        Optional before #46 and required after it: at one write a row rather
        than one a file, the sweep spends about 1.4% of its life mid-write
        instead of 0.01%, and `_recorded` reads a truncated report as *nothing*.
        A recovery mechanism whose own recovery file is the likeliest casualty
        is not one.

        The write is killed **part-way through**, which is the only failure the
        two spellings answer differently. The first version of this raised from
        `json.dumps` instead -- so nothing was written at all, in place or
        aside, and the mutation putting the write back in place *survived* it.
        Here the file is opened, half the text lands, and then it raises: what a
        full disk or an OOM kill leaves behind.
        """
        box = self.repository()
        report = box / "r.json"
        self.sweep(box, report)
        before = report.read_text(encoding="utf-8")

        real = Path.write_text
        opened: list[Path] = []

        def half(target: Path, data: str, encoding: str | None = None, **rest: Any) -> int:
            opened.append(target)
            real(target, data[: len(data) // 2], encoding=encoding)
            raise MemoryError("killed part-way through the write")

        with mock.patch.object(Path, "write_text", half), self.assertRaises(MemoryError):
            mutate._persist(mutate.Report([], widened=True), report, announce=False)
        self.assertEqual(before, report.read_text(encoding="utf-8"), "the report was damaged")
        self.assertNotIn(report, opened, "the report itself was opened for writing")
        self.assertEqual(real, Path.write_text, "the patch leaked")

        # And the precondition: a write that *completes* still replaces it, or
        # the assertion above is satisfied by a `_persist` that never writes.
        mutate._persist(mutate.Report([], widened=True), report, announce=False)
        self.assertEqual([], json.loads(report.read_text(encoding="utf-8"))["results"])

    def bogus(self, box: Path) -> Path:
        """A record holding one key for a file this fixture has never had.

        Bogus on purpose: a real key would be *reached* under `--all` and the
        paired test below could not tell the two runs apart.
        """
        where = box / "known-survivors.json"
        where.write_text(
            json.dumps({"deadbeefdeadbeef": {"why": "read, in a file far away", "seen": 1}}),
            encoding="utf-8",
        )
        return where

    def kept(self, where: Path) -> bool:
        return "deadbeefdeadbeef" in json.loads(where.read_text(encoding="utf-8"))

    def test_a_diff_run_may_not_drop_a_record_entry(self) -> None:
        """The defect, driven through `main` rather than through the predicate.

        `--base` generates rows for the changed lines alone, so it "fails to
        generate" every key in an untouched file -- and `_accept` **drops** what
        `stale` names. Measured before the fix on this repository's own record:
        `python -m tools.mutate --base main --accept`, the command CLAUDE.md
        gives, reported 206 of 210 entries stale and would have deleted them,
        with nothing in the output saying so.

        Driving the real `main` because the thing under test is which flags it
        reads: a test of the predicate alone passes just as well when `main`
        computes it and then hands `sort_survivors` a hard-coded `True`.
        """
        box = self.repository()
        where = self.bogus(box)
        code, said = self.sweep(box, box / "r.json", extra=["--accept"])
        self.assertTrue(self.kept(where), f"a diff run deleted a reviewed reason\n{said}")
        self.assertNotIn("match nothing", said, "and claimed the evidence for it")
        self.assertEqual(0, code, said)

    def test_a_whole_tree_run_still_drops_one(self) -> None:
        """The other half, and the precondition for the test above: with the
        evidence in hand the record must still be prunable, or the fix would
        have bought safety by making `stale` unreachable -- an entry nobody
        notices going stale is a reason still standing for code that has gone.
        """
        box = self.repository()
        where = self.bogus(box)
        code, said = self.sweep(box, box / "r.json", extra=["--accept"], scope=["--all"])
        self.assertIn("match nothing", said)
        self.assertFalse(self.kept(where), f"a whole-tree run kept a stale reason\n{said}")
        self.assertEqual(0, code, said)

    def test_a_filtered_whole_tree_run_may_not_drop_one_either(self) -> None:
        """`--all` narrowed by `--only` is a subset again, and the same loss.

        Found by the sweep, which left `or` becoming `and` alive on the line:
        with all three filters empty the two spellings agree, so the two tests
        above cannot tell them apart -- and under the mutant `--all --only x
        --accept` prunes the record on the evidence of one file. A third
        direction rather than a fourth: `--operator` and `--skip-operator` sit
        in the same `or` and are narrowing for the same reason.
        """
        box = self.repository()
        where = self.bogus(box)
        code, said = self.sweep(
            box,
            box / "r.json",
            extra=["--accept", "--only", "tupferl/zeta.py"],
            scope=["--all"],
        )
        self.assertTrue(self.kept(where), f"a filtered run deleted a reviewed reason\n{said}")
        self.assertNotIn("match nothing", said, "and claimed the evidence for it")
        self.assertEqual(0, code, said)

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
    """`_budget` asks the kernel what is free, and halves only when it will not say.

    **The halving is a guess about people, and it is wrong in both directions.**
    It assumes someone else wants half whether or not anyone is there. On the
    idle cloud container this was found on it left 8 GiB of 16 unused, and
    because `_share` gives up *lanes* once each ceiling would fall under
    `_FLOOR`, halving the budget more than halved the parallelism: 3 lanes where
    7 fit, and a 12-row table at 11.2s against 7.6s, three interleaved pairs. On
    a laptop already holding ten of sixteen gigabytes in an editor and a
    browser, the same rule hands out eight that are not there.

    `MemAvailable` answers the question actually being asked, and it accounts
    for every other process by construction. The halving survives as the
    fallback for a machine with no `/proc/meminfo` -- macOS, which is a CI leg,
    which is what keeps that path exercised.

    **Every test here pins the machine.** `_unclaimed` reads a real file, so a
    fixture that left it pointing at the real `/proc/meminfo` would assert
    against however much memory the developer happened to have free -- green on
    an idle laptop, red on a busy one, and telling nobody why. `machine` writes
    the file it is about.
    """

    #: Bigger than `_SPARE` and than `_FLOOR`, so "leave a gibibyte" and "never
    #: go under the floor" are both visible rather than clipping each other.
    VISIBLE = 16 << 30

    def meminfo(self, text: str | None) -> Any:
        """Point `mutate.MEMINFO` at a file of this test's own, or at nothing.

        `None` is a machine that will not say -- spelled as a path that does not
        exist rather than by patching `_unclaimed`, because the fallback is
        reached through `read_text` raising and that is the arm being claimed.
        """
        box = Path(tempfile.mkdtemp(prefix="tupferl-meminfo-"))
        self.addCleanup(shutil.rmtree, box, True)
        where = box / "meminfo"
        if text is not None:
            where.write_text(text, encoding="utf-8")
        return mock.patch.object(mutate, "MEMINFO", where)

    def kernel_says(self, available_kb: int | None, free_kb: int = 200) -> str:
        """A `/proc/meminfo` in the kernel's own format, and the `kB` it writes.

        `MemFree` is always present and always small, so a reader that took it
        instead of `MemAvailable` would size a pool from the page cache being
        full -- which is every machine that has read a file.
        """
        lines = [f"MemTotal:       {self.VISIBLE // 1024} kB", f"MemFree:{free_kb:15d} kB"]
        if available_kb is not None:
            lines.insert(1, f"MemAvailable:{available_kb:11d} kB")
        return "\n".join(lines) + "\n"

    def budget(self, available: int | None = None, /, **environment: str) -> int:
        """The budget on a machine with `available` bytes unclaimed, or on one
        whose kernel will not say when `available` is None.

        Positional-only, because `**environment` carries variable names --
        `mutate._TOTAL` among them -- and a keyword parameter beside it is one
        renamed constant away from a caller silently setting this instead of the
        environment. `mypy` says so rather than waiting for it to happen.
        """
        said = None if available is None else self.kernel_says(available // 1024)
        seen = mock.patch.object(mutate, "_visible_memory", lambda: self.VISIBLE)
        with seen, self.meminfo(said), mock.patch.dict(os.environ, environment, clear=True):
            return mutate._budget()

    def test_a_machine_with_room_gets_what_is_actually_free(self) -> None:
        """Not half of what exists. The whole point: an idle machine is measured
        rather than assumed to be half somebody else's."""
        self.assertEqual((14 << 30) - (1 << 30), self.budget(14 << 30))

    def test_a_busy_machine_gets_less_and_that_is_the_half_that_matters(self) -> None:
        """The direction a change like this is never tested in. Without it,
        every assertion above is equally satisfied by "always take nearly
        everything", which is the version that gets a laptop OOM-killed."""
        self.assertEqual((3 << 30) - (1 << 30), self.budget(3 << 30))
        self.assertLess(self.budget(3 << 30), self.budget(14 << 30))

    def test_a_cgroup_limit_still_binds_under_a_roomy_host(self) -> None:
        """Inside a container `/proc/meminfo` reports the **host's** numbers, so
        a 2 GiB cgroup on a 62 GiB host reads as 60 available. Taking that at
        face value is the OOM kill `_visible_memory` was written to prevent,
        arriving through a second source of truth."""
        confined = mock.patch.object(mutate, "_visible_memory", lambda: 4 << 30)
        roomy = self.meminfo(self.kernel_says((60 << 30) // 1024))
        with confined, roomy, mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual((4 << 30) - (1 << 30), mutate._budget())

    def test_a_kernel_that_will_not_say_falls_back_to_halving(self) -> None:
        """macOS has no `/proc/meminfo`, and the `macos` CI leg is what proves
        this arm stays reachable -- the same argument `tupferl/config.py`'s
        `tomli` fallback rests on."""
        self.assertEqual(self.VISIBLE // 2, self.budget(None))

    def test_a_malformed_available_line_is_refused(self) -> None:
        """A value that is not a number, or a unit that is not `kB`, is not a
        reading. Falling back to the halving is the honest answer; parsing it
        anyway would size a pool from whatever `int()` happened to accept.

        Three shapes, because a guard of three `and`ed clauses needs an input
        that fails each one on its own -- with only well-formed text, swapping
        an `and` for an `or` survives."""
        for said in ("MemAvailable: lots kB", "MemAvailable: 4096", "MemAvailable: 4096 MB"):
            with self.subTest(said=said), self.meminfo(said + "\n"):
                self.assertEqual(0, mutate._unclaimed(), f"{said!r} was read as a number")

    def test_mem_free_is_not_mistaken_for_mem_available(self) -> None:
        """An old kernel writes no `MemAvailable`. Reading `MemFree` instead
        would size the pool from whatever the page cache has not taken, which on
        any machine that has read a file is a small number and a wrong one."""
        with self.meminfo(self.kernel_says(None, free_kb=200)):
            self.assertEqual(0, mutate._unclaimed())

    def test_a_shared_machine_keeps_half_for_the_person_using_it(self) -> None:
        """The fallback rule, unchanged, on a machine that cannot be measured."""
        self.assertEqual(self.VISIBLE // 2, self.budget(None))

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
        `_share` in the first place.

        Four rules now, and each names itself. The measured one says the number
        it measured: "3 lanes" is a mystery, "3072 MiB unclaimed" is a machine
        with something else running on it, and the difference is the whole
        reason this line exists.
        """
        busy = self.meminfo(self.kernel_says((3 << 30) // 1024))
        with busy, mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn("unclaimed", mutate._why())
            self.assertIn(str(3 << 10), mutate._why(), "it does not say how much")
        silent = self.meminfo(None)
        with silent, mock.patch.dict(os.environ, {}, clear=True):
            self.assertIn("shared", mutate._why())
        with self.meminfo(None), mock.patch.dict(os.environ, {"CI": "true"}, clear=True):
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


class TestEveryReaderGivesTheFourFieldsThatDecideMembership(unittest.TestCase):
    """pid, parent, group and resident, from whichever reader this platform has.

    `_lane` walks parents and groups to decide which processes a lane answers
    for, and `_Lanes` kills on resident. Those four are what every platform must
    get right, so this class runs everywhere.

    Driven against the real kernel: the subject *is* what `/proc` and `ps`
    report, and a fixture supplying the text would assert this author's belief
    about their format rather than reading it.
    """

    def test_this_process_is_found_with_its_parentage_and_memory(self) -> None:
        me = mutate._processes()[os.getpid()]
        self.assertEqual(os.getppid(), me.parent)
        self.assertEqual(os.getpgrp(), me.group)
        self.assertGreater(me.resident, 0, "no resident memory read for this process")


@unittest.skipUnless(Path("/proc/self/stat").exists(), "there is no /proc here")
class TestWhereThereIsAProc(unittest.TestCase):
    """The half only Linux can answer, and the reader only Linux uses.

    `_from_proc` is unreachable without a `/proc`, so these compare it against
    `ps` and check the address-space field that `_report_headroom` rests on.
    Named in the macOS job's `--exclude` list rather than left to skip: that
    job passes `--no-skips`, which exists to catch a suite quietly doing
    nothing.
    """

    def test_address_space_is_read_and_is_the_larger_of_the_two(self) -> None:
        """Both fields, from the one read. Without `address` every ceiling
        question is unanswerable while every assertion about `resident` still
        passes."""
        me = mutate._from_proc()[os.getpid()]
        self.assertGreater(me.resident, 0)
        self.assertGreater(me.address, me.resident, "address space is not above resident")

    def test_the_group_is_read_where_a_session_would_not_do(self) -> None:
        """`pgid` and `sid` sit next to each other in `/proc/<pid>/stat`, and
        for this process they hold the **same number** -- so reading the wrong
        one is invisible here, and the mutant that does it survived the sweep.

        A child that starts a new process *group* without a new session tells
        them apart: its `pgid` becomes its own pid while its `sid` stays its
        parent's. `os.setpgrp` rather than `os.setsid`, which would make both.
        """
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], preexec_fn=os.setpgrp
        )
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        seen = mutate._from_proc()[child.pid]
        self.assertEqual(child.pid, seen.group, "the process group was read from another field")
        self.assertNotEqual(os.getsid(0), seen.group, "the fixture cannot tell the two apart")

    def test_the_two_readers_agree_on_resident(self) -> None:
        """`resident` is read in pages from `/proc` and in kibibytes from `ps`,
        so each has its own unit to get wrong -- and the mutation that turns
        either multiply into a divide leaves a number that is still positive,
        which is all "greater than zero" can see.

        Compared loosely on purpose: the two are read a moment apart and the
        process is running, so demanding equality would be a flake.
        """
        mine = mutate._from_proc()[os.getpid()]
        theirs = mutate._from_ps()[os.getpid()]
        self.assertLess(
            max(mine.resident, theirs.resident) / min(mine.resident, theirs.resident),
            2.0,
            f"proc says {mine.resident} resident and ps says {theirs.resident}",
        )

    #: Comfortably above one sampling interval and far below the harness's own
    #: 30s per-test alarm, which is the bound `tests/test_watch.py` learned to
    #: check a test's own timeout against.
    PATIENCE = 8.0

    def test_a_lane_nobody_kills_is_still_measured(self) -> None:
        """The whole change, and it lives here because the number it asserts on
        comes from `/proc`.

        `_from_ps` reports no address space at all, deliberately, so on macOS a
        watched lane is measured at 0 and this assertion could not hold. It was
        in the platform-independent class until the macOS leg said otherwise --
        which is the same mistake as the four tests above, caught before a
        second red run rather than after it.

        A ceiling this generous is never reached, so the old code would have
        recorded nothing at all about this lane.
        """
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
        )
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.addCleanup(mutate._WATCHED.forget)
        mutate._WATCHED.forget()
        mutate._WATCHED.watch(child.pid, 64 << 30)
        self.addCleanup(mutate._WATCHED.release, child.pid)

        deadline = time.monotonic() + self.PATIENCE
        while not mutate._WATCHED.widest() and time.monotonic() < deadline:
            time.sleep(mutate._SAMPLE / 4)
        self.assertGreater(
            mutate._WATCHED.widest(), 0, "a live lane was watched and never measured"
        )

    def test_the_two_readers_agree_on_parentage(self) -> None:
        mine = mutate._from_proc()[os.getpid()]
        theirs = mutate._from_ps()[os.getpid()]
        self.assertEqual(mine.parent, theirs.parent)
        self.assertEqual(mine.group, theirs.group)


class TestWhatPsIsAskedFor(unittest.TestCase):
    """Four columns, and no address space.

    The first version read `vsz` too, and the macOS leg reported `401357 MiB of
    its 4096 MiB ceiling (9799%)`. macOS counts reserved regions no `RLIMIT_AS`
    figure would -- and it does not enforce that ceiling at all, so there was
    nothing there for the number to be headroom *against*.

    Driven on real `ps` output, which is why the parse is its own function:
    `_from_ps` forks, and a test would otherwise have to mock the fork rather
    than read what a real `ps` prints.
    """

    def ran(self, *columns: str) -> str:
        listed = subprocess.run(
            ["ps", "-eo", ",".join(f"{name}=" for name in columns)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, listed.returncode, listed.stderr)
        return listed.stdout

    def test_the_four_fields_are_read_from_real_output(self) -> None:
        table = mutate._parse_ps(self.ran("pid", "ppid", "pgid", "rss"))
        self.assertIn(os.getpid(), table, "ps produced no process table")
        self.assertEqual(os.getppid(), table[os.getpid()].parent)
        self.assertGreater(table[os.getpid()].resident, 0)

    def test_no_address_space_is_invented(self) -> None:
        """0 rather than a guess, so `_report_headroom` stays silent where there
        is no enforced ceiling to compare against."""
        self.assertEqual(
            0, mutate._parse_ps(self.ran("pid", "ppid", "pgid", "rss"))[os.getpid()].address
        )

    def test_a_fifth_column_is_refused_rather_than_misread(self) -> None:
        """`vsz` is not asked for, so a row carrying it is not this reader's
        output. Taking the first four fields of it anyway would be reading a
        format nobody promised."""
        self.assertEqual({}, mutate._parse_ps("7 1 7 2048 4096\n"))

    def test_a_line_that_is_not_numbers_is_skipped(self) -> None:
        """`ps` writes a header when asked without `=`, and a machine may print
        anything on stderr-ish lines. A row that is not five integers is not a
        process.

        **The mixed row is the one that matters.** A header is *all* words, so
        it is refused by `all(...)` and by `any(...)` alike -- a fixture of only
        those cannot tell the two apart, and the mutation swapping them survived
        against exactly that. `123 456 789 abc def` is the shape that separates
        them, and it is also the realistic one: a column that failed to render
        leaves numbers beside it.
        """
        self.assertEqual({}, mutate._parse_ps("PID PPID PGID RSS VSZ\nnot a process\n"))
        self.assertEqual({}, mutate._parse_ps("123 456 789 abc def\n"))
        self.assertEqual({}, mutate._parse_ps("123 456 789 1024 zzz\n"))

    def test_the_kibibytes_are_multiplied_and_not_divided(self) -> None:
        """`ps` reports KiB. A divide leaves a number that is still positive,
        which is all an assertion of "greater than zero" can see -- so this
        pins the arithmetic against a known input instead."""
        self.assertEqual(2048 * 1024, mutate._parse_ps("7 1 7 2048\n")[7].resident)


class TestWhatTheHeaviestLaneHeld(unittest.TestCase):
    """`_Lanes` measures every lane, not only one it is about to kill.

    Until this existed the only address-space figure anywhere near this module
    was a constant copied from another repository, and it was 2.3x wrong here.
    A sweep that measures the thing it is bounded by can say whether the bound
    still fits; one that measures it *only when killing* can only say so
    afterwards.
    """

    #: Comfortably above one sampling interval and far below the harness's own
    #: 30s per-test alarm, which is the bound `tests/test_watch.py` learned to
    #: check a test's own timeout against.
    PATIENCE = 8.0

    def setUp(self) -> None:
        mutate._WATCHED.forget()
        self.addCleanup(mutate._WATCHED.forget)

    def test_a_fresh_sampler_has_no_mark_at_all(self) -> None:
        """Read before anything has been watched. Every other test here calls
        `forget` in `setUp`, so the value `__init__` sets is unobservable to
        them -- three mutants of it survived the sweep, including one that
        removed the assignment and left `widest` raising `AttributeError`."""
        self.assertEqual(0, mutate._Lanes().widest())

    def test_forget_starts_a_fresh_mark(self) -> None:
        """`_WATCHED` is a module-level singleton and a process may call `run`
        more than once -- a spec file calling `verify` twice is the shape that
        does it. Without this the second run reports the first one's peak."""
        mutate._WATCHED._widest = 123 << 20
        self.assertEqual(123 << 20, mutate._WATCHED.widest())
        mutate._WATCHED.forget()
        self.assertEqual(0, mutate._WATCHED.widest())

    def said(self, widest: int, ceiling: int, terminal: bool = False) -> str:
        held = mock.patch.object(mutate._WATCHED, "_widest", widest)
        with held, support.quiet(terminal) as spill:
            mutate._report_headroom(ceiling)
        return spill.getvalue()

    def test_the_line_names_what_was_held_and_what_was_allowed(self) -> None:
        """Both numbers, because either alone is unactionable: 1892 MiB means
        nothing without the ceiling, and the ceiling was already printed at the
        top of a run that may have scrolled."""
        said = self.said(1892 << 20, 2053 << 20)
        self.assertIn("1892 MiB", said)
        self.assertIn("2053 MiB", said)
        self.assertIn("92%", said)

    def test_it_says_the_figure_is_sampled_and_low(self) -> None:
        """A number presented as exact invites being divided into. This one is
        the *current* size read once a second, and measured 3% under the
        kernel's own high-water over a sweep."""
        self.assertIn("sampled", self.said(1892 << 20, 2053 << 20))

    def test_the_threshold_itself_counts_as_thin(self) -> None:
        """Exactly at `_TIGHT`, which is the only input that tells `>=` from
        `>`. The pair below uses 95% and 10% and cannot see the difference; the
        mutant survived the sweep against them."""
        at = int(mutate._TIGHT * (2000 << 20))
        self.assertIn(paint.ODD, self.said(at, 2000 << 20, terminal=True))

    def test_a_thin_margin_is_shouted_and_a_roomy_one_is_not(self) -> None:
        """The half that makes the line worth printing at all. Without it the
        report is the same colour whether the ceiling is comfortable or one
        test away from killing every lane."""
        tight = self.said(1900 << 20, 2000 << 20, terminal=True)
        roomy = self.said(200 << 20, 2000 << 20, terminal=True)
        self.assertIn(paint.ODD, tight, "a 95% margin was muttered")
        self.assertNotIn(paint.ODD, roomy, "a 10% margin was shouted")
        self.assertIn(paint.QUIET, roomy)

    def ran(self, *patched: Any) -> str:
        """One real `run`, with the rows stubbed out.

        `TestTheRunAccountsForItsLanes.ROW` rather than the module's `row()`:
        that helper's placeholder text appears 2148 times in its file, so
        `check` refuses it. Referenced at call time, since the class is defined
        below this one.
        """
        only = TestTheRunAccountsForItsLanes.ROW
        # `ExitStack` rather than a starred `with`, which is a syntax error --
        # and `TestCase.enterContext` is 3.11, which the 3.10 leg does not have.
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(
                    mutate, "_attempt", lambda *a, **k: mutate.Verdict("caught", "probe")
                )
            )
            for context in patched:
                stack.enter_context(context)
            spill = stack.enter_context(support.quiet())
            mutate.run([only], baseline=False, summarise=False)
        return spill.getvalue()

    def test_a_run_starts_from_a_fresh_mark(self) -> None:
        """A `run` that reports without forgetting prints the *previous* run's
        peak. Set one, and insist it does not come out."""
        mutate._WATCHED._widest = 999 << 20
        self.assertNotIn("999 MiB", self.ran(), "a mark from an earlier run was reported")

    def test_a_run_reports_its_headroom_at_all(self) -> None:
        """The other half of the wiring, and it needs its own test: with
        `forget` working and no lane sampled, a `run` that never calls
        `_report_headroom` prints nothing -- which is exactly what a correct run
        prints too. The two mutants are indistinguishable from one assertion,
        and both survived the sweep against one.

        So `widest` is pinned rather than the mark, which takes `forget` out of
        the picture and leaves only "was the report reached".
        """
        held = mock.patch.object(mutate._WATCHED, "widest", lambda: 512 << 20)
        self.assertIn("512 MiB", self.ran(held), "run never reported its headroom")

    def test_a_run_that_sampled_nothing_says_nothing(self) -> None:
        """Lanes that each finish inside one sampling interval leave no reading.
        Printing "0 MiB of 2053 MiB (0%)" would report a measurement that was
        never taken as though it were a result -- which is the shape this whole
        change exists to correct."""
        self.assertEqual("", self.said(0, 2053 << 20))

    def test_no_ceiling_means_no_share_to_report(self) -> None:
        """`--memory 0` is "no cap", and a percentage of nothing is a
        ZeroDivisionError rather than a fact."""
        self.assertEqual("", self.said(1892 << 20, 0))


class TestWhyAProbeThatWroteNothingDied(unittest.TestCase):
    """`_signalled`: the sentence a row gets when there is nothing else to say.

    It is reached only when a probe produced no report *and* said nothing on its
    way out, so it is the whole of what a reader sees about that row -- and it
    had no test. The distinction it exists to draw is between "the mutation did
    this" and "the machine did this", which is the difference between a row worth
    reading and a row worth re-running.
    """

    def test_an_ordinary_exit_names_the_status(self) -> None:
        self.assertEqual("the probe exited 3 without writing a report", mutate._signalled(3))

    def test_a_clean_exit_with_no_report_is_still_an_exit(self) -> None:
        """Zero is on the boundary, and it is the side that is not a signal:
        `subprocess` spells a signal as a *negative* number, so `>= 0` and `> 0`
        differ exactly here -- and under `> 0` a probe that exited 0 would be
        reported as killed by signal 0, which is not a signal at all."""
        self.assertEqual("the probe exited 0 without writing a report", mutate._signalled(0))

    def test_a_kill_says_which_signal_and_what_it_usually_means(self) -> None:
        """`SIGKILL` is the one that matters: it is what the host OOM killer
        sends, so it is what a lane looks like where `verdict.cap` is not
        enforced. Both causes are named because a sweep of this module produced
        the second, and a message naming the wrong one costs more than a message
        naming none."""
        said = mutate._signalled(-signal.SIGKILL)
        self.assertIn("killed by SIGKILL", said)
        self.assertIn("ran out of memory", said)
        self.assertIn("killed the session it was in", said)

    def test_any_other_signal_is_named_without_the_guess(self) -> None:
        """The other half. Without it, a `_signalled` that appended the
        out-of-memory clause to *every* signal passes the test above -- and
        would tell a reader their machine was out of memory every time a probe
        timed out."""
        said = mutate._signalled(-signal.SIGTERM)
        self.assertIn("killed by SIGTERM", said)
        self.assertNotIn("ran out of memory", said)

    def test_a_number_that_is_not_a_signal_does_not_raise(self) -> None:
        """`signal.Signals(-returncode)` raises for a number outside the set,
        and this runs inside the handler for a probe that already failed --
        where an exception replaces the row's reason with a traceback about the
        reason."""
        self.assertIn("killed by ?", mutate._signalled(-999))


class TestTheLastThingARunManagedToSay(unittest.TestCase):
    """`_tail`: the log's final line, which is a `BROKE` row's whole reason.

    `_run` reads it before falling back to `_signalled`, so an empty answer here
    is what decides whether a reader is told what the probe said or only how it
    died. Every arm returns a string, because the caller puts it straight into a
    `Verdict`.
    """

    def tail(self, text: str) -> str:
        box = Path(tempfile.mkdtemp(prefix="tupferl-tail-"))
        self.addCleanup(shutil.rmtree, box, True)
        noise = box / "noise.log"
        noise.write_text(text, encoding="utf-8")
        return mutate._tail(noise)

    def test_the_last_line_is_what_comes_back(self) -> None:
        self.assertEqual("the last one", self.tail("first\nsecond\nthe last one\n"))

    def test_trailing_blank_lines_are_not_the_last_line(self) -> None:
        """A process that dies mid-write leaves them, and "" as a reason reads
        as a row nobody can explain rather than as one that said something."""
        self.assertEqual("real", self.tail("real\n\n\n   \n"))

    def test_a_log_with_nothing_in_it_is_empty_rather_than_an_error(self) -> None:
        """`_run` spells this `_tail(noise) or _signalled(...)`, so the empty
        string is what hands the question on. An `IndexError` from the last-line
        read would replace the row's reason with a traceback."""
        self.assertEqual("", self.tail(""))

    def test_a_log_that_was_never_written_is_empty_too(self) -> None:
        """The `OSError` arm. A probe killed before it opened its log leaves no
        file at all, which is precisely the case `_signalled` exists for."""
        self.assertEqual("", mutate._tail(Path("/nonexistent/noise.log")))

    def test_bytes_that_are_not_utf8_do_not_stop_the_report(self) -> None:
        """`errors="replace"`. A probe's log is whatever the tests under it
        wrote, and this project has a test that deliberately puts invalid UTF-8
        in a path -- so a strict decode here would turn one row's reason into an
        exception during the summary of every other."""
        box = Path(tempfile.mkdtemp(prefix="tupferl-tail-"))
        self.addCleanup(shutil.rmtree, box, True)
        noise = box / "noise.log"
        noise.write_bytes(b"fine\nbroken \xff\xfe here\n")
        self.assertIn("broken", mutate._tail(noise))


class TestSurvivorsSomebodyHasAlreadyRead(unittest.TestCase):
    """`known-survivors.json`: a disposition per row, carried to the next sweep.

    **The problem it exists for.** A whole-tree sweep found 557 survivors, and
    triaging them in prose does not survive to the following Sunday: the sweep
    produces the same 557 rows with nothing to say which were already
    understood, so either somebody reads all of them again or nobody reads any.
    Both have happened here.

    Keyed by content, like `Killers` -- file, operator, and the text going in
    and out -- so a row keeps its disposition when the code around it moves.
    A key made from a line number would go empty on the first edit above it,
    which is every edit.

    **The hazard is the whole design.** A record of accepted survivors is how a
    project stops looking at them, so three things are load-bearing: the count
    is always printed, a stale entry is reported loudly, and `--accept` writes
    `TODO` rather than a reason it invented.
    """

    def rows(self, *labels: str, outcome: mutate.Outcome = "survived") -> list[mutate.Result]:
        return [
            mutate.Result(
                Mutation(
                    label,
                    "tupferl/sync.py",
                    f"old{n}",
                    f"new{n}",
                    "tests.test_sync",
                    operator="branch",
                ),
                mutate.Verdict(outcome, ""),
            )
            for n, label in enumerate(labels)
        ]

    def test_a_row_that_asked_nothing_is_recorded_too(self) -> None:
        """**Not caught, rather than `survived`.** `broke` and `timeout` were
        the one category with nowhere to be written down: 33 of them came back
        every whole-tree run with nothing to say which had been read, and three
        of them cannot be answered at all -- two run the whole suite nested
        inside a memory-capped sandbox, one is a fork bomb. Those want a written
        reason exactly as an equivalent mutant does.
        """
        outcomes: tuple[mutate.Outcome, ...] = ("broke", "timeout")
        for outcome in outcomes:
            with self.subTest(outcome=outcome):
                results = self.rows("a", outcome=outcome)
                self.assertEqual(1, len(mutate.sort_survivors(results, {}).fresh))

                key = mutate._key(results[0].mutation)
                found = mutate.sort_survivors(results, {key: mutate.Accepted("a fork bomb", 1)})
                self.assertEqual([], found.fresh)
                self.assertEqual([(results[0], "a fork bomb")], found.accepted)

    def test_a_caught_row_is_never_recorded(self) -> None:
        """The precondition for the test above, and the line the widening had to
        stop at: a record that absorbed caught rows would be the whole table,
        and `--accept` would write a `TODO` for every mutation in the tree.
        """
        results = self.rows("a", outcome="caught")
        found = mutate.sort_survivors(results, {})
        self.assertEqual([], found.fresh)
        self.assertEqual([], found.accepted)

    def test_a_partial_run_reports_nothing_stale(self) -> None:
        """A `--base` run generates rows for the changed lines alone, so every
        key belonging to an untouched file "matches nothing it generated".

        Not a cosmetic complaint. `_accept` **drops** what `stale` names, so
        `--base main --accept` -- the command CLAUDE.md gives -- deleted 206 of
        this record's 210 reviewed reasons, silently: the count simply came back
        smaller. Measured on the sweep for this very change.
        """
        results = self.rows("a")
        record = {"deadbeefdeadbeef": mutate.Accepted("read, in a file this run never touched", 1)}
        self.assertEqual([], mutate.sort_survivors(results, record, complete=False).stale)

    def test_a_complete_run_still_reports_it(self) -> None:
        """The precondition, and the half the record needs: a whole-tree sweep
        does have the evidence, and an entry nobody notices going stale is a
        reason still standing for code that has gone."""
        results = self.rows("a")
        record = {"deadbeefdeadbeef": mutate.Accepted("read, in a file that has gone", 1)}
        found = mutate.sort_survivors(results, record, complete=True)
        self.assertEqual(["deadbeefdeadbeef"], found.stale)

    def test_a_partial_run_still_matches_what_it_did_generate(self) -> None:
        """`complete` bounds `stale` and nothing else. A row this run produced is
        sorted on its record entry exactly as before -- otherwise the fix would
        have bought a safe `stale` by making every `--base` run report every
        survivor as new."""
        results = self.rows("a")
        key = mutate._key(results[0].mutation)
        found = mutate.sort_survivors(results, {key: mutate.Accepted("read", 1)}, complete=False)
        self.assertEqual([], found.fresh)
        self.assertEqual([(results[0], "read")], found.accepted)

    def test_a_row_now_caught_keeps_its_entry_rather_than_going_stale(self) -> None:
        """`stale` asks whether this run *generated* the key, not whether it
        sorted it -- so a mutation the suite has since learned to kill keeps its
        entry, silently, rather than being reported.

        Deliberate, and the alternative is worse. `stale` is loud because it
        means "the code this describes has moved or gone", and here the code is
        still there. An outcome, unlike a line of source, is not stable between
        runs: `Killers` reorders the selection each time, so a row near the
        alarm can be `caught` one Sunday and `timeout` the next, and a record
        that dropped the reason on the first would demand it be written again on
        the second. The cost is one dead row; the cost of the other reading is a
        record that churns exactly where it is least trustworthy.
        """
        results = self.rows("a", outcome="caught")
        key = mutate._key(results[0].mutation)
        self.assertEqual([], mutate.sort_survivors(results, {key: mutate.Accepted("r", 1)}).stale)

    def record(self, rows: dict[str, object] | str) -> Path:
        box = Path(tempfile.mkdtemp(prefix="tupferl-known-"))
        self.addCleanup(shutil.rmtree, box, True)
        where = box / "known-survivors.json"
        where.write_text(rows if isinstance(rows, str) else json.dumps(rows), encoding="utf-8")
        return where

    def test_an_unread_survivor_is_fresh(self) -> None:
        found = mutate.sort_survivors(self.rows("a"), {})
        self.assertEqual(1, len(found.fresh))
        self.assertEqual([], found.accepted)

    def test_a_recorded_survivor_is_not_fresh_and_keeps_its_reason(self) -> None:
        """The reason travels with it. A key alone would say "somebody looked"
        without saying what they concluded, which is the difference between a
        record and a mute list."""
        results = self.rows("a")
        why = "a progress line; nothing asserts on it"
        found = mutate.sort_survivors(
            results, {mutate._key(results[0].mutation): mutate.Accepted(why, 1)}
        )
        self.assertEqual([], found.fresh)
        self.assertEqual([(results[0], why)], found.accepted)

    def test_a_new_survivor_beside_a_known_one_is_still_reported(self) -> None:
        """The point of the whole thing: 556 read and 1 new must read as 1, not
        as 557 or as nothing."""
        results = self.rows("known", "brand new")
        found = mutate.sort_survivors(
            results, {mutate._key(results[0].mutation): mutate.Accepted("read", 1)}
        )
        self.assertEqual(1, len(found.fresh))
        self.assertEqual("brand new", found.fresh[0].mutation.label)

    def test_a_caught_row_is_not_a_survivor_however_it_is_recorded(self) -> None:
        """Accepting a key must not make a *caught* row invisible, or a test
        that starts failing to catch something would be silently absorbed."""
        caught = [mutate.Result(self.rows("a")[0].mutation, mutate.Verdict("caught", "t"))]
        found = mutate.sort_survivors(
            caught, {mutate._key(caught[0].mutation): mutate.Accepted("read", 1)}
        )
        self.assertEqual([], found.fresh)
        self.assertEqual([], found.accepted)

    def test_an_entry_matching_nothing_is_called_stale(self) -> None:
        """A record for code that has gone is a claim about a tree that no
        longer exists -- and the row it used to cover may have been replaced by
        one nobody has read. Reported rather than quietly kept."""
        found = mutate.sort_survivors(
            self.rows("a"), {"deadbeefdeadbeef": mutate.Accepted("long gone", 1)}
        )
        self.assertEqual(["deadbeefdeadbeef"], found.stale)

    def test_a_key_that_matched_a_caught_row_is_not_stale(self) -> None:
        """The row was generated; it simply stopped surviving. Calling that
        stale would churn the file every time a test started working."""
        caught = [mutate.Result(self.rows("a")[0].mutation, mutate.Verdict("caught", "t"))]
        found = mutate.sort_survivors(
            caught, {mutate._key(caught[0].mutation): mutate.Accepted("read", 1)}
        )
        self.assertEqual([], found.stale)

    def test_an_unreadable_record_reports_more_rather_than_less(self) -> None:
        """A JSON comma lost in a merge must not silently accept every survivor
        in the tree. Empty is the safe answer, and it is the loud one."""
        for broken in (
            "{not json",
            "[]",
            '"a string"',
            '{"key": 4}',
            '{"key": "a bare reason"}',  # the shape before `seen` existed
            '{"key": {"why": "read", "seen": 0}}',  # a count that covers nothing
            '{"key": {"why": "read"}}',
            '{"key": {"seen": 2}}',
        ):
            with self.subTest(broken=broken):
                self.assertEqual({}, mutate.known_survivors(self.record(broken)))

    def test_a_missing_record_is_not_an_error(self) -> None:
        """The ordinary case before anybody has accepted anything, and the case
        in a fresh clone."""
        self.assertEqual({}, mutate.known_survivors(Path("/nonexistent/known.json")))

    def test_a_well_formed_record_is_read(self) -> None:
        """The precondition. Without it, every assertion above is satisfied by
        a reader that always answers empty."""
        self.assertEqual(
            {"abc": mutate.Accepted("why", 3)},
            mutate.known_survivors(self.record({"abc": {"why": "why", "seen": 3}})),
        )

    def test_a_second_row_of_a_shape_already_read_is_fresh(self) -> None:
        """**The count is the whole point.** `_key` is content, not position, so
        two identical mutations in one file share a key -- measured, 557
        survivors over 432 keys on the first whole-tree sweep. Recorded as a
        set, accepting one would absorb 125 rows nobody read, and would keep
        absorbing every future one of that shape.

        Both rows here have the same operator and the same text, so they *are*
        one key; a record of `seen: 1` covers exactly one of them.
        """
        results = self.rows("a", "a")
        results[1] = mutate.Result(results[0].mutation, mutate.Verdict("survived", ""))
        key = mutate._key(results[0].mutation)
        self.assertEqual(key, mutate._key(results[1].mutation), "the fixture must collide")

        one = mutate.sort_survivors(results, {key: mutate.Accepted("read", 1)})
        self.assertEqual(1, len(one.fresh))
        self.assertEqual(1, len(one.accepted))

        both = mutate.sort_survivors(results, {key: mutate.Accepted("read", 2)})
        self.assertEqual([], both.fresh)
        self.assertEqual(2, len(both.accepted))


class TestWhenARecordedSweepGoesRed(unittest.TestCase):
    """`_status`: the one thing the record is allowed to change about CI.

    Excusing a row somebody read is the point; excusing anything else would make
    the weekly sweep a job that cannot fail, which is worse than not having it.
    So every other reason a run is not clean has a row here.
    """

    def report(self, *outcomes: mutate.Outcome, baseline_red: bool = False) -> mutate.Report:
        # A distinct `new` per row. `_key` is content, so rows identical but for
        # their outcome would share one key and no test below could accept the
        # `broke` one without also accepting the `survived` one beside it.
        return mutate.Report(
            [
                mutate.Result(
                    Mutation(f"row {n}", "tupferl/sync.py", "a", f"b{n}", "t", operator="branch"),
                    mutate.Verdict(outcome, ""),
                )
                for n, outcome in enumerate(outcomes)
            ],
            baseline_red=baseline_red,
        )

    def status(self, report: mutate.Report, unread: Container[int] = ()) -> int:
        """Every row that is not `caught` is recorded, except those in `unread`.

        Driving `sort_survivors` rather than building a `Survivors` by hand: the
        split is as much under test here as `_status` is, and the hand-rolled
        version of it went stale the moment the record widened -- it sorted only
        `survived`, so a `broke` row reached `_status` in neither list and the
        two assertions about it were really about a second arm that no longer
        exists.
        """
        accepted = {
            mutate._key(result.mutation): mutate.Accepted("read", 1)
            for n, result in enumerate(report.results)
            if not mutate.MEANING[result.verdict.outcome].clean and n not in unread
        }
        return mutate._status(report, mutate.sort_survivors(report.results, accepted))

    def test_a_sweep_whose_every_survivor_was_read_is_green(self) -> None:
        self.assertEqual(0, self.status(self.report("caught", "survived", "survived")))

    def test_one_survivor_nobody_read_is_red(self) -> None:
        self.assertEqual(1, self.status(self.report("caught", "survived", "survived"), {1}))

    def test_a_row_that_broke_and_nobody_read_is_red(self) -> None:
        """A question the run failed to put is not a question answered, and it
        is worth less than a survivor: a `broke` row is never `caught`, so the
        line it appeared to guard is guarded by nothing."""
        self.assertEqual(1, self.status(self.report("caught", "broke", "survived"), {1}))

    def test_a_row_that_broke_and_somebody_read_is_green(self) -> None:
        """The widening, and the assertion that would catch it being reverted.

        Three mutations in this tree cannot be answered at all -- two run the
        whole suite nested inside a memory-capped sandbox, one is a fork bomb --
        and a reason written for them was previously ignored, because `_status`
        tested `broke` a second time after the record had already excused it.
        A written reason that changes nothing is how a record stops being read.
        """
        self.assertEqual(0, self.status(self.report("caught", "broke", "survived")))

    def test_a_row_that_timed_out_and_nobody_read_is_red(self) -> None:
        self.assertEqual(1, self.status(self.report("caught", "timeout"), {1}))

    def test_a_red_baseline_is_red_even_with_nothing_else_wrong(self) -> None:
        """Its verdicts are meaningless, so "no fresh survivors" says nothing."""
        self.assertEqual(1, self.status(self.report("caught", baseline_red=True)))

    def test_an_all_caught_sweep_is_green(self) -> None:
        """The precondition: without it every assertion above is satisfied by a
        function that always answers 1."""
        self.assertEqual(0, self.status(self.report("caught", "caught")))

    def test_a_stale_entry_alone_does_not_turn_the_run_red(self) -> None:
        """It is reported loudly and dropped by `--accept`, but it describes
        code that has *gone* -- there is nothing in this tree to fix, and a red
        run demanding one would be the job that cries wolf."""
        report = self.report("caught")
        stale = mutate.Survivors([], [], ["deadbeefdeadbeef"])
        self.assertEqual(0, mutate._status(report, stale))


class TestWhatIsTriedAheadOfARow(unittest.TestCase):
    """`Learned.ahead`: the move-to-front head a row runs before its own
    selection, and the three ways it can quietly stop paying for itself."""

    def row(self, tests: str = "tests.test_sync", first: str = "") -> Mutation:
        return Mutation("a row", "tupferl/sync.py", "a", "b", tests, first=first)

    def learned(self, *tests: str) -> mutate.Learned:
        made = mutate.Learned()
        for test in tests:
            made.saw(test)
        return made

    def test_with_nothing_remembered_there_is_no_head(self) -> None:
        """The first row of every run. `""` and not `None`: the caller splits
        it, and `None.split()` is an `AttributeError` before the sweep starts."""
        self.assertEqual("", mutate.Learned().ahead(self.row()))

    def test_a_remembered_test_the_row_can_reach_is_offered(self) -> None:
        self.assertEqual(
            "tests.test_sync.T.test_it",
            self.learned("tests.test_sync.T.test_it").ahead(self.row()),
        )

    def test_a_test_outside_the_rows_selection_is_not_offered(self) -> None:
        """A test in a module that does not import the mutated file cannot see
        the mutation, so running it first is pure cost -- paid by every row."""
        self.assertEqual("", self.learned("tests.test_merge.T.test_it").ahead(self.row()))

    def test_one_reachable_test_among_several_is_the_one_offered(self) -> None:
        """`any`, not `all`. With a single-module selection the two agree, so
        this needs a row whose selection names two modules and a head holding a
        test from one of them -- under `all` nothing is ever offered and the
        cache silently stops working."""
        head = self.learned("tests.test_merge.T.test_it")
        row = self.row(tests="tests.test_sync tests.test_merge")
        self.assertEqual("tests.test_merge.T.test_it", head.ahead(row))

    def test_a_row_that_already_names_a_test_is_not_given_it_twice(self) -> None:
        """`first` is run in order, and naming a test twice buys nothing and
        costs a run."""
        head = self.learned("tests.test_sync.T.test_it")
        self.assertEqual("", head.ahead(self.row(first="tests.test_sync.T.test_it")))

    def test_a_row_with_no_selection_reaches_everything(self) -> None:
        """`WHOLE_SUITE` is the empty selection, and it means "run the lot" --
        so nothing in the head is out of reach."""
        head = self.learned("tests.test_merge.T.test_it")
        self.assertEqual(
            "tests.test_merge.T.test_it", head.ahead(self.row(tests=mutate.WHOLE_SUITE))
        )


class TestTheEdgesOfSizingALane(unittest.TestCase):
    """`_share` at its boundaries, which the ordinary cases cannot reach.

    Every other test of it asks for several lanes and a real cap. These ask what
    happens at one lane, at no cap, and when the budget divides to less than the
    floor -- the three places its `max`es and `min`s are doing something rather
    than agreeing.
    """

    def test_no_cap_still_yields_at_least_one_lane(self) -> None:
        """`--memory 0` passes straight through: there is no product to bound
        once a factor is infinite, and imposing one quietly would be the flag
        lying. The `max(1, ...)` is what stops `--workers 0` meaning no lanes at
        all, which would hang rather than fail."""
        self.assertEqual(1, mutate._share(0, 0, pinned=False).lanes)
        self.assertEqual(1, mutate._share(1, 0, pinned=False).lanes)
        self.assertEqual(0, mutate._share(4, 0, pinned=False).memory)

    def test_a_ceiling_never_falls_below_the_floor(self) -> None:
        """`max(floor, budget // lanes)`. Read as `min`, a pinned run that asks
        for more lanes than the budget divides into gives each of them a share
        far under the floor -- and every one is killed for holding what a lane
        normally holds, which reads as the mutation crashing.
        """
        with mock.patch.object(mutate, "_budget", return_value=mutate._FLOOR):
            share = mutate._share(8, mutate.MEMORY, pinned=True)
        self.assertEqual(8, share.lanes, "the pin was not honoured")
        self.assertGreaterEqual(share.memory, mutate._FLOOR)

    def test_an_explicit_cap_under_the_floor_is_the_callers_call(self) -> None:
        """They may be reproducing a small machine on purpose, so the cap is
        obeyed rather than corrected -- and it still decides how many fit."""
        asked = mutate._FLOOR // 4
        share = mutate._share(8, asked, pinned=False)
        self.assertEqual(asked, share.memory)


class TestTheSmallDecisionsNothingAsked(unittest.TestCase):
    """Five one-line judgements, each reached by every run and asserted by none.

    They are here together because they have nothing in common except that: a
    mixed report's verdict, what identifies a mutation across runs, which of two
    ways a row is applied, and the sweep of stale bytecode. Each was found by
    generating every mutation its function admits rather than by reading.
    """

    def report(self, *outcomes: mutate.Outcome) -> mutate.Report:
        return mutate.Report(
            [
                mutate.Result(
                    Mutation(f"row {n}", "tupferl/sync.py", "a", "b", "t"),
                    mutate.Verdict(outcome, ""),
                )
                for n, outcome in enumerate(outcomes)
            ]
        )

    def test_one_survivor_among_many_caught_is_not_a_clean_report(self) -> None:
        """`all`, never `any`. Every existing test of `clean` used a report
        whose rows agreed, and on those the two are the same function -- so a
        sweep with one survivor in a hundred reported itself complete."""
        self.assertTrue(self.report("caught", "caught").clean)
        self.assertFalse(self.report("caught", "survived").clean)
        self.assertFalse(self.report("survived", "caught").clean)

    def test_a_key_is_short_enough_to_read_in_a_diff(self) -> None:
        """The record and the killers cache are both keyed by this and both are
        reviewed by a person. A full sha256 is four times the width and makes a
        row wrap, which is the whole reason for the slice."""
        key = mutate._key(Mutation("a", "tupferl/sync.py", "x", "y", "t"))
        self.assertEqual(16, len(key))

    def test_a_key_ignores_where_the_line_is_and_notices_what_it_says(self) -> None:
        """The property the disposition record rests on: a row keeps its key
        when the code above it moves, and gets a new one when the edit itself
        changes."""
        base = Mutation("a", "tupferl/sync.py", "x", "y", "t", operator="branch")
        self.assertEqual(
            mutate._key(base),
            mutate._key(base._replace(label="a different label", tests="other", span=(9, 9))),
        )
        for changed in (
            base._replace(path="tupferl/merge.py"),
            base._replace(old="z"),
            base._replace(new="z"),
            base._replace(operator="arith"),
        ):
            self.assertNotEqual(mutate._key(base), mutate._key(changed), changed)

    def test_a_row_with_a_span_edits_only_there(self) -> None:
        """The two ways a row is applied, and the reason there are two: a
        generated `old` is usually *not* unique -- `if not path.exists():`
        appears many times -- so it carries the offsets it applies at. Applied
        by `replace` instead, one row would edit every occurrence in the file.
        """
        text = "if a:\n    pass\nif a:\n    pass\n"
        # Computed, never written out: a hand-counted offset that drifts by one
        # produces a plausible-looking file and a test that asserts it.
        second = text.index("if a:", 1)
        row = Mutation(
            "second only", "x.py", "if a:", "if True:", "t", span=(second, second + len("if a:"))
        )
        self.assertEqual("if a:\n    pass\nif True:\n    pass\n", mutate._applied(text, row))

    def test_a_row_without_a_span_replaces_its_text(self) -> None:
        """The hand-written shape, whose `old` `check` has already refused
        unless it appears exactly once."""
        row = Mutation("the only one", "x.py", "if a:", "if True:", "t")
        self.assertEqual("if True:\n    pass\n", mutate._applied("if a:\n    pass\n", row))

    def test_stale_bytecode_is_swept_out_of_a_sandbox(self) -> None:
        """A `__pycache__` left by a previous mutation's run is read by the next
        one that borrows the same sandbox -- the `(mtime, size)` collision this
        module's docstring exists to avoid. `ignore_errors` covers a directory
        that vanished under a concurrent lane, so nothing here may raise."""
        box = Path(tempfile.mkdtemp(prefix="tupferl-bytecode-"))
        self.addCleanup(shutil.rmtree, box, True)
        (box / "pkg").mkdir()
        for cache in (box / "__pycache__", box / "pkg" / "__pycache__"):
            cache.mkdir()
            (cache / "stale.pyc").write_bytes(b"\x00")
        (box / "pkg" / "keep.py").write_text("x = 1\n", encoding="utf-8")

        mutate._clear_bytecode(box)

        self.assertEqual([], list(box.rglob("__pycache__")), "stale bytecode was left behind")
        self.assertTrue((box / "pkg" / "keep.py").is_file(), "it took the source with it")


class TestReadingBackAReportToResumeFrom(unittest.TestCase):
    """`_recorded`: the rows a resumed sweep must not re-run or lose.

    Everything about it is a *reconstruction* -- a `Mutation` and a `Verdict`
    rebuilt from JSON -- and the rebuilt rows go on to `_summarise` and the exit
    status. A field dropped here is a survivor that goes unmentioned in a run
    that exits 0; a span rebuilt wrong is a row re-applied at the wrong offset.

    Reached only through a real resumed batch before this, which is why six of
    its mutations survived: that path asserts the *count* of rows carried over,
    and every one of them is wrong in the same way.
    """

    def saved(self, payload: object) -> Path:
        box = Path(tempfile.mkdtemp(prefix="tupferl-resume-"))
        self.addCleanup(shutil.rmtree, box, True)
        where = box / "report.json"
        where.write_text(json.dumps(payload), encoding="utf-8")
        return where

    ROW: typing.ClassVar[dict[str, Any]] = {
        "label": "a branch",
        "path": "tupferl/sync.py",
        "old": "if a:",
        "new": "if True:",
        "tests": "tests.test_sync",
        "span": [40, 45],
        "operator": "branch",
        "outcome": "survived",
        "detail": "nothing noticed",
        "killer": "",
    }

    def test_every_field_comes_back_the_way_it_went_in(self) -> None:
        """Asserted field by field rather than by count. The resume path counts
        rows, and a row rebuilt with the wrong span, the wrong operator or the
        wrong outcome is still one row."""
        (found,) = mutate._recorded(self.saved({"results": [self.ROW]}))
        self.assertEqual("a branch", found.mutation.label)
        self.assertEqual("tupferl/sync.py", found.mutation.path)
        self.assertEqual("if a:", found.mutation.old)
        self.assertEqual("if True:", found.mutation.new)
        self.assertEqual("tests.test_sync", found.mutation.tests)
        self.assertEqual("branch", found.mutation.operator)
        self.assertEqual("survived", found.verdict.outcome)
        self.assertEqual("nothing noticed", found.verdict.detail)

    def test_the_span_comes_back_as_the_pair_it_was(self) -> None:
        """Both ends, and in order. `_applied` splices `new` at exactly these
        offsets, so a pair rebuilt as `(start, start)` or reversed edits the
        wrong bytes of the file -- and the row still looks like the row it
        claims to be."""
        (found,) = mutate._recorded(self.saved({"results": [self.ROW]}))
        self.assertEqual((40, 45), found.mutation.span)

    def test_a_row_with_no_span_keeps_none(self) -> None:
        """A hand-written row carries no span and is applied by `replace`
        instead. Rebuilt as `(0, 0)` it would splice at the top of the file."""
        row = {**self.ROW}
        del row["span"]
        (found,) = mutate._recorded(self.saved({"results": [row]}))
        self.assertIsNone(found.mutation.span)

    def test_no_file_and_no_path_are_both_nothing_to_resume_from(self) -> None:
        """`None` is "resume was not asked for"; a missing file is "the run that
        would have written one never got there". Both mean the same thing to the
        caller, and neither may raise -- this runs before the sweep starts."""
        self.assertEqual([], mutate._recorded(None))
        self.assertEqual([], mutate._recorded(Path("/nonexistent/report.json")))

    def test_a_half_written_report_resumes_as_nothing(self) -> None:
        """Re-running everything is the safe reading of a crash mid-write. The
        dangerous one is a partial list read as complete, which drops whatever
        the crash cut off -- silently, and in the direction that flatters."""
        box = Path(tempfile.mkdtemp(prefix="tupferl-resume-"))
        self.addCleanup(shutil.rmtree, box, True)
        broken = box / "report.json"
        broken.write_text('{"results": [{"label": "cut off"', encoding="utf-8")
        self.assertEqual([], mutate._recorded(broken))

    def test_a_row_missing_a_field_takes_the_whole_file_with_it(self) -> None:
        """`KeyError` is caught, so a report from an older shape resumes as
        nothing rather than as a partial list nobody can tell is partial."""
        self.assertEqual([], mutate._recorded(self.saved({"results": [{"label": "only this"}]})))


class TestWhatTheKillersCacheWritesDown(unittest.TestCase):
    """`Killers.save`. The cache is read back by `Killers.__init__`, and every
    test of it went through a round trip -- which passes against a `save` that
    wrote nothing at all if the same process still holds the dict."""

    def cache(self, where: Path | None) -> mutate.Killers:
        made = mutate.Killers(where)
        made.learn(
            mutate.Report(
                [
                    mutate.Result(
                        Mutation("a row", "tupferl/sync.py", "a", "b", "tests.test_sync"),
                        mutate.Verdict("caught", "", "tests.test_sync.T.test_it"),
                    )
                ]
            )
        )
        return made

    def test_it_writes_a_file_a_later_run_can_read(self) -> None:
        box = Path(tempfile.mkdtemp(prefix="tupferl-killers-"))
        self.addCleanup(shutil.rmtree, box, True)
        # A directory that does not exist yet: `sweeps/` is gitignored, so the
        # first run on a fresh clone is always this case.
        where = box / "sweeps" / "killers.json"
        self.cache(where).save()
        self.assertTrue(where.is_file(), "nothing was written")
        written = json.loads(where.read_text(encoding="utf-8"))
        self.assertEqual(["tests.test_sync.T.test_it"], list(written["killers"].values()))

    def test_saving_a_second_time_over_an_existing_directory_is_fine(self) -> None:
        """`exist_ok=True`. Every run after the first is this case, and without
        it the second sweep on a machine dies at the end having done all the
        work."""
        box = Path(tempfile.mkdtemp(prefix="tupferl-killers-"))
        self.addCleanup(shutil.rmtree, box, True)
        where = box / "sweeps" / "killers.json"
        self.cache(where).save()
        self.cache(where).save()
        self.assertTrue(where.is_file())

    def test_no_path_writes_nothing_and_does_not_raise(self) -> None:
        """`--no-killers` and every spec-file run. A `save` that tried anyway
        would take down a sweep that had already finished its work."""
        self.cache(None).save()


class TestWhatAcceptWritesDown(unittest.TestCase):
    """`_accept` and `_report_known`, which had ten hand-written mutants between
    them and passed all of them.

    The hand table asked what `sort_survivors` *answers*. Nothing asked what
    `--accept` **writes** or what a run **says**, and a generated sweep of the
    same change found twenty-six mutations no test could see -- the file never
    being written at all among them. That is the argument for the generated
    sweep in one paragraph: a table written by the person who wrote the code
    tests the part they were thinking about.
    """

    def setUp(self) -> None:
        self.box = Path(tempfile.mkdtemp(prefix="tupferl-accept-"))
        self.addCleanup(shutil.rmtree, self.box, True)
        self.where = self.box / "known-survivors.json"
        patch = mock.patch.object(mutate, "KNOWN", self.where)
        patch.start()
        self.addCleanup(patch.stop)

    def survivor(self, label: str, old: str = "a", new: str = "b") -> mutate.Result:
        return mutate.Result(
            Mutation(label, "tupferl/sync.py", old, new, "tests.test_sync", operator="branch"),
            mutate.Verdict("survived", ""),
        )

    def accept(self, results: list[mutate.Result], accepted: dict[str, mutate.Accepted]) -> str:
        sorted_out = mutate.sort_survivors(results, accepted)
        with support.quiet() as said:
            mutate._accept(sorted_out, accepted)
        return said.getvalue()

    def written(self) -> dict[str, dict[str, object]]:
        return dict(json.loads(self.where.read_text(encoding="utf-8")))

    def test_a_new_survivor_is_written_with_a_todo_and_a_count_of_one(self) -> None:
        """The file is the whole product of the flag, and nothing read it: the
        `write_text` call could be deleted outright and every other test here
        still passed."""
        row = self.survivor("a branch nobody checks")
        self.accept([row], {})
        found = self.written()
        self.assertEqual([mutate._key(row.mutation)], list(found))
        self.assertEqual(1, found[mutate._key(row.mutation)]["seen"])
        why = found[mutate._key(row.mutation)]["why"]
        assert isinstance(why, str)
        self.assertTrue(why.startswith("TODO"), why)
        self.assertIn("a branch nobody checks", why, "the row does not say which mutation it is")

    def test_a_second_row_of_a_known_shape_raises_the_count_and_keeps_the_reason(self) -> None:
        """The 125-row case, on the writing side. The count rises by exactly one
        and the reason is *not* rewritten: what changed is how many there are,
        not what anybody decided about them."""
        row = self.survivor("the same edit twice")
        key = mutate._key(row.mutation)
        self.accept([row, row], {key: mutate.Accepted("read once", 1)})
        self.assertEqual({key: {"why": "read once", "seen": 2}}, self.written())

    def test_a_stale_row_is_dropped_rather_than_carried(self) -> None:
        """It describes code that has gone. Kept, it goes on matching nothing
        for ever, and the row it used to cover may have been replaced by one
        nobody has read."""
        row = self.survivor("still here")
        self.accept([row], {"deadbeefdeadbeef": mutate.Accepted("long gone", 1)})
        self.assertNotIn("deadbeefdeadbeef", self.written())

    def test_the_file_is_one_row_per_line_and_reads_back(self) -> None:
        """`indent` and `sort_keys` are what make this reviewable: a row added
        in a pull request has to show up as a line somebody can read, and two
        runs of the same tree have to produce the same bytes."""
        rows = [self.survivor(f"row {n}", old=f"o{n}", new=f"n{n}") for n in range(3)]
        self.accept(rows, {})
        text = self.where.read_text(encoding="utf-8")
        self.assertEqual(sorted(self.written()), list(self.written()), "the keys are not sorted")
        self.assertTrue(text.endswith("\n"), "no trailing newline; every append would conflict")
        self.assertGreater(len(text.splitlines()), len(rows), "the file is not laid out in lines")
        self.assertEqual(3, len(mutate.known_survivors(self.where)), "it does not read back")

    def test_it_says_how_many_it_recorded_and_how_many_it_dropped(self) -> None:
        """Both numbers, and both spellings of the size. 557 survivors shared
        432 keys on the first whole-tree sweep, so a line naming one of those
        reads as the other."""
        said = self.accept([self.survivor("new")], {"deadbeefdeadbeef": mutate.Accepted("gone", 1)})
        self.assertIn("recorded 1 new", said)
        self.assertIn("dropped 1 stale", said)
        self.assertIn("key(s)", said)
        self.assertIn("survivor(s)", said)

    def counts(self, accepted: int, stale: int) -> str:
        rows = [self.survivor(f"row {n}", old=f"o{n}", new=f"n{n}") for n in range(accepted)]
        record = {mutate._key(r.mutation): mutate.Accepted("read", 1) for r in rows}
        record.update({f"{n:016x}": mutate.Accepted("gone", 1) for n in range(stale)})
        with support.quiet() as said:
            mutate._report_known(mutate.sort_survivors(rows, record))
        return said.getvalue()

    def test_a_baseline_that_exists_is_counted_out_loud(self) -> None:
        """A baseline whose size is invisible is one nobody re-reads, and the
        number going up unnoticed is how a record stops meaning "understood"."""
        self.assertIn("2 survivor(s) already recorded", self.counts(accepted=2, stale=0))

    def test_a_run_with_no_baseline_says_nothing_about_one(self) -> None:
        """Every hand-written spec file is this case. A line on each of those
        runs is noise that trains the eye past the line that matters."""
        self.assertEqual("", self.counts(accepted=0, stale=0))

    def test_a_stale_entry_is_reported(self) -> None:
        self.assertIn("match nothing this run generated", self.counts(accepted=1, stale=2))

    def test_nothing_stale_is_not_reported(self) -> None:
        """The other half: a `_report_known` that always warned would pass the
        test above and mean nothing."""
        self.assertNotIn("match nothing", self.counts(accepted=1, stale=0))

    def test_stale_keys_come_back_in_a_settled_order(self) -> None:
        """**Eight keys, not two, and the count is the test.**

        The stale list is `sorted(set(accepted) - reached)`, and a *set* iterates
        in hash order -- which Python randomises per run. With two keys an
        unsorted list matches the sorted one about half the time, so the guard
        would hold or not depending on `PYTHONHASHSEED`: not a flaky test, but a
        guard that only sometimes guards. Measured: with two it survived the
        sweep; with eight the chance of the hash order being sorted is 1 in
        40320.

        Inserted in reverse, so insertion order is not sorted order either.
        """
        keys = [f"{n:016x}" for n in range(8)]
        found = mutate.sort_survivors(
            [], {key: mutate.Accepted("gone", 1) for key in reversed(keys)}
        )
        self.assertEqual(keys, found.stale)

    def test_a_row_read_once_is_a_row_read(self) -> None:
        """`seen > 0`, not `> 1`. Every row `--accept` writes starts at 1, so a
        bound that rejected it would drop the whole file on the next run and
        report all 557 as new."""
        self.where.write_text(json.dumps({"abc": {"why": "read", "seen": 1}}), encoding="utf-8")
        self.assertEqual({"abc": mutate.Accepted("read", 1)}, mutate.known_survivors(self.where))


class TestWhatBaselineOnlyAnswers(unittest.TestCase):
    """`_baseline_is_green`: `--baseline-only`, which exists to ask in one
    shard's time the question a sweep will ask in an hour.

    **All fourteen of its mutants survived, and nothing called it.** Two full
    sweeps were paid for here to learn what it prints, and `is not` becoming
    `is` at :2554 inverts its answer -- a red tree reported green, which sends
    someone into a sweep whose every verdict is already void.

    `_borrow` is stubbed. What this function *does* is fan shards out, read
    every verdict, and reduce them to one bool; driving real sandboxes would
    make the answer a property of this machine's suite, which is the reason
    nothing tested it. The shards it asks for come from `baseline_shards`, which
    is tested beside this.
    """

    ARGS = argparse.Namespace(workers=2, memory=mutate.MEMORY, timeout=30.0, each_test=0.0)

    def asked(self, *verdicts: mutate.Verdict) -> tuple[bool, str, list[list[str]]]:
        """Drive it with one canned verdict per shard, and record what each
        lane was asked to run."""
        seen: list[list[str]] = []
        answers = list(verdicts)

        def borrow(_available: Any, shard: list[str], *rest: Any) -> mutate.Verdict:
            seen.append(shard)
            return answers[len(seen) - 1]

        table = [
            Mutation(f"row {n}", "tupferl/sync.py", "a", "b", f"tests.shard{n}")
            for n in range(len(verdicts))
        ]
        with mock.patch.object(mutate, "_borrow", borrow), support.quiet() as spill:
            green = mutate._baseline_is_green(table, self.ARGS)
        return green, spill.getvalue(), seen

    def test_every_shard_green_is_green(self) -> None:
        """`survived` is the untouched suite passing, which is the one place
        the mutation vocabulary reads backwards."""
        green, said, _ = self.asked(mutate.Verdict("survived", ""), mutate.Verdict("survived", ""))
        self.assertTrue(green)
        self.assertIn("all green", said)

    def test_one_red_shard_is_enough(self) -> None:
        """It is an `and` over the shards. A tree where any shard fails is a
        tree where no verdict above it means anything."""
        green, said, _ = self.asked(
            mutate.Verdict("survived", ""), mutate.Verdict("caught", "boom")
        )
        self.assertFalse(green)
        self.assertIn("NOT green", said)

    def test_a_shard_that_asked_nothing_is_red_too(self) -> None:
        """`broke` and `timeout` are not passes. The old wording asserted a
        failure that may not have happened; what matters is that neither is
        evidence the tree is sound."""
        for outcome in ("broke", "timeout"):
            with self.subTest(outcome=outcome):
                green, _, _ = self.asked(mutate.Verdict(outcome, "d"))
                self.assertFalse(green, f"{outcome} was read as a pass")

    def test_it_asks_about_exactly_the_shards_the_sweep_will(self) -> None:
        """`baseline_shards` rather than a second spelling of it -- the flag is
        worth nothing if it asks a different question from the run it predicts,
        and it went stale that way once already."""
        table = [
            Mutation(f"row {n}", "tupferl/sync.py", "a", "b", f"tests.shard{n}") for n in range(2)
        ]
        _, _, seen = self.asked(mutate.Verdict("survived", ""), mutate.Verdict("survived", ""))
        self.assertEqual([s.split() for s in mutate.baseline_shards(table)], seen)

    def test_a_red_shard_says_which_and_why(self) -> None:
        """A red baseline is the one verdict that cannot be diagnosed by
        re-running the row: the shard is rarely reproducible by hand, so the
        reason has to come out with it."""
        _, said, _ = self.asked(mutate.Verdict("caught", "tests.shard0.T.test_x failed"))
        self.assertIn("tests.shard0", said)
        self.assertIn("test_x failed", said)


class TestWhatVerifyReturns(unittest.TestCase):
    """`verify`: the number a spec file's exit status is made of.

    **All seven of its mutants survived, and nothing in the suite calls it.**
    Every spec file in the repository ends `raise SystemExit(verify(...))`, so
    this is the function that decides whether a hand-written table passes --
    and `1` becoming `0` at :1819 means a table full of survivors exits green.
    That is woswoar#213's own symptom, the bug `_RUNS` was introduced to fix,
    reachable again through the function that reports it.

    `run` is stubbed rather than driven: what `verify` does is *count*, and a
    real sweep would make the count a property of the machine.
    """

    def counted(self, report: mutate.Report) -> int:
        with mock.patch.object(mutate, "run", lambda *a, **k: report):
            return mutate.verify([row()])

    def outcomes(self, *kinds: str, red: bool = False) -> mutate.Report:
        results = [
            mutate.Result(row(), mutate.Verdict(kind, "d"))  # type: ignore[arg-type]
            for kind in kinds
        ]
        return mutate.Report(results, red)

    def test_a_clean_table_counts_nothing(self) -> None:
        self.assertEqual(0, self.counted(self.outcomes("caught", "caught")))

    def test_every_survivor_is_counted(self) -> None:
        """Not "was there one". A spec file's author reads this number to know
        how much is wrong, and `1` for three survivors is a wrong answer that
        still exits red -- which is why an exit status alone cannot check it."""
        self.assertEqual(
            3, self.counted(self.outcomes("survived", "caught", "survived", "survived"))
        )

    def test_a_row_that_asked_nothing_is_not_a_survivor(self) -> None:
        """`broke` and `timeout` asked nothing. Counting them as survivors
        would send an author to rewrite a test that was never weak; not
        counting them at all is what the whole-table count below is for."""
        self.assertEqual(0, self.counted(self.outcomes("broke", "timeout", "caught")))

    def test_a_red_baseline_condemns_the_whole_table(self) -> None:
        """Every verdict above a red baseline is meaningless, so the count is
        the table's size rather than its survivors -- and a `0` there would let
        a spec file pass on a tree where nothing was proven at all."""
        self.assertEqual(3, self.counted(self.outcomes("caught", "caught", "caught", red=True)))

    def test_a_red_baseline_beats_a_clean_looking_table(self) -> None:
        """The precondition for the test above: with every row caught, the
        survivor count is 0, so only the baseline check can produce a non-zero
        answer. Without this pairing, "returns 3" is satisfied by a function
        that ignores the baseline and counts something else."""
        self.assertEqual(0, self.counted(self.outcomes("caught", "caught", "caught")))


class TestWhatMemoryTheMachineWillAdmitTo(unittest.TestCase):
    """`_visible_memory`: the smallest of everything that bounds this process.

    Fourteen of its fifteen mutants survived, all on lines the suite executes.
    Its own docstring names the failure: "in a 2 GiB container on a 62 GiB host
    it answers 62 and the container is OOM-killed with every per-lane cap
    respected." Dropping the `limits.append` restores that bug exactly, and
    nothing noticed.

    Each limit is supplied by a file this test writes or a variable it sets, so
    the answer is about the arithmetic rather than about this machine.
    """

    def limits(self, cgroup: int | None = None, /, **environment: str) -> int:
        """The answer on a machine whose only limits are the ones given here.

        Positional-only for the reason `TestWhoOwnsTheMachine.budget` is:
        `**environment` carries variable names, and a keyword parameter beside
        it is one renamed constant away from a caller setting this instead.
        """
        box = Path(tempfile.mkdtemp(prefix="tupferl-limits-"))
        self.addCleanup(shutil.rmtree, box, True)
        where = box / "memory.max"
        if cgroup is not None:
            where.write_text(f"{cgroup}\n", encoding="utf-8")
        # Pointed at a file this test wrote, so the cgroup arm is reachable at
        # all -- the paths were two literals inside two functions until now, and
        # nothing could put a limit where either would look.
        seen = mock.patch.object(mutate, "CGROUPS", (str(where),))
        with seen, mock.patch.dict(os.environ, environment, clear=True):
            return mutate._visible_memory()

    def test_a_cgroup_that_says_max_is_not_a_number(self) -> None:
        """cgroup v2 writes the literal `max` for "no limit". Read as a number
        it raises `ValueError` out of `_visible_memory`, which runs before every
        sweep -- so the tool would refuse to start on any machine whose cgroup
        is unlimited, which is most of them. Nothing wrote that word before.
        """
        box = Path(tempfile.mkdtemp(prefix="tupferl-limits-"))
        self.addCleanup(shutil.rmtree, box, True)
        where = box / "memory.max"
        where.write_text("max\n", encoding="utf-8")
        with (
            mock.patch.object(mutate, "CGROUPS", (str(where),)),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertGreater(mutate._visible_memory(), 0)

    def test_the_host_total_is_one_of_the_limits(self) -> None:
        """With no cgroup and no inherited budget, what this machine physically
        has is the only bound left -- and it has to be *in* the list. Dropping
        the append restores the bug this function was written for: a 2 GiB
        container on a 62 GiB host answering 62.

        Asserted against `sysconf` rather than a constant, because the number is
        this machine's and the claim is that it reached the answer.
        """
        physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        self.assertEqual(physical, self.limits())

    def test_a_budget_named_in_the_environment_binds(self) -> None:
        """`TUPFERL_MUTATE_BUDGET` is what a nested harness inherits, and it is
        the one limit a test can set without a kernel."""
        asked = 3 << 30
        self.assertLessEqual(self.limits(**{mutate._BUDGET: str(asked)}), asked)

    def test_the_smallest_limit_wins(self) -> None:
        """`min`, not the first found. A host with plenty of RAM and a small
        inherited budget must answer the budget -- that is the whole point."""
        small, large = 1 << 30, 900 << 30
        self.assertLessEqual(self.limits(**{mutate._BUDGET: str(small)}), small)
        self.assertGreater(self.limits(**{mutate._BUDGET: str(large)}), small)

    def test_nonsense_in_the_variable_is_ignored_rather_than_obeyed(self) -> None:
        """A limit of zero or a word is not a limit. Obeying it would hand every
        lane a ceiling of nothing, and the run would fail for a reason no output
        explains."""
        host = self.limits()
        for said in ("", "0", "-1", "lots"):
            with self.subTest(said=said):
                self.assertEqual(host, self.limits(**{mutate._BUDGET: said}))

    def test_a_cgroup_ceiling_binds_below_the_host(self) -> None:
        """The bug the function was written for: "in a 2 GiB container on a 62
        GiB host it answers 62 and the container is OOM-killed with every
        per-lane cap respected." A limit the kernel has carved out has to win."""
        self.assertEqual(2 << 30, self.limits(2 << 30))

    def test_a_cgroup_that_says_nothing_is_not_a_limit(self) -> None:
        """A missing file is the ordinary case on a machine with no cgroup, and
        reading it as zero would hand every lane a ceiling of nothing."""
        self.assertGreater(self.limits(None), 0)

    def test_it_never_answers_zero(self) -> None:
        """Zero divides into `_affordable` and `_share`. A machine that will say
        nothing at all still has to run something."""
        self.assertGreater(self.limits(), 0)


class TestWhichProcessesALaneAnswersFor(unittest.TestCase):
    """`_lane`: the process group, unioned with every descendant that left it.

    **Nine of its ten mutants survived**, including `and` becoming `or` and `in`
    becoming `not in` on the line that performs the union. The suite reaches
    every one of those lines -- `reached.py` classifies all nine as weak fixture
    rather than missing test -- so this is CLAUDE.md section 2's "suspect the
    fixture" with the evidence attached.

    The union is woswoar#234. A nested `_run` gives its probes sessions of their
    own, so they leave the group; one was found alive eleven minutes into a
    sweep whose per-row bound is 300 seconds, reparented to init and counted by
    nobody. Either half alone misses it, and a table of hand-built rows is the
    only way to put a process in exactly one of the two sets.
    """

    def table(self, *rows: tuple[int, int, int]) -> dict[int, mutate.Process]:
        """`(pid, parent, group)` triples. Memory is irrelevant to membership,
        so it is the same for every row -- a figure that varied would invite an
        assertion about the wrong thing."""
        return {
            pid: mutate.Process(parent=parent, group=group, resident=1 << 20, address=1 << 20)
            for pid, parent, group in rows
        }

    def test_the_group_alone_is_not_enough(self) -> None:
        """A grandchild that called `setsid` is in the leader's *descendants*
        and not in its group. Miss it and its memory is nobody's problem, which
        is exactly what woswoar#234 was."""
        escaped = self.table((10, 1, 10), (11, 10, 10), (12, 11, 12))
        self.assertEqual({10, 11, 12}, mutate._lane(10, escaped))

    def test_the_descendants_alone_are_not_enough(self) -> None:
        """The other half, and the fixture that makes the first one mean
        something. A process in the group whose parent is elsewhere -- a `git`
        the suite forked and then reparented -- is in the group and not in the
        tree."""
        adopted = self.table((10, 1, 10), (20, 1, 10))
        self.assertEqual({10, 20}, mutate._lane(10, adopted))

    def test_another_lane_is_not_swept_in(self) -> None:
        """The assertion that stops "return everything" from passing. Two lanes
        run side by side in every real sweep, and a membership rule that claims
        both would have `_end` killing a lane that was working."""
        two = self.table((10, 1, 10), (11, 10, 10), (30, 1, 30), (31, 30, 30))
        self.assertEqual({10, 11}, mutate._lane(10, two))
        self.assertEqual({30, 31}, mutate._lane(30, two))

    def test_a_pid_the_table_does_not_hold_is_not_invented(self) -> None:
        """The table is a snapshot and processes exit while it is being read.
        A member that is no longer there must not be returned, or `_end_lane`
        signals a pid the kernel has since handed to something else."""
        gone = self.table((10, 1, 10), (11, 10, 10))
        del gone[11]
        self.assertEqual({10}, mutate._lane(10, gone))

    def test_a_leader_that_is_gone_answers_for_nothing(self) -> None:
        """`release` may run after the lane has exited. An empty answer is
        right; a `KeyError` would take the sampler thread down with it."""
        self.assertEqual(set(), mutate._lane(99, self.table((10, 1, 10))))

    #: A grandchild that leaves the group, printed by the middle process so the
    #: test knows its pid without guessing. Bounded well under the harness's own
    #: 30s per-test alarm, and killed in `addCleanup` either way.
    ESCAPE = (
        "import os, subprocess, sys, time;"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],"
        " preexec_fn=os.setsid);"
        "print(g.pid, flush=True); time.sleep(20)"
    )

    def test_a_real_nested_probe_that_left_the_group_is_still_found(self) -> None:
        """The bug this class was written to test for, against real processes.

        The hand-built tables above are faithful -- pid, parent and group are
        everything `_lane` reads -- but this is the shape woswoar#234 describes
        and it is worth building for real: a probe (its own session), the suite
        it runs (inheriting that group), and a *nested* harness's probe that
        calls `setsid` and leaves it.

        Measured before the fix: `_lane` returned the first two and not the
        third. The walk tested membership in `found`, which starts holding the
        whole group -- so it stopped at every group member, and a group member
        is exactly what the middle process is. The descendant half only ever
        reached escapees whose parent was the leader itself, which is the one
        shape that needs it least.
        """
        middle = (
            f"import subprocess, sys, time;"
            f"m = subprocess.Popen([sys.executable, '-c', {self.ESCAPE!r}], stdout=sys.stdout);"
            f"time.sleep(20)"
        )
        leader = subprocess.Popen(
            [sys.executable, "-c", middle],
            start_new_session=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(leader.wait)
        self.addCleanup(leader.kill)
        assert leader.stdout is not None
        escapee = int(leader.stdout.readline().strip())
        self.addCleanup(self.reap, escapee)

        table = mutate._processes()
        self.assertNotEqual(
            leader.pid,
            table[escapee].group,
            "the fixture's grandchild never left the group, so it proves nothing",
        )
        self.assertIn(escapee, mutate._lane(leader.pid, table), "the escapee was not counted")

    def reap(self, escapee: int) -> None:
        """The grandchild is its own session leader, so killing the tree above
        it does not reach it -- which is the whole point of the fixture."""
        with contextlib.suppress(OSError):
            os.killpg(escapee, 9)

    def test_a_cycle_in_the_table_does_not_hang_the_sampler(self) -> None:
        """`/proc` is read row by row while the machine runs, so the snapshot
        need not be a consistent tree -- a pid reused between two reads can make
        one appear to be its own ancestor. The walk is `while stack`, and the
        sampler holds a lock."""
        looped = self.table((10, 1, 10), (11, 10, 10), (12, 11, 12))
        looped[10] = looped[10]._replace(parent=12)
        # Bounded, or this test cannot fail. Its subject is `seen`, the only
        # thing stopping the walk revisiting a pid for ever -- so a mutation that
        # removes it makes this *hang* rather than fail, and the harness files a
        # hang as `BROKE`, which is never `caught`. Both mutants of those two
        # lines came back that way on the whole-tree sweep: a test named
        # "does not hang the sampler" that hung, and read as a guard.
        with support.deadline(support.PATIENCE, "_lane never finished walking a cycle"):
            found = mutate._lane(10, looped)
        self.assertEqual({10, 11, 12}, found)


class TestWhatTheBaselineIsMeasuredAgainst(unittest.TestCase):
    """`baseline_shards`: one shard per distinct selection, plus one for the
    remembered killers.

    Six of its eight mutants survived, all on lines the suite executes. A red
    baseline voids every verdict above it, so a shard set that checks the wrong
    thing makes a whole sweep meaningless -- quietly, because the rows still
    print their verdicts.

    A function rather than a constant so `--baseline-only` and `run` cannot
    drift; it already went stale once. These assert the shape both callers
    depend on.
    """

    def rows(self, *pairs: tuple[str, str]) -> list[Mutation]:
        return [
            Mutation(f"row {n}", "tupferl/sync.py", "a", "b", tests, first=first)
            for n, (tests, first) in enumerate(pairs)
        ]

    def test_one_shard_per_distinct_selection(self) -> None:
        """Distinct, not one per row: a table of two hundred rows against three
        selections is three suite runs, not two hundred."""
        table = self.rows(("tests.a", ""), ("tests.b", ""), ("tests.a", ""))
        self.assertEqual(["tests.a", "tests.b"], mutate.baseline_shards(table))

    def test_the_remembered_killers_get_one_shard_between_them(self) -> None:
        """One for all of them, never one each. A shard per remembered test is
        the sharding explosion that took 372s to 730s, in a new disguise."""
        table = self.rows(("tests.a", "tests.x.X.test_one"), ("tests.a", "tests.y.Y.test_two"))
        shards = mutate.baseline_shards(table)
        self.assertEqual(2, len(shards), shards)
        self.assertIn("tests.x.X.test_one tests.y.Y.test_two", shards)

    def test_a_killer_outside_its_row_s_selection_is_still_covered(self) -> None:
        """Why the extra shard exists at all: a cached killer can name a test
        the row's own selection does not run, and an unchecked killer is the
        false `caught` the baseline exists to prevent."""
        table = self.rows(("tests.a", "tests.elsewhere.E.test_it"))
        self.assertIn("tests.elsewhere.E.test_it", " ".join(mutate.baseline_shards(table)))

    def test_no_killers_means_no_extra_shard(self) -> None:
        """The precondition. Without it, "the killers get a shard" is equally
        satisfied by a function that always appends one -- and an empty shard
        runs the whole suite, which is the one thing `baseline_shards`'
        docstring says it must not do."""
        self.assertEqual(["tests.a"], mutate.baseline_shards(self.rows(("tests.a", ""))))

    def test_an_empty_table_needs_nothing_checked(self) -> None:
        self.assertEqual([], mutate.baseline_shards([]))


class TestHowManyLanesFitAndHowBigEachMayBe(unittest.TestCase):
    """`_share` and `_affordable`: the product of lanes and ceiling, bounded.

    **These decide whether a sweep fits on the machine, and 22 of their 24
    mutants survived the whole-tree sweep.** `verdict.cap` bounds one lane and
    `_affordable` counts lanes, and for one release each was defensible while
    the pair was not: sixteen lanes at a 4 GiB ceiling is 64 GiB on a 62 GiB
    machine, so a table could drive the host into the OOM killer with every
    individual limit respected. That is woswoar#232, and it happened three times.

    **Asserted as properties, never against a second copy of the formula.**
    `_share`'s answer is `min(wanted, _affordable(), budget // floor)` paired
    with `min(memory, max(floor, budget // lanes))`; a test that recomputes that
    has copied the code and holds for any value of it. What follows are the
    claims a reader of `_share`'s docstring would make -- the product fits, the
    order of concessions is lanes-last, a pin is honoured -- each of which a
    wrong implementation can fail.

    Driven directly rather than through `run`, which is why they were untested:
    every existing test that reaches `_share` reaches it through a real sweep,
    where the lane count is whatever this machine allows, so the assertion ends
    up being about the machine and holds for any value of it.
    """

    def sharing(self, budget: int, wanted: int, memory: int, pinned: bool = False) -> mutate.Share:
        """`_share` on a machine with exactly `budget` to spend."""
        with mock.patch.object(mutate, "_budget", lambda: budget):
            return mutate._share(wanted, memory, pinned)

    def test_the_product_never_exceeds_what_may_be_committed(self) -> None:
        """woswoar#232 in one line, and the reason the two numbers are chosen
        together rather than separately. Swept across shapes, because a single
        pair is satisfied by an implementation that happens to fit it.

        The bound is `_COMMIT` times the budget rather than the budget: a
        ceiling is headroom for a pathological row and peaks do not coincide,
        which is the argument `_COMMIT` carries. It is still a *bound* -- the
        pair is chosen together, and that is what #232 was about.
        """
        allowed = mutate._COMMIT
        for budget in (2 << 30, 8 << 30, 64 << 30):
            for wanted in (1, 3, 7, 16):
                with self.subTest(budget=budget >> 20, wanted=wanted):
                    share = self.sharing(budget, wanted, mutate.MEMORY)
                    self.assertLessEqual(
                        share.lanes * share.memory,
                        int(budget * allowed),
                        f"{share.lanes} lanes x {share.memory >> 20} MiB exceeds "
                        f"{int(budget * allowed) >> 20} MiB",
                    )

    def test_the_commitment_is_really_more_than_the_machine_has(self) -> None:
        """Without this, the bound above passes just as well with `_COMMIT` at
        1.0 -- so the relaxation would be untested and a silent revert to the
        old rule would cost lanes with nothing going red.

        A 4 GiB machine is the shape that shows it: the lane count comes from
        `allowed // floor` there, and each lane still gets the floor.
        """
        share = self.sharing(4 << 30, 16, mutate.MEMORY)
        self.assertGreater(
            share.lanes * share.memory,
            4 << 30,
            "the ceilings fit inside the budget, so nothing is being committed",
        )
        self.assertLessEqual(share.lanes * share.memory, int((4 << 30) * mutate._COMMIT))

    def test_the_commitment_buys_lanes_rather_than_headroom(self) -> None:
        """Where the extra allowance is spent, which is the whole point of
        raising it and is not implied by the two assertions above.

        Applying `_COMMIT` only to the *ceiling* passes both of those: the same
        two lanes simply get a 3 GiB ceiling instead of 2 GiB, the product still
        exceeds the budget, and the run is no more parallel than before -- while
        nothing has ever been killed for reaching a ceiling, so the headroom
        buys nothing at all. Measured: that mutation survived every other test
        in this class.

        Stated against the rule being beaten -- what the uncommitted
        `budget // floor` would have allowed -- rather than against the current
        arithmetic, which would be a copy of the code.
        """
        share = self.sharing(4 << 30, 16, mutate.MEMORY)
        self.assertGreater(
            share.lanes,
            (4 << 30) // mutate._FLOOR,
            "the commitment went into ceilings nobody reaches instead of into lanes",
        )

    def test_more_memory_never_means_fewer_lanes(self) -> None:
        """Monotonicity. Nothing else here would catch a comparison flipped in
        the lane arithmetic, because any single budget still yields *a* number
        that looks plausible."""
        seen = [self.sharing(gib << 30, 16, mutate.MEMORY).lanes for gib in (2, 4, 8, 16, 64)]
        self.assertEqual(sorted(seen), seen, f"lanes fell as the budget grew: {seen}")

    def test_lanes_are_given_up_only_after_the_ceiling_has_been(self) -> None:
        """The order of concessions, which is `_share`'s whole argument: lower
        the ceiling first, because it is headroom for a pathological row rather
        than something an honest one spends, and give up lanes only when that
        share would fall under `_FLOOR`."""
        roomy = self.sharing(64 << 30, 8, mutate.MEMORY)
        self.assertEqual(8, roomy.lanes, "a big machine gave up lanes it did not need to")
        tight = self.sharing(4 << 30, 8, mutate.MEMORY)
        self.assertLess(tight.lanes, 8, "a small machine kept lanes it cannot afford")
        self.assertGreaterEqual(tight.memory, mutate._FLOOR, "the ceiling went under the floor")

    def test_a_pinned_worker_count_is_kept(self) -> None:
        """`--workers` is a caller with a reason this cannot see --
        `TestItRunsThemInParallel` pins four to assert that mutations overlap at
        all, and on a machine too small to afford four it would otherwise assert
        the machine rather than the mechanism."""
        self.assertEqual(9, self.sharing(2 << 30, 9, mutate.MEMORY, pinned=True).lanes)

    def test_the_ceiling_still_shrinks_around_a_pin(self) -> None:
        """The half of pinning that is worth having: the *count* is the caller's
        to fix, the ceiling is not."""
        share = self.sharing(4 << 30, 16, mutate.MEMORY, pinned=True)
        self.assertLess(share.memory, mutate.MEMORY, "a pinned run kept a ceiling it cannot fund")

    def test_no_cap_passes_straight_through(self) -> None:
        """`--memory 0` is "no cap", spelled the way `--limit 0` beside it
        already means. There is no product to bound once one factor is infinite,
        and quietly imposing one would be the flag lying."""
        share = self.sharing(1 << 30, 12, 0)
        self.assertEqual(12, share.lanes)
        self.assertEqual(0, share.memory)

    def test_there_is_always_at_least_one_lane(self) -> None:
        """A budget under one lane's floor still has to run. Zero lanes is a
        pool that never starts and a sweep that reports nothing."""
        self.assertGreaterEqual(self.sharing(1 << 20, 4, mutate.MEMORY).lanes, 1)
        self.assertGreaterEqual(self.sharing(1 << 20, 0, mutate.MEMORY).lanes, 1)

    def test_the_ceiling_never_exceeds_what_was_asked_for(self) -> None:
        """`--memory` is an upper bound the caller set. A roomy machine may not
        raise it -- a caller who already sandboxed us meant it."""
        share = self.sharing(64 << 30, 2, 512 << 20)
        self.assertLessEqual(share.memory, 512 << 20)

    def test_affordable_divides_the_budget_by_what_a_lane_uses(self) -> None:
        """`_LANE`, not `MEMORY`. Dividing by the *ceiling* assumes every lane is
        simultaneously pathological -- the over-restriction woswoar#227 removed,
        where a 16 GiB laptop dropped to two lanes and a 7 GiB runner to one."""
        with mock.patch.object(mutate, "_budget", lambda: 16 << 30):
            self.assertEqual((16 << 30) // mutate._LANE, mutate._affordable())

    def test_affordable_never_answers_zero(self) -> None:
        """A machine too small for one lane still gets one; the ceiling is what
        stops it, within seconds, rather than a pool that never starts."""
        with mock.patch.object(mutate, "_budget", lambda: 1 << 20):
            self.assertEqual(1, mutate._affordable())


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

    def test_nothing_but_the_cores_and_the_table_caps_what_is_asked_for(self) -> None:
        """A hardcoded `_LANES = 16` used to sit in this expression, with no
        measurement behind "the most lanes worth running, whatever the machine
        reports". On a 32-core machine it was the only binding term, and lifting
        it was worth 30% -- 214s against 303s over 1309 rows, two interleaved
        pairs.

        Asserted on what `run` *asks* `_share` for, not on what it gets: the
        cap was in the asking, and a machine that then declines on memory is a
        different and legitimate answer.
        """
        asked: list[int] = []

        def watch(wanted: int, memory: int, pinned: bool = False) -> mutate.Share:
            asked.append(wanted)
            return mutate.Share(1, memory)

        table = [self.ROW._replace(new=f"PROBE = {n}") for n in range(2, 60)]
        with (
            mock.patch.object(mutate, "_share", watch),
            mock.patch.object(mutate, "usable_cpus", lambda: 20),
            mock.patch.object(
                mutate, "_attempt", lambda *a, **k: mutate.Verdict("caught", "probe")
            ),
            support.quiet(),
        ):
            mutate.run(table, baseline=False, summarise=False)
        # 20 cores x 2 against a 58-row table: the cores are the smaller, so
        # that is what must come through. A constant of 16 would clip it.
        self.assertEqual([40], asked)


class TestWhatOrderTheFirstTestsRunIn(unittest.TestCase):
    """An exact killer goes ahead of the learned front; a general prefix behind.

    Two "run these first" mechanisms meet in `_attempt`, and until this was
    measured they met in the wrong order: `Learned`'s up-to-8 recently-successful
    tests ran *before* the one test recorded as catching this very row, on 1105
    of a 1309-row table.

    `Killers.ahead_of` already makes the argument one function away -- it drops
    the cheap prefix entirely for a row whose killer is known, because "exact
    beats general, the prefix would only be work before the answer". `Learned`
    is general in the same way: it is what caught the *previous* rows, a proxy
    for what catches this one, and for these rows the thing being proxied is
    already in hand.

    Driven through `_attempt` with `_run` watched, rather than by asserting on
    the pieces: the string those two are composed into is the whole of what
    changed, and each half is correct on its own.
    """

    KILLER = "tests.test_sync.TestTheDecisionTable.test_it"
    FRONT = "tests.test_sync.TestSomethingElse.test_other"

    def first_for(self, mutation: Mutation) -> str:
        """What `_attempt` hands `_run` as its `first`, for one row."""
        seen: list[str] = []

        def watch(*args: object, **kw: object) -> mutate.Verdict:
            seen.append(str(kw["first"]))
            return mutate.Verdict("caught", "probe", killer=self.KILLER)

        learned = mutate.Learned()
        learned.saw(self.FRONT)
        available: queue.Queue[Path] = queue.Queue()
        with tempfile.TemporaryDirectory() as box:
            root = Path(box)
            (root / "tupferl").mkdir()
            (root / mutation.path).write_text(mutation.old, encoding="utf-8")
            available.put(root)
            with mock.patch.object(mutate, "_run", watch):
                mutate._attempt(mutation, available, True, 60.0, 0, 30.0, True, learned)
        return seen[0]

    def row(self, **kw: object) -> Mutation:
        return mutants.Mutation(
            "tupferl/sync.py:1 in f() -- x",
            "tupferl/sync.py",
            "x",
            "y",
            "tests.test_sync",
            **kw,  # type: ignore[arg-type]
        )

    def test_a_recorded_killer_runs_before_the_learned_front(self) -> None:
        """The row this exists for. `exact` is what `Killers.ahead_of` sets when
        it found this row's own killer."""
        got = self.first_for(self.row(first=self.KILLER, exact=True)).split()
        self.assertEqual([self.KILLER, self.FRONT], got)

    def test_a_general_prefix_runs_after_it(self) -> None:
        """The other half, and without it "always put `first` in front" passes
        the test above. The cheap prefix is *not* about this row -- it is the
        tests that catch a lot per second across the table -- so the learned
        front, which is at least about this row's neighbours, precedes it."""
        got = self.first_for(self.row(first=self.KILLER, exact=False)).split()
        self.assertEqual([self.FRONT, self.KILLER], got)

    def test_the_learned_front_still_follows_a_killer_rather_than_being_dropped(
        self,
    ) -> None:
        """A recorded killer can be stale -- the code moved and the test no
        longer sees the mutation -- and the learned front is then the next
        guess before the whole selection. It costs nothing when the killer is
        right, because the killer has already answered by then.
        """
        got = self.first_for(self.row(first=self.KILLER, exact=True)).split()
        self.assertIn(self.FRONT, got, "the learned front was dropped, not demoted")


class TestHandingRowsOutToLanes(unittest.TestCase):
    """`Work` hands out every row exactly once, in table order.

    Two claims, and they matter for different reasons. **Once** is the one that
    would corrupt something: `Accepted.seen` counts occurrences of a
    content-addressed key, because two identical mutations in one file share
    one, so a row handed to two lanes would eat a `seen` slot a genuinely new
    survivor could then not claim, and `known-survivors.json` would drift in the
    flattering direction with nothing to say it had.

    **In table order** is what makes `slowest_first` mean anything at all. A
    dispatch free to pick rows in some other order would make ordering the table
    a no-op, and nothing else in the suite would notice -- same verdicts, same
    counts, just the slow tail back again.
    """

    def test_every_row_comes_out_exactly_once_under_threads(self) -> None:
        """Real threads rather than turns, because the race this is about is two
        lanes inside `take` at the same moment."""
        rows, lanes = 500, 8
        work = mutate.Work(rows)
        seen: list[int] = []
        guard = threading.Lock()

        def drain() -> None:
            while (index := work.take()) is not None:
                with guard:
                    seen.append(index)

        walkers = [threading.Thread(target=drain) for _ in range(lanes)]
        for walker in walkers:
            walker.start()
        for walker in walkers:
            walker.join()
        self.assertEqual(list(range(rows)), sorted(seen), "a row was dropped or run twice")

    def test_the_table_is_walked_front_to_back(self) -> None:
        work = mutate.Work(9)
        self.assertEqual(list(range(9)), [work.take() for _ in range(9)])

    def test_an_exhausted_table_says_so_rather_than_running_off_the_end(self) -> None:
        work = mutate.Work(2)
        self.assertEqual([0, 1], [work.take(), work.take()])
        self.assertIsNone(work.take())
        self.assertIsNone(work.take(), "a second ask past the end answered differently")

    def test_more_lanes_than_rows_hands_out_every_row_and_no_more(self) -> None:
        work = mutate.Work(3)
        self.assertEqual([0, 1, 2, None, None, None, None, None], [work.take() for _ in range(8)])


class TestOrderingTheTableByWhatItCostLastTime(unittest.TestCase):
    """`slowest_first`: longest-processing-time-first, but only within a file.

    The restriction is the interesting half. File contiguity is what lets
    `sweep` count a file down to zero before writing its rows, and what
    `by_size` needs to put the smallest file first, so a global sort by cost
    would be a correctness problem and not merely a different order.
    """

    def rows(self, *spec: tuple[str, str]) -> list[Mutation]:
        """One row per `(path, tag)`. `_key` is over `(path, operator, old,
        new)`, so a distinct tag in `new` is a distinct row."""
        return [mutants.Mutation(f"{path} {tag}", path, "x", tag, "tests.t") for path, tag in spec]

    def order(self, table: Sequence[Mutation], seconds: dict[str, float]) -> list[str]:
        with support.quiet():
            return [row.new for row in mutate.slowest_first(table, seconds)]

    def timed(self, table: Sequence[Mutation], *costs: float | None) -> dict[str, float]:
        """`mutate._key` rather than a second spelling of the hash here: a test
        carrying its own copy of the code it checks cannot fail (CLAUDE.md §2).

        `None` is "this row was never timed", so a fixture can lay its costs out
        in row order and leave gaps where the cold rows are.
        """
        return {
            mutate._key(row): cost
            for row, cost in zip(table, costs, strict=True)
            if cost is not None
        }

    def test_the_dearest_row_in_a_file_goes_first(self) -> None:
        """Expected order differs from the input *and* from its reverse, so a
        sort with the sign the wrong way round, or none at all, both fail."""
        table = self.rows(("a.py", "p"), ("a.py", "q"), ("a.py", "r"))
        self.assertEqual(["q", "r", "p"], self.order(table, self.timed(table, 1.0, 9.0, 5.0)))

    def test_a_dear_row_never_overtakes_a_file(self) -> None:
        """The claim contiguity rests on. Every row of `b.py` costs fifty times
        every row of `a.py`, so a sort over the whole table would interleave
        them -- and `sweep`'s per-file countdown would then write a file's rows
        out before they had all been answered."""
        table = self.rows(("a.py", "p"), ("a.py", "q"), ("b.py", "s"), ("b.py", "t"))
        got = self.order(table, self.timed(table, 1.0, 2.0, 100.0, 200.0))
        self.assertEqual(["q", "p", "t", "s"], got)

    def test_a_row_nobody_timed_sits_at_its_file_s_median(self) -> None:
        """Four timed rows at 12, 10, 4 and 2 give a median of 7, so the cold
        row lands strictly between the 10 and the 4. Front and back are the two
        obvious wrong answers and this fixture rejects both."""
        table = self.rows(*[("a.py", tag) for tag in ("p", "q", "cold", "r", "s")])
        seconds = self.timed(table, 12.0, 10.0, None, 4.0, 2.0)
        self.assertEqual(["p", "q", "cold", "r", "s"], self.order(table, seconds))

    def test_the_median_a_cold_row_takes_is_its_own_file_s(self) -> None:
        """Not the tree's, and the two answers differ by two orders of magnitude
        here on purpose. `gitrepo.py`'s rows each drive a real `git` subprocess
        and `merge.py`'s do not, so one figure for the tree would put every cold
        row of the cheap file ahead of the dear file's *timed* ones.

        Both files put their cold row third. Against a tree-wide median of 106
        the cheap file's would go first and the dear file's last, so this
        fixture rejects that answer in both directions.
        """
        tags = ("p", "q", "cold", "r", "s")
        table = self.rows(*[("cheap.py", tag) for tag in tags], *[("dear.py", tag) for tag in tags])
        seconds = self.timed(table, 12.0, 10.0, None, 4.0, 2.0, 1200.0, 1000.0, None, 400.0, 200.0)
        got = self.order(table, seconds)
        self.assertEqual(["p", "q", "cold", "r", "s"], got[:5], "the cheap file")
        self.assertEqual(["p", "q", "cold", "r", "s"], got[5:], "the dear file")

    def test_a_file_nothing_has_timed_is_left_exactly_as_it_arrived(self) -> None:
        """What a `--base` diff is almost entirely made of: its rows are new text
        by construction, so nothing remembers them. They must keep the line order
        `Learned` rests on rather than being shuffled by a tree-wide median."""
        table = self.rows(
            ("a.py", "p"), ("a.py", "q"), ("new.py", "s"), ("new.py", "t"), ("new.py", "u")
        )
        got = self.order(table, self.timed(table[:2], 1.0, 9.0))
        self.assertEqual(["s", "t", "u"], got[2:], "a file nobody has timed was reordered")

    def test_nothing_remembered_at_all_leaves_the_table_alone(self) -> None:
        table = self.rows(("a.py", "p"), ("a.py", "q"), ("b.py", "s"))
        self.assertEqual(["p", "q", "s"], self.order(table, {}))

    def test_it_says_how_much_of_the_table_it_could_order(self) -> None:
        """A silent reorder reads the same whether the cache loaded or not --
        which is exactly how `Killers.cost` was empty for a whole milestone."""
        table = self.rows(("a.py", "p"), ("a.py", "q"), ("b.py", "s"))
        with support.quiet() as said:
            mutate.slowest_first(table, self.timed(table[:1], 4.0))
        self.assertIn("1 of 3", said.getvalue())
        self.assertIn("2 never timed", said.getvalue())


class TestRememberingWhatEachRowCost(unittest.TestCase):
    """The measurement `slowest_first` orders by, end to end.

    Driven through a real `mutate.run` rather than a hand-built `Verdict`, for
    the reason the class above `test_a_run_measures_the_tests_it_ran` gives: a
    test that builds its own inputs cannot see a data path that never delivers
    them. A `spent` that stayed 0.0 would order nothing and say nothing.
    """

    #: One real sweep for both halves of the claim. Each `mutate.run` copies the
    #: tree and spawns an interpreter, and the second test asserts about the
    #: same numbers the first produced -- so running it twice bought nothing and
    #: cost a `copytree` and a subprocess on every suite execution.
    swept: typing.ClassVar[mutate.Report]

    @classmethod
    def setUpClass(cls) -> None:
        cls.swept = mutate.run([UNWATCHED], baseline=False, workers=1, summarise=False, walk=False)

    def test_a_real_run_times_the_row_it_ran(self) -> None:
        (only,) = self.swept.results
        self.assertGreater(only.verdict.spent, 0.0, "the row was not timed")

    def test_the_time_reaches_the_cache_under_the_row_s_key(self) -> None:
        cache = mutate.Killers(None)
        cache.learn(self.swept)
        self.assertEqual([mutate._key(UNWATCHED)], list(cache.seconds))
        self.assertEqual(self.swept.results[0].verdict.spent, cache.seconds[mutate._key(UNWATCHED)])

    def test_a_row_that_was_never_answered_is_timed_too(self) -> None:
        """`broke` and `timeout` rows are the *most* expensive there are -- a
        timeout costs the whole `--timeout` -- and they are never `caught`, so a
        record kept only for answered rows would miss precisely the rows worth
        starting first."""
        row = mutants.Mutation("a.py x", "a.py", "x", "y", "tests.t")
        broke = mutate.Verdict("broke", "nothing loaded", spent=41.0)
        cache = mutate.Killers(None)
        cache.learn(mutate.Report([mutate.Result(row, broke)]))
        self.assertEqual({mutate._key(row): 41.0}, cache.seconds)

    def test_a_survivor_keeps_its_cost_while_losing_its_killer(self) -> None:
        """The two records answer different questions. Whatever used to catch a
        survivor demonstrably does not any more, so the killer goes; what it
        cost is still true, and a survivor is the dearest row there is."""
        row = mutants.Mutation("a.py x", "a.py", "x", "y", "tests.t")
        cache = mutate.Killers(None)
        cache.known = {mutate._key(row): "tests.t.T.test_it"}
        cache.learn(mutate.Report([mutate.Result(row, mutate.Verdict("survived", spent=70.0))]))
        self.assertEqual({}, cache.known)
        self.assertEqual({mutate._key(row): 70.0}, cache.seconds)

    def test_it_survives_a_trip_through_the_file(self) -> None:
        row = mutants.Mutation("a.py x", "a.py", "x", "y", "tests.t")
        with tempfile.TemporaryDirectory() as box:
            where = Path(box) / "killers.json"
            made = mutate.Killers(where)
            made.learn(
                mutate.Report(
                    [mutate.Result(row, mutate.Verdict("caught", killer="t.T.m", spent=3.5))]
                )
            )
            made.save()
            self.assertEqual({mutate._key(row): 3.5}, mutate.Killers(where).seconds)

    def test_a_cost_survives_the_report_a_resume_reads_back(self) -> None:
        """The gap this closes, and it only exists on the sweeps that matter.

        A resumed sweep *skips* rows already in the `--json` report, so unless
        the cost rides in the report beside the verdict, a row answered by the
        run that crashed is never timed by the run that finishes -- and a table
        that kept crashing would stay permanently cold. Those are exactly the
        multi-hour whole-tree sweeps `slowest_first` is for.
        """
        row = mutants.Mutation("a.py:1 in f() -- x", "a.py", "x", "y", "tests.t", span=(0, 1))
        caught = mutate.Verdict("caught", killer="t.T.m", spent=8.25)
        with tempfile.TemporaryDirectory() as box:
            report = Path(box) / "r.json"
            mutate._persist(
                mutate.Report([mutate.Result(row, caught)], widened=True), report, announce=False
            )
            (back,) = mutate._recorded(report)
        self.assertEqual(8.25, back.verdict.spent, "the cost did not survive the report")
        cache = mutate.Killers(None)
        cache.learn(mutate.Report([back]))
        self.assertEqual({mutate._key(row): 8.25}, cache.seconds)

    def test_a_report_written_before_costs_existed_reads_back_untimed(self) -> None:
        """Cut from a real report rather than built by hand, so it holds exactly
        the fields `_persist` writes minus the one under test. It must read as
        "never timed" -- which `slowest_first` answers with the file median --
        rather than as a failure that loses every other row with it."""
        row = mutants.Mutation("a.py:1 in f() -- x", "a.py", "x", "y", "tests.t", span=(0, 1))
        with tempfile.TemporaryDirectory() as box:
            report = Path(box) / "r.json"
            mutate._persist(
                mutate.Report([mutate.Result(row, mutate.Verdict("survived", spent=70.0))]),
                report,
                announce=False,
            )
            written = json.loads(report.read_text(encoding="utf-8"))
            self.assertIn("seconds", written["results"][0], "nothing was removed")
            del written["results"][0]["seconds"]
            report.write_text(json.dumps(written), encoding="utf-8")
            (back,) = mutate._recorded(report)
        self.assertEqual("survived", back.verdict.outcome, "the whole row was lost")
        self.assertEqual(0.0, back.verdict.spent)

    def test_a_cache_written_before_costs_existed_still_loads(self) -> None:
        """The shape `killers.json` had until this landed. It must read as "no
        times recorded" rather than as a failure -- the worst an empty record
        does is run at yesterday's speed."""
        with tempfile.TemporaryDirectory() as box:
            where = Path(box) / "killers.json"
            where.write_text(json.dumps({"killers": {"k": "t.T.m"}, "costs": {"t.T.m": 1.0}}))
            cache = mutate.Killers(where)
            self.assertEqual({}, cache.seconds)
            self.assertEqual({"k": "t.T.m"}, cache.known)


class TestWhatTheFinalBlockSays(unittest.TestCase):
    """The four counts, the denominator, and the refusal on a red baseline."""

    def block(self, outcomes: Sequence[mutate.Outcome], red: bool = False) -> str:
        results = [
            mutate.Result(
                mutants.Mutation(f"f{n}.py", "x", "y", f"f{n}.py:1 in f() -- x", "tests.t"),
                mutate.Verdict(outcome),
            )
            for n, outcome in enumerate(outcomes)
        ]
        pace = mutate.Pace(10.0, 4, 2 << 30)
        with support.quiet() as said:
            mutate._report_stats(results, pace=pace, red=red)
        return said.getvalue()

    def test_all_four_outcomes_are_named_even_at_zero(self) -> None:
        """A category that vanishes when empty is one a reader stops expecting.
        `BROKE` and `TIMEOUT` are the two that matter: such a row is never
        `caught`, so the line it appears to guard is guarded by nothing."""
        said = self.block(["caught", "caught"])
        for headline in ("caught", "SURVIVED", "BROKE", "TIMEOUT"):
            self.assertIn(headline, said, f"{headline} is missing from the block")

    def test_the_score_names_what_it_is_a_score_of(self) -> None:
        said = self.block(["caught", "caught", "survived", "broke"])
        self.assertIn("2 caught of 3 answered", said)
        self.assertIn("1 row(s) answered nothing", said)

    def test_a_red_baseline_gets_no_percentage_at_all(self) -> None:
        """Not a flattering number under a warning. A failing suite notices
        every mutation, and a percentage is far more seductive than a wall of
        rows -- 51 of 51 was read twice here before anyone read the line."""
        said = self.block(["caught", "caught"], red=True)
        self.assertIn("no score", said)
        self.assertNotIn("%", said)

    def test_the_rate_is_reported_with_the_lane_count_beside_it(self) -> None:
        """A rate alone is comparable to nothing: this tree measured the same
        table at 1.84/s over 32 lanes and 1.49/s over 16."""
        said = self.block(["caught"] * 20)
        self.assertIn("over 4 lane(s)", said)
        self.assertIn("/s/lane", said)

    def test_a_report_with_no_pace_says_nothing_rather_than_zero(self) -> None:
        """A resumed run does not re-run anything, so it has no rate to report.
        Noughts there would be a measurement reported as a result."""
        results = [
            mutate.Result(
                mutants.Mutation("f.py", "x", "y", "f.py:1 in f() -- x", "tests.t"),
                mutate.Verdict("caught"),
            )
        ]
        with support.quiet() as said:
            mutate._report_stats(results, pace=None, red=False)
        self.assertEqual("", said.getvalue())
