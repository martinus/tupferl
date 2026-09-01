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
import functools
import io
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
from collections.abc import Iterator, Sequence
from contextlib import ExitStack, redirect_stderr, redirect_stdout, suppress
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from tests import support
from tools import mutants, mutate, paint, reached, verdict
from tools.mutants import Mutation, check
from tools.settings import SETTINGS

#: Seconds a driven probe may take before a test calls it hung. Well above the
#: ~0.5s an honest `collect(ALARM)` spends and well below `tools/mutate.py`'s
#: `EACH_TEST` of 30 -- see `TestAHungTestIsBoundedAndNotCredited.collect` for
#: what happens when it is not, and `tests/test_watch.py` for the same constant
#: and the same two bounds it has to sit between.
#:
#: Left at 20 when `ALARM` dropped from 2 to 0.5. The number that matters is the
#: gap to the alarm, not the gap to the honest wait: this bound exists to fail a
#: *hung* probe before the harness's own alarm does, and shrinking it in step
#: would buy nothing and narrow the margin that stops a slow runner reading as
#: a hang.
#:
#: **The gap was to 30 and 30 is only the default.** `--each-test` is a flag, so
#: at `--each-test 10` this sat back above the alarm -- the fourth instance of
#: the mistake `collect`'s docstring below counts to three, in the file that
#: counts it. Through `support.bounded` since B5, along with every other wait in
#: `tests/`; `test_support.TestEveryWaitOnAChildIsBounded` is what found it and
#: what keeps it.
BOUND = support.bounded(20.0)

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
NESTED = support.SLOW_ELSEWHERE

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


class TestTheHarnessAnswersBothWays:
    """The whole loop: copy the tree, apply the edit, run a suite, classify.

    Every test here runs under `NESTED`, because every one of them drives a real
    `mutate.run` and a broken walk does not stop. Six mutants of
    `verdict._reached` and `verdict.collect` came back `BROKE` on the whole-tree
    sweep -- never `caught`, so the widening this class exists to prove was
    proved by nothing.

    Armed by an autouse fixture on the class rather than around the one call,
    which was the first attempt and left two of the six still `BROKE`: `if not walk:` inverted hangs
    `test_a_deliberate_bug_is_caught` and `test_an_unwatched_bug_survives`, which
    pass `walk=False` and are not the test the bound was written on. A bound
    around one call covers that call and reads as though it covered the class --
    the same mistake `TestLineEndingsThatAreNotNewline` records one file over.

    **`strict=False` everywhere here, and it is not a detail.** A broken walk
    also produces rows the nested harness cannot answer, and under `strict` it
    answers those by raising `SystemExit` -- a `BaseException`, so it escapes the
    test, escapes `suite.run` and kills the probe, which the outer harness files
    as `BROKE` rather than `caught`. Three rows came back that way after the
    *hang* had been fixed, because a hang and a crash are two different ways to
    fail to answer and only the first had been measured.

    `run`'s own docstring draws this line: strict is for a hand-written table,
    where an unanswerable row is a mistake in the table. Nothing here is that --
    each of these wants the `Report` back so its own assertion can name what
    went wrong, which is also a better message than the exit ever gave.
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

    _bounded = support.bounds(NESTED, "a nested harness run never finished")

    def test_a_deliberate_bug_is_caught(self) -> None:
        report = mutate.run(
            [UNKNOWN_KEY_GUARD],
            baseline=True,
            workers=1,
            summarise=False,
            walk=self.WALK,
            strict=False,
        )
        assert not report.baseline_red, "the untouched tree is not green"
        assert [result.verdict.outcome for result in report.results] == ["caught"]

    def test_an_unwatched_bug_survives(self) -> None:
        """The other answer. Without this, `test_a_deliberate_bug_is_caught`
        passes just as well against a harness hard-wired to say `caught` -- the
        assertion that passes against its own mutation, from CLAUDE.md §2."""
        report = mutate.run(
            [UNWATCHED], baseline=True, workers=1, summarise=False, walk=self.WALK, strict=False
        )
        assert not report.baseline_red
        assert [result.verdict.outcome for result in report.results] == ["survived"]
        assert not report.widened, "a report that did not walk claimed it had"

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
        report = mutate.run([UNWATCHED], baseline=True, workers=1, summarise=False, strict=False)
        assert not report.baseline_red, "the untouched tree is not green"
        assert [result.verdict.outcome for result in report.results] == ["caught"]
        killer = report.results[0].verdict.killer
        assert killer.startswith("tests/test_config.py::"), (
            f"caught, but not by the module the walk had to reach: {killer}"
        )
        assert report.widened

    def test_the_working_tree_is_untouched(self) -> None:
        """CLAUDE.md §6: the harness must never edit the tree it is run from.

        Asserted on the file's own bytes, before and after, rather than on
        `git status` -- the guarantee is about this file, and `git status` would
        also be satisfied by an edit that was made and then put back.
        """
        where = Path(UNKNOWN_KEY_GUARD.path)
        before = where.read_bytes()
        mutate.run(
            [UNKNOWN_KEY_GUARD],
            baseline=False,
            workers=1,
            summarise=False,
            walk=self.WALK,
            strict=False,
        )
        assert where.read_bytes() == before


class TestTheDocumentedExampleIsReal:
    def test_the_line_it_names_exists_exactly_once(self) -> None:
        """`check` is what enforces it, and this is the case that keeps the
        docstring in `tools/mutate.py` from naming code nobody has."""
        check(UNKNOWN_KEY_GUARD)

    def test_a_row_naming_absent_code_is_refused(self) -> None:
        with pytest.raises(SystemExit):
            check(UNKNOWN_KEY_GUARD._replace(old="if key not in NOTHING_LIKE_THIS:"))

    def test_a_replacement_that_keeps_the_original_is_refused(self) -> None:
        """An edit that adds without removing leaves the code under test exactly
        as it was, so the run reports an outcome about nothing."""
        with pytest.raises(SystemExit):
            check(UNKNOWN_KEY_GUARD._replace(new="if key not in KNOWN:  # noqa"))

    def test_an_additive_row_is_allowed_when_it_says_so(self) -> None:
        check(UNKNOWN_KEY_GUARD._replace(new="if key not in KNOWN:  # noqa", additive=True))


class TestGeneratingFromADiff:
    """`--base` reads `git diff` and writes the table itself. Driven against a
    throwaway repository rather than this one, so the answer does not depend on
    what happens to be uncommitted while the suite runs."""

    @pytest.fixture(autouse=True)
    def _tree(self, boxes: support.Boxes) -> None:
        box = boxes.make("tupferl-mutants-")
        # A seeded home beside the tree, not the tree itself: git needs an
        # identity and a written `init.defaultBranch` to commit at all, and
        # `seed_home` is where both are decided for the whole suite.
        home = box / "home"
        home.mkdir()
        support.seed_home(home)
        env = support.sandbox_env(home)
        self.tree = box / "tree"
        self.tree.mkdir()
        support.git(["init", "--initial-branch=main"], self.tree, env)
        (self.tree / "tupferl").mkdir()
        self.source = self.tree / "tupferl" / "thing.py"
        self.source.write_text("def size(n: int) -> int:\n    return n + 1\n", encoding="utf-8")
        support.git(["add", "-A"], self.tree, env)
        support.git(["commit", "-m", "base"], self.tree, env)

    def test_only_the_changed_lines_are_generated_for(self) -> None:
        self.source.write_text(
            "def size(n: int) -> int:\n    return n + 1\n\n\ndef twice(n: int) -> int:\n"
            "    return n * 2\n",
            encoding="utf-8",
        )
        touched = mutants.changed_lines("main", self.tree)
        assert set(touched) == {"tupferl/thing.py"}
        # The added lines, not the whole file: a generator that ignored the diff
        # would offer the first function's lines too.
        assert 2 not in touched["tupferl/thing.py"]
        assert 6 in touched["tupferl/thing.py"]

    def test_tests_are_never_mutated(self) -> None:
        """Breaking a test proves nothing about the fix, and the run would then
        report the assertion it removed."""
        assert not mutants.mutable("tests/test_config.py")
        assert mutants.mutable("tupferl/config.py")
        assert mutants.mutable("tools/mutate.py")


class TestWhatASandboxDoesNotCopy:
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
        with support.tempdir(prefix="tupferl-skip-") as box:
            tree = Path(box) / "tree"
            (tree / self.KEPT).mkdir(parents=True)
            (tree / self.KEPT / "__init__.py").write_text("", encoding="utf-8")
            for name in self.KEPT_OUT:
                (tree / name).mkdir()
                (tree / name / "inside").write_text("x", encoding="utf-8")

            copy = self.sandbox(tree)
            assert self.KEPT_OUT, "nothing was asked about"
            for name in self.KEPT_OUT:
                assert not (copy / name).exists(), f"{name} was copied"
            assert (copy / self.KEPT / "__init__.py").is_file(), "the tree was not copied"

    def test_a_nested_one_is_kept_out_too(self) -> None:
        """`shutil.ignore_patterns` matches the base name at any depth. A
        pattern that only applied at the root would leave this copied, and
        nothing would notice until the next red leg."""
        with support.tempdir(prefix="tupferl-skip-") as box:
            tree = Path(box) / "tree"
            deep = tree / self.KEPT / "somewhere" / ".hypothesis"
            deep.mkdir(parents=True)
            (deep / "tmp").write_text("x", encoding="utf-8")

            copy = self.sandbox(tree)
            assert (copy / self.KEPT / "somewhere").is_dir(), "the tree was not copied"
            assert not (copy / self.KEPT / "somewhere" / ".hypothesis").exists()


# The `if __name__ == "__main__": unittest.main()` block that sat here is gone,
# and it is worth saying what it did rather than only that it was dead. It was
# 6000 lines above the end of the file, so running this module directly defined
# the classes above it, ran *those*, and exited -- reporting `OK` over a
# fraction of the file with nothing to say which fraction. That is the shape
# CLAUDE.md §8 collects, and pytest has no entry point to put it back at.


#: A test id that certainly resolves, used where the point is "a real one is
#: kept". This module's own name, so it cannot go stale without this file
#: being edited -- and if it is renamed, the test that depends on it is right
#: here rather than somewhere that would fail mysteriously.
#:
#: A **pytest nodeid**, because that is what `mutate._loadable` asks pytest
#: about and what a killers cache written by a sweep holds. Spelled with the
#: unittest dots it would simply be dropped as an id that no longer resolves,
#: and every assertion below would read "" -- which is how these four classes
#: failed when the backend changed, correctly.
REAL = (
    "tests/test_mutate.py::TestRememberingWhatCaughtEachMutation::test_a_remembered_test_runs_first"
)

#: The class holding it, spelled the way a **selection** is spelled: dotted,
#: because `mutants.targets_for` builds selections out of module names. The two
#: formats meeting in one comparison is exactly what `mutate._reaches` is for,
#: so this is written out rather than derived from `REAL`.
REAL_CLASS = "tests.test_mutate.TestRememberingWhatCaughtEachMutation"


def row(
    path: str = "tupferl/sync.py",
    old: str = "a",
    new: str = "b",
    label: str = "x:1 in f()",
) -> Mutation:
    """One generated-shaped mutation, with only the fields the cache reads."""
    return Mutation(label, path, old, new, "tests.test_sync", operator="branch")


class TestRememberingWhatCaughtEachMutation:
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
        assert ahead.first == (REAL,)
        assert ahead.tests == "tests.test_sync"

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
        assert ahead.exact, "a remembered killer was not marked exact"

    def test_the_cheap_prefix_is_not_marked_as_exact(self) -> None:
        """The other half. The prefix is what a row with *no* remembered killer
        falls back on -- tests that catch a lot per second across the table,
        which is a claim about the suite and not about this row -- so it must
        not claim the precedence an exact killer has earned."""
        with support.quiet():
            (ahead,) = mutate.Killers(None).ahead_of([row()])
        assert not ahead.exact, "the general prefix claimed to be exact"

    def test_the_whole_selection_is_kept_behind_it(self) -> None:
        """The safety argument, asserted rather than assumed. Substituting the
        remembered test for the selection would make every stale entry a
        `caught` that nothing verified -- flattering the tests, which is the
        direction every bug in this class errs."""
        one = row()._replace(tests="tests.test_sync tests.test_sync_cli")
        cached = self.cache({mutate._key(one): REAL})
        (ahead,) = cached.ahead_of([one])
        assert ahead.tests == "tests.test_sync tests.test_sync_cli"
        assert ahead.first == (REAL,)

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
        assert len({r.tests for r in ahead}) == 1, "the baseline would shard per row"

    def test_a_test_that_no_longer_exists_is_dropped(self) -> None:
        """A renamed test leaves its module in place, so `unittest`'s loader
        records an error rather than raising -- and an error there is classified
        `broke`, which would turn every mutant that remembered it into a
        non-answer. One rename would produce a wall of them."""
        one = row()
        cached = self.cache({mutate._key(one): "tests.test_mutate.NoSuchClass.no_such_test"})
        with support.quiet():
            (ahead,) = cached.ahead_of([one])
        assert ahead.first == ()

    def test_a_module_that_no_longer_exists_is_dropped_too(self) -> None:
        one = row()
        cached = self.cache({mutate._key(one): "tests.test_gone.Class.test_x"})
        with support.quiet():
            (ahead,) = cached.ahead_of([one])
        assert ahead.first == ()

    def test_a_row_nothing_is_remembered_about_is_left_alone(self) -> None:
        (ahead,) = mutate.Killers(None).ahead_of([row()])
        assert ahead.first == ()
        assert ahead.tests == "tests.test_sync"


class TestTheKeyIsContentNotPosition:
    """A line number is invalidated by any edit above it -- which is every edit,
    so a position-keyed cache would be empty exactly when it was most wanted."""

    def test_the_same_edit_at_a_different_line_is_the_same_key(self) -> None:
        moved = row(label="tupferl/sync.py:900 in f()")._replace(span=(9000, 9001))
        assert mutate._key(moved) == mutate._key(row())

    def test_a_different_edit_at_the_same_line_is_a_different_key(self) -> None:
        """Otherwise two operators' rows on one line would share an entry, and
        the second would run a test chosen for the first."""
        assert mutate._key(row(new="c")) != mutate._key(row())
        assert mutate._key(row(path="tupferl/manage.py")) != mutate._key(row())
        assert mutate._key(row()._replace(operator="return-value")) != mutate._key(row())


class TestWhatTheCacheLearns:
    def learned(self, outcome: mutate.Outcome, killer: str) -> dict[str, str]:
        one = row()
        with support.tempdir() as box:
            cache = mutate.Killers(box / "killers.json")
            cache.known = {mutate._key(one): "tests.test_old.C.t"}
            cache.learn(mutate.Report([mutate.Result(one, mutate.Verdict(outcome, "", killer))]))
            return cache.known

    def test_a_catch_is_remembered(self) -> None:
        assert self.learned("caught", REAL) == {mutate._key(row()): REAL}

    def test_a_survivor_forgets_whatever_used_to_catch_it(self) -> None:
        """Keeping it would put a test that cannot help at the front of every
        future run of this row, for ever."""
        assert self.learned("survived", "") == {}

    @pytest.mark.parametrize("outcome", ["broke", "timeout"])
    def test_a_run_that_asked_nothing_changes_nothing(self, outcome: mutate.Outcome) -> None:
        """`broke` and `timeout` are not answers, so they are not evidence that
        the remembered test stopped working."""
        assert self.learned(outcome, "") == {mutate._key(row()): "tests.test_old.C.t"}


class TestTheKillerIsRecordedAtAll:
    """The cache is worth nothing if `Verdict.killer` is empty, and it comes
    from `tools/verdict.py` through a JSON report -- so this drives the real
    thing rather than asserting on a field.

    It is also the running half of `TestTheProbeIsGradedByThisTreesClassifier`:
    that class reads the probe's source and this one puts a real row through
    it, which is the pair the deleted second backend was caught by not having.
    """

    def test_a_caught_mutation_names_the_test_in_a_form_pytest_takes_back(self) -> None:
        found = mutate.run(
            [UNKNOWN_KEY_GUARD], baseline=False, workers=1, summarise=False, strict=False
        )
        (result,) = found.results
        assert result.verdict.outcome == "caught"
        assert result.verdict.killer, "nothing recorded the killing test"
        # The claim: it can be selected again. A nodeid can; a display string
        # -- "method (dotted.id)", which is what `str(test)` gives -- cannot.
        assert mutate._loadable([result.verdict.killer]) == {result.verdict.killer}


class TestWhichSourceTheProbeIsHanded:
    """`_probe`: where the classifier a sandbox runs is read from.

    One backend now. `tools/verdict_unittest.py` and the
    `TUPFERL_MUTATE_VERDICT` switch that reached it were deleted at
    `docs/pytest-plan.md`'s Phase C, so what is left to assert is the isolation
    property the switch was layered on top of: the source comes out of *this*
    tree, never the sandbox's copy, because `tools/**.py` is itself something a
    generated table mutates and a verdict loaded out of the copy would let a
    mutation grade its own exam.

    **Reading the source is not running it, and that distinction cost the
    switch once.** `_run` gained a JSON `first` slot and only one of the two
    layers was taught to read it; every assertion about the *source* passed
    throughout while one backend answered `broke` for every row including the
    baseline. So this class is named for the source and nothing else --
    `TestTheKillerIsRecordedAtAll` is the one that drives a real row through it,
    and the pair is the claim.

    **One test, not two.** A substring check for a hook name only that file
    carries reads like a second guarantee and is a strictly weaker consequence
    of the equality below: it can only fail when `tools/verdict.py` stops being
    the pytest classifier, which `tests/test_verdict.py` drives end to end.
    Under two backends it discriminated between them; under one it is an
    assertion that passes against its own mutation.
    """

    def test_it_comes_from_the_running_tree(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The isolation property, driven rather than read: `_probe` is called
        from inside a directory holding a *different* `tools/verdict.py`, and
        must still hand back this repository's.

        The expectation comes through `verdict.__file__` -- the import system's
        answer -- rather than through `Path(mutate.__file__).with_name(...)`,
        which is `_probe`'s own body character for character and would make this
        a test containing a copy of the code it checks.
        """
        real = Path(verdict.__file__).read_text(encoding="utf-8")
        with support.tempdir() as box:
            (box / "tools").mkdir()
            (box / "tools" / "verdict.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
            monkeypatch.chdir(box)
            assert mutate._probe() == real


class TestTwoSpellingsOfTheSameTest:
    """`_dotted` and `_reaches`: a killer's nodeid meeting a dotted selection.

    A selection is dotted under either backend -- `mutants.targets_for` builds
    it out of module names -- while a killer under pytest is a nodeid. The two
    meet in three reachability filters, and a mismatch there is invisible:
    `run_tests.selects` anchors at a dot, so a nodeid compared as it stands
    matches *nothing*, every remembered and learned test is dropped, and the
    sweep loses the orderings measured at 3.9% and 6-10% with nothing red.
    """

    def test_a_nodeid_becomes_the_dotted_id(self) -> None:
        assert mutate._dotted("tests/test_sync.py::TestX::test_y") == "tests.test_sync.TestX.test_y"

    def test_a_file_with_no_node_parts_is_still_a_module(self) -> None:
        assert mutate._dotted("tests/test_sync.py") == "tests.test_sync"

    def test_an_id_that_is_already_dotted_is_left_alone(self) -> None:
        """Both spellings reach these filters while both backends exist, and
        translating twice would be as wrong as not translating at all."""
        assert mutate._dotted("tests.test_sync.TestX.test_y") == "tests.test_sync.TestX.test_y"

    @pytest.mark.parametrize(
        "killer", ["tests/test_sync.py::TestX::test_y", "tests.test_sync.TestX.test_y"]
    )
    def test_a_selection_covers_a_killer_in_either_spelling(self, killer: str) -> None:
        assert mutate._reaches(killer, "tests.test_sync")

    def test_it_still_refuses_a_neighbour_whose_name_is_a_prefix(self) -> None:
        """`selects` anchors at a dot so `--only tests.test_sync` does not drag
        `tests/test_sync_chunks.py` under the same policy. Translating the
        spellings must not lose that -- and a substring match would."""
        assert not mutate._reaches("tests/test_sync_chunks.py::T::test_it", "tests.test_sync")


class TestWhichRememberedIdsStillResolve:
    """`_loadable`: what may be put in front of a run.

    An id that no longer names a test is not a slow cache, it is a wall of
    `BROKE`: pytest refuses the whole invocation for one name it cannot find,
    so every row that remembered a renamed test would answer nothing.
    """

    def test_a_real_nodeid_survives_and_a_renamed_one_does_not(self) -> None:
        found = mutate._loadable([REAL, "tests/test_mutate.py::TestNothing::test_gone"])
        assert found == {REAL}

    def test_a_cache_from_the_other_backend_is_dropped_rather_than_handed_over(self) -> None:
        """`sweeps/killers.json` written before the conversion holds dotted ids,
        and the file is machine-local and gitignored -- so the first sweep after
        it simply runs at yesterday's speed. Handed to pytest instead, those
        ids are a usage error that refuses the run."""
        assert mutate._loadable([_dotted_form(REAL)]) == set()

    def test_asking_about_nothing_asks_pytest_nothing(self) -> None:
        """`ahead_of` calls this with an empty set on every fresh cache, and
        asking pytest what it collects is a subprocess and half a second.

        **The `cache_clear` is what makes this able to fail**, and without it
        the test was decoration. `_collected` is `functools.cache`d, so once any
        earlier test in the same process has asked for this tree, the guard
        under test can be removed and the fall-through hits the cache instead of
        `subprocess.run` -- the mock is never called either way and the
        assertion holds.

        Measured, with `if not wanted:` mutated to `if False:`: the whole
        module passed 371 for 371 with this line absent, and fails 5 with it
        present. Identical on `main`, so this is not something the pytest
        conversion introduced -- it is a latent order dependency the sweep
        happened to catch by running the killer first, through
        `Killers.ahead_of`, before anything had warmed the cache.
        """
        mutate._collected.cache_clear()
        with mock.patch.object(subprocess, "run") as never:
            assert mutate._loadable([]) == set()
        never.assert_not_called()


def _dotted_form(nodeid: str) -> str:
    """`REAL` as the backend before this one would have written it.

    Spelled out here rather than through `mutate._dotted`, so that this file
    does not check one function with another.
    """
    path, _, rest = nodeid.partition("::")
    return ".".join([path.removesuffix(".py").replace("/", "."), *rest.split("::")])


class TestWhatEveryProbeIsHandedOnItsCommandLine:
    """The sandbox contract: which argv slot is which, and the environment.

    Read off the spawn rather than asserted inside `_run`, because it *is* a
    protocol -- `tools/verdict.py` reads these positions out of `sys.argv`, and
    the two files only ever ship together. A slot that quietly moved shows up as
    every row coming back `BROKE` at once, which no single fixture diagnoses
    better than the first sweep does; these say which slot instead.
    """

    def spawn(self, **how: Any) -> tuple[list[str], dict[str, str]]:
        """The argv and environment `_run` built, through the one `Popen` fake.

        Borrowed from `TestHowOneRunsOutcomeIsClassified` rather than copied:
        this class asks what the spawn *looked like* where that one asks what
        the report *became*, and the two questions share a stand-in for the same
        protocol. Written twice, the second copy had already lost the `_end`
        patch and pinned `returncode` where the first sets it.
        """
        holder = TestHowOneRunsOutcomeIsClassified()
        holder.verdict(holder.GREEN, **how)
        return list(holder.spawned["argv"]), dict(holder.spawned["env"])

    def test_the_prefix_travels_as_json_rather_than_space_joined(self) -> None:
        """A pytest nodeid can hold a space the moment anything is parametrized,
        and a space-joined slot shreds one name into several that select
        nothing -- silently, because selecting nothing is not an error to
        pytest."""
        argv, _ = self.spawn(first=["a.py::T::test_one[a b]", "b.py::T::test_two"])
        assert json.loads(argv[8]) == ["a.py::T::test_one[a b]", "b.py::T::test_two"]

    def test_an_empty_prefix_is_still_a_list(self) -> None:
        """`verdict.main` reads the slot with `json.loads` unconditionally, so
        an empty prefix has to be `[]` rather than the empty string it was."""
        argv, _ = self.spawn()
        assert json.loads(argv[8]) == []

    def test_the_selection_comes_after_the_walk_flag_and_not_inside_it(self) -> None:
        """The trap `first`'s own slot exists for: an empty selection *means*
        the whole suite, so anything that slid into it would turn "run
        everything" into "run these"."""
        argv, _ = self.spawn(first=["a.py::T::test_one"], walk=True)
        assert argv[9] == "1"
        assert argv[10:] == ["tests.test_paths"]

    def test_the_report_lands_in_a_directory_named_for_this_project(self) -> None:
        """The report is written outside the tree the mutation edits -- inside
        it, the file the harness grades from would be one `open()` away from
        being the mutation's to write -- and the directory it lands in is named
        from `[tool.mutate] tmp_prefix`, so a leaked one says whose it was.

        That prefix reaches three places and this is one of them; the sandbox
        pool is the second, in `tests/test_settings.py`, and `Settings.tmp` is
        the third and is where the joining happens. Asserted off the spawn
        rather than by listing `/tmp`, because a directory that has already been
        removed is exactly what this is meant to be able to see.
        """
        argv, _ = self.spawn()
        assert Path(argv[4]).parent.name.startswith("tupferl-verdict-")

    def test_the_suite_runs_with_pytest_plugin_autoload_off(self) -> None:
        """Measured at 79.5 ms a probe, and it belongs here rather than in the
        verdict layer because it decides what the *suite* runs under: the
        sandbox contract is `mutate`'s to own, and a host project that needs an
        autoloaded plugin changes it in one place."""
        _, env = self.spawn()
        assert env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
        assert env["PYTHONDONTWRITEBYTECODE"] == "1"

    def test_the_probe_is_told_its_tree_is_a_mutated_copy(self) -> None:
        """Read off the spawn, like the rest of this class, because it is the
        same protocol: `tests/support.py`'s `over_a_mutated_tree` reads it at
        the other end and two tests in `tests/test_mutants.py` stand down on it.

        Spelled literally rather than through `mutate._MUTATED`, which would
        make this a restatement instead of a check -- the same argument
        `SANDBOX`'s docstring makes about asserting its keys.

        Unset, the two assertions it gates run against a mutated copy and fail
        for the mutation rather than for the code: measured, 226 rows of a
        2789-row table credited to a kill nothing behavioural made (#110).
        """
        _, env = self.spawn()
        assert env["TUPFERL_MUTATE_MUTATED"] == "1"

    def test_the_probe_is_told_which_pid_to_outlive(self) -> None:
        """A probe collects itself when this pid stops existing, so this is the
        one slot that has to be *this* process rather than anything derived.

        Spelled literally, like the marker above: `mutate` and `verdict` each
        write the name down because `verdict.py` is read as source into the
        sandbox and may import nothing from `tools`, so Phase D could not route
        it through `[tool.mutate]`.
        """
        _, env = self.spawn()
        assert env["MUTATE_OWNER_PID"] == str(os.getpid())

    def test_the_two_modules_spell_that_name_the_same(self) -> None:
        """The cost of the exception above, and the check that pays it. A typo
        in either leaves the sweep setting a name nothing reads and the probe
        falling back to `os.getppid()` -- which works right up until the sweep
        dies during the probe's own startup, and then leaks for ever."""
        assert mutate._OWNER_PID == verdict.OWNER

    def test_the_marker_is_not_part_of_the_sandbox_contract(self) -> None:
        """The sandbox contract is applied to `_collected` as well, which runs
        over the *real* tree — so a marker living there would claim a mutated
        copy in the one place the distinction matters, and the two gated
        assertions would stop running in the preflight with nothing saying so.

        Asked of the environment `_collected` would build rather than of the
        dict, because since Phase D the contract is `Settings.environment` and
        the question is about what a process actually receives."""
        assert "TUPFERL_MUTATE_MUTATED" not in SETTINGS.environment({})


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


class TestCollectingWhatAKilledSweepLeft:
    """`_collect_abandoned`: remove a sweep's trees when the sweep is gone.

    Every teardown path in `tools/mutate.py` runs *inside* the sweep, so a
    `SIGKILL` leaves whole copies of the tree behind for ever -- 2681 of them,
    2.5 GiB, when #114 was written. This is the only thing that can collect
    them, and because it *deletes*, every test below is about what it refuses
    to touch rather than what it removes.
    """

    _bounded = support.bounds(support.PATIENCE, "collecting abandoned trees hung")

    def dead(self) -> int:
        """A pid that is certainly not running: one we waited for.

        Not `max(_born()) + 1`, which is a guess about a wrapping number, and
        not a large constant, which is a guess about the machine. Checked
        against a fresh `_born` afterwards, so the fixture cannot be the thing
        that fails.
        """
        child = subprocess.Popen([sys.executable, "-c", ""])
        child.wait(timeout=support.PATIENCE)
        assert child.pid not in mutate._born(), "the pid was reused before the test could use it"
        return child.pid

    def tree(self, root: Path, name: str, stamp: str | None) -> Path:
        made = root / f"{SETTINGS.tmp_prefix}{name}"
        made.mkdir()
        (made / "big").write_text("a copy of the tree", encoding="utf-8")
        if stamp is not None:
            (made / mutate._OWNER).write_text(stamp, encoding="utf-8")
        return made

    def test_a_tree_whose_sweep_is_gone_is_removed(self) -> None:
        with support.tempdir(prefix="tupferl-collect-") as box:
            gone = self.tree(box, "mutate-x", json.dumps({"pid": self.dead(), "born": 1.0}))
            assert mutate._collect_abandoned(box) == [gone]
            assert not gone.exists()

    def test_a_tree_whose_sweep_is_still_running_is_kept(self) -> None:
        """Our own pid and our own birth time -- the concurrent-sweep case, and
        the one where deleting would break a run in progress."""
        with support.tempdir(prefix="tupferl-collect-") as box:
            mine = self.tree(box, "mutate-y", mutate._stamp())
            assert mutate._collect_abandoned(box) == []
            assert mine.exists()

    def test_a_recycled_pid_cannot_impersonate_the_owner(self) -> None:
        """The reason the stamp carries a birth time at all. A dead sweep whose
        number has since been handed to somebody's editor would otherwise read
        as alive for ever, and its gigabytes never collected."""
        with support.tempdir(prefix="tupferl-collect-") as box:
            stale = json.dumps({"pid": os.getpid(), "born": -1.0})
            gone = self.tree(box, "mutate-z", stale)
            assert mutate._collect_abandoned(box) == [gone]

    def test_a_birth_time_that_could_not_be_read_falls_back_to_the_pid(self) -> None:
        """`born: null` is macOS, where `_born` may have nothing to say. Weaker,
        and still only ever wrong towards keeping a tree somebody is using --
        which is why this asserts the tree *survives*."""
        with support.tempdir(prefix="tupferl-collect-") as box:
            mine = self.tree(box, "mutate-m", json.dumps({"pid": os.getpid(), "born": None}))
            assert mutate._collect_abandoned(box) == []
            assert mine.exists()

    @pytest.mark.parametrize(
        ("what", "stamp"),
        [
            ("no stamp at all", None),
            ("a half-written stamp", '{"pid": 1'),
            ("a stamp with no pid", "{}"),
            ("a stamp whose pid is not a number", '{"pid": "later", "born": 1.0}'),
        ],
    )
    def test_anything_it_cannot_read_is_left_alone(self, what: str, stamp: str | None) -> None:
        """All four are the same rule: **this deletes, so it acts only on what
        it can prove.** A half-written stamp in particular means a sweep was
        interrupted while *starting*, and may still be running."""
        with support.tempdir(prefix="tupferl-collect-") as box:
            kept = self.tree(box, "mutate-q", stamp)
            assert mutate._collect_abandoned(box) == [], what
            assert kept.exists(), what

    def test_the_suites_own_throwaway_directories_are_never_reported(self) -> None:
        """`support.tempdir` defaults to `tupferl-test-`, under the same prefix.
        `unstamped` names only the three kinds this tool makes, so a person
        following its `rm -rf` hint cannot be pointed at a running test's
        directory."""
        with support.tempdir(prefix="tupferl-collect-") as box:
            self.tree(box, "test-something", None)
            ours = self.tree(box, "mutate-old", None)
            assert mutate.unstamped(box) == [ours]

    def test_a_stamped_tree_is_not_reported_as_unstamped(self) -> None:
        """The other half: once stamped, a tree is `_collect_abandoned`'s
        business and must not also appear in a hint telling a person to delete
        it by hand while a sweep is using it."""
        with support.tempdir(prefix="tupferl-collect-") as box:
            self.tree(box, "mutate-new", mutate._stamp())
            assert mutate.unstamped(box) == []

    def test_the_tree_we_are_standing_in_is_never_removed(self) -> None:
        """**The veto, and it is why deleting here is acceptable at all.**

        A probe runs a *mutated* copy of this module, so the liveness test above
        is not something the harness may rely on: the first sweep taken over
        this change mutated it on row 1 and the probe deleted the live sweep's
        own sandbox out from under it -- `FileNotFoundError: .../tree3/tools/
        verdict.py`, and the run died. `Path.cwd()` is the kernel's answer
        rather than anything computed here, which is the second, independent
        fact #91 says to ask any new delete for.

        The stamp says the owner is long dead, so every other rule here votes to
        remove it; only being *inside* it saves it.
        """
        with support.tempdir(prefix="tupferl-collect-") as box:
            mine = self.tree(box, "mutate-here", json.dumps({"pid": self.dead(), "born": 1.0}))
            (mine / "tree0").mkdir()
            here = Path.cwd()
            os.chdir(mine / "tree0")
            try:
                assert mutate._collect_abandoned(box) == []
            finally:
                os.chdir(here)
            assert (mine / "tree0").exists()

    def test_a_sibling_tree_of_the_one_we_are_in_is_also_spared(self) -> None:
        """The lane next door. Deleting `tree3` while this probe works in
        `tree5` breaks the sweep exactly as thoroughly, so the veto is on the
        holder rather than on the one directory."""
        with support.tempdir(prefix="tupferl-collect-") as box:
            mine = self.tree(box, "mutate-holder", json.dumps({"pid": self.dead(), "born": 1.0}))
            (mine / "tree3").mkdir()
            (mine / "tree5").mkdir()
            here = Path.cwd()
            os.chdir(mine / "tree5")
            try:
                assert mutate._collect_abandoned(box) == []
            finally:
                os.chdir(here)
            assert (mine / "tree3").exists()

    def test_a_dry_run_names_them_and_removes_nothing(self) -> None:
        """What every ordinary sweep does. Counting is safe under a mutation in
        a way that removing is not, so the count is unconditional and the `rm`
        is `--collect`."""
        with support.tempdir(prefix="tupferl-collect-") as box:
            gone = self.tree(box, "mutate-dry", json.dumps({"pid": self.dead(), "born": 1.0}))
            assert mutate._collect_abandoned(box, dry=True) == [gone]
            assert gone.exists(), "a dry run deleted something"

    def test_an_ordinary_run_says_nothing(self) -> None:
        """Every sweep on a clean machine. A line about temporary directories on
        a run that has none is noise in a tool whose output a person greps."""
        with support.tempdir(prefix="tupferl-collect-") as box:
            assert mutate.about_temporary_trees(collect=False, where=box) == []

    def test_a_run_counts_what_is_waiting_and_offers_the_flag(self) -> None:
        with support.tempdir(prefix="tupferl-collect-") as box:
            self.tree(box, "mutate-a", json.dumps({"pid": self.dead(), "born": 1.0}))
            said = mutate.about_temporary_trees(collect=False, where=box)
            assert len(said) == 1
            assert "1 temporary tree(s)" in said[0]
            assert "--collect" in said[0], "the line has to say how to act on it"

    def test_the_flag_names_each_one_it_removed(self) -> None:
        """Named rather than counted, and this is the one place the two differ:
        a count is a claim and a path is evidence, and this is the run that
        actually deleted something."""
        with support.tempdir(prefix="tupferl-collect-") as box:
            gone = self.tree(box, "mutate-b", json.dumps({"pid": self.dead(), "born": 1.0}))
            said = mutate.about_temporary_trees(collect=True, where=box)
            assert said == [f"collected {gone}"]
            assert not gone.exists()

    def test_an_unstamped_tree_is_reported_with_the_command_that_removes_it(self) -> None:
        """It cannot be collected -- nothing here can tell an old tree from a
        live one -- so the line has to hand the decision to a person, with the
        command in it rather than a description of the command."""
        with support.tempdir(prefix="tupferl-collect-") as box:
            self.tree(box, "mutate-c", None)
            said = mutate.about_temporary_trees(collect=False, where=box)
            assert len(said) == 1
            assert "no owner stamp" in said[0]
            assert f"rm -rf {box}/{SETTINGS.tmp_prefix}*" in said[0]

    def test_collecting_does_not_silence_the_unstamped_ones(self) -> None:
        """Both lines, because `--collect` cannot act on the second kind. A run
        that removed what it could and said nothing about the rest would read as
        having cleaned up."""
        with support.tempdir(prefix="tupferl-collect-") as box:
            self.tree(box, "mutate-d", json.dumps({"pid": self.dead(), "born": 1.0}))
            self.tree(box, "mutate-e", None)
            said = mutate.about_temporary_trees(collect=True, where=box)
            assert len(said) == 2, said
            assert "collected" in said[0]
            assert "no owner stamp" in said[1]

    def test_a_real_run_prints_what_it_found(self) -> None:
        """`main` end to end, because the lines above are only useful if
        something says them and the `print` loop is a line of its own.

        `tempfile.tempdir` is patched rather than `TMPDIR`, because
        `gettempdir()` caches its answer on first use and an environment
        variable set afterwards would change nothing.

        **`--only` names a path nothing matches, and that is not decoration.**
        The first version passed `--base HEAD` alone and leaned on the working
        tree being clean, so it exited early only when nobody had edited
        anything -- green standing alone and red under the runner the moment
        this very tag was added to `tools/verdict.py`, where it started a whole
        real sweep instead. Filtering to an empty table reaches the report and
        exits for a reason the test controls.
        """
        with support.tempdir(prefix="tupferl-collect-") as box:
            gone = self.tree(box, "mutate-said", json.dumps({"pid": self.dead(), "born": 1.0}))
            with (
                mock.patch.object(tempfile, "tempdir", str(box)),
                support.quiet() as spill,
                pytest.raises(SystemExit),
            ):
                mutate.main(["--base", "HEAD", "--only", "no/such/path", "--collect"])
            assert f"collected {gone}" in spill.getvalue()

    def test_stamping_spawns_nothing(self) -> None:
        """**`_stamp` must not go through `_born`**, and this is a macOS bug in
        the shape of a test.

        `_born` reads `/proc` where there is one and falls back to `ps` where
        there is not -- so calling it here put a *subprocess spawn* inside
        `_run`, and `TestWhatEveryProbeIsHandedOnItsCommandLine` patches
        `subprocess.Popen`: on the macos legs the fake intercepted `ps` instead
        of the probe and all seven of that class's tests failed with `'int'
        object has no attribute 'name'`. Every Linux leg was green, because
        there `_born` reads `/proc` and spawns nothing.

        Asserted against `_born` rather than against `subprocess`, so that it
        fails on *this* platform too. A test that watched for a spawn could only
        go red on macOS, which is the leg nobody runs before pushing.
        """
        mutate._stamp.cache_clear()
        try:
            with mock.patch.object(mutate, "_born", side_effect=AssertionError("walked /proc")):
                assert json.loads(mutate._stamp())["pid"] == os.getpid()
        finally:
            mutate._stamp.cache_clear()

    @pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux reads /proc")
    def test_our_own_birth_agrees_with_the_walk(self) -> None:
        """The two are compared with each other -- a stamp against `_born`'s map
        -- so reading them differently would make every stamp look stale and
        every live sweep's tree collectable."""
        assert mutate._my_birth() == mutate._born()[os.getpid()]

    def test_a_birth_that_cannot_be_read_is_none_rather_than_an_error(self) -> None:
        """macOS, where there is no `/proc`. `_collect_abandoned` already treats
        `None` as "the pid alone decides", so the weaker check is reached rather
        than the stamp failing to be written at all."""
        with mock.patch.object(Path, "read_text", side_effect=OSError("no /proc")):
            assert mutate._my_birth() is None

    def test_a_tree_it_makes_is_stamped_before_anything_else_goes_in(self) -> None:
        """A sweep killed a millisecond after `mkdtemp` still has to leave a
        tree that can be identified rather than one that has to be guessed at."""
        with mutate._owned_temp("mutate-") as made:
            assert made.name.startswith(SETTINGS.tmp("mutate-"))
            assert json.loads((made / mutate._OWNER).read_text(encoding="utf-8"))["pid"] == (
                os.getpid()
            )


@pytest.mark.usefixtures("_alarm_put_back")
class TestAHungTestIsBoundedAndNotCredited:
    """A per-test alarm, and the classification that makes it safe.

    **The mark is the other half of #115.** `test_zero_arms_nothing` calls
    `verdict.each_test` in *this* process, and that installs `_ring` as the
    `SIGALRM` handler and leaves it there. Harmless on its own -- nothing arms a
    timer afterwards -- and one half of a two-module failure: with
    `tests/test_support.py` leaking an armed 30s `ITIMER_REAL` and this leaking
    the handler it would fire into, a `SIGALRM` landed in `_ring` some thirty
    seconds later and raised `Hung` inside whatever unrelated test was running.
    Neither module reproduced it alone, which is why it read as shared state
    with no owner.

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
        first: tuple[str, ...] = (),
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

        **And `BOUND` above was the fourth**, three screens from this sentence:
        20 beats the alarm's *default* and loses to `--each-test 10`. Counting
        the instances in prose is what a person does instead of a check, so B5
        wrote the check -- `test_support.TestEveryWaitOnAChildIsBounded`.
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
                    json.dumps(list(first)),
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
            assert report.is_file(), done.stderr[-800:]
            return dict(json.loads(report.read_text(encoding="utf-8")))

    def test_a_hung_test_is_interrupted_rather_than_waited_out(self) -> None:
        """A blocking `read()` on a fifo, which is the shape `tupferl/copies.py`
        hangs in when its not-a-regular-file guard is mutated away. PEP 475
        retries a syscall interrupted by a signal, so this only works because the
        handler *raises* rather than setting a flag."""
        found = self.collect(ALARM)
        assert found["ran"] == 2, "the run did not get past the hung test"

    def test_it_is_never_counted_as_the_test_noticing(self) -> None:
        """The whole safety argument. `noticed` is what `caught` is made of."""
        found = self.collect(ALARM)
        assert found["noticed"] == []
        broke = [str(line) for line in found["broke"]]
        assert len(broke) == 1, broke
        assert "test_hangs_on_a_fifo" in broke[0]
        assert "did not finish" in broke[0]

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
        with pytest.raises(subprocess.TimeoutExpired):
            self.collect(0, wait=3)

    def test_zero_arms_nothing(self) -> None:
        """The same claim without a subprocess at all, because the one above can
        only ever say "it did not finish in three seconds"."""
        assert verdict.each_test(0) == 0.0
        assert verdict.each_test(2) == 2.0


class TestTheCheapPrefix:
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
        assert cache.prefix() == ["tests.m.C.quick", "tests.m.C.rare", "tests.m.C.slow"]

    def test_it_stops_at_the_budget(self) -> None:
        """Every row pays this up front, so it is bounded in seconds rather than
        in tests -- tests do not all cost the same."""
        cache = self.cache(
            {f"k{n}": f"tests.m.C.t{n}" for n in range(20)},
            {f"tests.m.C.t{n}": 0.2 for n in range(20)},
        )
        chosen = cache.prefix()
        assert sum(0.2 for _ in chosen) <= mutate.PREFIX
        assert len(chosen) < 20

    def test_a_test_with_no_measured_cost_is_not_guessed_at(self) -> None:
        """A killer recorded before costs existed has no denominator, and
        inventing one would put it anywhere at all in the order."""
        cache = self.cache({"k": "tests.m.C.unmeasured"}, {})
        assert cache.prefix() == []

    def test_nothing_is_covered_twice(self) -> None:
        """Greedy credits a test only with rows nothing before it caught, or the
        second-best test rides on the first one's coverage and the prefix fills
        with duplicates."""
        cache = self.cache(
            {"a": "tests.m.C.one", "b": "tests.m.C.one", "c": "tests.m.C.two"},
            {"tests.m.C.one": 0.01, "tests.m.C.two": 0.02},
        )
        assert cache.prefix() == ["tests.m.C.one", "tests.m.C.two"]


class TestWhichRowsGetThePrefix:
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
        assert ahead.first == (REAL,)

    def test_a_row_with_nothing_remembered_gets_the_prefix(self) -> None:
        cache = self.cache()
        cache.known = {"someone-else": REAL}
        with support.quiet():
            (ahead,) = cache.ahead_of(self.rows("tests.test_mutate"))
        assert ahead.first == (REAL,)

    def test_the_prefix_is_cut_to_what_the_row_can_reach(self) -> None:
        """A test in a module that does not import the mutated file cannot see
        the mutation, so running it would be pure cost."""
        cache = self.cache()
        cache.known = {"someone-else": REAL}
        with support.quiet():
            (ahead,) = cache.ahead_of(self.rows("tests.test_paths"))
        assert ahead.first == ()


class TestTheCacheLearnsFromARealRun:
    """The plumbing, not the algorithm.

    `TestTheCheapPrefix` sets `cost` by hand, so every one of its assertions
    passed while the harness was recording **zero** costs -- `sweep` re-wrapped
    the report without `times` and the prefix quietly ordered nothing. A test
    that builds its own inputs cannot see a data path that never delivers them,
    which is CLAUDE.md §8's pass nobody can explain.
    """

    def test_a_run_measures_the_tests_it_ran(self) -> None:
        found = swept_once(baseline=True)
        times = found.times or {}
        assert times, "the run recorded no test timings at all"
        # `tests.test_paths` is UNWATCHED's whole selection, so its tests are
        # exactly what should have been measured. Keyed by nodeid, which is what
        # the ids `Killers` orders by are.
        assert any(name.startswith("tests/test_paths.py::") for name in times), sorted(times)[:5]
        assert all(seconds >= 0 for seconds in times.values())

    def test_they_reach_the_cache(self) -> None:
        found = swept_once(baseline=True)
        cache = mutate.Killers(None)
        cache.learn(found)
        assert cache.cost == (found.times or {})


class TestThePrefixReachesTheExpensiveRows:
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
        assert self.ahead(mutate.WHOLE_SUITE).first == (REAL,)

    def test_a_selection_naming_a_class_still_matches_its_tests(self) -> None:
        """`tests.test_mutate.TestX` selects `tests.test_mutate.TestX.test_y`.
        Comparing module names made this never match, so any row selected at
        class granularity silently lost the prefix."""
        assert self.ahead(REAL_CLASS).first == (REAL,)

    def test_a_row_that_cannot_reach_it_still_does_not_pay(self) -> None:
        """The guard the two above must not break: a test in a module that does
        not import the mutated file cannot see the mutation."""
        assert self.ahead("tests.test_paths").first == ()


class TestEverySurvivorHasRunTheWholeSuite:
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
        assert report.widened, "a walked report did not claim the guarantee"

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
            first=("tests.test_hang.TestOne.test_is_fine",),
            names=(),
        )
        # Two tests in that module, and the prefix names one of them -- so it
        # runs twice, once in front and once as discovery reaches it. Anything
        # less than three means the empty selection stopped discovering.
        assert found["ran"] == 3


class TestARowActuallyRunsWithItsPrefix:
    """The gap that let the `WHOLE_SUITE` defect through.

    Every other test here asserts on what `ahead_of` *returns*. None drove
    `_attempt`, so nothing noticed that the argv it built turned "discover
    everything" into "run these three" -- the review found it by reading. These
    drive a real mutation with a real `first`.
    """

    def test_a_prefix_that_catches_it_still_reports_caught(self) -> None:
        one = UNKNOWN_KEY_GUARD._replace(
            first=(
                "tests.test_config.TestRejectingAnUnknownKey"
                ".test_a_typo_is_an_error_rather_than_silence",
            )
        )
        found = mutate.run([one], baseline=False, workers=1, summarise=False, strict=False)
        assert [r.verdict.outcome for r in found.results] == ["caught"]

    def test_a_prefix_that_misses_falls_through_to_the_selection(self) -> None:
        """The safety argument, driven rather than asserted on a data structure:
        a prefix that cannot see the mutation must cost one test, not the
        answer."""
        one = UNKNOWN_KEY_GUARD._replace(first=("tests.test_paths.TestWhereTheRepositoryGoes",))
        found = mutate.run([one], baseline=False, workers=1, summarise=False, strict=False)
        assert [r.verdict.outcome for r in found.results] == ["caught"]

    # The `WHOLE_SUITE` case is *not* driven through `mutate.run` here. It would
    # discover and run this entire suite inside a mutation sandbox -- ~50s, for a
    # claim `TestConfirmationReallyRunsTheWholeSuite` already proves against the
    # probe's own two-test tree in milliseconds. Adding a minute to every run to
    # re-state something is the mistake `test_zero_disables_it` already made
    # once today.


class TestASpecFileGetsTheFlagsItWasGiven:
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

        with (
            support.tempdir(prefix="tupferl-spec-") as box,
            mock.patch.object(mutate, "run", watch),
        ):
            mutate.main([str(self.spec(box)), *flags])
        return seen

    def test_workers_reaches_the_run(self) -> None:
        assert self.asked("--workers", "1")["workers"] == 1

    def test_memory_reaches_the_run(self) -> None:
        assert self.asked("--memory", "0")["memory"] == 0

    def test_the_timeout_reaches_the_run(self) -> None:
        assert self.asked("--timeout", "7")["timeout"] == 7.0

    def test_the_per_test_alarm_reaches_the_run(self) -> None:
        assert self.asked("--each-test", "3")["each"] == 3.0

    def test_no_baseline_reaches_the_run(self) -> None:
        assert not self.asked("--no-baseline")["baseline"]

    def test_the_baseline_is_on_by_default(self) -> None:
        """The other half: a wiring that hard-coded `False` would pass the test
        above and quietly stop checking the untouched tree."""
        assert self.asked()["baseline"]

    def test_json_is_written_and_marked_done(self) -> None:
        """Not through the `run` stub -- this one drives the real thing, because
        the report and its `.done` marker are what a watcher reads and a stub
        cannot produce them."""
        with support.tempdir(prefix="tupferl-spec-") as box:
            report = box / "out.json"
            mutate.main([str(self.spec(box)), "--no-baseline", "--json", str(report)])
            assert report.is_file(), "--json wrote nothing"
            assert "results" in json.loads(report.read_text(encoding="utf-8"))
            assert report.with_suffix(".json.done").is_file(), "the run left no done marker"


class TestWhatASpecFileExitsWith:
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
        with support.tempdir(prefix="tupferl-exit-") as name, support.quiet():
            return mutate.main([str(self.spec(Path(name), old, new)), "--no-baseline", *flags])

    #: A mutation `tests.test_merge` notices, and one it does not. Both were
    #: measured rather than assumed: `PROBE` shrinking to 1 survives, which is
    #: itself a fair finding about that constant and not this test's business.
    CAUGHT = ("WHOLE_FILE = 1", "WHOLE_FILE = 2")
    SURVIVES = ("PROBE = 8000", "PROBE = 1")

    def test_a_caught_table_exits_zero(self) -> None:
        assert self.status(*self.CAUGHT) == 0

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
        with support.tempdir(prefix="tupferl-exit-") as name, support.quiet():
            spec = self.spec(Path(name), *self.SURVIVES)
            with mock.patch.object(mutate, "_attempt", lambda *a, **k: survived):
                assert mutate.main([str(spec), "--no-baseline"]) == 1


class TestWhichKillersNothingBaselined:
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

    def shards(self, *selections: tuple[str, ...]) -> list[tuple[str, ...]]:
        """The shard list `run` builds, in a shape mypy can check.

        Written out, these tests said `mutate._unbaselined(rows,
        ["tests.test_paths"])` -- and `_unbaselined` takes
        `Sequence[Sequence[str]]`, of which a `list[str]` is one. So that
        type-checks clean and hands it a shard covering `t`, `e`, `s` and
        nothing real. When `first` became a sequence three of these eight tests
        went red and five went on passing unchanged, and the five are the
        dangerous half: they assert a killer is *not* covered, which a shard of
        single characters satisfies perfectly.

        `tuple[str, ...]` per shard and not `Sequence[str]`, because a `str` is
        not a `tuple` -- so the shape that was wrong is now a type error at the
        call site rather than a fixture passing for the wrong reason. That is the
        whole of the helper: it splits nothing and joins nothing, which is also
        what lets a caller hand it a parametrized nodeid whole. `WHOLE_SUITE`'s
        shard is `()`, no names rather than one empty name.
        """
        return list(selections)

    def test_a_killer_its_shard_covered_needs_nothing(self) -> None:
        found = self.caught("tests.test_paths.TestA.test_b")
        assert mutate._unbaselined([found], self.shards(("tests.test_paths",))) == []

    def test_a_killer_no_shard_covered_is_returned(self) -> None:
        """The one the walk produces. `UNWATCHED` is exactly this shape in the
        real harness: selected on `tests.test_paths`, caught in `test_config`."""
        found = self.caught("tests.test_config.TestRejectingAnUnknownKey.test_it")
        assert mutate._unbaselined([found], self.shards(("tests.test_paths",))) == [
            "tests.test_config.TestRejectingAnUnknownKey.test_it"
        ]

    def test_the_whole_suite_shard_covers_everything(self) -> None:
        """`WHOLE_SUITE` is the *empty* selection and means "run the lot", so a
        shard list holding one covers every test. Read as a plain string it
        matches nothing instead, and every killer would be re-checked."""
        found = self.caught("tests.test_config.TestRejectingAnUnknownKey.test_it")
        assert mutate._unbaselined([found], self.shards(())) == []

    def test_only_caught_rows_are_asked_about(self) -> None:
        """A survivor has no killer to stand behind, and a `broke` row's is not
        an answer. Without the outcome check a stale `killer` on either would
        send the run off to baseline a test that decided nothing."""
        survivor = mutate.Result(row(), mutate.Verdict("survived"))
        broke = mutate.Result(row(), mutate.Verdict("broke", "d", "tests.test_config.T.t"))
        covered = self.shards(("tests.test_paths",))
        assert mutate._unbaselined([survivor, broke], covered) == []

    def test_a_class_shard_still_covers_its_own_tests(self) -> None:
        """`run_tests.selects` rather than comparing module names: a shard naming
        a class never matched at all when this was spelled by hand, and every row
        it caught was sent for re-baselining."""
        found = self.caught("tests.test_config.TestRejectingAnUnknownKey.test_it")
        covered = self.shards(("tests.test_config.TestRejectingAnUnknownKey",))
        assert mutate._unbaselined([found], covered) == []

    def test_one_whole_suite_shard_among_several_covers_everything(self) -> None:
        """`any`, not `all`. With a single shard the two agree, which is why the
        test above cannot tell them apart: `all` over one element *is* `any` over
        it. Given a `WHOLE_SUITE` shard beside a narrow one -- the shape every
        table with a file nothing imports produces -- `all` is False, the guard
        does not fire, and every killer is sent off to be re-baselined against a
        run that already covered it.
        """
        found = self.caught("tests.test_config.TestRejectingAnUnknownKey.test_it")
        mixed = self.shards((), ("tests.test_paths",))
        assert mutate._unbaselined([found], mixed) == []

    def test_a_killer_covered_by_one_shard_of_several_needs_nothing(self) -> None:
        """The second `any`, and the same trap. A killer is baselined if *some*
        shard ran it, not if every shard did -- and a table always has several.
        Read as `all`, a killer in `test_config` is called uncovered because
        `test_paths` did not also run it, so every caught row in a multi-shard
        sweep would drag the run into a re-baseline it does not need.
        """
        found = self.caught("tests.test_config.TestRejectingAnUnknownKey.test_it")
        several = self.shards(("tests.test_paths",), ("tests.test_config",))
        assert mutate._unbaselined([found], several) == []

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
        assert mutate._unbaselined([found], self.shards(("tests.test_sync",))) == [
            "tests.test_sync_cli.TestTheRemoteLine.test_it"
        ]


class TestARowCaughtByAnUnbaselinedTest:
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
        assert "caught a row without being baselined" in self.said
        assert "1 test(s)" in self.said

    def test_it_says_when_the_check_came_back_red(self) -> None:
        """The other line, and the more important one: a row silently demoted to
        `broke` with nothing said about why reads as the harness malfunctioning
        rather than as the guarantee working."""
        self.report("caught")
        assert "NOT GREEN" in self.said
        assert "reported broke" in self.said

    def test_a_green_check_leaves_the_verdict_alone(self) -> None:
        """The common case, and the one that must stay cheap: the test was not
        baselined, it is green anyway, the row is caught and stays caught."""
        found = self.report("survived")
        assert [r.verdict.outcome for r in found.results] == ["caught"]
        assert found.results[0].verdict.killer == self.KILLER

    def test_a_red_check_refuses_the_verdict(self) -> None:
        """The failure this exists for. Caught by a test that also fails
        untouched is not an answer, and reporting it as one is the false
        `caught` this module is built to make impossible."""
        found = self.report("caught")
        assert [r.verdict.outcome for r in found.results] == ["broke"]
        assert "also fails untouched" in found.results[0].verdict.detail

    def test_the_rest_of_the_run_is_not_voided(self) -> None:
        """Only the rows that test caught. Every other verdict rests on a shard
        that *was* green, and throwing those away would discard answers this
        found nothing wrong with -- which is what setting `baseline_red` would
        do."""
        found = self.report("caught")
        assert not found.baseline_red, "one loose test voided the whole run"

    def test_the_killers_traceback_is_printed_when_the_check_is_red(self) -> None:
        """The diagnosable case, and the easier of the two: the extra shard also
        says what went wrong, so this is corroboration rather than the only
        evidence."""
        self.report("caught")
        assert "dJk9 marker" in self.said

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
        assert "dJk9 marker" in self.said
        assert f"{self.KILLER} caught 1 row(s)" in self.said


class TestWhatAnUnbaselinedKillerIsMadeToSay:
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
        assert "t.A.test_x" in head
        assert "caught 3 row(s)" in head
        assert "tupferl/sync.py:7 in f()" in head

    def test_it_prints_one_traceback_and_not_one_per_row(self) -> None:
        """24 copies of one traceback is the noise `Verdict.why`'s docstring
        refuses, and the count above already carries the rest."""
        rows = [self.caught("t.A.test_x", f"tupferl/sync.py:{n} in f()") for n in (7, 8, 9)]
        said = mutate._loose_evidence(rows, ["t.A.test_x"])
        assert len(said) == 2
        assert "\n".join(said).count("boom") == 1

    def test_a_row_with_no_traceback_says_so(self) -> None:
        """A `caught` verdict takes `why` from `verdict.py`'s `reasons`, and that
        list can come back empty -- so this is a state the tool really reaches
        rather than a defensive arm. An empty indented block reads as "the test
        failed for no reason", which is a different claim and a wrong one."""
        said = mutate._loose_evidence(
            [self.caught("t.A.test_x", "x:1 in f()", why="")], ["t.A.test_x"]
        )
        assert "no traceback recorded" in "\n".join(said)
        assert "boom" not in "\n".join(said)

    def test_a_killer_matching_no_row_is_skipped_rather_than_guessed_at(self) -> None:
        """Unreachable from `run`, which passes the names `_unbaselined` derived
        from these same rows. Pinned anyway because the alternative -- a header
        with nothing under it -- would attribute the *next* killer's traceback to
        this one, which is the exact misreading the block exists to prevent."""
        rows = [self.caught("t.A.test_x", "x:1 in f()")]
        assert mutate._loose_evidence(rows, ["t.B.test_gone"]) == []

    def test_each_killer_gets_its_own_entry(self) -> None:
        """Two loose killers is two separate questions. Folding them into one
        block would attribute one test's traceback to the other's rows."""
        rows = [self.caught("t.A.test_x", "x:1 in f()"), self.caught("t.B.test_y", "x:2 in g()")]
        said = mutate._loose_evidence(rows, ["t.A.test_x", "t.B.test_y"])
        assert len(said) == 4
        assert "t.A.test_x" in said[0]
        assert "t.B.test_y" in said[2]


class TestMovingTheKillerToTheFront:
    """`Learned`: whatever caught the last row goes first on the next.

    The only ordering mechanism in the file that learns *during* a run.
    `Killers.known` is keyed on the mutation's text, so it misses by
    construction on `--base main`, whose rows are new lines; `Killers.prefix()`
    is computed once before the table starts. Neither looks at the fact that
    consecutive rows sit in the same function, which is what this is for --
    measured at 27-42% same killing test as the previous row, against 1-3% by
    chance.
    """

    def row(self, tests: str = "tests.test_sync", first: Sequence[str] = ()) -> Mutation:
        return row()._replace(tests=tests, first=first)

    def test_the_last_killer_comes_first(self) -> None:
        learned = mutate.Learned()
        learned.saw("tests.test_sync.TestTheDecisionTable.test_it")
        assert learned.ahead(self.row()) == ("tests.test_sync.TestTheDecisionTable.test_it",)

    def test_the_newest_wins(self) -> None:
        """Move-to-*front*, not append. Without the reordering this is a queue,
        and a queue hands back the oldest killer first -- which is the one the
        walk has moved furthest away from."""
        learned = mutate.Learned()
        for name in ("a", "b", "c"):
            learned.saw(f"tests.test_sync.T.test_{name}")
        assert learned.ahead(self.row()) == (
            "tests.test_sync.T.test_c",
            "tests.test_sync.T.test_b",
            "tests.test_sync.T.test_a",
        )

    def test_seeing_one_again_moves_it_rather_than_repeating_it(self) -> None:
        """The move half of move-to-front. Appending a duplicate would spend a
        slot on a test already in the list and push a real one off the end."""
        learned = mutate.Learned()
        for name in ("a", "b", "a"):
            learned.saw(f"tests.test_sync.T.test_{name}")
        assert learned.ahead(self.row()) == ("tests.test_sync.T.test_a", "tests.test_sync.T.test_b")

    def test_it_is_bounded(self) -> None:
        """Or it grows to the size of the suite, and every row pays for the whole
        of it before reaching its own selection -- a second `prefix()` with no
        budget."""
        learned = mutate.Learned(keep=3)
        for index in range(10):
            learned.saw(f"tests.test_sync.T.test_{index}")
        assert len(learned.ahead(self.row())) == 3

    def test_a_test_the_row_cannot_reach_is_not_offered(self) -> None:
        """The guard `Killers.ahead_of` gives: a test in a module that does not
        import the mutated file cannot see the mutation, so running it first is
        pure cost."""
        learned = mutate.Learned()
        learned.saw("tests.test_paths.T.test_it")
        assert learned.ahead(self.row(tests="tests.test_sync")) == ()

    def test_a_whole_suite_row_reaches_everything(self) -> None:
        """`WHOLE_SUITE` is the *empty* selection and means "run the lot", so
        every learned test is reachable from it. Read as a plain string it
        matches nothing and the row is offered none of them."""
        learned = mutate.Learned()
        learned.saw("tests.test_paths.T.test_it")
        assert learned.ahead(self.row(tests=mutate.WHOLE_SUITE)) == ("tests.test_paths.T.test_it",)

    def test_what_the_row_already_remembers_is_not_repeated(self) -> None:
        """`first` runs in order, so naming a test twice costs a run and buys
        nothing."""
        learned = mutate.Learned()
        learned.saw("tests.test_sync.T.test_it")
        assert learned.ahead(self.row(first=("tests.test_sync.T.test_it",))) == ()

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
        assert learned.recent == [], "an empty killer took a slot"


class TestWhatAnOutcomeMeans:
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
        assert set(mutate.MEANING) == set(typing.get_args(mutate.Outcome)), (
            "MEANING and Outcome have drifted apart"
        )

    def test_only_a_real_verdict_counts_as_answered(self) -> None:
        """`caught` and `survived` say something about the tests. `broke` and
        `timeout` are the run failing to put the question, and folding either
        into an answer is the false `caught` this module exists to prevent."""
        answered = {name for name, what in mutate.MEANING.items() if what.answered}
        assert answered == {"caught", "survived"}

    def test_only_a_caught_row_leaves_a_sweep_clean(self) -> None:
        """A survivor is the finding; a broken or timed-out row is a question
        never put. Neither may report the table as done -- it would claim the
        table was complete while it was smaller than it looked."""
        clean = {name for name, what in mutate.MEANING.items() if what.clean}
        assert clean == {"caught"}

    def test_a_non_answer_is_not_evidence_that_a_line_ran(self) -> None:
        """What `tools/reached.py` reads it for: it crosses survivors with
        coverage, and a row that never got to ask is not evidence its line was
        executed."""
        usable = {name for name, what in mutate.MEANING.items() if what.usable}
        assert usable == {"caught", "survived"}

    def test_the_readers_go_through_the_table(self) -> None:
        """The point of the table is that nothing keeps its own copy. Asserted on
        the real properties rather than on `MEANING` alone, or the table could be
        right while every reader ignored it."""
        for outcome, what in mutate.MEANING.items():
            verdict = mutate.Verdict(outcome, "d")  # type: ignore[arg-type]
            assert verdict.answered == what.answered, outcome
            assert mutate.Report([mutate.Result(row(), verdict)]).clean == what.clean, outcome

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
            assert verdict.answered, "`answered` is not reading the table"

    def test_clean_follows_a_new_outcome(self) -> None:
        verdict = mutate.Verdict(self.INVENTED, "d")  # type: ignore[arg-type]
        with self.imagined():
            assert mutate.Report([mutate.Result(row(), verdict)]).clean, (
                "`clean` is not reading the table"
            )

    def test_reached_follows_a_new_outcome(self) -> None:
        with self.imagined():
            assert reached.Row("l", "p.py", 1, self.INVENTED).answered, (
                "`reached` is not reading the table"
            )

    def test_every_outcome_says_what_colour_it_is(self) -> None:
        """The fifth column, and the one a reader uses *before* reading a word.
        An outcome with no colour is an outcome that looks like every other line
        in a screen of nine hundred."""
        assert mutate.MEANING, "there are no outcomes to check"
        for outcome, what in mutate.MEANING.items():
            assert re.match(r"^\x1b\[[0-9;]+m$", what.colour), f"{outcome}: {what.colour!r}"

    def test_the_two_real_verdicts_do_not_share_one(self) -> None:
        """`caught` and `SURVIVED` are the good news and the finding, and the
        whole value of the channel is telling them apart at a glance."""
        assert mutate.MEANING["survived"].colour != mutate.MEANING["caught"].colour

    def test_an_outcome_that_forgets_to_say_claims_nothing(self) -> None:
        """The default, chosen the way `reached.Row.answered` chooses its own:
        an outcome this build has never heard of gets the colour that makes no
        claim, rather than the one that says everything is fine."""
        invented = mutate.Meaning("FUTURE", answered=True, clean=True, usable=True)
        assert invented.colour == paint.QUIET

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
        assert self.MAGENTA in said.getvalue(), "the survivor list keeps its own colour"

    def test_reached_reads_the_table_for_colour_too(self) -> None:
        """The fifth reader, in the other module. It already imports `MEANING`
        for `answered`; the colour comes from the same row."""
        survivor = reached.Row("l", "p.py", 1, "survived")
        with self.repainted(), support.quiet(terminal=True) as said:
            reached._summarise([survivor], reached.Split([survivor], []), 0)
        assert self.MAGENTA in said.getvalue(), "reached keeps its own colour"

    def test_reached_reads_the_same_table(self) -> None:
        """The fourth copy, in another module. It imports `MEANING` rather than
        spelling the outcomes again, and an outcome this build does not know is
        read conservatively -- a report from a newer `mutate` is not evidence."""
        for outcome, what in mutate.MEANING.items():
            seen = reached.Row("l", "p.py", 1, outcome)
            assert seen.answered == what.usable, outcome
        assert not reached.Row("l", "p.py", 1, "from-the-future").answered


class TestTheParagraphAPullRequestQuotes:
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
        assert "2 survived" in said
        assert "a.py:1 in f()" in said
        assert "b.py:2 in g()" in said

    def test_the_selection_is_named_beside_each_label(self) -> None:
        """Which tests the row ran against. Without it a survivor cannot be
        told from a row that was pointed at the wrong suite -- which is the
        error `tools/README.md` says points the expensive way, at rewriting a
        test that was never weak."""
        assert "tests.test_sync" in self.summarised([self.survivor()])

    def test_the_instruction_to_suspect_the_fixture_is_printed(self) -> None:
        """The one line of the paragraph that says what to *do*, and the only
        one that is not derived from the results -- so it is also the one a
        `print`-dropping mutation removes with no other symptom."""
        assert "Suspect the fixture" in self.summarised([self.survivor()])

    def test_a_clean_run_says_none_of_it(self) -> None:
        """The precondition. Without it every assertion above is equally
        satisfied by a function that prints the paragraph unconditionally, and
        `if survivors:` becoming `if True:` would survive all four."""
        said = self.summarised([mutate.Result(row(), mutate.Verdict("caught", ""))])
        assert said == ""

    def test_rows_that_asked_nothing_get_their_own_paragraph(self) -> None:
        """Counted separately and never as survivors -- the error this module
        exists to prevent, one level up. The count, the label and the reason
        are three separate prints and each is a line a reader needs."""
        broke = mutate.Result(row(label="c.py:3 in h()"), mutate.Verdict("broke", "no such name"))
        said = self.summarised([broke])
        assert "1 asked nothing" in said
        assert "c.py:3 in h()" in said
        assert "no such name" in said

    def test_an_unanswered_row_is_not_counted_as_a_survivor(self) -> None:
        """Both paragraphs from one table, so the two counts are visibly about
        different rows. A `broke` row folded into the survivor count is the
        false finding; a survivor folded into the other is the false clean."""
        said = self.summarised(
            [self.survivor(), mutate.Result(row(), mutate.Verdict("timeout", "30s"))]
        )
        assert "1 survived" in said
        assert "1 asked nothing" in said

    def tagged(self, boxes: support.Boxes, body: str, needle: str) -> tuple[Path, mutate.Result]:
        box = boxes.make("tupferl-sum-")
        (box / "tupferl").mkdir()
        (box / "tupferl" / "sync.py").write_text(body, encoding="utf-8")
        at = body.index(needle)
        return box, mutate.Result(
            Mutation(
                "tupferl/sync.py:3 in h() -- x",
                "tupferl/sync.py",
                needle,
                "mutated",
                "tests.test_sync",
                span=(at, at + len(needle)),
                operator="branch",
            ),
            mutate.Verdict("broke", "a fork bomb"),
        )

    def test_an_excused_row_that_asked_nothing_is_counted_not_listed(
        self, boxes: support.Boxes
    ) -> None:
        """The same terms an excused survivor gets, and the reason the record
        covers `broke` at all: a row somebody wrote a reason for should stop
        being one of the rows the sweep asks them to read. Listed, it is noise;
        counted, the number going up is still visible.
        """
        box, broke = self.tagged(
            boxes, "y = 2  # survivor: branch -- cannot be answered\n", "y = 2"
        )
        with support.quiet() as said:
            mutate._summarise([broke], mutate.sort_survivors([broke], box))
        assert "asked nothing" not in said.getvalue()
        assert "1 survivor(s) excused" in said.getvalue()

    def test_an_untagged_row_that_asked_nothing_is_still_listed(self, boxes: support.Boxes) -> None:
        """The precondition. Without it the assertion above is satisfied by a
        `_summarise` that prints nothing at all once a root is passed."""
        box, broke = self.tagged(boxes, "y = 2\n", "y = 2")
        with support.quiet() as said:
            mutate._summarise([broke], mutate.sort_survivors([broke], box))
        assert "1 asked nothing" in said.getvalue()
        assert "tupferl/sync.py:3 in h()" in said.getvalue()


class GeneratedTable:
    """A repository with a real diff in it, and one way to build a table from it.

    **Not a `Test...` class and holding no tests of its own.** Subclassing one
    that *does* makes every test in it run again under the subclass's name --
    `tests/test_gitrepo.py`'s `ConflictedIndex` says the same thing, and this
    file did it anyway: the class below inherited six tests, one of them a
    `git init` with two commits and a full `generated()` run, and its docstring
    said it "inherits nothing else".
    """

    #: On the base, so both subclasses inherit it. `generated` calls
    #: `mutants.generate` and `mutants.cap`, which is two routes into loops a
    #: one-line mutation makes infinite -- `line_starts`' counter and `cap`'s
    #: round-robin. `test_mutants` bounds the classes that call those two
    #: *directly*; these reach them through `generated` and had nothing, which
    #: is CLAUDE.md's recorded mistake for the sixth time: the bound went where
    #: the sweep pointed and the hang was somewhere else. Measured -- four
    #: `cap` rows the gate's control arm reported `caught` came back `BROKE`
    #: here, naming `TestWhatTheGeneratedTableSaysBeforeItRuns` as the killer.
    #:
    #: The honest wait is 0.02s per test, measured, against `PATIENCE`'s 5s and
    #: the harness's 30s alarm -- comfortably between the two, which is what a
    #: bound has to be.
    _bounded = support.bounds(support.PATIENCE, "generating the table hung")

    def repository(self, boxes: support.Boxes) -> Path:
        """Two mutable files whose changed lines differ in number, committed and
        then changed, so `generated` has a real diff to read.

        `wee.py` sorts *after* `many.py`, so path order and size order disagree
        -- without that the fixture cannot tell a sorted table from an
        unsorted one, which is the shape that made an earlier attempt here
        useless (`--limit 40` gave every file two rows, so every ordering
        looked identical).
        """
        box = boxes.make("tupferl-order-")
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
        assert list(grouped) == ["b.py", "c.py", "a.py"]

    def test_every_row_survives_the_ordering(self) -> None:
        """A sort that drops rows would still pass the assertion above. The
        table is a work list before it is an order."""
        table = self.rows({"a.py": 5, "b.py": 1, "c.py": 3})
        assert sorted(
            row.label for rows in mutate.by_size(table).values() for row in rows
        ) == sorted(row.label for row in table)

    def test_a_file_keeps_its_rows_together_and_in_order(self) -> None:
        """Contiguity is load-bearing twice: `sweep.finished` counts a file down
        to zero by relying on it, and `Learned`'s move-to-front rests on
        consecutive rows sitting in the same function. Interleaving them is a
        measured dead end -- see CLAUDE.md."""
        grouped = mutate.by_size(self.rows({"a.py": 3, "b.py": 1}))
        assert [r.label for r in grouped["a.py"]] == ["a.py:0 x", "a.py:1 x", "a.py:2 x"]

    def test_files_of_the_same_size_keep_a_stable_order(self) -> None:
        """Nothing about size separates them, so the answer must not depend on
        dictionary iteration luck -- a table that reorders equal files between
        runs would make two sweeps of one tree incomparable."""
        even = {"c.py": 2, "a.py": 2, "b.py": 2}
        assert list(mutate.by_size(self.rows(even))) == ["a.py", "b.py", "c.py"]

    def test_an_empty_table_is_not_an_error(self) -> None:
        """`--base main` on a diff that touches no mutable file. `generated`
        passes whatever it has."""
        assert mutate.by_size([]) == {}

    def test_the_generated_table_itself_comes_back_smallest_first(
        self, boxes: support.Boxes
    ) -> None:
        """The fix, rather than the rule it uses.

        Every assertion above drives `by_size` directly, so a `generated` that
        never called it would satisfy all of them -- and that is exactly the
        state this issue describes: the rule existed in `sweep` and the plain
        path did not use it.
        """
        table, _ = self.table(self.repository(boxes))
        reached = [path for path, _ in itertools.groupby(row.path for row in table)]
        assert reached == ["tupferl/wee.py", "tupferl/many.py"], (
            "the smaller file did not come first"
        )
        assert len(reached) == 2, "a file's rows were split rather than kept together"


class TestWhatTheGeneratedTableSaysBeforeItRuns(GeneratedTable):
    """`generated`'s filtering and its four printed lines.

    It shares the repository fixture with the ordering test above through
    `GeneratedTable`, which holds no tests of its own -- see that class for what
    went wrong when this one subclassed the test class instead.

    Twenty-two mutations of this function survived a sweep, and the printed
    lines were most of them -- a table built from the wrong files still has rows
    in it, and a cap applied silently reads as "everything was covered".
    """

    def test_only_keeps_every_pattern_that_matches_rather_than_the_overlap(
        self, boxes: support.Boxes
    ) -> None:
        """**Two patterns, and that is the fixture.** With one, `any` and `all`
        agree -- so a filter that required a path to match *every* pattern
        passed, and `--only a --only b` (which the tool's own help offers as
        repeatable) would have produced an empty table and the refusal below.
        """
        box = self.repository(boxes)
        table, _ = self.table(box, only=["wee", "many"])
        assert {row.path for row in table} == {"tupferl/many.py", "tupferl/wee.py"}

    def test_only_drops_what_it_does_not_name(self, boxes: support.Boxes) -> None:
        """The other half: a filter that kept everything passes the test above."""
        box = self.repository(boxes)
        table, _ = self.table(box, only=["wee"])
        assert {row.path for row in table} == {"tupferl/wee.py"}

    def test_a_selection_matching_nothing_refuses_rather_than_runs_empty(
        self, boxes: support.Boxes
    ) -> None:
        """An empty table is a sweep that reports every row caught, because
        there are none -- the green run of nothing again, one tool along."""
        box = self.repository(boxes)
        with pytest.raises(SystemExit) as raised:
            self.table(box, only=["no-such-file"])
        assert "nothing mutable changed" in str(raised.value)

    def test_it_says_how_many_files_lines_and_mutants(self, boxes: support.Boxes) -> None:
        """The header a reader uses to tell a table of three rows from one of
        three hundred before waiting for either."""
        box = self.repository(boxes)
        table, said = self.table(box)
        assert "2 file(s)" in said
        assert f"-> {len(table)} mutants" in said

    def imported(self, boxes: support.Boxes) -> Path:
        """The repository above, plus a test module that imports one of the two.

        Without it every file takes the whole-suite fallback, so `targets_for(
        ...) or WHOLE_SUITE` and the notice beside it are unobservable: the
        fallback is what the fixture produces either way. This is the shape
        CLAUDE.md calls two symmetric inputs -- both files answer the same, so
        which branch ran is not visible.
        """
        box = self.repository(boxes)
        (box / "tests").mkdir()
        (box / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (box / "tests" / "test_wee.py").write_text(
            "from tupferl import wee  # noqa: F401\n", encoding="utf-8"
        )
        return box

    def test_a_row_runs_the_tests_of_whatever_imports_its_file(self, boxes: support.Boxes) -> None:
        """`targets_for(...) or WHOLE_SUITE`. Read as `and`, every row that has
        a real target gets the empty selection instead -- which *is* the
        whole-suite fallback, so the sweep still finishes and takes many times
        as long for no extra signal."""
        table, _ = self.table(self.imported(boxes))
        by_path = {row.path: row.tests for row in table}
        assert by_path["tupferl/wee.py"] == "tests.test_wee"
        assert by_path["tupferl/many.py"] == mutate.WHOLE_SUITE

    def test_the_notice_is_only_for_the_file_nothing_imports(self, boxes: support.Boxes) -> None:
        """The other half of the branch. Printed for every file, the line stops
        distinguishing the slow rows from the ordinary ones -- and it exists
        only to make that difference visible before the wait."""
        _, said = self.table(self.imported(boxes))
        assert "nothing imports tupferl/many.py" in said
        assert "nothing imports tupferl/wee.py" not in said

    def test_a_file_nothing_imports_says_its_rows_run_the_whole_suite(
        self, boxes: support.Boxes
    ) -> None:
        """`targets_for` finds no importer, so the rows fall back to the whole
        suite -- which is much slower and is a fact about the *tree*, not about
        the change. Silence here reads as a normal table.

        The fixture is the repository as it stands: nothing imports either
        module, so both rows take the fallback.
        """
        box = self.repository(boxes)
        table, said = self.table(box)
        assert "nothing imports tupferl/wee.py" in said
        assert all(row.tests == mutate.WHOLE_SUITE for row in table), (
            "the fixture no longer takes the fallback, so the notice proves nothing"
        )

    def test_a_capped_table_says_what_it_dropped_and_from_where(self, boxes: support.Boxes) -> None:
        """A silent cap reads as "everything was covered", and the count looks
        right either way. Per file, because which file lost its rows is what
        decides whether the cap mattered."""
        box = self.repository(boxes)
        whole, _ = self.table(box)
        table, said = self.table(box, limit=2)
        assert len(table) == 2
        assert "--limit 2" in said
        assert f"{len(whole) - 2} not run" in said
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
        assert len(pairs) == 2, "one file cannot show the order of the list"
        assert [path for path, _ in pairs] == sorted(path for path, _ in pairs)
        assert sum(int(count) for _, count in pairs) == len(whole) - 2

    def test_an_uncapped_table_says_nothing_about_a_cap(self, boxes: support.Boxes) -> None:
        """The other half. A line that always appears is one nobody reads."""
        box = self.repository(boxes)
        _, said = self.table(box)
        assert "not run" not in said


class TestABatchSweepEndToEnd:
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

    def repository(self, boxes: support.Boxes) -> Path:
        """A committed base, then one changed line for `--base HEAD` to find."""
        box = boxes.make("tupferl-batch-")
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

    def test_a_batch_run_writes_its_report_and_marks_itself_done(
        self, boxes: support.Boxes
    ) -> None:
        """The whole path: `generated`, `sweep`, the per-file write, the final
        write, and the marker `tools/watch.py --done` waits on."""
        box = self.repository(boxes)
        report = box / "r.json"
        code, said = self.sweep(box, report)
        assert code == 0, said
        written = json.loads(report.read_text(encoding="utf-8"))
        outcomes = [row["outcome"] for row in written["results"]]
        assert outcomes == ["caught"] * len(outcomes), said
        assert {row["path"] for row in written["results"]} == {
            "tupferl/tiny.py",
            "tupferl/zeta.py",
        }, "the batch did not cover both files"

        assert written["widened"], "a swept report dropped the guarantee"
        assert report.with_suffix(".json.done").is_file(), "no done marker"

    def test_a_redirected_sweep_carries_no_escape_codes(self, boxes: support.Boxes) -> None:
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
        box = self.repository(boxes)
        code, said = self.sweep(box, box / "r.json")
        assert code == 0, said
        assert "\x1b" not in said, "a captured run was painted"
        assert "L0 caught    tupferl/tiny.py:" in said

    def test_the_same_sweep_on_a_terminal_is_coloured(self, boxes: support.Boxes) -> None:
        """The half that stops the test above from being satisfied by a tool
        that never colours anything -- which is every version of this code
        before the colour was added, and would be every version after it broke.

        Same fixture, same run, one stream. The only difference is `isatty`.
        """
        box = self.repository(boxes)
        code, said = self.sweep(box, box / "r.json", terminal=True)
        assert code == 0, said
        assert f"{paint.GOOD}caught" in said, "a terminal run was not painted"

    def test_the_colour_does_not_move_the_column(self, boxes: support.Boxes) -> None:
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
        box = self.repository(boxes)
        _, coloured = self.sweep(box, box / "r.json", terminal=True)
        bare = re.sub(r"\x1b\[[0-9;]*m", "", coloured)
        assert "L0 caught    tupferl/tiny.py:" in bare
        # The counter keeps its width too: `[1/8]`, not `[1/8] ` shifted by the
        # escapes that were around it.
        assert re.search(r"\[\d+/8\] L0 caught    tupferl/tiny\.py:", bare), bare

    def test_a_capped_run_says_what_it_did_not_run(self, boxes: support.Boxes) -> None:
        """The one print in `generated` whose absence changes a decision.

        A cap that drops rows silently reads as "everything was covered", and
        the counts underneath look right either way -- they are counts of what
        ran. CLAUDE.md is explicit about this shape, and the code carries a
        comment saying so; neither is a test. Measured before this: both prints
        and both halves of the `if` survived.
        """
        box = self.repository(boxes)
        code, said = self.sweep(box, box / "r.json", extra=["--limit", "2"])
        assert code == 0, said
        assert "--limit 2" in said
        assert "not run" in said
        # The file names, not just a number: a reader deciding whether the cap
        # mattered needs to know *which* file went unswept.
        assert "tupferl/tiny.py" in said
        assert "Counts below are out of what ran" in said

    def test_an_uncapped_run_says_none_of_it(self, boxes: support.Boxes) -> None:
        """The precondition, without which the assertions above are equally
        satisfied by a run that prints the warning unconditionally."""
        box = self.repository(boxes)
        _, said = self.sweep(box, box / "r.json")
        assert "not run" not in said

    def test_it_writes_after_every_row_and_again_at_the_end(self, boxes: support.Boxes) -> None:
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
        box = self.repository(boxes)
        code, said = self.sweep(box, box / "r.json")
        assert code == 0, said
        assert len(self.wrote) >= 3, f"writes: {self.wrote}\n{said}"
        assert self.wrote[0] < self.wrote[-1], f"nothing was written mid-run: {self.wrote}"
        assert max(self.wrote) == self.wrote[-1], "the last write was not the complete one"

        # **A write after every row**, which is the claim "recorded per row"
        # makes and the three assertions above do not: they hold just as well
        # for the per-file version this replaced. Counted against the report's
        # own size rather than against a constant, so the fixture growing a
        # file does not turn this into a test of the number 5.
        written = json.loads((box / "r.json").read_text(encoding="utf-8"))
        rows = len(written["results"])
        assert self.wrote[:rows] == list(range(1, rows + 1)), (
            f"a row landed without a write; {rows} rows, writes {self.wrote}"
        )

    def test_the_pidfile_is_cleared_when_the_run_is_over(self, boxes: support.Boxes) -> None:
        """A stale pid is the false liveness `watch.py` refuses to answer with,
        so the file naming a process that no longer exists must not outlive it."""
        box = self.repository(boxes)
        report = box / "r.json"
        self.sweep(box, report)
        assert not mutate._pidfile(report).is_file(), "the run left its pidfile behind"

    def test_a_red_baseline_reaches_the_report(self, boxes: support.Boxes) -> None:
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
        box = self.repository(boxes)
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
        assert written["baseline_red"], (
            f"a red baseline never reached the report\n{spill.getvalue()}"
        )

    def test_a_real_sweep_feeds_the_killer_forward(self, boxes: support.Boxes) -> None:
        """`Learned` on the real path, which the unit tests above cannot show.

        They drive the class directly; this drives `main`, so it is the only
        thing that proves the lane consults it at all and that a verdict landing
        updates it. Both halves are asserted: something *was* fed forward, and
        the verdicts are unchanged -- an ordering that altered an answer would
        be the one failure this whole file exists to prevent.
        """
        fed: list[tuple[str, ...]] = []
        real = mutate.Learned.ahead

        def watched(inner: mutate.Learned, row: Mutation) -> tuple[str, ...]:
            got = real(inner, row)
            fed.append(got)
            return got

        box = self.repository(boxes)
        report = box / "r.json"
        with mock.patch.object(mutate.Learned, "ahead", watched):
            code, said = self.sweep(box, report)
        assert code == 0, said
        assert any(fed), f"nothing was ever fed forward: {fed}"
        outcomes = [
            row["outcome"] for row in json.loads(report.read_text(encoding="utf-8"))["results"]
        ]
        assert outcomes == ["caught"] * len(outcomes), "the ordering changed an answer"

    def truncated(self, report: Path, keep: int) -> list[str]:
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

    def test_a_crash_mid_file_costs_one_row_and_not_the_file(self, boxes: support.Boxes) -> None:
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
        box = self.repository(boxes)
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
        assert cut is not None, f"the fixture has no colliding pair; see MODULE\n{every}"
        keep = cut

        kept = self.truncated(report, keep)
        self.wrote = []
        code, said = self.sweep(box, report)
        assert code == 0, said

        again = json.loads(report.read_text(encoding="utf-8"))
        assert sorted(row["label"] for row in again["results"]) == sorted(every), (
            f"the resumed report is not the whole table\n{said}"
        )
        # `self.wrote[0]` is the size of the first write of the second run: the
        # rows carried over plus the one just answered. Anything larger means
        # rows were carried that this run did not do and did not keep.
        assert max(self.wrote) - len(kept) == len(every) - len(kept), (
            f"the second run did not re-run exactly the missing rows\n{said}"
        )
        # Summed across the printed lines, because the count is per file and
        # the cut leaves two files partly recorded. Asserting on one line would
        # pass for a run that reported the other as untouched.
        #
        # `-?\d+`, not `\d+`. Without the sign this reads "-2" as 2, and the
        # counter's `+ 1` becoming `- 1` then prints -2 and -1 where 2 and 1
        # belong and still sums to 3. The mutation survived on exactly that.
        assert sum(int(n) for n in re.findall(r"(-?\d+) row\(s\) already recorded", said)) == len(
            kept
        ), f"the skip lines do not account for every recorded row\n{said}"

    def test_the_per_row_writes_are_silent_and_the_last_one_is_not(
        self, boxes: support.Boxes
    ) -> None:
        """`announce`, which nothing asserted: both its mutants survived.

        Since #46 a whole-tree run writes 3124 times, and "wrote N row(s)" after
        every one is the loudest thing in the log while saying the same thing
        each time. Both halves, because either alone is satisfied by a
        `_persist` that never prints or always does.
        """
        box = self.repository(boxes)
        report = box / "r.json"
        code, said = self.sweep(box, report)
        assert code == 0, said
        rows = len(json.loads(report.read_text(encoding="utf-8"))["results"])
        wrote = said.count("wrote ")
        assert wrote < rows, f"a line per row reached the log\n{said}"
        assert wrote >= 1, f"no write was ever announced\n{said}"

    def test_the_skipped_files_are_listed_in_order(self, boxes: support.Boxes) -> None:
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
        box = self.repository(boxes)
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
        assert appeared != sorted(appeared), "the fixture no longer distinguishes the two orders"

        listed = re.findall(r"(\S+): -?\d+ row\(s\) already recorded", said)
        assert len(listed) == 2, f"both files should be listed\n{said}"
        assert listed == sorted(listed), f"the skip lines are not in order\n{said}"

    def test_a_batch_sweep_without_a_report_runs_and_writes_nothing(
        self, boxes: support.Boxes
    ) -> None:
        """`if args.json:` around the per-row write, which every other test here
        satisfies by always passing `--json`.

        Found by the sweep: the guard becoming `if True:` survived, because no
        test drives a batch run without one -- and there it would call
        `_persist(..., None)` and die in `with_name` on a `NoneType`. The rows
        still have to run and the verdicts still have to be reported; the only
        thing absent is the file.
        """
        box = self.repository(boxes)
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
        assert code == 0, said
        assert "caught" in said, f"no row was reported\n{said}"
        assert sorted(box.glob("*.json")) == [], "a run with no --json wrote one anyway"

    def test_a_crash_during_a_write_leaves_the_previous_report(self, boxes: support.Boxes) -> None:
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
        box = self.repository(boxes)
        report = box / "r.json"
        self.sweep(box, report)
        before = report.read_text(encoding="utf-8")

        real = Path.write_text
        opened: list[Path] = []

        def half(target: Path, data: str, encoding: str | None = None, **rest: Any) -> int:
            opened.append(target)
            real(target, data[: len(data) // 2], encoding=encoding)
            raise MemoryError("killed part-way through the write")

        with mock.patch.object(Path, "write_text", half), pytest.raises(MemoryError):
            mutate._persist(mutate.Report([], widened=True), report, announce=False)
        assert report.read_text(encoding="utf-8") == before, "the report was damaged"
        assert report not in opened, "the report itself was opened for writing"
        assert Path.write_text == real, "the patch leaked"

        # And the precondition: a write that *completes* still replaces it, or
        # the assertion above is satisfied by a `_persist` that never writes.
        mutate._persist(mutate.Report([], widened=True), report, announce=False)
        assert json.loads(report.read_text(encoding="utf-8"))["results"] == []

    def tags(self, box: Path) -> int:
        return sum(
            where.read_text(encoding="utf-8").count("# survivor:")
            for where in (box / "tupferl").glob("*.py")
        )

    @pytest.mark.parametrize(
        ("scope", "narrow"),
        [
            (("--base", "HEAD"), ()),
            (("--all",), ()),
            (("--all",), ("--only", "tupferl/zeta.py")),
        ],
    )
    def test_accept_only_ever_adds(
        self, boxes: support.Boxes, scope: Sequence[str], narrow: Sequence[str]
    ) -> None:
        """The invariant that replaced three tests about *when* it was safe to
        delete.

        The hash record's `--accept` dropped every entry a run had not
        generated, so `--base` -- which generates rows for the changed lines
        alone -- reported 206 of this repository's 210 entries stale and would
        have deleted them, with nothing in the output saying so. A whole flag
        (`complete`) existed to decide when that was allowed, and three tests
        existed to pin the flag.

        A tag is a line of source. Nothing here removes one, on any scope; a
        disposition is deleted by deleting code, which is a person's job and
        shows up as one. So the three cases collapse to one assertion made on
        all of them.
        """
        box = self.repository(boxes)
        marked = box / "tupferl" / "zeta.py"
        marked.write_text(
            "# survivor: branch -- a reason nothing in this run reaches\n"
            + marked.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        code, said = self.sweep(box, box / "r.json", extra=["--accept", *narrow], scope=scope)
        assert "a reason nothing in this run reaches" in marked.read_text(encoding="utf-8"), (
            f"a run deleted a written reason\n{said}"
        )
        assert code == 0, said

    def test_accept_does_not_tag_a_row_that_already_has_one(self, boxes: support.Boxes) -> None:
        """`fresh` is *untagged by definition*, so a second `--accept` writes
        nothing. Without that the tags would double on every run and the file
        would grow a comment per sweep, which is the shape a record takes when
        it stops being read."""
        box = self.repository(boxes)
        self.sweep(box, box / "r.json", extra=["--accept"])
        first = self.tags(box)
        self.sweep(box, box / "r2.json", extra=["--accept"])
        assert self.tags(box) == first, "a second --accept tagged the same rows again"

    def test_a_second_run_skips_the_file_already_recorded(self, boxes: support.Boxes) -> None:
        """Resume, which is the reason any of this records per file.

        The second run reaches `if not by_file` with everything already done and
        returns without touching a sandbox -- so it says it is skipping, runs no
        mutant, and still reports the recorded rows rather than an empty sweep.
        Both halves matter: returning early with `[]` would report a clean sweep
        of nothing, which is the flattering direction.
        """
        box = self.repository(boxes)
        report = box / "r.json"
        self.sweep(box, report)
        code, said = self.sweep(box, report)
        assert "already recorded, skipping" in said
        assert "in one pool" not in said, "the second run swept anyway"
        assert code == 0, said
        written = json.loads(report.read_text(encoding="utf-8"))
        assert len(written["results"]) >= 4, "the resume lost rows"


class TestASweepRecordsAsItGoes:
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
        assert fired == [True], "the mid-run callback never ran"
        self.midway = midway[0]
        return done

    def test_the_mid_run_write_does_not_reach_for_the_run_s_own_report(self) -> None:
        with support.tempdir(prefix="tupferl-sweep-") as box:
            report = self.sweep(box)
            assert (box / "out.json").is_file(), "the sweep recorded nothing"
            assert report.widened, "a swept report dropped the guarantee"

    def test_what_it_wrote_claims_the_guarantee(self) -> None:
        """The durable half, asserted on both writes.

        `tools/reached.py` reads this file back and prints a caveat about
        survivors nobody widened, so a `false` here is a claim about the rows
        that outlives the run -- and the mid-run file is the one a sweep killed
        by the machine leaves behind, which is the case the per-file recording
        exists for.
        """
        with support.tempdir(prefix="tupferl-sweep-") as box:
            self.sweep(box)
            assert self.midway["widened"], f"the mid-run write: {self.midway}"
            written = json.loads((box / "out.json").read_text(encoding="utf-8"))
            assert written["widened"], written


class TestASpecFileWithNothingInIt:
    """A script that defines no table and never calls `verify` is a mistake, and
    saying so is the only useful thing left to do.

    Guarded because the branch that reaches it is one `if mutations:` away from
    handing `None` to a function that expects a table -- which the sweep found
    unguarded, and which would report the mistake as a traceback from inside the
    harness rather than as the sentence that says what shape a spec file takes.
    """

    def test_it_says_what_a_spec_file_should_look_like(self) -> None:
        with support.tempdir(prefix="tupferl-empty-") as name:
            where = Path(name) / "spec.py"
            where.write_text("x = 1\n", encoding="utf-8")
            with pytest.raises(SystemExit) as raised:
                mutate.main([str(where)])
        assert "MUTATIONS" in str(raised.value)


class TestWhoOwnsTheMachine:
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

    def meminfo(self, boxes: support.Boxes, text: str | None) -> Any:
        """Point `mutate.MEMINFO` at a file of this test's own, or at nothing.

        `None` is a machine that will not say -- spelled as a path that does not
        exist rather than by patching `_unclaimed`, because the fallback is
        reached through `read_text` raising and that is the arm being claimed.
        """
        box = boxes.make("tupferl-meminfo-")
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

    def budget(
        self, boxes: support.Boxes, available: int | None = None, /, **environment: str
    ) -> int:
        """The budget on a machine with `available` bytes unclaimed, or on one
        whose kernel will not say when `available` is None.

        Positional-only, because `**environment` carries variable names --
        `mutate._TOTAL` among them -- and a keyword parameter beside it is one
        renamed constant away from a caller silently setting this instead of the
        environment. `mypy` says so rather than waiting for it to happen.
        """
        said = None if available is None else self.kernel_says(available // 1024)
        seen = mock.patch.object(mutate, "_visible_memory", lambda: self.VISIBLE)
        with seen, self.meminfo(boxes, said), mock.patch.dict(os.environ, environment, clear=True):
            return mutate._budget()

    def test_a_machine_with_room_gets_what_is_actually_free(self, boxes: support.Boxes) -> None:
        """Not half of what exists. The whole point: an idle machine is measured
        rather than assumed to be half somebody else's."""
        assert self.budget(boxes, 14 << 30) == (14 << 30) - (1 << 30)

    def test_a_busy_machine_gets_less_and_that_is_the_half_that_matters(
        self, boxes: support.Boxes
    ) -> None:
        """The direction a change like this is never tested in. Without it,
        every assertion above is equally satisfied by "always take nearly
        everything", which is the version that gets a laptop OOM-killed."""
        assert self.budget(boxes, 3 << 30) == (3 << 30) - (1 << 30)
        assert self.budget(boxes, 3 << 30) < self.budget(boxes, 14 << 30)

    def test_a_cgroup_limit_still_binds_under_a_roomy_host(self, boxes: support.Boxes) -> None:
        """Inside a container `/proc/meminfo` reports the **host's** numbers, so
        a 2 GiB cgroup on a 62 GiB host reads as 60 available. Taking that at
        face value is the OOM kill `_visible_memory` was written to prevent,
        arriving through a second source of truth."""
        confined = mock.patch.object(mutate, "_visible_memory", lambda: 4 << 30)
        roomy = self.meminfo(boxes, self.kernel_says((60 << 30) // 1024))
        with confined, roomy, mock.patch.dict(os.environ, {}, clear=True):
            assert mutate._budget() == (4 << 30) - (1 << 30)

    def test_a_kernel_that_will_not_say_falls_back_to_halving(self, boxes: support.Boxes) -> None:
        """macOS has no `/proc/meminfo`, and the `macos` CI leg is what proves
        this arm stays reachable -- the same argument `tupferl/config.py`'s
        `tomli` fallback rests on."""
        assert self.budget(boxes, None) == self.VISIBLE // 2

    @pytest.mark.parametrize(
        "said", ["MemAvailable: lots kB", "MemAvailable: 4096", "MemAvailable: 4096 MB"]
    )
    def test_a_malformed_available_line_is_refused(self, boxes: support.Boxes, said: str) -> None:
        """A value that is not a number, or a unit that is not `kB`, is not a
        reading. Falling back to the halving is the honest answer; parsing it
        anyway would size a pool from whatever `int()` happened to accept.

        Three shapes, because a guard of three `and`ed clauses needs an input
        that fails each one on its own -- with only well-formed text, swapping
        an `and` for an `or` survives."""
        with self.meminfo(boxes, said + "\n"):
            assert mutate._unclaimed() == 0, f"{said!r} was read as a number"

    def test_mem_free_is_not_mistaken_for_mem_available(self, boxes: support.Boxes) -> None:
        """An old kernel writes no `MemAvailable`. Reading `MemFree` instead
        would size the pool from whatever the page cache has not taken, which on
        any machine that has read a file is a small number and a wrong one."""
        with self.meminfo(boxes, self.kernel_says(None, free_kb=200)):
            assert mutate._unclaimed() == 0

    def test_a_shared_machine_keeps_half_for_the_person_using_it(
        self, boxes: support.Boxes
    ) -> None:
        """The fallback rule, unchanged, on a machine that cannot be measured."""
        assert self.budget(boxes, None) == self.VISIBLE // 2

    def test_a_ci_runner_is_not_shared(self, boxes: support.Boxes) -> None:
        """Nobody is waiting for their editor on a CI runner, and every CI
        system sets this.

        The gibibyte is written out rather than taken from `mutate._SPARE`:
        against the constant this assertion changes with the code it checks and
        holds for any value of it, which is CLAUDE.md §2's copy-of-the-code by
        name. The sweep found it -- both mutations of `_SPARE` survived.
        """
        assert self.budget(boxes, CI="true") == self.VISIBLE - (1 << 30)

    def test_a_cgroup_limit_means_the_share_is_already_carved_out(
        self, boxes: support.Boxes
    ) -> None:
        """Halving a cgroup limit double-counts the same reservation: the
        container has no other half to leave, because nobody else is in it."""
        with mock.patch.object(mutate, "_confined", lambda: 1 << 30):
            assert self.budget(boxes) == self.VISIBLE - (1 << 30)

    def test_being_told_beats_both(self, boxes: support.Boxes) -> None:
        asked = 12 << 30
        assert self.budget(boxes, **{mutate._TOTAL: str(asked)}) == asked
        assert self.budget(boxes, CI="true", **{mutate._TOTAL: str(asked)}) == asked

    @pytest.mark.parametrize("said", ["", "0", "-1", "lots"])
    def test_nonsense_in_the_variable_is_ignored_rather_than_obeyed(
        self, boxes: support.Boxes, said: str
    ) -> None:
        assert self.budget(boxes, **{mutate._TOTAL: said}) == self.VISIBLE // 2

    def test_a_tiny_dedicated_machine_never_drops_under_the_floor(self) -> None:
        """Otherwise subtracting `_SPARE` hands it less than one lane's ceiling
        and it gets *fewer* lanes than the shared rule would have given -- the
        opposite of the point."""
        tiny = mock.patch.object(mutate, "_visible_memory", lambda: (1 << 30) + (1 << 20))
        with tiny, mock.patch.dict(os.environ, {"CI": "true"}, clear=True):
            assert mutate._budget() == mutate._FLOOR

    def test_the_run_says_which_rule_it_used(self, boxes: support.Boxes) -> None:
        """A lane count nobody can account for is what sent this author reading
        `_share` in the first place.

        Four rules now, and each names itself. The measured one says the number
        it measured: "3 lanes" is a mystery, "3072 MiB unclaimed" is a machine
        with something else running on it, and the difference is the whole
        reason this line exists.
        """
        busy = self.meminfo(boxes, self.kernel_says((3 << 30) // 1024))
        with busy, mock.patch.dict(os.environ, {}, clear=True):
            assert "unclaimed" in mutate._why()
            assert str(3 << 10) in mutate._why(), "it does not say how much"
        silent = self.meminfo(boxes, None)
        with silent, mock.patch.dict(os.environ, {}, clear=True):
            assert "shared" in mutate._why()
        with self.meminfo(boxes, None), mock.patch.dict(os.environ, {"CI": "true"}, clear=True):
            assert "dedicated" in mutate._why()
        with mock.patch.dict(os.environ, {mutate._TOTAL: "123"}, clear=True):
            # `mutate._TOTAL`, not the literal. This asserted `"--budget"` and
            # passed for a release, naming a flag the parser has never had --
            # so someone reading the printed line and typing it got
            # "unrecognized arguments". A test that pins prose has to pin it
            # against the thing it describes, or it guards the wrong name just
            # as firmly as the right one.
            assert mutate._why() == mutate._TOTAL


class TestReadingACgroupLimit:
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
        assert self.confined(str(2 << 30)) == 2 << 30

    def test_cgroup_v2_writes_max_for_no_limit(self) -> None:
        assert self.confined("max") == 0

    def test_cgroup_v1_writes_a_sentinel_near_two_to_the_sixty_three(self) -> None:
        """Which is what this machine's `memory.limit_in_bytes` actually holds:
        9223372036854771712. Read as a limit it would look like the largest
        dedicated machine ever built."""
        assert self.confined("9223372036854771712") == 0


class TestWhenTheMachineCannotSayHowBigItIs:
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
            assert mutate._confined() == 0

    def test_no_cgroup_file_means_the_host_bounds_us(self) -> None:
        with mock.patch("pathlib.Path.read_text", side_effect=OSError):
            assert mutate._confined() == 0

    def test_a_shared_machine_is_reported_as_the_empty_string(self) -> None:
        """`dedicated` returns a *reason*, and "no reason" has to be falsy for
        `_budget` to read it. `None` would work there and break the line that
        prints it."""
        alone = mock.patch.object(mutate, "_confined", lambda: 0)
        with mock.patch.dict(os.environ, {}, clear=True), alone:
            assert mutate.dedicated() == ""

    def test_a_budget_of_one_byte_is_still_a_budget(self) -> None:
        """The boundary on `int(said) > 0`. A fixture using a comfortable number
        cannot tell that from `> 1`."""
        with mock.patch.dict(os.environ, {mutate._TOTAL: "1"}, clear=True):
            assert mutate._budget() == 1


class TestEveryReaderGivesTheFourFieldsThatDecideMembership:
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
        assert me.parent == os.getppid()
        assert me.group == os.getpgrp()
        assert me.resident > 0, "no resident memory read for this process"


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="there is no /proc here")
class TestWhereThereIsAProc:
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
        assert me.resident > 0
        assert me.address > me.resident, "address space is not above resident"

    def test_the_group_is_read_where_a_session_would_not_do(
        self, request: pytest.FixtureRequest
    ) -> None:
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
        request.addfinalizer(child.wait)
        request.addfinalizer(child.kill)
        seen = mutate._from_proc()[child.pid]
        assert seen.group == child.pid, "the process group was read from another field"
        assert seen.group != os.getsid(0), "the fixture cannot tell the two apart"

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
        assert max(mine.resident, theirs.resident) / min(mine.resident, theirs.resident) < 2.0, (
            f"proc says {mine.resident} resident and ps says {theirs.resident}"
        )

    #: Comfortably above one sampling interval and far below the harness's own
    #: 30s per-test alarm, which is the bound `tests/test_watch.py` learned to
    #: check a test's own timeout against.
    PATIENCE = 8.0

    def test_a_lane_nobody_kills_is_still_measured(self, request: pytest.FixtureRequest) -> None:
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
        request.addfinalizer(child.wait)
        request.addfinalizer(child.kill)
        request.addfinalizer(mutate._WATCHED.forget)
        mutate._WATCHED.forget()
        mutate._WATCHED.watch(child.pid, 64 << 30)
        request.addfinalizer(functools.partial(mutate._WATCHED.release, child.pid))

        deadline = time.monotonic() + self.PATIENCE
        while not mutate._WATCHED.widest() and time.monotonic() < deadline:
            time.sleep(mutate._SAMPLE / 4)
        assert mutate._WATCHED.widest() > 0, "a live lane was watched and never measured"

    def test_the_two_readers_agree_on_parentage(self) -> None:
        mine = mutate._from_proc()[os.getpid()]
        theirs = mutate._from_ps()[os.getpid()]
        assert theirs.parent == mine.parent
        assert theirs.group == mine.group


class TestWhatPsIsAskedFor:
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
        assert listed.returncode == 0, listed.stderr
        return listed.stdout

    def test_the_four_fields_are_read_from_real_output(self) -> None:
        table = mutate._parse_ps(self.ran("pid", "ppid", "pgid", "rss"))
        assert os.getpid() in table, "ps produced no process table"
        assert table[os.getpid()].parent == os.getppid()
        assert table[os.getpid()].resident > 0

    def test_no_address_space_is_invented(self) -> None:
        """0 rather than a guess, so `_report_headroom` stays silent where there
        is no enforced ceiling to compare against."""
        assert mutate._parse_ps(self.ran("pid", "ppid", "pgid", "rss"))[os.getpid()].address == 0

    def test_a_fifth_column_is_refused_rather_than_misread(self) -> None:
        """`vsz` is not asked for, so a row carrying it is not this reader's
        output. Taking the first four fields of it anyway would be reading a
        format nobody promised."""
        assert mutate._parse_ps("7 1 7 2048 4096\n") == {}

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
        assert mutate._parse_ps("PID PPID PGID RSS VSZ\nnot a process\n") == {}
        assert mutate._parse_ps("123 456 789 abc def\n") == {}
        assert mutate._parse_ps("123 456 789 1024 zzz\n") == {}

    def test_the_kibibytes_are_multiplied_and_not_divided(self) -> None:
        """`ps` reports KiB. A divide leaves a number that is still positive,
        which is all an assertion of "greater than zero" can see -- so this
        pins the arithmetic against a known input instead."""
        assert mutate._parse_ps("7 1 7 2048\n")[7].resident == 2048 * 1024


class TestWhatMayBeSignalled:
    """`_permitted`: the guard that stopped #91 from being repeatable.

    A sweep mutates its own source and runs it, so `_lane` -- which decides
    *what gets killed* -- is itself under test. Mutated from `row.group ==
    leader` to `!=` it returns every process the user owns except the lane, and
    `_end_lane` sent SIGKILL to all of them. A real desktop session died.

    The guard cannot be a better membership walk, because the walk is the thing
    being mutated. It is a second fact read somewhere else: a process older than
    this one cannot be in a lane this one started.
    """

    def permitted(self, members: list[int], born: dict[int, float]) -> list[int]:
        with mock.patch.object(mutate, "_born", lambda: dict(born)):
            return mutate._permitted(members)

    def test_a_process_older_than_this_one_is_refused(self) -> None:
        """**#91 in one assertion.** The desktop, the browser and the shell all
        started hours before the sweep; the lane started after it."""
        mine = os.getpid()
        got = self.permitted(
            [4321, 4322, 999],
            {mine: 5000.0, 4321: 5001.0, 4322: 5002.0, 999: 12.0},
        )
        assert got == [4321, 4322], "something older than the sweep was permitted"

    def test_this_process_is_never_signalled(self) -> None:
        """Its own start time is equal to its own, so `>=` alone would let the
        harness kill itself -- and `_end_lane`'s `killpg` had the same hole."""
        mine = os.getpid()
        assert self.permitted([mine], {mine: 5000.0}) == []

    def test_the_same_tick_counts_as_ours(self) -> None:
        """`>=` and not `>`, and this is the only input that tells them apart.
        A tick is 10ms and a `Popen` is well inside one, so a lane born in the
        sweep's own tick is the sweep's."""
        mine = os.getpid()
        assert self.permitted([7], {mine: 5000.0, 7: 5000.0}) == [7]

    def test_a_pid_that_has_gone_is_still_permitted(self) -> None:
        """It exited between the walk and here. `os.kill` on it is an `OSError`
        the caller already suppresses; refusing it would be the guard deciding
        something it has no evidence about."""
        mine = os.getpid()
        assert self.permitted([7], {mine: 5000.0}) == [7]

    def test_an_unreadable_self_refuses_nothing(self) -> None:
        """No `/proc/self`, or a `ps` that did not run. Refusing everything
        would leave a fork storm alive, which is the failure `_Lanes` exists to
        stop -- so the guard stands down rather than inverting into a different
        hazard. It is the behaviour that existed before it."""
        assert self.permitted([1, 2, 3], {}) == [1, 2, 3]

    def test_it_reads_the_clock_itself_rather_than_the_process_table(self) -> None:
        """The independence is the design. `_processes` is what `_lane` walks,
        so a guard reading the same table could be disabled by the same
        mutation -- and `_from_proc`'s `drop-assign` row, which empties that
        table, is one of the rows this exists to survive."""
        with (
            mock.patch.object(mutate, "_processes", lambda: {}),
            mock.patch.object(mutate, "_born", lambda: {os.getpid(): 5000.0, 7: 12.0}),
        ):
            assert mutate._permitted([7]) == []


class TestWhichClockIsRead:
    """`_born`'s dispatch: `/proc` where there is one, `ps` where there is not."""

    def test_it_prefers_proc_where_there_is_one(self) -> None:
        with (
            mock.patch.object(mutate, "_born_from_proc", lambda: {11: 1.0}),
            mock.patch.object(mutate, "_born_from_ps", lambda: {22: 2.0}),
        ):
            got = mutate._born()
        # The assertion is conditional rather than skipped, because the macos
        # job runs `--no-skips`: on a machine with no `/proc` the other reader
        # is the right answer and this test still says something true.
        expected = {11: 1.0} if Path("/proc/self/stat").exists() else {22: 2.0}
        assert got == expected

    def test_it_falls_back_to_ps_where_there_is_none(self) -> None:
        """The macOS half, driven on every platform by taking `/proc` away."""
        with (
            mock.patch.object(Path, "exists", lambda self: False),
            mock.patch.object(mutate, "_born_from_ps", lambda: {22: 2.0}),
        ):
            assert mutate._born() == {22: 2.0}


class TestTheKillListIsVettedForReal:
    """`_end_lane` end to end, against the exact list #91's mutant produces.

    Every other test here decides; this one *signals*. It is safe to run
    because `members` is a two-element list of processes this test started, so
    a guard that failed open would kill only those two -- where the defect it
    guards killed everything the user owned.
    """

    #: Long enough that neither child can exit on its own before the assertions,
    #: and far below the harness's 30s per-test alarm.
    ALIVE = 30

    def sleeper(self, request: pytest.FixtureRequest) -> subprocess.Popen[bytes]:
        child = subprocess.Popen(
            [sys.executable, "-c", f"import time; time.sleep({self.ALIVE})"],
            start_new_session=True,
        )
        request.addfinalizer(child.wait)
        request.addfinalizer(child.kill)
        return child

    def alive(self, child: subprocess.Popen[bytes]) -> bool:
        """Whether `child` is still running, without reaping it."""
        return child.poll() is None

    def test_it_never_signals_its_own_process_group(self) -> None:
        """The other half of the kill, and the half `_permitted` cannot reach.

        `_end_lane` opens with `killpg(leader, SIGKILL)`, and `leader` does not
        come from `_lane` -- so the per-pid guard never sees it. A leader equal
        to this process's own group would take the sweep, the shell that started
        it and every sibling lane with it. `killpg` is mocked because the claim
        is about *which* group is asked for, and a fixture that got it wrong
        would kill the suite proving it.
        """
        with mock.patch("os.killpg") as never:
            mutate._end_lane(os.getpgrp(), [])
        never.assert_not_called()

    def test_it_does_signal_a_group_that_is_not_its_own(self) -> None:
        """The other side, without which "never call killpg" would pass."""
        with mock.patch("os.killpg") as called:
            mutate._end_lane(os.getpgrp() + 1, [])
        called.assert_called_once_with(os.getpgrp() + 1, signal.SIGKILL)

    def test_something_older_than_the_sweep_survives_being_named(
        self, request: pytest.FixtureRequest
    ) -> None:
        """**#91, driven rather than argued.** The mutant's `_lane` returns
        processes the sweep never started; this hands `_end_lane` exactly such a
        list and watches the bystander live.

        The two children differ only in when they started, which is the one
        fact the guard reads -- so a guard that read anything else would fail
        this, and a guard that read nothing would kill both.
        """
        bystander = self.sleeper(request)
        # `_born` is read fresh inside `_end_lane`, so the fake has to place
        # this process's own start *after* the bystander's and before the lane's
        # -- which is what a real sweep looks like from the bystander's side.
        real = mutate._born()
        assert bystander.pid in real, "the reader could not see the bystander"
        lane = self.sleeper(request)
        fake = {os.getpid(): 500.0, bystander.pid: 100.0, lane.pid: 900.0}
        with mock.patch.object(mutate, "_born", lambda: dict(fake)):
            mutate._end_lane(lane.pid, [bystander.pid, lane.pid])
        lane.wait(timeout=support.PATIENCE)
        assert self.alive(bystander), "a process older than the sweep was killed"
        assert not self.alive(lane), "the lane itself was not killed"


class TestWhenEachProcessStarted:
    """`_born`, and the two readers behind it. Both are exercised on every
    platform for the reason `_processes` gives: the fallback must not be
    discovered to be broken on the machine that has nothing else."""

    def test_this_process_is_in_the_answer_and_so_is_its_parent(self) -> None:
        """Driven against the real machine rather than a fixture, because the
        claim is that the field being read is the one meant."""
        born = mutate._born()
        assert os.getpid() in born, "the reader did not find this process"
        assert os.getppid() in born, "the reader did not find its parent"

    def test_the_processes_do_not_all_report_the_same_instant(self) -> None:
        """**The field is a start time, and this is what says so.**

        `/proc/<pid>/stat` field 21 is `itrealvalue`, one index below the one
        this reads, and the kernel has reported it as 0 for every process since
        2.6. Read by mistake, every comparison in `_permitted` becomes
        `0 >= 0` -- so the guard permits everything and #91 is back with a test
        suite that still passes. Measured: that off-by-one survived every other
        test in this class.

        Asserted as "the values differ" rather than against a known instant,
        because the unit is not the same on both readers and only the ordering
        is ever used.
        """
        born = mutate._born()
        assert len(set(born.values())) > 1, "every process reports the same start time"

    def test_a_child_started_now_is_not_older_than_this_process(
        self, request: pytest.FixtureRequest
    ) -> None:
        """The ordering, which is the only property `_permitted` uses. A weaker
        test -- that the numbers merely differ -- would pass against a reader
        that had picked the wrong field entirely."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3)"])
        request.addfinalizer(child.wait)
        request.addfinalizer(child.kill)
        born = mutate._born()
        assert child.pid in born
        assert born[child.pid] >= born[os.getpid()]
        # And strictly later than the oldest process on the machine. "The
        # values differ" is not enough on its own: `/proc/<pid>/stat` field 20
        # is `num_threads`, which also differs between processes and is not a
        # time at all -- read by mistake, a one-threaded child compares equal to
        # a one-threaded init and the guard permits it. Measured: that
        # off-by-one survived every other test in this class.
        if 1 in born:  # absent only where this user cannot see init
            assert born[child.pid] > born[1], "a child is not younger than init"

    def test_the_ps_reader_parses_what_ps_prints(self) -> None:
        """macOS's half, driven from text on every platform. The date shape is
        `ps -o lstart=`'s, which is `%c`-like and fixed by BSD `ps`."""
        got = mutate._parse_lstart("  431 Sat Aug 30 17:28:03 2026\n 9999 Sat Aug 30 17:28:04 2026")
        assert sorted(got) == [431, 9999]
        assert got[431] < got[9999], "the two instants did not order"

    def test_the_ps_reader_reads_this_machine_s_own_ps(self) -> None:
        """**Driven on real `ps`, and the fixture above is why that matters.**

        `lstart` prints the day and month through the *process* locale while
        `strptime` reads them through *Python's*, which is C. On this machine
        real `ps` says `Fr Aug 28 18:41:02 2026` -- German -- and every line was
        refused, so the parser returned nothing, `_born` returned nothing and
        `_permitted` refused nothing. The guard stood down in silence on any
        machine whose operator does not speak English.

        The hand-written fixture could not see it, because the person who wrote
        it wrote English. `_born_from_ps` pins `LC_ALL=C` and this drives the
        fork to prove it, which is CLAUDE.md §2's "prefer driving the real
        thing".

        Linux `ps` supports `lstart` too, so this runs everywhere rather than
        only on the platform that needs it -- the fallback must not be
        discovered to be broken on the machine that has nothing else.
        """
        born = mutate._born_from_ps()
        assert os.getpid() in born, "real ps output parsed to nothing"
        assert len(born) > 1

    def test_a_line_it_cannot_read_is_skipped_rather_than_raising(self) -> None:
        """An unreadable date must not stop a lane being killed -- it must only
        stop this vetoing the kill. A header line, a locale this cannot parse,
        and a pid column with no date all take that path."""
        assert mutate._parse_lstart("  PID STARTED\n  431 not a date at all\n") == {}


class TestTheLogIsReadableWhileItIsBeingWritten:
    """`main` asks for line buffering, so a detached sweep says something.

    A stream that is not a terminal is *block* buffered, and every documented
    way of running a sweep redirects to a log -- so an unflushed header arrives
    in 8 KiB steps of about a hundred rows. `_attempt` already passes
    `flush=True` for its progress line, and its comment records a sweep whose
    log "sat empty for five minutes because its header was 250 bytes and
    nothing had flushed it". The header prints never got the same treatment,
    and a sweep launched detached did it again: six minutes of empty log while
    forty lanes worked.

    `tools/watch.py` exists because silence reads identically to progress, so
    this is not cosmetic -- it is the premise that tool rests on.
    """

    class Recording(io.StringIO):
        """A capture that remembers being reconfigured, which `StringIO` cannot.

        The claim under test is that `main` *asks*; whether CPython then
        flushes is CPython's to keep. Driving a real redirected sweep would
        test the interpreter and take a sweep to do it.
        """

        def __init__(self) -> None:
            super().__init__()
            self.asked: dict[str, object] = {}

        def reconfigure(self, **how: object) -> None:
            self.asked.update(how)

    def test_it_asks_for_line_buffering(self) -> None:
        spill = self.Recording()
        with redirect_stdout(spill), redirect_stderr(io.StringIO()), suppress(SystemExit):
            mutate.main(["--list", "--base", "HEAD"])
        assert spill.asked == {"line_buffering": True}

    def test_a_stream_that_cannot_be_reconfigured_is_not_an_error(self) -> None:
        """A dozen tests here call `main` under `support.quiet`, whose capture
        is a plain `StringIO` with no `reconfigure`. Asked rather than
        suppressed, so this is the behaviour rather than a swallowed
        `AttributeError` -- and without the guard those tests all raise."""
        assert not hasattr(io.StringIO(), "reconfigure")
        with support.quiet(), suppress(SystemExit):
            mutate.main(["--list", "--base", "HEAD"])


class TestWhatEveryLaneHeldBetweenThem:
    """`_Lanes.crowded`: the sum across lanes at one instant, which is #90.

    **The number `_COMMIT` was calibrated without.** That constant lets the
    lanes' ceilings add up to 150% of the budget on the argument that peaks are
    not simultaneous, and nothing measured whether they were -- so the first
    time they did, the host's OOM killer took a desktop session rather than the
    sweep. `widest` cannot answer it: it is the heaviest *single* process, which
    `verdict.cap` already bounds, and a machine dies from the sum of honest
    lanes.

    Driven by calling `_sample` once against a stubbed process table rather than
    by starting real lanes, because the claim is arithmetic -- what gets added to
    what -- and a real fixture could not make two lanes hold chosen amounts at a
    chosen instant.
    """

    @pytest.fixture(autouse=True)
    def _forgotten(self) -> Iterator[None]:
        mutate._WATCHED.forget()
        try:
            yield
        finally:
            mutate._WATCHED.forget()

    class Once:
        """A stop event that lets the sampling loop run exactly once.

        `_sample` is `while not stop.wait(_SAMPLE)`, so a *set* `threading.Event`
        runs the body **zero** times -- which is how the first version of these
        tests read 0 and looked like a broken sampler. Returning `False` once and
        `True` afterwards is what "one pass, no sleeping" actually spells.
        """

        def __init__(self) -> None:
            self.waited = 0

        def wait(self, timeout: float | None = None) -> bool:
            self.waited += 1
            return self.waited > 1

    def sampled(self, table: dict[int, Any], members: dict[int, set[int]], start: int = 0) -> int:
        """One `_sample` pass over `table`, and what it made of the crowd.

        `members` maps each watched leader to the pids `_lane` would find for
        it, and every leader is given a ceiling far above anything in `table`,
        so the kill branch is never the thing under test.
        """
        lanes = mutate._Lanes()
        lanes._crowd = start
        lanes._ceilings = dict.fromkeys(members, 1 << 40)
        stop = self.Once()
        with (
            mock.patch.object(mutate, "_processes", lambda: table),
            mock.patch.object(mutate, "_lane", lambda leader, tbl: members[leader]),
            mock.patch.object(mutate, "_end_lane", lambda *rest: None),
        ):
            lanes._sample(stop)  # type: ignore[arg-type]
        assert stop.waited == 2, "the sampler did not run exactly one pass"
        return lanes.crowded()

    def table(self, **held: int) -> dict[int, Any]:
        """A process table where pid `n` holds `held[f"p{n}"]` bytes resident."""
        return {
            int(name[1:]): mutate.Process(parent=1, group=1, resident=size, address=size)
            for name, size in held.items()
        }

    def test_it_adds_the_lanes_up_rather_than_taking_the_heaviest(self) -> None:
        """**The whole point, and the one assertion `widest` would pass.** Two
        lanes of 100 MiB each are 200 MiB to the machine and 100 MiB to
        `_report_headroom`. Sized unequally so that "the sum" and "twice the
        first" are also different answers."""
        got = self.sampled(self.table(p10=100 << 20, p20=300 << 20), {10: {10}, 20: {20}})
        assert got == 400 << 20

    def test_a_lane_inside_its_ceiling_is_still_counted(self) -> None:
        """The loop `continue`s past a lane that is within its ceiling, and that
        `continue` is *before* where a running total would have been kept. Every
        honest lane takes that branch, so a total kept there would count only
        the lanes about to be killed -- which is the old behaviour with more
        code."""
        # Far inside its ceiling, so the kill branch is not reached at all.
        got = self.sampled(self.table(p10=7 << 20), {10: {10}})
        assert got == 7 << 20

    def test_a_process_in_two_lanes_is_counted_once(self) -> None:
        """A pid reachable from two leaders -- `_lane` unions a process group
        with every descendant, so a lane reparented under another appears in
        both. Adding the per-lane sums would report 600 MiB of a machine that
        holds 400."""
        got = self.sampled(
            self.table(p10=100 << 20, p20=100 << 20, p30=200 << 20),
            {10: {10, 30}, 20: {20, 30}},
        )
        assert got == 400 << 20

    def test_it_keeps_the_worst_instant_rather_than_the_last(self) -> None:
        """A high-water mark. The machine died at the peak, not at whatever was
        true when the sweep finished."""
        got = self.sampled(self.table(p10=5 << 20), {10: {10}}, start=900 << 20)
        assert got == 900 << 20

    def test_a_fresh_sampler_has_no_crowd_mark(self) -> None:
        assert mutate._Lanes().crowded() == 0

    def test_forget_starts_a_fresh_crowd_mark(self) -> None:
        """`_WATCHED` is a singleton and a process may call `run` twice, so
        without this the second run reports the first one's peak -- the same
        reason `widest` is reset, and it was reset in the same place."""
        mutate._WATCHED._crowd = 77 << 20
        assert mutate._WATCHED.crowded() == 77 << 20
        mutate._WATCHED.forget()
        assert mutate._WATCHED.crowded() == 0

    def said(self, crowd: int, budget: int, terminal: bool = False) -> str:
        with (
            mock.patch.object(mutate._WATCHED, "_crowd", crowd),
            mock.patch.object(mutate, "_budget", lambda: budget),
            support.quiet(terminal) as spill,
        ):
            mutate._report_crowding()
        return spill.getvalue()

    def test_the_line_names_what_was_held_and_what_there_was(self) -> None:
        said = self.said(41000 << 20, 53000 << 20)
        assert "41000 MiB" in said
        assert "53000 MiB" in said
        assert "77%" in said

    def test_it_says_resident_because_that_is_what_the_host_counts(self) -> None:
        """Address space would be the wrong number by 25x -- the storm that
        prompted `_Lanes` held 26 GB resident against 961 GB of address space,
        and only one of those is a reason anything dies."""
        assert "resident" in self.said(100 << 20, 1000 << 20)

    def test_nothing_sampled_says_nothing(self) -> None:
        """A zero here would be a measurement reported as a result -- the shape
        `_report_headroom` exists to correct, in the same run."""
        assert self.said(0, 53000 << 20) == ""

    def test_a_crowded_machine_is_shouted_and_a_roomy_one_is_not(self) -> None:
        """The half that makes it worth printing. Without it the line reads the
        same whether the sweep used a tenth of the machine or all of it."""
        tight = self.said(50000 << 20, 52000 << 20, terminal=True)
        roomy = self.said(5000 << 20, 52000 << 20, terminal=True)
        assert paint.ODD in tight, "a 96% crowd was muttered"
        assert paint.ODD not in roomy, "a 10% crowd was shouted"

    def test_the_threshold_itself_counts_as_crowded(self) -> None:
        """Exactly at `_TIGHT`, the only input that tells `>=` from `>`."""
        at = int(mutate._TIGHT * (52000 << 20))
        assert paint.ODD in self.said(at, 52000 << 20, terminal=True)

    def test_a_machine_that_reports_no_memory_is_said_nothing_about(self) -> None:
        """`_budget` can answer 0 -- a machine publishing no `/proc/meminfo`, no
        cgroup and no `sysconf`, where `_BLIND_LANES` is the fallback. Dividing
        by it is a `ZeroDivisionError` in the reporting line of a sweep that has
        otherwise finished, which is the worst place to raise."""
        assert self.said(900 << 20, 0) == ""

    def test_a_machine_that_reports_one_byte_is_still_reported_on(self) -> None:
        """The other side of `<= 0`, and the only input that tells it from
        `<= 1`: a budget of exactly one byte is absurd but it is a *reading*,
        and a reading gets reported. Without this the guard may quietly widen
        until it swallows real machines."""
        assert "900 MiB" in self.said(900 << 20, 1)

    def test_it_is_still_said_when_there_is_no_ceiling_to_report_against(self) -> None:
        """The two lines go quiet for different reasons, so one must not be
        nested inside the other. `_report_headroom` says nothing when no lane
        process was measured or when there is no ceiling -- and a run with no
        ceiling is exactly one where "was the machine big enough" is the only
        question left. Nested, this figure disappeared with the other."""
        with (
            mock.patch.object(mutate._WATCHED, "_crowd", 900 << 20),
            mock.patch.object(mutate._WATCHED, "_widest", 0),
            mock.patch.object(mutate, "_budget", lambda: 52000 << 20),
            support.quiet() as spill,
        ):
            mutate._report_headroom(0)
        said = spill.getvalue()
        assert "900 MiB" in said, "the crowd went quiet with the headroom line"
        assert "ceiling" not in said, "a headroom line was printed with nothing to report"


class TestWhatTheHeaviestLaneHeld:
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

    @pytest.fixture(autouse=True)
    def _forgotten(self) -> Iterator[None]:
        mutate._WATCHED.forget()
        try:
            yield
        finally:
            mutate._WATCHED.forget()

    def test_a_fresh_sampler_has_no_mark_at_all(self) -> None:
        """Read before anything has been watched. The autouse fixture above
        calls `forget` before every test in this class, so the value `__init__`
        sets is unobservable to the rest -- three mutants of it survived the
        sweep, including one that
        removed the assignment and left `widest` raising `AttributeError`."""
        assert mutate._Lanes().widest() == 0

    def test_forget_starts_a_fresh_mark(self) -> None:
        """`_WATCHED` is a module-level singleton and a process may call `run`
        more than once -- a spec file calling `verify` twice is the shape that
        does it. Without this the second run reports the first one's peak."""
        mutate._WATCHED._widest = 123 << 20
        assert mutate._WATCHED.widest() == 123 << 20
        mutate._WATCHED.forget()
        assert mutate._WATCHED.widest() == 0

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
        assert "1892 MiB" in said
        assert "2053 MiB" in said
        assert "92%" in said

    def test_it_says_the_figure_is_sampled_and_low(self) -> None:
        """A number presented as exact invites being divided into. This one is
        the *current* size read once a second, and measured 3% under the
        kernel's own high-water over a sweep."""
        assert "sampled" in self.said(1892 << 20, 2053 << 20)

    def test_the_threshold_itself_counts_as_thin(self) -> None:
        """Exactly at `_TIGHT`, which is the only input that tells `>=` from
        `>`. The pair below uses 95% and 10% and cannot see the difference; the
        mutant survived the sweep against them."""
        at = int(mutate._TIGHT * (2000 << 20))
        assert paint.ODD in self.said(at, 2000 << 20, terminal=True)

    def test_a_thin_margin_is_shouted_and_a_roomy_one_is_not(self) -> None:
        """The half that makes the line worth printing at all. Without it the
        report is the same colour whether the ceiling is comfortable or one
        test away from killing every lane."""
        tight = self.said(1900 << 20, 2000 << 20, terminal=True)
        roomy = self.said(200 << 20, 2000 << 20, terminal=True)
        assert paint.ODD in tight, "a 95% margin was muttered"
        assert paint.ODD not in roomy, "a 10% margin was shouted"
        assert paint.QUIET in roomy

    def ran(self, *patched: Any) -> str:
        """One real `run`, with the rows stubbed out.

        `TestTheRunAccountsForItsLanes.ROW` rather than the module's `row()`:
        that helper's placeholder text appears 2148 times in its file, so
        `check` refuses it. Referenced at call time, since the class is defined
        below this one.
        """
        only = TestTheRunAccountsForItsLanes.ROW
        # `ExitStack` rather than a starred `with`, which is a syntax error.
        with ExitStack() as stack:
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
        assert "999 MiB" not in self.ran(), "a mark from an earlier run was reported"

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
        assert "512 MiB" in self.ran(held), "run never reported its headroom"

    def test_a_run_that_sampled_nothing_says_nothing(self) -> None:
        """Lanes that each finish inside one sampling interval leave no reading.
        Printing "0 MiB of 2053 MiB (0%)" would report a measurement that was
        never taken as though it were a result -- which is the shape this whole
        change exists to correct."""
        assert self.said(0, 2053 << 20) == ""

    def test_no_ceiling_means_no_share_to_report(self) -> None:
        """`--memory 0` is "no cap", and a percentage of nothing is a
        ZeroDivisionError rather than a fact."""
        assert self.said(1892 << 20, 0) == ""


class TestWhyAProbeThatWroteNothingDied:
    """`_signalled`: the sentence a row gets when there is nothing else to say.

    It is reached only when a probe produced no report *and* said nothing on its
    way out, so it is the whole of what a reader sees about that row -- and it
    had no test. The distinction it exists to draw is between "the mutation did
    this" and "the machine did this", which is the difference between a row worth
    reading and a row worth re-running.
    """

    def test_an_ordinary_exit_names_the_status(self) -> None:
        assert mutate._signalled(3) == "the probe exited 3 without writing a report"

    def test_a_clean_exit_with_no_report_is_still_an_exit(self) -> None:
        """Zero is on the boundary, and it is the side that is not a signal:
        `subprocess` spells a signal as a *negative* number, so `>= 0` and `> 0`
        differ exactly here -- and under `> 0` a probe that exited 0 would be
        reported as killed by signal 0, which is not a signal at all."""
        assert mutate._signalled(0) == "the probe exited 0 without writing a report"

    def test_a_kill_says_which_signal_and_what_it_usually_means(self) -> None:
        """`SIGKILL` is the one that matters: it is what the host OOM killer
        sends, so it is what a lane looks like where `verdict.cap` is not
        enforced. Both causes are named because a sweep of this module produced
        the second, and a message naming the wrong one costs more than a message
        naming none."""
        said = mutate._signalled(-signal.SIGKILL)
        assert "killed by SIGKILL" in said
        assert "ran out of memory" in said
        assert "killed the session it was in" in said

    def test_any_other_signal_is_named_without_the_guess(self) -> None:
        """The other half. Without it, a `_signalled` that appended the
        out-of-memory clause to *every* signal passes the test above -- and
        would tell a reader their machine was out of memory every time a probe
        timed out."""
        said = mutate._signalled(-signal.SIGTERM)
        assert "killed by SIGTERM" in said
        assert "ran out of memory" not in said

    def test_a_number_that_is_not_a_signal_does_not_raise(self) -> None:
        """`signal.Signals(-returncode)` raises for a number outside the set,
        and this runs inside the handler for a probe that already failed --
        where an exception replaces the row's reason with a traceback about the
        reason."""
        assert "killed by ?" in mutate._signalled(-999)


class TestTheLastThingARunManagedToSay:
    """`_tail`: the log's final line, which is a `BROKE` row's whole reason.

    `_run` reads it before falling back to `_signalled`, so an empty answer here
    is what decides whether a reader is told what the probe said or only how it
    died. Every arm returns a string, because the caller puts it straight into a
    `Verdict`.
    """

    def tail(self, boxes: support.Boxes, text: str) -> str:
        box = boxes.make("tupferl-tail-")
        noise = box / "noise.log"
        noise.write_text(text, encoding="utf-8")
        return mutate._tail(noise)

    def test_the_last_line_is_what_comes_back(self, boxes: support.Boxes) -> None:
        assert self.tail(boxes, "first\nsecond\nthe last one\n") == "the last one"

    def test_trailing_blank_lines_are_not_the_last_line(self, boxes: support.Boxes) -> None:
        """A process that dies mid-write leaves them, and "" as a reason reads
        as a row nobody can explain rather than as one that said something."""
        assert self.tail(boxes, "real\n\n\n   \n") == "real"

    def test_a_log_with_nothing_in_it_is_empty_rather_than_an_error(
        self, boxes: support.Boxes
    ) -> None:
        """`_run` spells this `_tail(noise) or _signalled(...)`, so the empty
        string is what hands the question on. An `IndexError` from the last-line
        read would replace the row's reason with a traceback."""
        assert self.tail(boxes, "") == ""

    def test_a_log_that_was_never_written_is_empty_too(self) -> None:
        """The `OSError` arm. A probe killed before it opened its log leaves no
        file at all, which is precisely the case `_signalled` exists for."""
        assert mutate._tail(Path("/nonexistent/noise.log")) == ""

    def test_bytes_that_are_not_utf8_do_not_stop_the_report(self, boxes: support.Boxes) -> None:
        """`errors="replace"`. A probe's log is whatever the tests under it
        wrote, and this project has a test that deliberately puts invalid UTF-8
        in a path -- so a strict decode here would turn one row's reason into an
        exception during the summary of every other."""
        box = boxes.make("tupferl-tail-")
        noise = box / "noise.log"
        noise.write_bytes(b"fine\nbroken \xff\xfe here\n")
        assert "broken" in mutate._tail(noise)


class TestSurvivorsATagBesideTheCodeExcuses:
    """`excused` and `sort_survivors`: a disposition written where the code is.

    The record this replaces was a file of sha256 keys, and it was not kept.
    Twelve equivalences proved in one sitting went into commit messages instead,
    and seventeen of its entries had come to match nothing the tree generated.

    **The hazard is the whole design.** Any record of accepted survivors is how
    a project stops looking at them, so three things are load-bearing: the count
    is always printed, a tag that has stopped earning its place is reported, and
    `--accept` writes `TODO` rather than a reason it invented.
    """

    # Bounded, because everything here goes through `mutants.line_starts` and
    # `Tags`, and `line_starts` is a `while` whose every arm advances its
    # counter. A mutation dropping one spins, and a hang is filed `BROKE` rather
    # than `caught` -- so the lines these tests exist to guard would be guarded
    # by nothing. Measured: that row came back `BROKE` on the sweep that
    # followed these tests being written.
    _bounded = support.bounds(support.PATIENCE, "reading a tag hung")

    def tree(self, boxes: support.Boxes, body: str) -> Path:
        """A one-file tree, whose text is what a tag is read out of."""
        box = boxes.make("tupferl-tags-")
        (box / "tupferl").mkdir()
        (box / "tupferl" / "sync.py").write_text(body, encoding="utf-8")
        return box

    def rows(
        self,
        boxes: support.Boxes,
        body: str,
        needle: str,
        outcome: mutate.Outcome = "survived",
        operator: str = "branch",
    ) -> tuple[Path, list[mutate.Result]]:
        """One row whose span really points at `needle` inside `body`.

        Built from a real offset rather than a made-up one, because the span is
        how `excused` finds the line -- a fixture that guessed would be testing
        its own arithmetic.
        """
        box = self.tree(boxes, body)
        at = body.index(needle)
        row = mutate.Result(
            Mutation(
                "tupferl/sync.py:1 in f() -- x",
                "tupferl/sync.py",
                needle,
                "mutated",
                "tests.test_sync",
                span=(at, at + len(needle)),
                operator=operator,
            ),
            mutate.Verdict(outcome, ""),
        )
        return box, [row]

    def test_a_row_with_no_tag_is_unread(self, boxes: support.Boxes) -> None:
        box, results = self.rows(boxes, "x = 1\ny = 2\n", "y = 2")
        found = mutate.sort_survivors(results, box)
        assert found.fresh == results
        assert found.accepted == []

    def test_a_tag_on_the_line_excuses_it(self, boxes: support.Boxes) -> None:
        box, results = self.rows(
            boxes, "x = 1\ny = 2  # survivor: branch -- it cannot matter\n", "y = 2"
        )
        found = mutate.sort_survivors(results, box)
        assert found.fresh == []
        assert found.accepted == [(results[0], "it cannot matter")]

    def test_a_tag_on_the_line_above_excuses_it_too(self, boxes: support.Boxes) -> None:
        """Both forms, because a trailing tag is unreadable on a long line and a
        tag above one is ambiguous after another statement -- so the second is
        taken only where the whole line is the comment."""
        box, results = self.rows(
            boxes, "x = 1\n# survivor: branch -- it cannot matter\ny = 2\n", "y = 2"
        )
        assert mutate.sort_survivors(results, box).accepted == [(results[0], "it cannot matter")]

    def test_a_wrapped_tag_reads_as_one_sentence(self, boxes: support.Boxes) -> None:
        """The reason is the whole value of the record, and one that had to fit
        in what was left of a line would be the format shaping the argument.

        End to end through `tag`, because a writer that wraps and a reader that
        does not leave every long reason unread -- and each half's own tests
        would still pass. The reason here is longer than the 100 columns `ruff`
        enforces, which is what makes the assertion mean anything.
        """
        why = (
            "equivalent because the kernel refuses a soft limit above the hard one, so the "
            "clamp can never change an outcome and dropping it reaches the same state by a "
            "longer road"
        )
        written = mutate.tag("branch", why, "    ")
        assert len(written) > 1, "the reason did not wrap"
        assert all(len(line) <= 100 for line in written), written
        body = "\n".join([*written, "    y = 2"]) + "\n"
        box = self.tree(boxes, body)
        at = body.index("y = 2")
        row = mutate.Result(
            Mutation(
                "tupferl/sync.py:1 in f() -- x",
                "tupferl/sync.py",
                "y = 2",
                "mutated",
                "tests.test_sync",
                span=(at, at + 5),
                operator="branch",
            ),
            mutate.Verdict("survived", ""),
        )
        assert mutate.sort_survivors([row], box).accepted[0][1] == why

    def test_a_comment_block_is_not_crossed_by_a_blank_line(self, boxes: support.Boxes) -> None:
        """A tag reaches the statement under it, not across an unrelated comment
        further up -- or a `# survivor:` written about one line would silently
        excuse whatever ended up beneath it."""
        body = "# survivor: branch -- about something else\n\ny = 2\n"
        box, results = self.rows(boxes, body, "y = 2")
        assert mutate.sort_survivors(results, box).fresh == results

    def test_a_tag_is_spent_only_when_it_excuses_nothing(self, boxes: support.Boxes) -> None:
        """One tag answers every operator it names, and one operator covers
        mutations that need not have the same answer.

        Found on this mechanism's first real sweep: `conflicts.somewhere_in`'s
        `range(len(whole) - len(run) + 1)` is `arith` twice over -- widening it
        is equivalent, because the extra slices are shorter than `run`, and
        narrowing it is caught, because a match at the last position is missed.
        A check that called the tag spent on the first caught row it saw
        reported a live tag as dead, which is the direction that loses a written
        reason.
        """
        body = "y = 2  # survivor: branch -- still needed by the other row\n"
        box = self.tree(boxes, body)
        at = body.index("y = 2")

        def row(outcome: mutate.Outcome) -> mutate.Result:
            return mutate.Result(
                Mutation(
                    f"tupferl/sync.py:1 in f() -- {outcome}",
                    "tupferl/sync.py",
                    "y = 2",
                    "mutated",
                    "tests.test_sync",
                    span=(at, at + 5),
                    operator="branch",
                ),
                mutate.Verdict(outcome, ""),
            )

        found = mutate.sort_survivors([row("caught"), row("survived")], box)
        assert len(found.accepted) == 1
        assert found.spent == [], "a tag still excusing a survivor was called spent"

    def test_a_tag_for_another_operator_does_not_excuse_this_one(
        self, boxes: support.Boxes
    ) -> None:
        """**The measurement the format rests on.** Mutations average 2.1 per
        source line and reach 13, and 53% of the lines carrying a survivor also
        carry a row that is *caught* -- so a tag without an operator would
        excuse a live guard about half the time it was used, and would go on
        excusing operators `mutants.py` has not learnt yet."""
        box, results = self.rows(
            boxes, "y = 2  # survivor: arith -- about the other one\n", "y = 2"
        )
        assert mutate.sort_survivors(results, box).fresh == results

    def test_one_tag_can_name_several_operators(self, boxes: support.Boxes) -> None:
        body = "y = 2  # survivor: arith, branch -- both are the same argument\n"
        box, results = self.rows(boxes, body, "y = 2")
        assert len(mutate.sort_survivors(results, box).accepted) == 1

    @pytest.mark.parametrize("outcome", ["broke", "timeout"])
    def test_a_row_that_asked_nothing_is_excused_on_the_same_terms(
        self, boxes: support.Boxes, outcome: mutate.Outcome
    ) -> None:
        """**Not caught, rather than `survived`.** `broke` and `timeout` were
        the one category with nowhere to be written down: 33 came back every
        whole-tree run with nothing to say which had been read, and three cannot
        be answered at all -- two run the whole suite nested inside a
        memory-capped sandbox, one is a fork bomb.

        **The per-case bound this had is gone, and `parametrize` is why.** It
        read: `support.deadline` is a one-shot alarm and `subTest` *catches* the
        `TimeoutError` it raises, so the first iteration failed as it should and
        the second ran on with nothing armed -- measured, this test hung past
        120s under a mutation the class bound was written to catch, while its
        siblings failed in five seconds each. One case is one test now, so the
        class fixture arms the bound afresh for each and there is nothing left
        for a second copy to fix. Same finding as `test_mutants`'
        `TestTheOperators`, met twice in one cluster.
        """
        body = "y = 2  # survivor: branch -- a fork bomb\n"
        box, results = self.rows(boxes, body, "y = 2", outcome=outcome)
        found = mutate.sort_survivors(results, box)
        assert found.fresh == []
        assert found.accepted == [(results[0], "a fork bomb")]

    def test_a_tag_on_a_row_the_suite_now_catches_is_reported_as_spent(
        self, boxes: support.Boxes
    ) -> None:
        """The direction the hash record could not see at all. Its key ignores
        the outcome deliberately, so a reason written for a survivor went on
        excusing the same row once it started being killed -- silently. A tag
        that is no longer needed is good news, and good news nobody is told is
        how a mute list forms."""
        body = "y = 2  # survivor: branch -- no longer true\n"
        box, results = self.rows(boxes, body, "y = 2", outcome="caught")
        found = mutate.sort_survivors(results, box)
        assert found.fresh == []
        assert found.accepted == []
        assert len(found.spent) == 1
        assert "now caught" in found.spent[0]

    def test_a_caught_row_with_no_tag_is_simply_not_mentioned(self, boxes: support.Boxes) -> None:
        """The other half: most rows are caught and have no tag, and a line
        about each would bury the ones that matter."""
        box, results = self.rows(boxes, "y = 2\n", "y = 2", outcome="caught")
        found = mutate.sort_survivors(results, box)
        assert (found.fresh, found.accepted, found.spent) == ([], [], [])

    def test_a_row_with_no_span_cannot_be_excused(self, boxes: support.Boxes) -> None:
        """A hand-written row has no span, and guessing a line from its prose
        label is how a tag lands on the wrong statement. Unread is the safe
        direction: it gets reported."""
        box, results = self.rows(boxes, "y = 2  # survivor: branch -- x\n", "y = 2")
        loose = [mutate.Result(results[0].mutation._replace(span=None), results[0].verdict)]
        assert mutate.sort_survivors(loose, box).fresh == loose

    def test_a_file_that_cannot_be_read_excuses_nothing(self, boxes: support.Boxes) -> None:
        """More than it should, never less -- the same direction the old record
        took when its JSON would not parse."""
        box, results = self.rows(boxes, "y = 2  # survivor: branch -- x\n", "y = 2")
        (box / "tupferl" / "sync.py").unlink()
        assert mutate.sort_survivors(results, box).fresh == results

    def test_two_identical_mutations_on_two_lines_need_two_tags(self, boxes: support.Boxes) -> None:
        """What `Accepted.seen` was for, obtained by construction. The hash was
        content-addressed, so two identical mutations in one file collapsed to
        one key -- 557 survivors to 432 -- and a count was needed to tell the
        126th from the 125th. A tag sits on a line, so the second row is
        untagged and unread."""
        body = "y = 2  # survivor: branch -- the first one\ny = 2\n"
        box = self.tree(boxes, body)
        rows = []
        for at in (body.index("y = 2"), body.rindex("y = 2")):
            rows.append(
                mutate.Result(
                    Mutation(
                        "tupferl/sync.py:1 in f() -- x",
                        "tupferl/sync.py",
                        "y = 2",
                        "mutated",
                        "tests.test_sync",
                        span=(at, at + 5),
                        operator="branch",
                    ),
                    mutate.Verdict("survived", ""),
                )
            )
        found = mutate.sort_survivors(rows, box)
        assert len(found.accepted) == 1
        assert len(found.fresh) == 1


class TestWhatIsTriedAheadOfARow:
    """`Learned.ahead`: the move-to-front head a row runs before its own
    selection, and the three ways it can quietly stop paying for itself."""

    def row(self, tests: str = "tests.test_sync", first: Sequence[str] = ()) -> Mutation:
        return Mutation("a row", "tupferl/sync.py", "a", "b", tests, first=first)

    def learned(self, *tests: str) -> mutate.Learned:
        made = mutate.Learned()
        for test in tests:
            made.saw(test)
        return made

    def test_with_nothing_remembered_there_is_no_head(self) -> None:
        """The first row of every run. An empty *sequence* and not `None`:
        `_attempt` unpacks it with `*`, and `*None` is a `TypeError` raised on
        the lane, one row at a time, for every row of the sweep."""
        assert mutate.Learned().ahead(self.row()) == ()

    def test_a_remembered_test_the_row_can_reach_is_offered(self) -> None:
        assert self.learned("tests.test_sync.T.test_it").ahead(self.row()) == (
            "tests.test_sync.T.test_it",
        )

    def test_a_test_outside_the_rows_selection_is_not_offered(self) -> None:
        """A test in a module that does not import the mutated file cannot see
        the mutation, so running it first is pure cost -- paid by every row."""
        assert self.learned("tests.test_merge.T.test_it").ahead(self.row()) == ()

    def test_one_reachable_test_among_several_is_the_one_offered(self) -> None:
        """`any`, not `all`. With a single-module selection the two agree, so
        this needs a row whose selection names two modules and a head holding a
        test from one of them -- under `all` nothing is ever offered and the
        cache silently stops working."""
        head = self.learned("tests.test_merge.T.test_it")
        row = self.row(tests="tests.test_sync tests.test_merge")
        assert head.ahead(row) == ("tests.test_merge.T.test_it",)

    def test_a_row_that_already_names_a_test_is_not_given_it_twice(self) -> None:
        """`first` is run in order, and naming a test twice buys nothing and
        costs a run."""
        head = self.learned("tests.test_sync.T.test_it")
        assert head.ahead(self.row(first=("tests.test_sync.T.test_it",))) == ()

    def test_a_row_with_no_selection_reaches_everything(self) -> None:
        """`WHOLE_SUITE` is the empty selection, and it means "run the lot" --
        so nothing in the head is out of reach."""
        head = self.learned("tests.test_merge.T.test_it")
        assert head.ahead(self.row(tests=mutate.WHOLE_SUITE)) == ("tests.test_merge.T.test_it",)


class TestTheEdgesOfSizingALane:
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
        assert mutate._share(0, 0, pinned=False).lanes == 1
        assert mutate._share(1, 0, pinned=False).lanes == 1
        assert mutate._share(4, 0, pinned=False).memory == 0

    def test_a_ceiling_never_falls_below_the_floor(self) -> None:
        """`max(floor, budget // lanes)`. Read as `min`, a pinned run that asks
        for more lanes than the budget divides into gives each of them a share
        far under the floor -- and every one is killed for holding what a lane
        normally holds, which reads as the mutation crashing.
        """
        with mock.patch.object(mutate, "_budget", return_value=mutate._FLOOR):
            share = mutate._share(8, mutate.MEMORY, pinned=True)
        assert share.lanes == 8, "the pin was not honoured"
        assert share.memory >= mutate._FLOOR

    def test_an_explicit_cap_under_the_floor_is_the_callers_call(self) -> None:
        """They may be reproducing a small machine on purpose, so the cap is
        obeyed rather than corrected -- and it still decides how many fit."""
        asked = mutate._FLOOR // 4
        share = mutate._share(8, asked, pinned=False)
        assert share.memory == asked


class TestTheSmallDecisionsNothingAsked:
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
        assert self.report("caught", "caught").clean
        assert not self.report("caught", "survived").clean
        assert not self.report("survived", "caught").clean

    def test_a_key_is_short_enough_to_read_in_a_diff(self) -> None:
        """The record and the killers cache are both keyed by this and both are
        reviewed by a person. A full sha256 is four times the width and makes a
        row wrap, which is the whole reason for the slice."""
        key = mutate._key(Mutation("a", "tupferl/sync.py", "x", "y", "t"))
        assert len(key) == 16

    def test_a_key_ignores_where_the_line_is_and_notices_what_it_says(self) -> None:
        """The property the disposition record rests on: a row keeps its key
        when the code above it moves, and gets a new one when the edit itself
        changes."""
        base = Mutation("a", "tupferl/sync.py", "x", "y", "t", operator="branch")
        assert mutate._key(
            base._replace(label="a different label", tests="other", span=(9, 9))
        ) == mutate._key(base)
        for changed in (
            base._replace(path="tupferl/merge.py"),
            base._replace(old="z"),
            base._replace(new="z"),
            base._replace(operator="arith"),
        ):
            assert mutate._key(changed) != mutate._key(base), changed

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
        assert mutate._applied(text, row) == "if a:\n    pass\nif True:\n    pass\n"

    def test_a_row_without_a_span_replaces_its_text(self) -> None:
        """The hand-written shape, whose `old` `check` has already refused
        unless it appears exactly once."""
        row = Mutation("the only one", "x.py", "if a:", "if True:", "t")
        assert mutate._applied("if a:\n    pass\n", row) == "if True:\n    pass\n"

    def test_stale_bytecode_is_swept_out_of_a_sandbox(self, boxes: support.Boxes) -> None:
        """A `__pycache__` left by a previous mutation's run is read by the next
        one that borrows the same sandbox -- the `(mtime, size)` collision this
        module's docstring exists to avoid. `ignore_errors` covers a directory
        that vanished under a concurrent lane, so nothing here may raise."""
        box = boxes.make("tupferl-bytecode-")
        (box / "pkg").mkdir()
        for cache in (box / "__pycache__", box / "pkg" / "__pycache__"):
            cache.mkdir()
            (cache / "stale.pyc").write_bytes(b"\x00")
        (box / "pkg" / "keep.py").write_text("x = 1\n", encoding="utf-8")

        mutate._clear_bytecode(box)

        assert list(box.rglob("__pycache__")) == [], "stale bytecode was left behind"
        assert (box / "pkg" / "keep.py").is_file(), "it took the source with it"


class TestReadingBackAReportToResumeFrom:
    """`_recorded`: the rows a resumed sweep must not re-run or lose.

    Everything about it is a *reconstruction* -- a `Mutation` and a `Verdict`
    rebuilt from JSON -- and the rebuilt rows go on to `_summarise` and the exit
    status. A field dropped here is a survivor that goes unmentioned in a run
    that exits 0; a span rebuilt wrong is a row re-applied at the wrong offset.

    Reached only through a real resumed batch before this, which is why six of
    its mutations survived: that path asserts the *count* of rows carried over,
    and every one of them is wrong in the same way.
    """

    def saved(self, boxes: support.Boxes, payload: object) -> Path:
        box = boxes.make("tupferl-resume-")
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

    def test_every_field_comes_back_the_way_it_went_in(self, boxes: support.Boxes) -> None:
        """Asserted field by field rather than by count. The resume path counts
        rows, and a row rebuilt with the wrong span, the wrong operator or the
        wrong outcome is still one row."""
        (found,) = mutate._recorded(self.saved(boxes, {"results": [self.ROW]}))
        assert found.mutation.label == "a branch"
        assert found.mutation.path == "tupferl/sync.py"
        assert found.mutation.old == "if a:"
        assert found.mutation.new == "if True:"
        assert found.mutation.tests == "tests.test_sync"
        assert found.mutation.operator == "branch"
        assert found.verdict.outcome == "survived"
        assert found.verdict.detail == "nothing noticed"

    def test_the_span_comes_back_as_the_pair_it_was(self, boxes: support.Boxes) -> None:
        """Both ends, and in order. `_applied` splices `new` at exactly these
        offsets, so a pair rebuilt as `(start, start)` or reversed edits the
        wrong bytes of the file -- and the row still looks like the row it
        claims to be."""
        (found,) = mutate._recorded(self.saved(boxes, {"results": [self.ROW]}))
        assert found.mutation.span == (40, 45)

    def test_a_row_with_no_span_keeps_none(self, boxes: support.Boxes) -> None:
        """A hand-written row carries no span and is applied by `replace`
        instead. Rebuilt as `(0, 0)` it would splice at the top of the file."""
        row = {**self.ROW}
        del row["span"]
        (found,) = mutate._recorded(self.saved(boxes, {"results": [row]}))
        assert found.mutation.span is None

    def test_no_file_and_no_path_are_both_nothing_to_resume_from(self) -> None:
        """`None` is "resume was not asked for"; a missing file is "the run that
        would have written one never got there". Both mean the same thing to the
        caller, and neither may raise -- this runs before the sweep starts."""
        assert mutate._recorded(None) == []
        assert mutate._recorded(Path("/nonexistent/report.json")) == []

    def test_a_half_written_report_resumes_as_nothing(self, boxes: support.Boxes) -> None:
        """Re-running everything is the safe reading of a crash mid-write. The
        dangerous one is a partial list read as complete, which drops whatever
        the crash cut off -- silently, and in the direction that flatters."""
        box = boxes.make("tupferl-resume-")
        broken = box / "report.json"
        broken.write_text('{"results": [{"label": "cut off"', encoding="utf-8")
        assert mutate._recorded(broken) == []

    def test_a_row_missing_a_field_takes_the_whole_file_with_it(self, boxes: support.Boxes) -> None:
        """`KeyError` is caught, so a report from an older shape resumes as
        nothing rather than as a partial list nobody can tell is partial."""
        assert mutate._recorded(self.saved(boxes, {"results": [{"label": "only this"}]})) == []


class TestWhatTheKillersCacheWritesDown:
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

    def test_it_writes_a_file_a_later_run_can_read(self, boxes: support.Boxes) -> None:
        box = boxes.make("tupferl-killers-")
        # A directory that does not exist yet: `sweeps/` is gitignored, so the
        # first run on a fresh clone is always this case.
        where = box / "sweeps" / "killers.json"
        self.cache(where).save()
        assert where.is_file(), "nothing was written"
        written = json.loads(where.read_text(encoding="utf-8"))
        assert list(written["killers"].values()) == ["tests.test_sync.T.test_it"]

    def test_saving_a_second_time_over_an_existing_directory_is_fine(
        self, boxes: support.Boxes
    ) -> None:
        """`exist_ok=True`. Every run after the first is this case, and without
        it the second sweep on a machine dies at the end having done all the
        work."""
        box = boxes.make("tupferl-killers-")
        where = box / "sweeps" / "killers.json"
        self.cache(where).save()
        self.cache(where).save()
        assert where.is_file()

    def test_no_path_writes_nothing_and_does_not_raise(self) -> None:
        """`--no-killers` and every spec-file run. A `save` that tried anyway
        would take down a sweep that had already finished its work."""
        self.cache(None).save()


class TestWhatAcceptWritesDown:
    """`_accept` and `_report_known`: what `--accept` writes, and what a run says.

    A hand table once asked what `sort_survivors` *answers* and nothing asked
    either of these; a generated sweep found twenty-six mutations no test could
    see, the record never being written at all among them.

    What it writes now is a line of source beside the row it excuses, which is
    the whole reason the record moved out of a file of hashes: a `TODO` in a
    diff next to the code is read, and a `TODO` under a sha256 key is not.
    """

    # Bounded, because everything here goes through `mutants.line_starts` and
    # `Tags`, and `line_starts` is a `while` whose every arm advances its
    # counter. A mutation dropping one spins, and a hang is filed `BROKE` rather
    # than `caught` -- so the lines these tests exist to guard would be guarded
    # by nothing. Measured: that row came back `BROKE` on the sweep that
    # followed these tests being written.
    _bounded = support.bounds(support.PATIENCE, "reading a tag hung")

    def tree(self, boxes: support.Boxes, body: str) -> Path:
        box = boxes.make("tupferl-accept-")
        (box / "tupferl").mkdir()
        (box / "tupferl" / "sync.py").write_text(body, encoding="utf-8")
        return box

    def row(self, body: str, needle: str, operator: str = "branch") -> mutate.Result:
        at = body.index(needle)
        return mutate.Result(
            Mutation(
                f"tupferl/sync.py:1 in f() -- {operator}",
                "tupferl/sync.py",
                needle,
                "mutated",
                "tests.test_sync",
                span=(at, at + len(needle)),
                operator=operator,
            ),
            mutate.Verdict("survived", ""),
        )

    def accept(self, boxes: support.Boxes, body: str, *needles: str) -> str:
        box = self.tree(boxes, body)
        rows = [self.row(body, needle) for needle in needles]
        with support.quiet():
            mutate._accept(mutate.Survivors(rows, [], []), box)
        return (box / "tupferl" / "sync.py").read_text(encoding="utf-8")

    def test_it_writes_a_tag_above_the_row(self, boxes: support.Boxes) -> None:
        written = self.accept(boxes, "x = 1\ny = 2\n", "y = 2")
        assert "# survivor: branch --" in written
        assert written.index("# survivor") < written.index("y = 2")

    def test_the_tag_says_todo_on_purpose(self, boxes: support.Boxes) -> None:
        """A reason nobody wrote is not a reason. The row is there to be edited
        and a reviewer seeing `TODO` in the diff is the point rather than an
        oversight -- which is exactly what a file of hashes could not offer,
        because the diff showed a key nobody could read."""
        assert "TODO" in self.accept(boxes, "y = 2\n", "y = 2")

    def test_the_tag_names_the_operator_it_excuses(self, boxes: support.Boxes) -> None:
        """Or it would excuse the row's siblings on the same line, and the
        operators `mutants.py` has not learnt yet."""
        assert "# survivor: branch --" in self.accept(boxes, "y = 2\n", "y = 2")

    def test_the_tag_keeps_the_indentation_of_the_line_it_guards(
        self, boxes: support.Boxes
    ) -> None:
        """Or the file no longer parses, and the next run's baseline is red for
        a reason that has nothing to do with any mutation."""
        written = self.accept(boxes, "def f():\n    y = 2\n", "y = 2")
        assert "    # survivor: branch --" in written
        compile(written, "sync.py", "exec")

    def test_two_rows_in_one_file_both_get_tags(self, boxes: support.Boxes) -> None:
        """Written bottom upwards, or the first insertion moves the line the
        second was measured against and the tag lands on the wrong statement."""
        written = self.accept(boxes, "y = 2\nz = 3\n", "y = 2", "z = 3")
        assert written.count("# survivor: branch --") == 2
        for line, guard in ((1, "y = 2"), (3, "z = 3")):
            assert "# survivor" in written.split("\n")[line - 1]
            assert written.split("\n")[line].strip() == guard

    def test_a_tag_goes_above_the_statement_not_inside_it(self, boxes: support.Boxes) -> None:
        """A mutation inside brackets sits on a *continuation* line, and a
        comment inserted there is legal Python that splits the expression.

        Found by running `--accept` for real: it put a tag in the middle of a
        set comprehension in `tools/mutants.py`, and `ruff format --check` then
        wanted to reflow the file -- the flag handing back a tree that fails the
        preflight it exists to be reviewed under. The file still has to parse
        *and* still has to be formatted, so both are asserted.
        """
        body = "def f():\n    return {\n        n\n        for n in range(3)\n    }\n"
        box = self.tree(boxes, body)
        at = body.index("range(3)")
        row = mutate.Result(
            Mutation(
                "tupferl/sync.py:4 in f() -- boundary",
                "tupferl/sync.py",
                "range(3)",
                "range(4)",
                "tests.test_sync",
                span=(at, at + 8),
                operator="boundary",
            ),
            mutate.Verdict("survived", ""),
        )
        with support.quiet():
            mutate._accept(mutate.Survivors([row], [], []), box)
        after = (box / "tupferl" / "sync.py").read_text(encoding="utf-8")
        compile(after, "sync.py", "exec")
        assert after.index("# survivor") < after.index("return {"), f"the tag went inside\n{after}"

    def test_a_row_with_no_span_is_left_alone(self, boxes: support.Boxes) -> None:
        """There is nothing to hang a tag on, and guessing a line from a prose
        label is how one lands on the wrong statement."""
        box = self.tree(boxes, "y = 2\n")
        loose = self.row("y = 2\n", "y = 2")
        loose = mutate.Result(loose.mutation._replace(span=None), loose.verdict)
        with support.quiet():
            mutate._accept(mutate.Survivors([loose], [], []), box)
        assert (box / "tupferl" / "sync.py").read_text(encoding="utf-8") == "y = 2\n"

    def test_what_it_wrote_is_then_read_back_as_an_excuse(self, boxes: support.Boxes) -> None:
        """End to end, and the only assertion that proves the two halves agree:
        a writer and a reader that disagree about the format leave every row
        unread for ever, and each half's own tests would still pass.

        **The row is rebuilt against the rewritten file**, because inserting a
        tag moves every offset below it -- so the table in hand is stale the
        moment `--accept` returns. That is why `main` runs it last, and why the
        first draft of this test failed against a tag that had been written
        perfectly well.
        """
        body = "x = 1\ny = 2\n"
        box = self.tree(boxes, body)
        with support.quiet():
            mutate._accept(mutate.Survivors([self.row(body, "y = 2")], [], []), box)
        after = (box / "tupferl" / "sync.py").read_text(encoding="utf-8")
        found = mutate.sort_survivors([self.row(after, "y = 2")], box)
        assert found.fresh == [], "what --accept wrote did not excuse the row"
        assert "TODO" in found.accepted[0][1]

    def test_a_second_operator_on_a_tagged_line_gets_its_own_readable_tag(
        self, boxes: support.Boxes
    ) -> None:
        """Two tags in one comment block, both findable.

        The first version joined the block into one string and ran one regex
        over it, so a second tag was swallowed into the first one's reason --
        its rows stayed unread and `--accept` stacked an identical `TODO` under
        them on every run. Reproduced before this was written: three runs, three
        copies, and the row still unexcused.

        `test_accept_does_not_tag_a_row_that_already_has_one` named that failure
        in its own docstring and could not see it, because its fixture has one
        tag per line. This is the fixture that can.
        """
        body = "def f(a):\n    # survivor: branch -- the branch operator only.\n    if a > 0:\n"
        box = self.tree(boxes, body)
        where = box / "tupferl" / "sync.py"

        def boundary(text: str) -> mutate.Result:
            # Against the file as it stands: inserting a tag moves every offset
            # below it, so a real second sweep regenerates its table first.
            at = text.index("if a > 0")
            return mutate.Result(
                Mutation(
                    "tupferl/sync.py:3 in f() -- boundary",
                    "tupferl/sync.py",
                    "if a > 0",
                    "if True",
                    "tests.test_sync",
                    span=(at, at + 8),
                    operator="boundary",
                ),
                mutate.Verdict("survived", ""),
            )

        with support.quiet():
            mutate._accept(mutate.Survivors([boundary(body)], [], []), box)
        after = where.read_text(encoding="utf-8")
        assert after.count("TODO") == 1, f"a tag was stacked rather than added\n{after}"

        tags = mutants.Tags(after)
        line = mutants.Offsets(after).line_of(after.index("if a > 0"))
        assert tags.operators(line) == {"branch", "boundary"}
        both = (tags.excuse(line, "branch"), tags.excuse(line, "boundary"))
        assert all(both), "a tag in the block became unreadable"
        assert "the branch operator only." in (both[0][1] if both[0] else "")
        assert "TODO" in (both[1][1] if both[1] else "")

        # And a second --accept adds nothing, because the row is now excused.
        with support.quiet():
            mutate._accept(mutate.Survivors([boundary(after)], [], []), box)
        assert where.read_text(encoding="utf-8").count("TODO") == 1

    def test_the_count_of_excused_rows_is_always_printed(self) -> None:
        """A baseline whose size is invisible is one nobody re-reads: the number
        going up unnoticed is how a record stops meaning "understood" and starts
        meaning "ignored"."""
        row = self.row("y = 2\n", "y = 2")
        with support.quiet() as said:
            mutate._report_known(mutate.Survivors([], [(row, "read")], []))
        assert "1 survivor(s) excused" in said.getvalue()

    def test_tags_that_still_say_todo_are_counted_out_loud(self) -> None:
        """A `TODO` tag silences its row exactly as a written reason does -- that
        is what makes `--accept` usable at all -- so without this line the
        unfinished ones are invisible and a green sweep is a claim nobody made.

        93 of this tree's 159 tags arrived unfinished from the record they
        replaced, carrying `reached.py`'s classification ("weak fixture or
        equivalent") rather than anybody's decision.
        """
        row = self.row("y = 2\n", "y = 2")
        with support.quiet() as said:
            mutate._report_known(
                mutate.Survivors([], [(row, "TODO: why?"), (row, "a real reason")], [])
            )
        assert "2 survivor(s) excused" in said.getvalue()
        assert "1 of those say TODO" in said.getvalue()

    def test_unanswerable_rows_are_counted_apart_from_the_rest(self) -> None:
        """The denominator, said by the run rather than by a document.

        A row whose mutation disables the bound its own probe runs under is not a
        guard the suite failed to provide -- it is a question a sweep cannot ask,
        so `caught / answered` is over a smaller table than the row count
        suggests. Nineteen of `tools/mutate.py`'s 1030 rows on 2026-08-31.

        Keyed on the word in the reason, as `TODO` is, because a tag is free text.
        """
        row = self.row("y = 2\n", "y = 2")
        with support.quiet() as said:
            mutate._report_known(
                mutate.Survivors(
                    [], [(row, "unanswerable: the pool deadlocks"), (row, "equivalent")], []
                )
            )
        assert "2 survivor(s) excused" in said.getvalue()
        assert "1 of those are unanswerable under a sweep" in said.getvalue()

    def test_a_record_with_nothing_unanswerable_says_so_by_silence(self) -> None:
        """The other half, for `TODO`'s reason: a line on every run is noise."""
        row = self.row("y = 2\n", "y = 2")
        with support.quiet() as said:
            mutate._report_known(mutate.Survivors([], [(row, "equivalent")], []))
        assert "unanswerable" not in said.getvalue()

    def test_a_finished_record_says_nothing_about_todo(self) -> None:
        """The other half: a line on every clean run trains the eye past it."""
        row = self.row("y = 2\n", "y = 2")
        with support.quiet() as said:
            mutate._report_known(mutate.Survivors([], [(row, "a real reason")], []))
        assert "TODO" not in said.getvalue()

    def test_a_spent_tag_is_named_rather_than_counted(self) -> None:
        """Few enough to list, and the one way this record becomes a mute list."""
        with support.quiet() as said:
            mutate._report_known(mutate.Survivors([], [], ["sync.py:9 -- now caught"]))
        assert "spent tag" in said.getvalue()
        assert "sync.py:9" in said.getvalue()

    def test_a_run_with_nothing_to_say_says_nothing(self) -> None:
        """Every hand-written spec file. A line on every `verify()` run is noise
        that trains the eye past the line that matters."""
        with support.quiet() as said:
            mutate._report_known(mutate.Survivors([], [], []))
        assert said.getvalue() == ""


class TestWhatBaselineOnlyAnswers:
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

    def asked(self, *verdicts: mutate.Verdict) -> tuple[bool, str, list[Sequence[str]]]:
        """Drive it with one canned verdict per shard, and record what each
        lane was asked to run.

        Recorded as it arrived, not re-typed. A `list(shard)` here would let the
        assertion below pass against a caller that had converted the shard on the
        way in, which is exactly the conversion this class exists to say is not
        happening.
        """
        seen: list[Sequence[str]] = []
        answers = list(verdicts)

        def borrow(_available: Any, shard: Sequence[str], *rest: Any) -> mutate.Verdict:
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
        assert green
        assert "all green" in said

    def test_one_red_shard_is_enough(self) -> None:
        """It is an `and` over the shards. A tree where any shard fails is a
        tree where no verdict above it means anything."""
        green, said, _ = self.asked(
            mutate.Verdict("survived", ""), mutate.Verdict("caught", "boom")
        )
        assert not green
        assert "NOT green" in said

    @pytest.mark.parametrize("outcome", ["broke", "timeout"])
    def test_a_shard_that_asked_nothing_is_red_too(self, outcome: mutate.Outcome) -> None:
        """`broke` and `timeout` are not passes. The old wording asserted a
        failure that may not have happened; what matters is that neither is
        evidence the tree is sound."""
        green, _, _ = self.asked(mutate.Verdict(outcome, "d"))
        assert not green, f"{outcome} was read as a pass"

    def test_it_asks_about_exactly_the_shards_the_sweep_will(self) -> None:
        """`baseline_shards` rather than a second spelling of it -- the flag is
        worth nothing if it asks a different question from the run it predicts,
        and it went stale that way once already."""
        table = [
            Mutation(f"row {n}", "tupferl/sync.py", "a", "b", f"tests.shard{n}") for n in range(2)
        ]
        _, _, seen = self.asked(mutate.Verdict("survived", ""), mutate.Verdict("survived", ""))
        assert seen == mutate.baseline_shards(table)

    def test_a_red_shard_says_which_and_why(self) -> None:
        """A red baseline is the one verdict that cannot be diagnosed by
        re-running the row: the shard is rarely reproducible by hand, so the
        reason has to come out with it."""
        _, said, _ = self.asked(mutate.Verdict("caught", "tests.shard0.T.test_x failed"))
        assert "tests.shard0" in said
        assert "test_x failed" in said


class TestWhatVerifyReturns:
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
        assert self.counted(self.outcomes("caught", "caught")) == 0

    def test_every_survivor_is_counted(self) -> None:
        """Not "was there one". A spec file's author reads this number to know
        how much is wrong, and `1` for three survivors is a wrong answer that
        still exits red -- which is why an exit status alone cannot check it."""
        assert self.counted(self.outcomes("survived", "caught", "survived", "survived")) == 3

    def test_a_row_that_asked_nothing_is_not_a_survivor(self) -> None:
        """`broke` and `timeout` asked nothing. Counting them as survivors
        would send an author to rewrite a test that was never weak; not
        counting them at all is what the whole-table count below is for."""
        assert self.counted(self.outcomes("broke", "timeout", "caught")) == 0

    def test_a_red_baseline_condemns_the_whole_table(self) -> None:
        """Every verdict above a red baseline is meaningless, so the count is
        the table's size rather than its survivors -- and a `0` there would let
        a spec file pass on a tree where nothing was proven at all."""
        assert self.counted(self.outcomes("caught", "caught", "caught", red=True)) == 3

    def test_a_red_baseline_beats_a_clean_looking_table(self) -> None:
        """The precondition for the test above: with every row caught, the
        survivor count is 0, so only the baseline check can produce a non-zero
        answer. Without this pairing, "returns 3" is satisfied by a function
        that ignores the baseline and counts something else."""
        assert self.counted(self.outcomes("caught", "caught", "caught")) == 0


class TestWhatMemoryTheMachineWillAdmitTo:
    """`_visible_memory`: the smallest of everything that bounds this process.

    Fourteen of its fifteen mutants survived, all on lines the suite executes.
    Its own docstring names the failure: "in a 2 GiB container on a 62 GiB host
    it answers 62 and the container is OOM-killed with every per-lane cap
    respected." Dropping the `limits.append` restores that bug exactly, and
    nothing noticed.

    Each limit is supplied by a file this test writes or a variable it sets, so
    the answer is about the arithmetic rather than about this machine.
    """

    def limits(self, boxes: support.Boxes, cgroup: int | None = None, /, **environment: str) -> int:
        """The answer on a machine whose only limits are the ones given here.

        Positional-only for the reason `TestWhoOwnsTheMachine.budget` is:
        `**environment` carries variable names, and a keyword parameter beside
        it is one renamed constant away from a caller setting this instead.
        """
        box = boxes.make("tupferl-limits-")
        where = box / "memory.max"
        if cgroup is not None:
            where.write_text(f"{cgroup}\n", encoding="utf-8")
        # Pointed at a file this test wrote, so the cgroup arm is reachable at
        # all -- the paths were two literals inside two functions until now, and
        # nothing could put a limit where either would look.
        seen = mock.patch.object(mutate, "CGROUPS", (str(where),))
        with seen, mock.patch.dict(os.environ, environment, clear=True):
            return mutate._visible_memory()

    def test_a_cgroup_that_says_max_is_not_a_number(self, boxes: support.Boxes) -> None:
        """cgroup v2 writes the literal `max` for "no limit". Read as a number
        it raises `ValueError` out of `_visible_memory`, which runs before every
        sweep -- so the tool would refuse to start on any machine whose cgroup
        is unlimited, which is most of them. Nothing wrote that word before.
        """
        box = boxes.make("tupferl-limits-")
        where = box / "memory.max"
        where.write_text("max\n", encoding="utf-8")
        with (
            mock.patch.object(mutate, "CGROUPS", (str(where),)),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            assert mutate._visible_memory() > 0

    def test_the_host_total_is_one_of_the_limits(self, boxes: support.Boxes) -> None:
        """With no cgroup and no inherited budget, what this machine physically
        has is the only bound left -- and it has to be *in* the list. Dropping
        the append restores the bug this function was written for: a 2 GiB
        container on a 62 GiB host answering 62.

        Asserted against `sysconf` rather than a constant, because the number is
        this machine's and the claim is that it reached the answer.
        """
        physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        assert self.limits(boxes) == physical

    def test_a_budget_named_in_the_environment_binds(self, boxes: support.Boxes) -> None:
        """`TUPFERL_MUTATE_BUDGET` is what a nested harness inherits, and it is
        the one limit a test can set without a kernel."""
        asked = 3 << 30
        assert self.limits(boxes, **{mutate._BUDGET: str(asked)}) <= asked

    def test_the_smallest_limit_wins(self, boxes: support.Boxes) -> None:
        """`min`, not the first found. A host with plenty of RAM and a small
        inherited budget must answer the budget -- that is the whole point."""
        small, large = 1 << 30, 900 << 30
        assert self.limits(boxes, **{mutate._BUDGET: str(small)}) <= small
        assert self.limits(boxes, **{mutate._BUDGET: str(large)}) > small

    @pytest.mark.parametrize("said", ["", "0", "-1", "lots"])
    def test_nonsense_in_the_variable_is_ignored_rather_than_obeyed(
        self, boxes: support.Boxes, said: str
    ) -> None:
        """A limit of zero or a word is not a limit. Obeying it would hand every
        lane a ceiling of nothing, and the run would fail for a reason no output
        explains."""
        assert self.limits(boxes, **{mutate._BUDGET: said}) == self.limits(boxes)

    def test_a_cgroup_ceiling_binds_below_the_host(self, boxes: support.Boxes) -> None:
        """The bug the function was written for: "in a 2 GiB container on a 62
        GiB host it answers 62 and the container is OOM-killed with every
        per-lane cap respected." A limit the kernel has carved out has to win."""
        assert self.limits(boxes, 2 << 30) == 2 << 30

    def test_a_cgroup_that_says_nothing_is_not_a_limit(self, boxes: support.Boxes) -> None:
        """A missing file is the ordinary case on a machine with no cgroup, and
        reading it as zero would hand every lane a ceiling of nothing."""
        assert self.limits(boxes, None) > 0

    def test_it_never_answers_zero(self, boxes: support.Boxes) -> None:
        """Zero divides into `_affordable` and `_share`. A machine that will say
        nothing at all still has to run something."""
        assert self.limits(boxes) > 0


class TestWhichProcessesALaneAnswersFor:
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
        assert mutate._lane(10, escaped) == {10, 11, 12}

    def test_the_descendants_alone_are_not_enough(self) -> None:
        """The other half, and the fixture that makes the first one mean
        something. A process in the group whose parent is elsewhere -- a `git`
        the suite forked and then reparented -- is in the group and not in the
        tree."""
        adopted = self.table((10, 1, 10), (20, 1, 10))
        assert mutate._lane(10, adopted) == {10, 20}

    def test_another_lane_is_not_swept_in(self) -> None:
        """The assertion that stops "return everything" from passing. Two lanes
        run side by side in every real sweep, and a membership rule that claims
        both would have `_end` killing a lane that was working."""
        two = self.table((10, 1, 10), (11, 10, 10), (30, 1, 30), (31, 30, 30))
        assert mutate._lane(10, two) == {10, 11}
        assert mutate._lane(30, two) == {30, 31}

    def test_a_pid_the_table_does_not_hold_is_not_invented(self) -> None:
        """The table is a snapshot and processes exit while it is being read.
        A member that is no longer there must not be returned, or `_end_lane`
        signals a pid the kernel has since handed to something else."""
        gone = self.table((10, 1, 10), (11, 10, 10))
        del gone[11]
        assert mutate._lane(10, gone) == {10}

    def test_a_leader_that_is_gone_answers_for_nothing(self) -> None:
        """`release` may run after the lane has exited. An empty answer is
        right; a `KeyError` would take the sampler thread down with it."""
        assert mutate._lane(99, self.table((10, 1, 10))) == set()

    #: A grandchild that leaves the group, printed by the middle process so the
    #: test knows its pid without guessing. Bounded well under the harness's own
    #: 30s per-test alarm, and killed by a finalizer either way.
    ESCAPE = (
        "import os, subprocess, sys, time;"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'],"
        " preexec_fn=os.setsid);"
        "print(g.pid, flush=True); time.sleep(20)"
    )

    def test_a_real_nested_probe_that_left_the_group_is_still_found(
        self, request: pytest.FixtureRequest
    ) -> None:
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
        request.addfinalizer(leader.wait)
        request.addfinalizer(leader.kill)
        assert leader.stdout is not None
        escapee = int(leader.stdout.readline().strip())
        request.addfinalizer(functools.partial(self.reap, escapee))

        table = mutate._processes()
        assert table[escapee].group != leader.pid, (
            "the fixture's grandchild never left the group, so it proves nothing"
        )
        assert escapee in mutate._lane(leader.pid, table), "the escapee was not counted"

    def reap(self, escapee: int) -> None:
        """The grandchild is its own session leader, so killing the tree above
        it does not reach it -- which is the whole point of the fixture."""
        with suppress(OSError):
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
        assert found == {10, 11, 12}


class TestWhatTheBaselineIsMeasuredAgainst:
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

    def rows(self, *pairs: tuple[str, Sequence[str]]) -> list[Mutation]:
        return [
            # `first=first`, not `first=tuple(first)`. The conversion looks like a guard and
            # is the opposite of one: handed a bare string it explodes it into characters,
            # which is the very shape `mutants.check` refuses. Every call site writes a tuple.
            Mutation(f"row {n}", "tupferl/sync.py", "a", "b", tests, first=first)
            for n, (tests, first) in enumerate(pairs)
        ]

    def test_one_shard_per_distinct_selection(self) -> None:
        """Distinct, not one per row: a table of two hundred rows against three
        selections is three suite runs, not two hundred."""
        table = self.rows(("tests.a", ()), ("tests.b", ()), ("tests.a", ()))
        assert mutate.baseline_shards(table) == [("tests.a",), ("tests.b",)]

    #: Eight killers, and the number is the assertion below working. The shard is
    #: built by `sorted` over a *set*, and CLAUDE.md's rule for that is here in
    #: full: a set iterates in hash order, Python randomises it per run, so
    #: `sorted` becoming `list` is caught only when that order happens to differ
    #: from sorted. Two names is a coin flip -- a guard that guards half the time
    #: and reads exactly like one that always does, and this fixture had two
    #: until a sweep reported the reversal caught and `sorted` -> `list` survived
    #: on the same line. Eight is 1 in 40320.
    KILLERS = tuple(f"tests.{letter}.{letter.upper()}.test_it" for letter in "hbfdaegc")

    def test_the_remembered_killers_get_one_shard_between_them(self) -> None:
        """One for all of them, never one each. A shard per remembered test is
        the sharding explosion that took 372s to 730s, in a new disguise.

        The exact tuple rather than its membership, which is what makes this the
        killer for both of `order`'s edits. Written unsorted above and asserted
        sorted here, so neither "keep the elements, drop the guarantee" nor
        "reverse them" can satisfy it.
        """
        table = self.rows(*(("tests.a", (killer,)) for killer in self.KILLERS))
        shards = mutate.baseline_shards(table)
        assert len(shards) == 2, shards
        assert tuple(sorted(self.KILLERS)) in shards

    def test_a_killer_outside_its_row_s_selection_is_still_covered(self) -> None:
        """Why the extra shard exists at all: a cached killer can name a test
        the row's own selection does not run, and an unchecked killer is the
        false `caught` the baseline exists to prevent."""
        table = self.rows(("tests.a", ("tests.elsewhere.E.test_it",)))
        covered = [name for shard in mutate.baseline_shards(table) for name in shard]
        assert "tests.elsewhere.E.test_it" in covered

    def test_no_killers_means_no_extra_shard(self) -> None:
        """The precondition. Without it, "the killers get a shard" is equally
        satisfied by a function that always appends one -- and an empty shard
        runs the whole suite, which is the one thing `baseline_shards`'
        docstring says it must not do."""
        assert mutate.baseline_shards(self.rows(("tests.a", ()))) == [("tests.a",)]

    def test_a_parametrized_killer_reaches_its_shard_whole(self) -> None:
        """The worst place in the harness for a space-joined id, and the only one
        where getting it wrong is silent in the flattering direction.

        This shard is what proves the remembered killers green, and every verdict
        a stale killer catches rests on it. Split in half, its two pieces select
        nothing -- and selecting nothing is not an error to pytest, so the shard
        comes back green having run none of the tests it exists to check. The
        sweep above it then reports a wall of `caught` rows that nothing
        verified.
        """
        killer = "tests/test_errors.py::test_the_shape[tupferl/manage.py a b]"
        table = self.rows(("tests.a", (killer,)))
        assert (killer,) in mutate.baseline_shards(table)

    def test_an_empty_table_needs_nothing_checked(self) -> None:
        assert mutate.baseline_shards([]) == []


class TestHowManyLanesFitAndHowBigEachMayBe:
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

    @pytest.mark.parametrize("wanted", [1, 3, 7, 16])
    @pytest.mark.parametrize("budget", [2 << 30, 8 << 30, 64 << 30])
    def test_the_product_never_exceeds_what_may_be_committed(
        self, budget: int, wanted: int
    ) -> None:
        """woswoar#232 in one line, and the reason the two numbers are chosen
        together rather than separately. Swept across shapes, because a single
        pair is satisfied by an implementation that happens to fit it.

        The bound is `_COMMIT` times the budget rather than the budget: a
        ceiling is headroom for a pathological row and peaks do not coincide,
        which is the argument `_COMMIT` carries. It is still a *bound* -- the
        pair is chosen together, and that is what #232 was about.
        """
        allowed = mutate._COMMIT
        share = self.sharing(budget, wanted, mutate.MEMORY)
        assert share.lanes * share.memory <= int(budget * allowed), (
            f"{share.lanes} lanes x {share.memory >> 20} MiB exceeds "
            f"{int(budget * allowed) >> 20} MiB"
        )

    def test_the_commitment_is_really_more_than_the_machine_has(self) -> None:
        """Without this, the bound above passes just as well with `_COMMIT` at
        1.0 -- so the relaxation would be untested and a silent revert to the
        old rule would cost lanes with nothing going red.

        A 4 GiB machine is the shape that shows it: the lane count comes from
        `allowed // floor` there, and each lane still gets the floor.
        """
        share = self.sharing(4 << 30, 16, mutate.MEMORY)
        assert share.lanes * share.memory > 4 << 30, (
            "the ceilings fit inside the budget, so nothing is being committed"
        )
        assert share.lanes * share.memory <= int((4 << 30) * mutate._COMMIT)

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
        assert share.lanes > (4 << 30) // mutate._FLOOR, (
            "the commitment went into ceilings nobody reaches instead of into lanes"
        )

    def test_more_memory_never_means_fewer_lanes(self) -> None:
        """Monotonicity. Nothing else here would catch a comparison flipped in
        the lane arithmetic, because any single budget still yields *a* number
        that looks plausible."""
        seen = [self.sharing(gib << 30, 16, mutate.MEMORY).lanes for gib in (2, 4, 8, 16, 64)]
        assert seen == sorted(seen), f"lanes fell as the budget grew: {seen}"

    def test_lanes_are_given_up_only_after_the_ceiling_has_been(self) -> None:
        """The order of concessions, which is `_share`'s whole argument: lower
        the ceiling first, because it is headroom for a pathological row rather
        than something an honest one spends, and give up lanes only when that
        share would fall under `_FLOOR`."""
        roomy = self.sharing(64 << 30, 8, mutate.MEMORY)
        assert roomy.lanes == 8, "a big machine gave up lanes it did not need to"
        tight = self.sharing(4 << 30, 8, mutate.MEMORY)
        assert tight.lanes < 8, "a small machine kept lanes it cannot afford"
        assert tight.memory >= mutate._FLOOR, "the ceiling went under the floor"

    def test_a_pinned_worker_count_is_kept(self) -> None:
        """`--workers` is a caller with a reason this cannot see --
        `TestItRunsThemInParallel` pins four to assert that mutations overlap at
        all, and on a machine too small to afford four it would otherwise assert
        the machine rather than the mechanism."""
        assert self.sharing(2 << 30, 9, mutate.MEMORY, pinned=True).lanes == 9

    def test_the_ceiling_still_shrinks_around_a_pin(self) -> None:
        """The half of pinning that is worth having: the *count* is the caller's
        to fix, the ceiling is not."""
        share = self.sharing(4 << 30, 16, mutate.MEMORY, pinned=True)
        assert share.memory < mutate.MEMORY, "a pinned run kept a ceiling it cannot fund"

    def test_no_cap_passes_straight_through(self) -> None:
        """`--memory 0` is "no cap", spelled the way `--limit 0` beside it
        already means. There is no product to bound once one factor is infinite,
        and quietly imposing one would be the flag lying."""
        share = self.sharing(1 << 30, 12, 0)
        assert share.lanes == 12
        assert share.memory == 0

    def test_there_is_always_at_least_one_lane(self) -> None:
        """A budget under one lane's floor still has to run. Zero lanes is a
        pool that never starts and a sweep that reports nothing."""
        assert self.sharing(1 << 20, 4, mutate.MEMORY).lanes >= 1
        assert self.sharing(1 << 20, 0, mutate.MEMORY).lanes >= 1

    def test_the_ceiling_never_exceeds_what_was_asked_for(self) -> None:
        """`--memory` is an upper bound the caller set. A roomy machine may not
        raise it -- a caller who already sandboxed us meant it."""
        share = self.sharing(64 << 30, 2, 512 << 20)
        assert share.memory <= 512 << 20

    def test_affordable_divides_the_budget_by_what_a_lane_uses(self) -> None:
        """`_LANE`, not `MEMORY`. Dividing by the *ceiling* assumes every lane is
        simultaneously pathological -- the over-restriction woswoar#227 removed,
        where a 16 GiB laptop dropped to two lanes and a 7 GiB runner to one."""
        with mock.patch.object(mutate, "_budget", lambda: 16 << 30):
            assert mutate._affordable() == (16 << 30) // mutate._LANE

    def test_affordable_never_answers_zero(self) -> None:
        """A machine too small for one lane still gets one; the ceiling is what
        stops it, within seconds, rather than a pool that never starts."""
        with mock.patch.object(mutate, "_budget", lambda: 1 << 20):
            assert mutate._affordable() == 1


class TestTheRunAccountsForItsLanes:
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
        assert "1 lane(s)" in said
        assert "see tools.mutate._share" in said

    def test_it_stays_quiet_when_nothing_was_taken_away(self) -> None:
        """The other half. Without it, "always print" passes the test above and
        every run carries a line about a limit that did not bind."""
        assert "lane(s)" not in self.lines(8, mutate.Share(self.WANTED, mutate.MEMORY))

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
        assert asked == [40]


class TestWhatOrderTheFirstTestsRunIn:
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

    def first_for(self, mutation: Mutation, front: str = FRONT) -> list[str]:
        """What `_attempt` hands `_run` as its `first`, for one row.

        A list, because that is the slot's shape now: `_run` JSON-encodes it
        rather than joining it with spaces, so that a nodeid containing one
        survives the argv. Read here as the list it is -- `str()` on it and a
        `split()` afterwards would compare the repr's brackets and quotes and
        agree with almost nothing.
        """
        seen: list[list[str]] = []

        def watch(*args: object, **kw: object) -> mutate.Verdict:
            seen.append([str(name) for name in typing.cast(Sequence[str], kw["first"])])
            return mutate.Verdict("caught", "probe", killer=self.KILLER)

        learned = mutate.Learned()
        learned.saw(front)
        available: queue.Queue[Path] = queue.Queue()
        with support.tempdir(prefix="tupferl-test-") as root:
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
        got = self.first_for(self.row(first=(self.KILLER,), exact=True))
        assert got == [self.KILLER, self.FRONT]

    def test_a_general_prefix_runs_after_it(self) -> None:
        """The other half, and without it "always put `first` in front" passes
        the test above. The cheap prefix is *not* about this row -- it is the
        tests that catch a lot per second across the table -- so the learned
        front, which is at least about this row's neighbours, precedes it."""
        got = self.first_for(self.row(first=(self.KILLER,), exact=False))
        assert got == [self.FRONT, self.KILLER]

    def test_the_learned_front_still_follows_a_killer_rather_than_being_dropped(
        self,
    ) -> None:
        """A recorded killer can be stale -- the code moved and the test no
        longer sees the mutation -- and the learned front is then the next
        guess before the whole selection. It costs nothing when the killer is
        right, because the killer has already answered by then.
        """
        got = self.first_for(self.row(first=(self.KILLER,), exact=True))
        assert self.FRONT in got, "the learned front was dropped, not demoted"

    def test_a_parametrized_id_survives_the_composition_from_both_sides(self) -> None:
        """The step this change exists for. `_attempt` used to build its `first`
        as `f"{mutation.first} {ahead}".split()`, a join and a re-split that is
        the identity for every id without a space in it -- which was every id in
        the tree while every test was a `TestCase` method.

        A parametrized nodeid has one, and both sides of the composition can
        carry it: the recorded killer comes from `Killers.known` and the front
        from `Learned.recent`, and each is whatever a previous verdict named. So
        both are parametrized here. Split, each becomes two names that select
        nothing, and pytest does not call selecting nothing an error -- the probe
        runs its selection alone and the row is answered as though the ordering
        had simply missed.
        """
        killer = "tests/test_sync.py::TestTheDecisionTable::test_it[a b]"
        front = "tests/test_sync.py::TestSomethingElse::test_other[c d]"
        got = self.first_for(self.row(first=(killer,), exact=True), front=front)
        assert got == [killer, front]


class TestHandingRowsOutToLanes:
    """`Work` hands out every row exactly once, in table order.

    Two claims, and they matter for different reasons. **Once** is the one that
    would corrupt something: a row handed to two lanes is counted twice in every
    number the run reports, and the second copy carries a verdict nothing ran to
    earn. It used to be worse -- the record counted occurrences of a
    content-addressed key, so a duplicated row ate a slot a genuinely new
    survivor could then not claim -- and a positional tag has removed that
    particular way of drifting in the flattering direction.

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
        assert sorted(seen) == list(range(rows)), "a row was dropped or run twice"

    def test_the_table_is_walked_front_to_back(self) -> None:
        work = mutate.Work(9)
        assert [work.take() for _ in range(9)] == list(range(9))

    def test_an_exhausted_table_says_so_rather_than_running_off_the_end(self) -> None:
        work = mutate.Work(2)
        assert [work.take(), work.take()] == [0, 1]
        assert work.take() is None
        assert work.take() is None, "a second ask past the end answered differently"

    def test_more_lanes_than_rows_hands_out_every_row_and_no_more(self) -> None:
        work = mutate.Work(3)
        assert [work.take() for _ in range(8)] == [0, 1, 2, None, None, None, None, None]


class TestOrderingTheTableByWhatItCostLastTime:
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
        assert self.order(table, self.timed(table, 1.0, 9.0, 5.0)) == ["q", "r", "p"]

    def test_a_dear_row_never_overtakes_a_file(self) -> None:
        """The claim contiguity rests on. Every row of `b.py` costs fifty times
        every row of `a.py`, so a sort over the whole table would interleave
        them -- and `sweep`'s per-file countdown would then write a file's rows
        out before they had all been answered."""
        table = self.rows(("a.py", "p"), ("a.py", "q"), ("b.py", "s"), ("b.py", "t"))
        got = self.order(table, self.timed(table, 1.0, 2.0, 100.0, 200.0))
        assert got == ["q", "p", "t", "s"]

    def test_a_row_nobody_timed_sits_at_its_file_s_median(self) -> None:
        """Four timed rows at 12, 10, 4 and 2 give a median of 7, so the cold
        row lands strictly between the 10 and the 4. Front and back are the two
        obvious wrong answers and this fixture rejects both."""
        table = self.rows(*[("a.py", tag) for tag in ("p", "q", "cold", "r", "s")])
        seconds = self.timed(table, 12.0, 10.0, None, 4.0, 2.0)
        assert self.order(table, seconds) == ["p", "q", "cold", "r", "s"]

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
        assert got[:5] == ["p", "q", "cold", "r", "s"], "the cheap file"
        assert got[5:] == ["p", "q", "cold", "r", "s"], "the dear file"

    def test_a_file_nothing_has_timed_is_left_exactly_as_it_arrived(self) -> None:
        """What a `--base` diff is almost entirely made of: its rows are new text
        by construction, so nothing remembers them. They must keep the line order
        `Learned` rests on rather than being shuffled by a tree-wide median."""
        table = self.rows(
            ("a.py", "p"), ("a.py", "q"), ("new.py", "s"), ("new.py", "t"), ("new.py", "u")
        )
        got = self.order(table, self.timed(table[:2], 1.0, 9.0))
        assert got[2:] == ["s", "t", "u"], "a file nobody has timed was reordered"

    def test_nothing_remembered_at_all_leaves_the_table_alone(self) -> None:
        table = self.rows(("a.py", "p"), ("a.py", "q"), ("b.py", "s"))
        assert self.order(table, {}) == ["p", "q", "s"]

    def test_it_says_how_much_of_the_table_it_could_order(self) -> None:
        """A silent reorder reads the same whether the cache loaded or not --
        which is exactly how `Killers.cost` was empty for a whole milestone."""
        table = self.rows(("a.py", "p"), ("a.py", "q"), ("b.py", "s"))
        with support.quiet() as said:
            mutate.slowest_first(table, self.timed(table[:1], 4.0))
        assert "1 of 3" in said.getvalue()
        assert "2 never timed" in said.getvalue()


@functools.cache
def swept_once(baseline: bool = False) -> mutate.Report:
    """One real sweep of `UNWATCHED`, shared by every test that reads it.

    Each `mutate.run` copies the tree and spawns an interpreter, and the tests
    that call this assert about the same numbers -- so running it per test
    bought nothing and cost a `copytree` and a subprocess each time.
    `functools.cache` rather than a `ClassVar` set by a fixture: same memo, no
    `None` state to narrow and no cast, and it is reachable from more than one
    class. Keyed on `baseline`, because a run with the shards has `times` the
    row's own selection cannot produce and a run without them is cheaper.

    `strict=False` for the reason `TestTheHarnessAnswersBothWays` gives: an
    unanswerable row must come back *in the report* rather than as a
    `SystemExit` that escapes whichever test happened to ask first.
    """
    return mutate.run(
        [UNWATCHED], baseline=baseline, workers=1, summarise=False, walk=False, strict=False
    )


class TestRememberingWhatEachRowCost:
    """The measurement `slowest_first` orders by, end to end.

    Driven through a real `mutate.run` rather than a hand-built `Verdict`, for
    the reason the class above `test_a_run_measures_the_tests_it_ran` gives: a
    test that builds its own inputs cannot see a data path that never delivers
    them. A `spent` that stayed 0.0 would order nothing and say nothing.
    """

    @pytest.fixture
    def report(self) -> mutate.Report:
        """The one real sweep both tests read, memoised by `swept_once`.

        A fixture over a memoised module-level function, **not a `setUpClass`**
        and not a class-scoped fixture. The saving is the same and the failure
        is not: work done outside a test is work no test answers for, and
        `verdict` files that as `broke` -- never `caught`. Measured: mutating
        `_Lanes.release` to report the memory held by every row came back
        `BROKE` here, on a line the sweep had previously reported as guarded.
        Raised inside a test, the same failure answers.
        """
        return swept_once()

    def test_a_real_run_times_the_row_it_ran(self, report: mutate.Report) -> None:
        (only,) = report.results
        assert only.verdict.spent > 0.0, "the row was not timed"

    def test_the_time_reaches_the_cache_under_the_row_s_key(self, report: mutate.Report) -> None:
        cache = mutate.Killers(None)
        cache.learn(report)
        assert list(cache.seconds) == [mutate._key(UNWATCHED)]
        assert cache.seconds[mutate._key(UNWATCHED)] == report.results[0].verdict.spent

    def test_a_row_that_was_never_answered_is_timed_too(self) -> None:
        """`broke` and `timeout` rows are the *most* expensive there are -- a
        timeout costs the whole `--timeout` -- and they are never `caught`, so a
        record kept only for answered rows would miss precisely the rows worth
        starting first."""
        row = mutants.Mutation("a.py x", "a.py", "x", "y", "tests.t")
        broke = mutate.Verdict("broke", "nothing loaded", spent=41.0)
        cache = mutate.Killers(None)
        cache.learn(mutate.Report([mutate.Result(row, broke)]))
        assert cache.seconds == {mutate._key(row): 41.0}

    def test_a_survivor_keeps_its_cost_while_losing_its_killer(self) -> None:
        """The two records answer different questions. Whatever used to catch a
        survivor demonstrably does not any more, so the killer goes; what it
        cost is still true, and a survivor is the dearest row there is."""
        row = mutants.Mutation("a.py x", "a.py", "x", "y", "tests.t")
        cache = mutate.Killers(None)
        cache.known = {mutate._key(row): "tests.t.T.test_it"}
        cache.learn(mutate.Report([mutate.Result(row, mutate.Verdict("survived", spent=70.0))]))
        assert cache.known == {}
        assert cache.seconds == {mutate._key(row): 70.0}

    def test_it_survives_a_trip_through_the_file(self) -> None:
        row = mutants.Mutation("a.py x", "a.py", "x", "y", "tests.t")
        with support.tempdir(prefix="tupferl-test-") as box:
            where = Path(box) / "killers.json"
            made = mutate.Killers(where)
            made.learn(
                mutate.Report(
                    [mutate.Result(row, mutate.Verdict("caught", killer="t.T.m", spent=3.5))]
                )
            )
            made.save()
            assert mutate.Killers(where).seconds == {mutate._key(row): 3.5}

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
        with support.tempdir(prefix="tupferl-test-") as box:
            report = Path(box) / "r.json"
            mutate._persist(
                mutate.Report([mutate.Result(row, caught)], widened=True), report, announce=False
            )
            (back,) = mutate._recorded(report)
        assert back.verdict.spent == 8.25, "the cost did not survive the report"
        cache = mutate.Killers(None)
        cache.learn(mutate.Report([back]))
        assert cache.seconds == {mutate._key(row): 8.25}

    def test_a_report_written_before_costs_existed_reads_back_untimed(self) -> None:
        """Cut from a real report rather than built by hand, so it holds exactly
        the fields `_persist` writes minus the one under test. It must read as
        "never timed" -- which `slowest_first` answers with the file median --
        rather than as a failure that loses every other row with it."""
        row = mutants.Mutation("a.py:1 in f() -- x", "a.py", "x", "y", "tests.t", span=(0, 1))
        with support.tempdir(prefix="tupferl-test-") as box:
            report = Path(box) / "r.json"
            mutate._persist(
                mutate.Report([mutate.Result(row, mutate.Verdict("survived", spent=70.0))]),
                report,
                announce=False,
            )
            written = json.loads(report.read_text(encoding="utf-8"))
            assert "seconds" in written["results"][0], "nothing was removed"
            del written["results"][0]["seconds"]
            report.write_text(json.dumps(written), encoding="utf-8")
            (back,) = mutate._recorded(report)
        assert back.verdict.outcome == "survived", "the whole row was lost"
        assert back.verdict.spent == 0.0

    def test_a_cache_written_before_costs_existed_still_loads(self) -> None:
        """The shape `killers.json` had until this landed. It must read as "no
        times recorded" rather than as a failure -- the worst an empty record
        does is run at yesterday's speed."""
        with support.tempdir(prefix="tupferl-test-") as box:
            where = Path(box) / "killers.json"
            where.write_text(json.dumps({"killers": {"k": "t.T.m"}, "costs": {"t.T.m": 1.0}}))
            cache = mutate.Killers(where)
            assert cache.seconds == {}
            assert cache.known == {"k": "t.T.m"}


class TestWhatTheFinalBlockSays:
    """The four counts, the denominator, and the refusal on a red baseline."""

    #: A run long enough that the rate is an ordinary number. Overridden by the
    #: two tests that are *about* the rate.
    PACE = mutate.Pace(10.0, 4, 2 << 30)

    def block(
        self,
        outcomes: Sequence[mutate.Outcome],
        red: bool = False,
        *,
        pace: mutate.Pace | None = None,
        headroom: bool = True,
    ) -> str:
        results = [
            mutate.Result(
                mutants.Mutation(f"f{n}.py", "x", "y", f"f{n}.py:1 in f() -- x", "tests.t"),
                mutate.Verdict(outcome),
            )
            for n, outcome in enumerate(outcomes)
        ]
        with support.quiet() as said:
            mutate._report_stats(
                results, pace=self.PACE if pace is None else pace, red=red, headroom=headroom
            )
        return said.getvalue()

    def test_all_four_outcomes_are_named_even_at_zero(self) -> None:
        """A category that vanishes when empty is one a reader stops expecting.
        `BROKE` and `TIMEOUT` are the two that matter: such a row is never
        `caught`, so the line it appears to guard is guarded by nothing."""
        said = self.block(["caught", "caught"])
        for headline in ("caught", "SURVIVED", "BROKE", "TIMEOUT"):
            assert headline in said, f"{headline} is missing from the block"

    def test_the_score_names_what_it_is_a_score_of(self) -> None:
        said = self.block(["caught", "caught", "survived", "broke"])
        assert "2 caught of 3 answered" in said
        assert "1 row(s) answered nothing" in said

    def test_a_red_baseline_gets_no_percentage_at_all(self) -> None:
        """Not a flattering number under a warning. A failing suite notices
        every mutation, and a percentage is far more seductive than a wall of
        rows -- 51 of 51 was read twice here before anyone read the line."""
        said = self.block(["caught", "caught"], red=True)
        assert "no score" in said
        assert "%" not in said

    def test_the_rate_is_reported_with_the_lane_count_beside_it(self) -> None:
        """A rate alone is comparable to nothing: this tree measured the same
        table at 1.84/s over 32 lanes and 1.49/s over 16."""
        said = self.block(["caught"] * 20)
        assert "over 4 lane(s)" in said
        assert "/s/lane" in said

    def test_a_run_that_took_no_measurable_time_reports_a_rate_of_zero(self) -> None:
        """`pace.seconds > 0`, and the guard is not decoration: a one-row table
        on a fast machine really does land on 0.00s, and dividing by it raises
        `ZeroDivisionError` out of the block a reader looks at *after* a
        successful sweep -- so the run's own answer is lost to a crash in the
        summary of it."""
        assert "0.00/s" in self.block(["caught"], pace=mutate.Pace(0.0, 4, 2 << 30))

    def test_a_run_shorter_than_a_second_still_reports_its_rate(self) -> None:
        """The other side of the same guard, and not a hypothetical: a small
        table really does finish in a fraction of a second -- these very specs
        report "0s" beside rates in the tens. A bound that read `> 1` rather
        than `> 0` would report every such run at 0.00/s, which is the number
        the guard exists to avoid, arrived at by the other route.
        """
        assert "6.00/s" in self.block(["caught"] * 3, pace=mutate.Pace(0.5, 4, 2 << 30))

    def test_rows_that_went_missing_are_named_and_counted(self) -> None:
        """The one failure nothing else here would see. An outcome this build
        has never heard of -- which `_recorded` really can rebuild from a report
        written by a newer `mutate` -- is counted in `total` and in none of the
        four lines, so without this line the block silently adds up to less than
        the table and every number in it reads as complete.
        """
        said = self.block(["caught", "caught", typing.cast(mutate.Outcome, "invented")])
        assert "2 row(s) accounted for of 3" in said
        assert "1 went missing" in said

    def test_a_complete_report_says_nothing_about_missing_rows(self) -> None:
        """The other half, and the reason there is no tick beside it: a line that
        always prints is one a reader stops reading, and CLAUDE.md's bar is that
        the sum be *silent when right and loud when wrong*."""
        assert "went missing" not in self.block(["caught", "survived"])

    def test_a_table_that_answered_nothing_gets_no_score(self) -> None:
        """`caught / answered` with no answers is a division by zero, and there
        is nothing to say anyway: a table of nothing but `broke` rows has not
        put a question, so a percentage would be a claim about no evidence."""
        said = self.block(["broke", "timeout"])
        assert "%" not in said
        assert "caught of" not in said

    def test_a_table_that_answered_everything_adds_no_caveat(self) -> None:
        """The clause is conditional for the same reason the missing-rows line
        is: "; 0 row(s) answered nothing" on every clean sweep trains a reader
        to skip the sentence that matters on the one sweep it does not."""
        said = self.block(["caught", "survived"])
        assert "1 caught of 2 answered" in said
        assert "answered nothing" not in said

    def test_the_headroom_line_can_be_left_to_the_caller(self) -> None:
        """`run` reports it itself when it is not summarising, so `main` asks for
        the block without it. Printed twice it reads as two measurements, and
        the second would be the one a reader believed."""
        # Watched rather than read out of the output: `_report_headroom` prints
        # nothing at all when no lane was sampled, which is the case in this
        # process, so asserting on the text would pass for both arms and pin
        # neither. The ceiling it is handed is asserted too -- passing the wrong
        # one reports a percentage of a number that was never in force.
        asked: list[int] = []
        with mock.patch.object(mutate, "_report_headroom", asked.append):
            self.block(["caught"], headroom=False)
            assert asked == [], "the block reported headroom it was told to skip"
            self.block(["caught"], headroom=True)
            assert asked == [self.PACE.ceiling]

    def test_a_report_with_no_pace_says_nothing_rather_than_zero(self) -> None:
        """A resumed run does not re-run anything, so it has no rate to report.
        Noughts there would be a measurement reported as a result."""
        # Driven directly rather than through `block`, which uses `pace=None` to
        # mean "the class default". This is the one case that wants a real None.
        results = [
            mutate.Result(
                mutants.Mutation("f.py", "x", "y", "f.py:1 in f() -- x", "tests.t"),
                mutate.Verdict("caught"),
            )
        ]
        with support.quiet() as said:
            mutate._report_stats(results, pace=None, red=False)
        assert said.getvalue() == ""


class TestWhatMainDoesOnTheGeneratedPath:
    """`main`'s `if args.base:` arm -- the one a person actually types.

    Forty-nine of its mutants survived the whole-tree sweep and twenty of those
    sat on lines **no test executed at all**, which is the largest unreached
    cluster in the repository. Every test in the class above drives the *spec
    file* arm instead, so the flag wiring was covered and everything the
    generated path decides -- what `--all` implies, which errors are refused,
    what the exit status is, what a watcher can read and when -- was not.

    `generated` and the two runners are stubbed, and nothing else is. What is
    under test is the sequencing and the answers, not what a mutation does: a
    real sweep here would cost minutes per assertion to re-prove something
    `TestTheHarnessAnswersBothWays` already proves once.

    `--no-killers` throughout, or these would read and rewrite the developer's
    own `sweeps/killers.json` -- a test that edits the machine it runs on.
    """

    ROW = Mutation(
        "x:1 in f()", "tupferl/merge.py", "WHOLE_FILE = 1", "WHOLE_FILE = 2", "tests.test_merge"
    )

    def drive(
        self,
        *flags: str,
        report: mutate.Report | None = None,
        runner: Any = None,
    ) -> tuple[int, str, argparse.Namespace]:
        """`main(flags)`, with the table and the run supplied.

        Hands back the exit status, everything printed, and the `Namespace`
        `generated` was given -- which is the only way to see what `--all`
        rewrote before anything ran. Unpacked rather than returned as a
        maybe-`None`: `main` calls `generated` unconditionally on this path,
        before `--list` and before `--baseline-only`, so an empty `seen` is a
        broken fixture and `(args,) = seen` says so where it happens.
        """
        seen: list[argparse.Namespace] = []

        def fake_generated(args: argparse.Namespace) -> list[Mutation]:
            seen.append(args)
            return [self.ROW]

        run = runner or (lambda *a, **k: report or mutate.Report([]))
        with (
            mock.patch.object(mutate, "generated", fake_generated),
            mock.patch.object(mutate, "sweep", run),
            mock.patch.object(mutate, "_run_generated", run),
            support.quiet() as spill,
        ):
            status = mutate.main(["--no-killers", *flags])
        (args,) = seen
        return status, spill.getvalue(), args

    def refused(self, *flags: str) -> str:
        """`main` exiting through `parser.error`, and what it said.

        `argparse` writes to stderr and raises `SystemExit(2)`; both halves are
        asserted, because a message printed on the way to exit 0 would read the
        same in a terminal and mean the opposite to a script.

        **The table and the runners are stubbed even though nothing should reach
        them.** That is the point: a mutation removing one of these guards lets
        `--all --base main` fall through to a *real* whole-tree sweep, and this
        came back `TIMEOUT` -- never `caught` -- until the stubs were added. A
        test asserting that something is refused has to fail, not run, when it
        is not.
        """

        def nothing(*a: Any, **k: Any) -> mutate.Report:
            return mutate.Report([])

        with (
            mock.patch.object(mutate, "generated", lambda args: [self.ROW]),
            mock.patch.object(mutate, "sweep", nothing),
            mock.patch.object(mutate, "_run_generated", nothing),
            # `quiet` takes stderr as well as stdout, which is what argparse
            # writes to -- so it is both the silencer and the capture, and a
            # nested `redirect_stderr` would only fight it for the same stream.
            support.quiet() as said,
            pytest.raises(SystemExit) as bad,
        ):
            mutate.main([*flags, "--no-killers"])
        assert bad.value.code == 2
        return said.getvalue()

    def test_all_and_base_together_are_refused(self) -> None:
        """They mean different tables. Taking either silently would run the one
        the user did not ask for, over a table size that differs by 20x."""
        assert "Not both" in self.refused("--all", "--base", "main")

    def test_neither_a_script_nor_a_table_is_refused(self) -> None:
        assert "give a spec file" in self.refused()

    def test_a_script_and_a_table_together_are_refused(self) -> None:
        """The same message, and the same `bool(...) == bool(...)` line. Without
        this the mutation that drops one side of the comparison is invisible:
        the empty case above still refuses."""
        assert "give a spec file" in self.refused("spec.py", "--base", "main")

    def test_all_becomes_a_base_of_its_own(self) -> None:
        """Downstream asks "generated or a spec file?" and nothing else, so
        `--all` has to arrive as a `base`. Left unset it falls through to the
        spec-file arm with no script and raises about a file nobody named."""
        _, _, args = self.drive("--all")
        assert args.base == "--all"

    def test_all_lifts_the_default_cap(self) -> None:
        """The cap is sized for a diff. Left on, the documented `--all` ran 200
        rows of 4451 -- and, because the cap spreads across files, in batches of
        seven, so batching, incremental `--json` and resume all did nothing."""
        _, _, args = self.drive("--all")
        assert args.limit == 0

    def test_an_explicit_cap_survives_all(self) -> None:
        """The other half. A rule that lifted the cap unconditionally passes the
        test above and takes `--limit` away from the one command that most needs
        it -- and says nothing, because the count would look deliberate."""
        _, _, args = self.drive("--all", "--limit", "5")
        assert args.limit == 5

    def test_a_base_of_its_own_leaves_the_cap_alone(self) -> None:
        """`--base` is a diff, which is what the default cap is for."""
        _, _, args = self.drive("--base", "main")
        assert args.limit == mutate.LIMIT

    def test_list_prints_the_table_and_runs_nothing(self) -> None:
        """`--list` is about the table, not about running it. A version that
        listed *and* ran would look right on a five-row diff and start an hour
        of sandboxes on the command a reader types to avoid exactly that."""
        ran: list[int] = []
        status, said, _ = self.drive(
            "--base", "main", "--list", runner=lambda *a, **k: ran.append(1)
        )
        assert status == 0
        assert "x:1 in f()" in said
        assert ran == [], "--list ran the table"

    @pytest.mark.parametrize(("green", "expected"), [(True, 0), (False, 1)])
    def test_baseline_only_answers_with_the_exit_status(self, green: bool, expected: int) -> None:
        """Its whole point: ask in one shard's time the question a sweep asks in
        an hour, and say so in a way a script can read. Both arms, because a
        constant return passes either one alone."""
        with mock.patch.object(mutate, "_baseline_is_green", lambda *a, _g=green: _g):
            status, _, _ = self.drive("--base", "main", "--baseline-only")
        assert status == expected

    def test_baseline_only_starts_no_sandbox(self) -> None:
        """Before the prefix is announced and before any sandbox is built. A
        version that asked after building them would still answer correctly and
        would cost a sweep's setup to say it."""
        ran: list[int] = []
        with mock.patch.object(mutate, "_baseline_is_green", lambda *a: True):
            self.drive("--base", "main", "--baseline-only", runner=lambda *a, **k: ran.append(1))
        assert ran == []

    def test_the_json_pidfile_is_written_before_the_first_row(self) -> None:
        """A watcher started alongside this one has to have something to read
        straight away -- and `tools/watch.py` refuses to identify a job by
        pattern, so the pidfile is the only identity there is. Written *after*
        the rows it would name a sweep that had already finished."""
        seen: list[str] = []
        with support.tempdir(prefix="tupferl-test-") as name:
            where = Path(name) / "r.json"

            def note(*a: Any, **k: Any) -> mutate.Report:
                seen.append(mutate._pidfile(where).read_text(encoding="utf-8").strip())
                return mutate.Report([])

            self.drive("--base", "main", "--json", str(where), runner=note)
        assert seen == [str(os.getpid())], "the pidfile named the wrong process, or none"

    def test_the_done_marker_is_cleared_before_the_run_and_set_after(self) -> None:
        """A resumed sweep points `--json` at a part-written report, and a marker
        the interrupted run left would tell a watcher that *this* one had
        finished before it began. Both halves, because clearing alone leaves a
        run nothing can wait on and setting alone is the stale marker."""
        seen: list[bool] = []
        with support.tempdir(prefix="tupferl-test-") as name:
            where = Path(name) / "r.json"
            mutate._marker(where).touch()

            def note(*a: Any, **k: Any) -> mutate.Report:
                seen.append(mutate._marker(where).exists())
                return mutate.Report([])

            self.drive("--base", "main", "--json", str(where), runner=note)
            assert seen == [False], "a stale done marker survived into the run"
            assert mutate._marker(where).is_file(), "the finished run left no marker"

    def test_listing_a_table_does_not_retract_an_earlier_marker(self) -> None:
        """`--list` returns before the `--json` block on purpose: listing is not
        a run, and it must not tell a watcher that a complete run had not
        happened."""
        with support.tempdir(prefix="tupferl-test-") as name:
            where = Path(name) / "r.json"
            mutate._marker(where).touch()
            self.drive("--base", "main", "--json", str(where), "--list")
            assert mutate._marker(where).is_file()

    def test_a_clean_run_exits_zero_and_a_survivor_does_not(self) -> None:
        """Both arms of `_status`. A constant zero is the shape that makes every
        sweep in CI green, which is the failure this whole tool exists to make
        impossible."""
        caught = mutate.Report([mutate.Result(self.ROW, mutate.Verdict("caught", "d", "t.A.b"))])
        alive = mutate.Report([mutate.Result(self.ROW, mutate.Verdict("survived"))])
        assert self.drive("--base", "main", report=caught)[0] == 0
        assert self.drive("--base", "main", report=alive)[0] != 0

    def test_a_red_baseline_remembers_nothing_and_says_so(self) -> None:
        """A killer recorded from a red tree is a test that fails untouched,
        which is exactly what must never be run in front of a later row. This is
        the supply line for the false `caught` the baseline shard guards
        against, and both ends have to be closed."""
        red = mutate.Report([], baseline_red=True)
        learned: list[object] = []
        with mock.patch.object(mutate.Killers, "learn", lambda self, r: learned.append(r)):
            _, said, _ = self.drive("--base", "main", report=red)
        assert learned == [], "a red run's killers were remembered"
        assert "nothing was remembered" in said

    def test_a_green_run_does_remember(self) -> None:
        """The other half, without which "never remember" passes the test above
        and silently turns off the ordering two sweeps were measured to gain."""
        learned: list[object] = []
        with mock.patch.object(mutate.Killers, "learn", lambda self, r: learned.append(r)):
            self.drive("--base", "main")
        assert len(learned) == 1

    @pytest.mark.parametrize(
        "flags", [("--all",), ("--base", "main"), ("--all", "--only", "tupferl/")]
    )
    def test_a_narrowed_run_says_nothing_about_the_tags_it_did_not_reach(
        self, flags: Sequence[str]
    ) -> None:
        """What `complete` used to arrange, now true by construction.

        A hash record could only call an entry stale if the run had generated
        everything, so a flag had to say whether it had -- and a `--base` run
        reported 206 of 210 entries stale, which `--accept` then *dropped*.
        A tag is judged where it sits: a run that never generated a row for a
        line never looks at that line's tag, so a narrowed run reports exactly
        the tags it reached and nothing else. There is no flag left to get wrong.
        """
        seen: list[Any] = []
        with mock.patch.object(
            mutate,
            "sort_survivors",
            lambda r, *a, _s=seen: _s.append(r) or mutate.Survivors([], [], []),
        ):
            self.drive(*flags)
        assert seen, "sort_survivors was never asked"
        # Only the rows this run produced are ever offered to it, which is the
        # whole of the guarantee `complete` was reaching for.
        assert all(rows == seen[0] for rows in seen)


class TestHowOneRunsOutcomeIsClassified:
    """`_run`'s ladder: which `Verdict` a probe's report becomes.

    Twenty-one of its mutants survived the whole-tree sweep and twelve were on
    lines **no test executed**, which is the second-largest unreached cluster.
    The reason is visible from the function: every arm needs a probe that
    behaved a particular way -- killed for memory, killed before writing,
    unable to load, having noticed nothing -- and the suite only ever produced
    the two ordinary ones by running a real mutation.

    So the probe is faked and *everything else is real*: the fake writes a
    chosen report into the path `_run` gave it on the command line, which is
    how the real probe communicates, and the ladder reads it exactly as it
    reads the real one. Nothing here asserts that `_run` calls the functions it
    calls; each test names an outcome a user would see in a sweep.
    """

    #: Every key the ladder reads. Required rather than `.get`, deliberately --
    #: `_probe` runs `verdict.py` out of the same tree, so a missing key is a
    #: protocol break and not an old probe. Spelling them all out here is what
    #: makes a *changed* protocol fail these tests rather than pass them.
    GREEN: typing.ClassVar[dict[str, Any]] = {
        "loaded": True,
        "broke": [],
        "noticed": [],
        "killers": [],
        "reasons": [],
        "times": {},
        "ran": 3,
    }

    def verdict(
        self,
        written: dict[str, Any] | None = None,
        *,
        returncode: int = 0,
        held: int = 0,
        stderr: str = "",
        hang: bool = False,
        **how: Any,
    ) -> mutate.Verdict:
        """`_run` against a probe that behaved as described.

        The spawn itself is recorded on `self.spawned` as well, because
        `TestWhatEveryProbeIsHandedOnItsCommandLine` asks a different question of
        the same fake -- what argv and environment `_run` built -- and a second
        copy of this `Popen` stand-in was already drifting from this one within a
        single change.
        """
        spawned: dict[str, Any] = {}
        self.spawned = spawned

        class Probe:
            pid = 4242

            def __init__(self, argv: list[str], **kwargs: Any) -> None:
                # argv[4] is the report path: `_probe()` is argv[3], and the
                # first thing it is handed is where to write. Reading it back
                # off the command line rather than reaching into `_run` is what
                # keeps this a test of the protocol.
                self.returncode = returncode
                spawned["argv"], spawned["env"] = list(argv), dict(kwargs["env"])
                Path(kwargs["stderr"].name).write_text(stderr, encoding="utf-8")
                if written is not None:
                    Path(argv[4]).write_text(json.dumps(written), encoding="utf-8")

            def wait(self, timeout: float | None = None) -> int:
                if hang:
                    raise subprocess.TimeoutExpired("probe", timeout or 0)
                return self.returncode

        class Watched:
            def watch(self, pid: int, memory: int) -> None:
                pass

            def release(self, pid: int) -> int:
                return held

        with (
            support.tempdir(prefix="tupferl-probe-") as root,
            mock.patch.object(subprocess, "Popen", Probe),
            mock.patch.object(mutate, "_WATCHED", Watched()),
            mock.patch.object(mutate, "_end", lambda probe: None),
        ):
            # `how` is whatever the caller wants `_run` itself told -- `first`,
            # `walk`, `failfast`. Nothing here reads them; they are how
            # `TestWhatEveryProbeIsHandedOnItsCommandLine` varies the spawn.
            return mutate._run(["tests.test_paths"], Path(root), memory=1 << 31, **how)

    def test_a_probe_that_never_answers_is_a_timeout(self) -> None:
        """Its own outcome, not an exception: a generated mutant can turn a loop
        bound into one that never fires, and with no limit that holds a lane for
        the rest of the run."""
        found = self.verdict(self.GREEN, hang=True)
        assert found.outcome == "timeout"
        assert "no answer within" in found.detail

    def test_a_lane_killed_for_memory_broke_rather_than_answered(self) -> None:
        """And it is read *before* the report, which is the whole subtlety. A
        killed lane may well have written one -- the kill lands on whichever
        process is running -- and reading it would report a verdict about a run
        that was stopped."""
        found = self.verdict(self.GREEN, held=3 << 30)
        assert found.outcome == "broke"
        assert "MiB" in found.detail
        assert "killed" in found.detail

    def test_a_lane_killed_for_memory_says_so_even_with_no_report(self) -> None:
        """The ordering, and the only fixture that can see it.

        A killed lane usually writes *something*, and then both orders agree --
        measured: moving the `held` check below the report read survives every
        assertion in this class until the report is missing too. That is the
        real case: the kill lands on whichever process is running, so the probe
        often dies before writing at all. Read second, this row reports a
        signal number, and "killed by SIGKILL" sends a reader looking for a
        crash instead of at the memory ceiling that caused it.
        """
        found = self.verdict(None, returncode=-9, held=3 << 30)
        assert found.outcome == "broke"
        assert "MiB" in found.detail
        assert "share it was given" in found.detail

    def test_a_probe_that_wrote_nothing_says_what_reached_stderr(self) -> None:
        """Killed before it could write. A process killed by a signal writes no
        stderr either, so this row used to print with no reason at all -- and it
        is exactly what a host OOM-kill produces."""
        found = self.verdict(None, stderr="Traceback ...\nMemoryError\n")
        assert found.outcome == "broke"
        assert "MemoryError" in found.detail

    def test_a_probe_that_wrote_nothing_and_said_nothing_names_the_signal(self) -> None:
        """The half with no stderr at all, which is the OOM-kill case. Without
        it the row prints an empty reason, and an empty reason reads as the
        harness malfunctioning rather than as the machine running out."""
        found = self.verdict(None, returncode=-9)
        assert found.outcome == "broke"
        assert found.detail.strip() != "", "a killed probe explained nothing"

    def test_a_module_that_would_not_load_prefers_the_recorded_reason(self) -> None:
        """`verdict.main` writes `why` deliberately; `_tail` is whatever happened
        to reach stderr. Preferring the tail let a stray import-time warning
        outrank the reason the probe took the trouble to record."""
        found = self.verdict(
            {**self.GREEN, "loaded": False, "why": "SyntaxError: bad"},
            stderr="DeprecationWarning: unrelated\n",
        )
        assert found.outcome == "broke"
        assert "SyntaxError" in found.detail
        assert "Deprecation" not in found.detail

    def test_a_module_that_would_not_load_falls_back_to_stderr(self) -> None:
        """The other half: `why` can be absent, and then the tail is all there
        is. Without this, "always take `why`" passes the test above and turns a
        real failure into a blank line."""
        found = self.verdict(
            {**self.GREEN, "loaded": False, "why": ""}, stderr="ImportError: no module\n"
        )
        assert "ImportError" in found.detail

    def test_a_test_that_errored_outside_an_assertion_broke(self) -> None:
        """`broke` in the report is the probe's own word for "this asked
        nothing", and it outranks everything below -- a run that errored has not
        answered, however many tests it also ran."""
        found = self.verdict({**self.GREEN, "broke": ["it went wrong"], "noticed": ["t.A.b"]})
        assert found.outcome == "broke"
        assert "it went wrong" in found.detail

    def test_a_notice_becomes_caught_and_carries_its_killer(self) -> None:
        """The killer is what `Killers` runs first next time, and it is recorded
        separately from the display string on purpose: a display format is not
        an API, and `unittest`'s changed in 3.11."""
        found = self.verdict(
            {
                **self.GREEN,
                "noticed": ["test_it (t.A.test_it)"],
                "killers": ["t.A.test_it"],
                "reasons": ["AssertionError: 1 != 2"],
                "times": {"t.A.test_it": 0.5},
            }
        )
        assert found.outcome == "caught"
        assert found.killer == "t.A.test_it"
        assert "AssertionError" in found.why
        assert found.times == {"t.A.test_it": 0.5}

    def test_a_notice_with_no_remembered_killer_still_counts(self) -> None:
        """`killers` can be empty where `noticed` is not, and the row is still a
        catch. Raising instead would turn a real answer into a `broke`."""
        found = self.verdict({**self.GREEN, "noticed": ["test_it (t.A.test_it)"]})
        assert found.outcome == "caught"
        assert found.killer == ""

    def test_a_selection_that_held_no_tests_broke_rather_than_survived(self) -> None:
        """The flattering failure, and the one CLAUDE.md opens with: a run that
        executed nothing notices nothing, and calling that `survived` credits
        the mutation with beating a suite that never ran."""
        found = self.verdict({**self.GREEN, "ran": 0})
        assert found.outcome == "broke"
        assert "no tests" in found.detail

    def test_a_green_run_that_noticed_nothing_survived(self) -> None:
        """The ordinary survivor, and the timings come back with it -- a
        survivor ran its whole selection, so its numbers are the complete ones
        `slowest_first` orders the next sweep by."""
        found = self.verdict({**self.GREEN, "times": {"t.A.b": 1.5}})
        assert found.outcome == "survived"
        assert found.times == {"t.A.b": 1.5}
