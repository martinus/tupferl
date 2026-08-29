"""Revert a fix, watch its test fail, restore it -- the loop CLAUDE.md §2 asks for.

Ported from `martinus/woswoar` (Apache-2.0). The measurements quoted below were
taken there, against that project's suite; they are kept because they are the
argument for this module's shape, and attributed because a number measured
somewhere else is not a claim about this repository.

A test that passes whether or not the fix is present is decoration, and reading
it will not tell you which kind you have. The only way to know is to break the
code and watch. This does that, for a table of edits:

    from tools.mutate import Mutation

    MUTATIONS = [
        Mutation(
            "an unknown config key is accepted rather than refused",
            "tupferl/config.py",
            "if key not in KNOWN:",
            "if False:",
            "tests.test_config.TestRejectingAnUnknownKey",
        ),
    ]

Run it with ``python -m tools.mutate <script>``. The *table* is the new work and
everything below is not. Paths are relative to the working directory, so run it
from the repo root, as with `tools.run_tests`.

``MUTATIONS`` at module level is the shape, and this example used to show a
`verify(...)` call instead -- which works, and then exited non-zero with "defines
no MUTATIONS" printed *above* its own green results (woswoar#213). Calling `verify`
yourself is still supported and still correct; it is simply no longer what the
documentation teaches, because the two shapes in one file run the table twice.

Four things it does that a hand-rolled loop forgets, all of which have cost real
time here:

- **The bytecode cache lies.** A ``.pyc`` is validated against
  ``(mtime_seconds, size)``, so two mutations that change a file by the same
  number of bytes inside one second run each other's cached bytecode. That
  reported a *correct* test as decoration once, and the test was nearly
  rewritten because of it. Three things now make it impossible rather than
  merely unlikely: each copy of the tree starts with no ``__pycache__``, the
  runs are ``-B`` *and* carry ``PYTHONDONTWRITEBYTECODE`` so the real ``age``
  and ``git`` subprocesses the suite spawns cannot write one either, and a
  reused copy is swept between mutations anyway.
- **An edit that adds without removing is refused.** A replacement that still
  contains the text it replaced leaves the code under test exactly as it was, so
  the run reports "caught" or "SURVIVED" about nothing. That is the easy way to
  write a *move* wrongly -- put the whole span in ``old``, including the line you
  mean to relocate -- and it shipped three times in one session before this
  check. Pass ``additive=True`` for the rare edit that really does mean to insert
  in front of code that stays.
- **"caught" means a test method noticed, and nothing else.** A mutation that
  turns a working import into a failing one exits non-zero and leaves ``Ran 1
  test`` behind, exactly like a test catching it -- so a check reading the exit
  status and the count reported ``caught`` about a test that never executed.
  That shipped. The only fixture guarding it used a *syntax* error, which
  ``unittest.loader`` does not wrap and which therefore takes a different path
  entirely, so the check passed while the case it was named for went unasked --
  the too-weak fixture CLAUDE.md rule 3 warns about, in the harness that exists
  to find them. `tools/verdict.py` now classifies where the result objects still are, and
  a run that could not put the question says ``BROKE`` rather than either answer.
- **Your working tree is never edited.** Each mutation is applied to a throwaway
  copy, so a kill at the wrong moment cannot leave a mutated source behind --
  the state CLAUDE.md rule 6 is about, and one this used to guard against with a
  ``finally`` and a rescue file in ``/tmp``. A copy is 2 MB and takes
  milliseconds; the earlier design paid for its cheapness with the one failure
  mode the whole rule exists to prevent.

  That is also what makes the mutations run **in parallel**. They are
  independent by construction, each is mostly waiting on a subprocess, and the
  suite a mutation runs is plain serial ``unittest`` rather than the sharded
  runner. That last clause used to end "so there is no nested pool to
  oversubscribe", which is true of every file in ``tupferl/`` and false of the
  ones in ``tools/``: `tests/test_mutate.py` starts this harness and
  `tests/test_run_tests.py` starts the sharded runner, so a lane mutating those
  hosts a pool of its own -- sixteen lanes each hosting sixteen, which is how
  4,340 processes came to be alive at once and how a 63 GiB machine was
  OOM-killed three times (woswoar#232). `_share`, `_BUDGET` and `_Lanes` are what that
  cost; the sentence is left here rather than deleted because a comment that
  reassures is worse than none. Measured there as
  ``workers=1`` against the default, same design either way: four mutations
  against that project's slowest suite went 197.5 s to 51.5 s, and eleven
  against its faster modules 2.4 s to 0.5 s. Both figures are
  serial-versus-parallel, not before-and-after this module was rewritten --
  the old design also ran in the working tree rather than a copy, so no
  measurement here separates those two changes and none is quoted as if it
  did.

**What a sandbox cannot check.** ``.git`` is not copied, and the copy lands
wherever ``$TMPDIR`` points -- tmpfs on most machines. So a test that needs
*this* repository's history to check a revision out of, or that distinguishes
two filesystems, skips in here and its guard cannot be mutation-verified through
this module. Tests that build their own git repository in a temporary directory
-- which is every git test in this project -- are unaffected. Worth knowing
before concluding that a surviving mutation means a weak test.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, suppress
from pathlib import Path
from statistics import median
from textwrap import indent
from typing import Literal, NamedTuple

from tools import mutants, paint, run_tests
from tools.cpus import usable_cpus
from tools.mutants import Mutation, check

#: Re-exported, because every spec file and every pasted pull-request output in
#: this repository's history says `from tools.mutate import Mutation`. The
#: definition lives with the generator that produces most of them now.
__all__ = ["Mutation", "Report", "Result", "Verdict", "run", "verify"]

#: Seconds one mutation may take before the run gives up on it. Generous, because
#: a suite that drives real `git` is tens of seconds and a loaded machine is
#: slower still; the point is only that a mutant which never terminates cannot hold a
#: lane for the rest of the table.
TIMEOUT = 300.0

#: Seconds one *test* may take before the run gives up on it, which is a much
#: tighter question than `TIMEOUT`'s. The suite this guards is 27s and its
#: slowest single test 1.21s, so this is fifteen to twenty-five times the
#: slowest honest one -- and it names the test, which `TIMEOUT` cannot: a
#: whole-run bound reports "no answer within 300s" and leaves the reader to find
#: out which of seventy tests it was.
#:
#: `TIMEOUT` stays, as the backstop for a hang that happens outside any test --
#: a `setUpModule`, or an import. This does not replace it, it makes it rare.
EACH_TEST = 30.0

#: How much of the budget the lanes' *ceilings* may add up to. A ceiling is
#: headroom for a pathological row, not what an honest one spends, and peaks are
#: not simultaneous -- so requiring `lanes x ceiling <= budget` prices every lane
#: as though all of them were pathological at once.
#:
#: Measured before it was raised: `--workers 32` already committed **126%** of
#: this machine's budget (32 x 2048 MiB against 52135 MiB) across dozens of
#: sweeps, whole-tree ones included, and nothing has ever been killed for
#: memory. The heaviest single lane process ran 14-18% of its ceiling on
#: `--only tupferl/` and 92% on a whole-tree run.
#:
#: **Not applied to `_affordable`**, which divides by what a lane is *measured*
#: to use rather than by its ceiling. That number already assumes peaks are
#: independent, so scaling it here would spend the same allowance twice.
#:
#: The quantity that would actually justify a number rather than a judgement is
#: the *sum* of lane RSS at one instant, and nothing samples it --
#: `_report_headroom` watches the heaviest single process. Until something does,
#: this is calibrated against "126% has never been killed" and no more.
_COMMIT = 1.5

#: The budget to assume on a machine that publishes nothing at all -- no
#: `/proc/meminfo`, no cgroup, no `sysconf`. Sixteen lanes' worth of measured
#: use, which is a guess and is only ever reached when every real source has
#: failed.
_BLIND_LANES = 16

#: Rows a generated table runs before it stops, when nobody said otherwise.
#: Sized for a diff; `--all` resets it, because a cap meant to keep one
#: change's table readable turns a whole-package sweep into 4% of itself.
LIMIT = 200

#: Address space one mutant may occupy, handed to `verdict.cap`.
#:
#: `TIMEOUT` answers "this mutation never finishes"; this answers "this mutation
#: never finishes *while allocating*", which is a different failure and the more
#: dangerous one -- a timeout cannot fire on a machine that is already gone.
#: A generated `at -= ...` in `mutants.line_starts` reached 15.5 GB in 73 s and
#: OOM-killed the machine twice while this branch was being written, well
#: inside the 300 s above.
#:
#: 4 GiB is measured rather than chosen: one honest whole-suite run peaks at
#: 317 MB resident and 1537 MB of address space, so this leaves a 2.7x margin
#: over the thing it must never interrupt. If a legitimate suite ever reports
#: `ran out of memory`, raise it -- the message names the test on purpose.
#:
#: What a lane is actually given is `_share`'s answer rather than this, because
#: sixteen lanes promised four gigabytes each is 64 GiB of promises on a 63 GiB
#: machine. This stays the ceiling a lane may *ask* for; `_FLOOR` is the point
#: below which the answer stops being safe to give.
MEMORY = 4 << 30

#: The smallest ceiling `_share` hands a lane before it takes a *lane* away
#: instead. `MEMORY` is what one runaway may reach; this is what an honest run
#: needs, and keeping them as two numbers is the whole fix for woswoar#232. Dividing a
#: small machine's budget by the ceiling was the over-restriction woswoar#227 removed
#: (a 16 GiB laptop dropped to two lanes); multiplying the lane count back up
#: without lowering the ceiling was the over-commitment that replaced it, at 16
#: lanes x 4 GiB = 64 GiB on a 63 GiB machine. One number cannot answer both.
#:
#: **The margin this used to claim does not hold, and the figure behind it was
#: another machine's.** The line here read: "one whole-suite probe peaks at 838
#: MiB of address space *here*, and survives a cap of 1 GiB but not 768 MiB.
#: Twice the smallest cap that works." That 838 MiB was measured in woswoar, and
#: "here" travelled with the sentence when the file did.
#:
#: Measured on this machine, sampling every process inside a real lane over four
#: sweeps: **1766, 1828, 1892 and 1901 MiB**. So an honest lane process needs
#: about 1.85 GiB, and against the 2053 MiB ceiling a seven-lane run hands out
#: that is a margin of roughly **1.1x, not 2x**.
#:
#: Kept at 2 GiB rather than raised, and the reason is evidence rather than
#: nerve: no lane has ever been killed for memory on this machine, so the number
#: works even though the argument for it did not. Raising it to an honest 2x of
#: 1901 MiB would mean 3802 MiB a lane and **three lanes where seven run today**
#: -- a real cost, for a risk nothing has yet demonstrated.
#:
#: What replaces the stale claim is a measurement rather than a better constant:
#: every run now prints what its heaviest lane process actually held against
#: what it was allowed (`_report_headroom`), so the margin on *this* machine is
#: visible without going looking, and a machine where 2 GiB is not enough says
#: so on the first run instead of after a wall of `BROKE`.
#:
#: Do not read the 1.85 GiB as a licence to lower this. It is what an honest run
#: needs; the ceiling has to be above it, not near it.
_FLOOR = 2 << 30

#: How a lane tells a harness it starts itself what it may spend.
#: `tests/test_mutate.py` starts this module and `tests/test_run_tests.py`
#: starts the sharded runner, so a lane mutating `tools/` hosts a second harness --
#: which, reading the host's memory as its own, sizes itself for a machine it does
#: not have. Sixteen lanes each hosting sixteen is how 4,340 processes came to be
#: alive at once. Carried in the environment because that is what survives
#: ``python -c``, and read back by `_visible_memory` as one limit among the
#: cgroup's.
_BUDGET = "TUPFERL_MUTATE_BUDGET"

#: What the whole run may spend, in bytes, when the caller says so outright.
#: Set by `--budget`, and an environment variable rather than a module global so
#: that a nested harness inherits the answer instead of re-deriving it from a
#: machine it can no longer see.
_TOTAL = "TUPFERL_MUTATE_TOTAL"

#: What a run leaves for the operating system and for itself when the machine is
#: its own. A gibibyte, which is `_LANE` -- the same number, because the thing
#: being reserved is exactly "room for one more lane's worth of everything else".
_SPARE = 1 << 30

#: Which Hypothesis profile the suites this runs should use. Set for every probe
#: and *not* conditional on the caller's own value: a sweep runs one suite per
#: mutation, so the full example budget multiplies by the size of the table.
#: `tests/profiles.py` makes the profile derandomised as well as small, which is
#: the half that matters for correctness -- a randomised baseline and a
#: randomised mutant draw different examples, and "it failed" then means nothing.
_PROFILE = "TUPFERL_HYPOTHESIS_PROFILE"

#: The value it is set to. A constant rather than a literal in the `env` dict
#: below, so a test can assert that the profile this asks for is one
#: `tests/profiles.py` has registered -- `load_profile` on a name nobody
#: registered raises inside the probe, where it surfaces as `BROKE` on every row
#: rather than as the typo it is.
_MUTATION_PROFILE = "mutation"

#: Names never copied into a mutation's sandbox. ``.git`` because it is large and
#: nothing under test reads it; the caches because a stale one is the trap this
#: module documents, and the cheapest way to not have it is to not copy it.
#:
#: ``.hypothesis`` and ``sweeps`` were added by #32, and the first of the two is
#: a *bug fix* rather than thrift:
#:
#: - **``.hypothesis`` is written while this copy is being taken.** Hypothesis
#:   creates and removes ``.hypothesis/tmp`` as it runs, `tools/run_tests.py`
#:   shards across eight workers, and `tests/test_mutate.py` starts this module
#:   *inside* the suite -- so one shard copies the tree while another is using
#:   that directory. `shutil.copytree` scans an entry and it is gone before it
#:   copies, which reaches CI as
#:   ``shutil.Error: [(... '.hypothesis/tmp', "[Errno 2] No such file or
#:   directory")]`` on a diff that touches nothing here. Seen on PR #31.
#:
#:   It also carries Hypothesis's *example database* into every sandbox, so a
#:   mutant could be replayed against examples recorded by a different tree.
#:   That is not measured to have changed a verdict here and is not claimed to
#:   have; it is a second reason not to copy a cache, not the reason.
#:
#: - **``sweeps``** is this repository's own mutation reports -- 3.5 MB and
#:   growing, per lane, that no mutant reads. `Killers.save` does
#:   ``mkdir(parents=True, exist_ok=True)``, so a nested harness that wants to
#:   write one in a sandbox still can; it simply starts with a cold cache, which
#:   is a miss rather than a failure.
#:
#: `shutil.ignore_patterns` matches on the **base name at any depth**, checked
#: rather than assumed -- a pattern that only applied at the root would leave a
#: nested one copied and nothing here would notice until the next red leg.
_SKIP = shutil.ignore_patterns(
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".venv",
    "*.egg-info",
    ".hypothesis",
    "sweeps",
)


#: What one run of the suite under one mutation is allowed to conclude.
#:
#: `broke` and `timeout` are not answers. They say the run could not ask the
#: question, and they must never be folded into either real verdict -- a
#: `broke` counted as `caught` blesses a test that never executed, which is the
#: failure this module's own docstring calls indistinguishable from a real one.
Outcome = Literal["caught", "survived", "broke", "timeout"]


class Meaning(NamedTuple):
    """Everything the rest of the tools need to know about one outcome.

    **One row per outcome, because four scattered spellings is a bug waiting for
    the next one.** What an outcome implies was written out in four places, and
    nothing made them agree:

        Verdict.answered   in ("caught", "survived")
        Report.clean       == "caught"
        _HEADLINE          [outcome], a plain lookup
        reached.answered   in ("caught", "survived")

    Adding a fifth outcome means visiting all four, and only one of them fails
    loudly: `_HEADLINE` is a `[]` and raises `KeyError` *mid-sweep*, throwing
    away every answer already paid for. The other three are `in (...)` tests
    that fall through to a **wrong answer, silently** -- a new outcome would
    quietly count as not-answered, not-clean and not-usable whether or not that
    is what it means, and nothing anywhere would say so.

    That is not hypothetical: it is what #33 hit while adding one.

    A dict cannot be made total by `mypy`, so the enforcement is a test --
    `set(get_args(Outcome)) == set(MEANING)`, both directions, derived from the
    type rather than listing it.
    """

    #: What a row prints. Lower case is good news or no news; the shouted ones
    #: are what a reader must not scroll past.
    headline: str
    #: Was a question put to the tests at all? `caught` and `survived` are the
    #: two real verdicts; `broke` and `timeout` asked nothing.
    answered: bool
    #: May a sweep of only these rows be called done?
    clean: bool
    #: Does the row say anything about whether its line ran? `tools/reached.py`
    #: crosses survivors with coverage and must not read a non-answer as
    #: evidence that a line was executed.
    usable: bool
    #: What colour the headline is on a terminal. Here rather than at the eleven
    #: `print`s that show an outcome, for the reason everything else in this
    #: table is: a fifth outcome that reads as good news in one module and bad
    #: news in another is the same drift, in the one channel a reader skims
    #: before reading a word. `paint.QUIET` is the honest default for an outcome
    #: this build has never heard of -- neither green nor red is a claim it can
    #: make.
    colour: str = paint.QUIET


#: Keyed by `str`, not by `Outcome`, because `tools/reached.py` reads outcomes
#: back out of a JSON report where they are plain strings, and a report written
#: by a newer `mutate` may carry one this build has never heard of.
MEANING: dict[str, Meaning] = {
    "caught": Meaning("caught", answered=True, clean=True, usable=True, colour=paint.GOOD),
    "survived": Meaning("SURVIVED", answered=True, clean=False, usable=True, colour=paint.BAD),
    "broke": Meaning("BROKE", answered=False, clean=False, usable=False, colour=paint.ODD),
    "timeout": Meaning("TIMEOUT", answered=False, clean=False, usable=False, colour=paint.ODD),
}


class Verdict(NamedTuple):
    outcome: Outcome
    #: The first test that noticed, or the reason nothing could. Printed for
    #: everything except a plain `caught`, where the label already says it.
    detail: str = ""
    #: The same test as `module.Class.method`, which is what `unittest` takes
    #: back. Empty unless the outcome is `caught`. `detail` is for a reader and
    #: this is for `killers`, which runs it again next time -- see `Killers`.
    killer: str = ""
    #: What each test that ran cost, by id. Not persisted to the `--json`
    #: report, which is about verdicts; `Killers` keeps them, because ordering
    #: by yield-per-second needs a denominator and measuring it separately would
    #: be a second source of truth about the same suite.
    times: dict[str, float] | None = None
    #: The failing traceback, for a `caught` verdict. Carried but never printed
    #: by a mutation row -- `caught` is the whole answer there, and two hundred
    #: tracebacks is noise. `run`'s baseline branch is the one reader: a red
    #: baseline voids every verdict above it, and a test's *name* is not enough
    #: to diagnose one. Five hand-built reproductions of a red baseline all came
    #: back green because the thing that differed was never guessed.
    why: str = ""
    #: What this row cost, in seconds, measured around the suite run alone.
    #: `Killers` keeps it and `slowest_first` orders the *next* run's table by
    #: it: a survivor costs ~10x a caught row, so which rows are expensive is
    #: the single most useful thing one sweep can tell the next.
    #:
    #: Persisted to the `--json` report, unlike `times` beside it. The
    #: distinction is whose property it is: `times` is per *test* and describes
    #: the suite, where this is per *row* and belongs with the row's verdict --
    #: which is also what makes a resumed sweep able to learn from rows it
    #: skipped rather than leaving them cold for ever.
    #:
    #: Timed around `_run` rather than around the whole of `_attempt`: waiting
    #: for a sandbox is a property of how busy the machine was, not of the row,
    #: and an ordering keyed on it would learn the contention rather than the
    #: cost. `0.0` on a verdict that never ran a suite -- a baseline shard, or a
    #: report read back from disk.
    spent: float = 0.0

    @property
    def answered(self) -> bool:
        """Was a question put at all?

        One definition, because three sites need it and the run, the summary and
        the exit status each spelled it differently -- and one of the three said
        something subtly other than the other two.
        """
        return MEANING[self.outcome].answered


class Result(NamedTuple):
    mutation: Mutation
    verdict: Verdict


class Pace(NamedTuple):
    """What a run cost, for the block that reports it.

    On the `Report` rather than printed where it is measured, because the run
    that measures it is not always the one that owns the output: `--all` has
    `main` do the summarising, so a block printed inside `run` would land above
    a hundred and sixty survivors instead of after them. Carried, not global --
    `_RUNS` already holds more than one report per process.
    """

    seconds: float
    lanes: int
    ceiling: int


class Report(NamedTuple):
    results: list[Result]
    #: Nothing in `results` means anything when this is set: a suite that already
    #: fails catches every mutation, including the ones no test can see.
    baseline_red: bool = False
    #: Whether every survivor here was run against the whole suite, which is what
    #: CLAUDE.md promises about a survivor before it is reported.
    #:
    #: Now structural rather than earned. Every row walks outward past its
    #: selection until something notices (`verdict.collect`), so a row *called* a
    #: survivor has run everything by construction -- there is no second pass to
    #: skip, no `--no-confirm` to turn off, and no red confirmation baseline to
    #: suppress a correction. `run` sets it, and the only report that carries
    #: False is one written before this existed.
    #:
    #: Kept on the report rather than dropped, because `_persist` writes these
    #: rows and `tools/reached.py` reads them back: a sweep recorded under the
    #: old two-pass tool is still read correctly, and says so (woswoar#269).
    widened: bool = False
    #: What each test that ran cost, by id, across every row *and every baseline
    #: shard* of this run. Not persisted with the verdicts -- it is not an answer
    #: about the code, it is what `Killers` needs to put the cheap high-yield
    #: tests first.
    times: dict[str, float] | None = None
    #: What the run cost, for `_report_stats`. `None` on a report assembled
    #: rather than run -- an empty table, or one read back from disk.
    pace: Pace | None = None

    @property
    def clean(self) -> bool:
        """Every row caught, and the baseline green. The definition of done.

        Here rather than in the CLI helper that used to own it, because `verify`
        and any generated-table driver need the same answer and only one of them
        had a way to ask. A row that broke or timed out is not clean: it is a
        question the run failed to put, and calling it green would report the
        table as smaller than it was while claiming it was complete.
        """
        return not self.baseline_red and all(
            MEANING[result.verdict.outcome].clean for result in self.results
        )


#: Every table `run` has finished in this process, in order.
#:
#: Only `main` reads it, and only to tell "the script defined nothing" apart from
#: "the script did the work itself". Before this existed, a spec written exactly
#: as the docstring showed it ran correctly and *then* exited 1 with
#: "defines no MUTATIONS" -- printed above its own green results, because stderr
#: is not line-buffered against stdout (woswoar#213).
_RUNS: list[Report] = []

#: Nine wide, as before. `caught` stays lowercase and everything else shouts,
#: because the eye scanning a pasted table is looking for the rows that are not
#: the good news.


def _probe() -> str:
    """`tools/verdict.py`, read from *this* tree rather than the sandbox's copy.

    Handed to ``python -c``, which is what puts the sandbox on ``sys.path`` so its
    test modules import at all. Reading it from `__file__`'s directory is the
    whole isolation property: `tools/**.py` is itself something a table may
    mutate, and a verdict loaded out of the copy would let a mutation grade its
    own exam. See that module's docstring for why the classification lives there
    and not in a string constant here.
    """
    return (Path(__file__).resolve().with_name("verdict.py")).read_text(encoding="utf-8")


def _clear_bytecode(root: Path) -> None:
    """The sweep the trap in the module docstring needs, for a real reason.

    Not belt and braces: a test that builds its own environment from literals to
    drive a subprocess -- which `tests/support.py` does, on purpose, so that a
    sandbox cannot inherit the real installation -- carries no
    ``PYTHONDONTWRITEBYTECODE``, and its ``PYTHONPATH`` points at the tree running
    the suite: the sandbox. Those `python -m tupferl` children do write a
    ``__pycache__`` into a copy that a later mutation reuses, which is exactly the
    ``(mtime, size)`` collision this module exists to avoid. Measured in woswoar
    at 0.6 ms on the full tree and 12 directories in a sandbox, against a
    multi-second run.
    """
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def _signalled(returncode: int) -> str:
    """Why a probe that wrote nothing died, when nothing else can say.

    A negative return code is a signal in `subprocess`'s spelling. `SIGKILL` is
    the interesting one: it is what the host OOM killer sends, so it is what a
    lane looks like on a platform where `verdict.cap` is not enforced -- the
    macOS case the cap's own docstring admits to. Naming it is the difference
    between a row that reports nothing and a row that says the machine, not the
    mutation, ended the run.

    It names two causes rather than one because a sweep of this module produced
    the second: a mutation to `_lane` makes the *nested* harness inside a lane
    compute the wrong membership and kill the session hosting it, and the row
    then reported an out-of-memory that had not happened. A message that names
    the wrong cause costs more than one that names none.
    """
    if returncode >= 0:
        return f"the probe exited {returncode} without writing a report"
    name = signal.Signals(-returncode).name if -returncode in set(signal.Signals) else "?"
    killed = (
        " -- either the host ran out of memory and this lane was chosen, or a harness "
        "running inside it killed the session it was in"
        if name == "SIGKILL"
        else ""
    )
    return f"the probe was killed by {name}{killed}"


#: How often `_Lanes` counts what the lanes are holding. Seconds rather than
#: minutes: the storm that prompted this reached 26 GB inside a single 20-second
#: gap between two `free` readings. One pass over `/proc` and no fork, so the
#: sample is cheap enough to take at this rate for the length of a sweep.
_SAMPLE = 1.0


class Process(NamedTuple):
    """One row of the process table, in the four fields a lane cares about."""

    parent: int
    group: int
    resident: int
    #: Address space, which is what `verdict.cap` limits and therefore the only
    #: number that says whether a lane's *ceiling* is big enough. Kept beside
    #: `resident` rather than instead of it because the two answer different
    #: questions and both are needed -- see `_processes`.
    #:
    #: Free: `/proc/<pid>/stat` already carries it one field from the one this
    #: read was doing anyway, so nothing here forks or opens a second file.
    #:
    #: **No default.** Both readers fill it, so a default would only ever serve
    #: a third that had forgotten to -- and forgetting is silent: every ceiling
    #: question then answers 0 while every assertion about `resident` still
    #: passes. Two mutants of the default survived the sweep for that reason,
    #: which is the generator saying the value was unobservable.
    address: int


def _processes() -> dict[int, Process]:
    """Every process this user can see, by pid.

    **Both, because they answer different questions and the two are 25x apart.**

    `_Lanes` intervenes on *resident*, because resident is what the OOM killer
    counts and what a fork storm actually spends: the storm that took this
    machine down was 4,340 processes holding 26 GB of RSS between them and 961
    GB of address space, and only one of those is a reason to kill anything.

    `address` is carried for the opposite reason -- it is what `verdict.cap`
    limits, so it is the only number that says whether a lane's *ceiling* is big
    enough. Reading it costs nothing: `/proc/<pid>/stat` has it one field from
    the resident figure this was already parsing.

    Keeping only one of them is how `_FLOOR` came to state a margin that was not
    holding. Confusing the two is worse: a lane's tree holds ~73 MiB resident
    and one of its processes reaches ~1.85 GiB of address space, so a lane count
    divided out of the resident figure gives every lane a ceiling that honest
    work cannot fit inside, and the sweep comes back all `BROKE`.

    Two readers rather than one, and `/proc` first: reading it forks nothing,
    while `ps` is a fork that can fail at exactly the moment its answer matters.
    macOS has no `/proc`, so there it is `ps` or nothing -- and there it is also
    the *only* one of this module's two guards that works at all, since that
    kernel ignores the `RLIMIT_AS` `verdict.cap` asks for. Both are exercised by
    the tests on every platform for that reason: the fallback must not be
    discovered to be broken on the machine that has nothing else.
    """
    return _from_proc() if Path("/proc/self/stat").exists() else _from_ps()


def _from_proc() -> dict[int, Process]:
    """`_processes` where there is a `/proc`. Reads, never forks."""
    table: dict[int, Process] = {}
    page = os.sysconf("SC_PAGE_SIZE")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            said = (entry / "stat").read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Exited between the listing and the read, or belongs to another
            # user. Both are ordinary, and both are why this is a loop with a
            # `try` in it rather than a comprehension.
            continue
        # Everything after the comm field, which is the one that may itself
        # contain spaces and brackets -- so the split starts past its closing
        # one rather than at the beginning of the line. Counting from `pid`,
        # `ppid` is the fourth field, `pgrp` the fifth and `rss` the
        # twenty-fourth, which is why the indices below are those minus three.
        fields = said.rpartition(") ")[2].split()
        if len(fields) < 22:
            continue
        with suppress(ValueError):
            table[int(entry.name)] = Process(
                parent=int(fields[1]),
                group=int(fields[2]),
                resident=int(fields[21]) * page,
                # Field 23 of `proc(5)`, in bytes already -- unlike `rss` two
                # fields along, which is in pages.
                address=int(fields[20]),
            )
    return table


def _from_ps() -> dict[int, Process]:
    """`_processes` where there is no `/proc`, which is macOS."""
    listed = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,pgid=,rss="], capture_output=True, text=True, check=False
    )
    return _parse_ps(listed.stdout)


def _parse_ps(text: str) -> dict[int, Process]:
    """`ps` output: pid, parent, group and resident, and no address space.

    **`vsz` is asked for and then not used, so it is not asked for.** The first
    version of this read it, and the macOS leg printed `heaviest lane process
    held 401357 MiB of its 4096 MiB ceiling (9799%)`. That is not a bug in the
    arithmetic: macOS counts reserved regions no Linux `RLIMIT_AS` figure would,
    so the number is real and means something else entirely.

    Reporting it against a ceiling would be twice wrong, because macOS **does
    not enforce that ceiling** -- `verdict.cap` has said so since it was written
    and `tests/test_verdict.py` names two classes the workflow excludes for it.
    There is no ceiling here to have headroom against, so `address` stays 0,
    `_report_headroom` finds nothing to report, and the run says nothing rather
    than something impressive and false.

    What macOS still gets is the half that works: `_Lanes` counts *resident*,
    which is what a fork storm spends, and on the platform that ignores
    `RLIMIT_AS` it is the only guard there is.

    Its own function so the parse can be driven from text. `_from_ps` forks, and
    a test would otherwise have to mock the fork rather than read what a real
    `ps` prints.
    """
    table: dict[int, Process] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 4 or not all(field.isdigit() for field in fields):
            continue
        pid, parent, group, resident = (int(field) for field in fields)
        # Kibibytes, which POSIX does not promise and both platforms do.
        table[pid] = Process(parent=parent, group=group, resident=resident * 1024, address=0)
    return table


def _lane(leader: int, table: dict[int, Process]) -> set[int]:
    """Every process belonging to the lane `leader` started.

    Two answers unioned, because neither is enough on its own and the gap
    between them is woswoar#234:

    - everything in `leader`'s **process group**, which is what `start_new_session`
      buys and covers the `git`, `age` and `bash` a suite forks;
    - every **descendant** of `leader`, which is what catches a grandchild that
      called `setsid` and thereby left that group. A nested `_run` does exactly
      that, and one was found alive eleven minutes into a sweep whose per-row
      bound is 300 seconds, reparented to init and counted by nobody.

    A descendant whose parent has already died is in neither set: init has
    adopted it and the link is gone. That is why callers enumerate *before*
    killing rather than after -- while the lane is alive, the tree is still
    connected.
    """
    kids: dict[int, list[int]] = {}
    for pid, row in table.items():
        kids.setdefault(row.parent, []).append(pid)
    found = {pid for pid, row in table.items() if row.group == leader}
    # `seen` rather than `found` as the visited set, and the difference is the
    # whole of woswoar#234 rather than a tidy-up.
    #
    # `found` starts holding the entire process *group*, so testing membership
    # in it stopped the walk at every group member -- and a group member is
    # precisely what a nested harness's `_run` is. The descendant half then only
    # ever reached escapees whose parent was the leader itself, which is the one
    # shape that needs it least.
    #
    # Measured against real processes, leader -> middle (in the group) ->
    # grandchild (`setsid`, out of it): the grandchild was in neither the count
    # nor the kill. That is the eleven-minutes-alive symptom the union was added
    # to remove, still present in the fix for it.
    seen: set[int] = set()
    stack = [leader]
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        found.add(pid)
        stack.extend(kids.get(pid, ()))
    return found & set(table)


def _end_lane(leader: int, members: Iterable[int]) -> None:
    """Kill the group, then everything that left it.

    The group first because it is one syscall for most of the lane, and the
    stragglers by pid because they are exactly the processes `killpg` cannot
    reach. `members` is enumerated by the caller beforehand: after the first
    kill the tree is no longer connected, so a walk done here would find less
    than a walk done a moment earlier.
    """
    with suppress(OSError):
        os.killpg(leader, signal.SIGKILL)
    for pid in members:
        with suppress(OSError):
            os.kill(pid, signal.SIGKILL)


class _Lanes:
    """Kills a lane whose process *tree* outgrows its share, which no rlimit can.

    `verdict.cap` bounds one process's address space, and that is the wrong
    shape for the failure that actually took this machine down. From the kernel
    log of the last one: 4,340 python processes alive at once, six megabytes of
    resident memory each, 26 GB between them -- and not one of them within two
    orders of magnitude of its own 4 GiB ceiling. Nothing ran away. There were
    simply thousands of them, because a lane mutating `tools/` hosts a harness
    that starts lanes of its own.

    A per-process limit cannot see that, and cannot be made to: the mutation
    under test is what decides how many processes the lane starts, so a limit it
    inherits is beside the point. `_BUDGET` shrinks an honest nested harness;
    this is what answers a mutant that ignores it.

    What counts as the lane is `_lane`: its process group, plus any descendant
    that left that group by starting a session of its own. The group alone was
    the first shape and it left a hole (woswoar#234) -- a nested `_run` gives its own
    probes their own sessions, so those escaped both the count and the kill, and
    one was found alive eleven minutes into a sweep whose per-row bound is 300
    seconds. Counting the union is also what makes an escaped probe's memory
    the lane's problem rather than nobody's.

    Sampling rather than a cgroup, for the reasons `verdict.cap` gives for
    `RLIMIT_AS` over `systemd-run --scope -p MemoryMax=`: no root, no delegated
    controller, nothing to configure, and it exists on macOS. What sampling buys
    over both is that it needs no cooperation from the code under test.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: What each watched group may hold.
        self._ceilings: dict[int, int] = {}
        #: What a group was holding when this killed it, until `release` says it.
        self._killed: dict[int, int] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        #: The most address space any single watched process has been seen
        #: holding. Run-wide rather than per lane: what it answers is "was the
        #: ceiling big enough", and one process coming close is the whole
        #: answer -- `verdict.cap` bounds each process separately.
        self._widest = 0

    def watch(self, group: int, ceiling: int) -> None:
        """Count `group` against `ceiling` from now on. ``0`` is no cap."""
        if ceiling <= 0:
            return
        with self._lock:
            self._ceilings[group] = ceiling
            if self._thread is None:
                # One sampler for every lane, started with the first and stopped
                # with the last. Per-lane threads would each walk the whole
                # process table, and the table is longest exactly when a storm
                # makes the walk expensive.
                self._stop = threading.Event()
                self._thread = threading.Thread(
                    target=self._sample, args=(self._stop,), name="mutate-lanes", daemon=True
                )
                self._thread.start()

    def widest(self) -> int:
        """The most address space one watched process has held since `forget`."""
        with self._lock:
            return self._widest

    def forget(self) -> None:
        """Start a fresh high-water mark, so one run does not report another's."""
        with self._lock:
            self._widest = 0

    def release(self, group: int) -> int:
        """Stop counting, and say what it held if this is what killed it."""
        with self._lock:
            self._ceilings.pop(group, None)
            held = self._killed.pop(group, 0)
            if not self._ceilings and self._thread is not None:
                self._stop.set()
                self._thread = None
        return held

    def _sample(self, stop: threading.Event) -> None:
        """Every `_SAMPLE` seconds until the last lane is released."""
        while not stop.wait(_SAMPLE):
            with self._lock:
                watched = dict(self._ceilings)
            if not watched:
                continue
            table = _processes()
            for leader, ceiling in watched.items():
                members = _lane(leader, table)
                held = sum(table[pid].resident for pid in members)
                # Recorded for every lane, not only for one that is killed.
                # Until this existed the only address-space figure anywhere was
                # a constant carried in from another repository, and it was
                # 2.3x wrong here -- see `_FLOOR`. A sweep that measures the
                # thing it is bounded by can say whether the bound still fits.
                # Guarded rather than floored with a `0` sentinel. A lane whose
                # processes all exited between the table read and now has no
                # members, and `max(..., 0)` would then leave the mark at its
                # old value -- harmless -- while `max(..., 1)` would raise it to
                # one byte, which `_report_headroom` reads as a *reading* and
                # prints as "0 MiB of 2050 MiB". Both mutants of that sentinel
                # survived the sweep; asking whether anything was seen removes
                # the constant instead of testing it.
                if sizes := [table[pid].address for pid in members]:
                    with self._lock:
                        self._widest = max(self._widest, *sizes)
                if held <= ceiling:
                    continue
                with self._lock:
                    # Re-checked under the lock: `release` may have run while
                    # the process table was being read, and a kill after that
                    # names ids the kernel is free to have reused.
                    if leader not in self._ceilings:
                        continue
                    self._killed[leader] = held
                _end_lane(leader, members)


#: One per process, because `_run` is what registers and it has no run-wide
#: object to hang this on -- `run` is a function, and a lane borrowed by
#: `verify` or by a test goes through the same door.
_WATCHED = _Lanes()


def _end(probe: subprocess.Popen[bytes]) -> None:
    """End a lane: the whole session, not just the process this holds a handle to.

    `subprocess.run(timeout=...)` kills the child and returns, which for a suite
    that forks `git` and `python -m tupferl` leaves the
    grandchildren running -- reparented to init, holding whatever they held, and
    invisible to the run that started them. A sweep that times out often enough
    accumulates them until the machine is gone, which is the slow half of woswoar#232;
    the fast half is `_Lanes`.

    The walk happens *before* the first kill, which is woswoar#234: a grandchild that
    started a session of its own is outside the group, so `killpg` never reaches
    it -- and once its parent is dead it is no longer a descendant either, so a
    walk done afterwards would not find it. Between those two moments is the
    only time anything can see it.
    """
    _end_lane(probe.pid, _lane(probe.pid, _processes()))
    with suppress(OSError):
        probe.kill()  # if the session is somehow gone, at least reap this one
    with suppress(subprocess.TimeoutExpired):
        probe.wait(timeout=_SAMPLE * 5)


def _run(
    tests: Sequence[str],
    root: Path,
    *,
    failfast: bool = False,
    timeout: float = TIMEOUT,
    memory: int = MEMORY,
    each: float = EACH_TEST,
    first: str = "",
    walk: bool = False,
) -> Verdict:
    """What the suite concluded about one mutation, and by which route.

    ``walk`` is "keep going past the selection when nothing in it notices", which
    is what a *mutation* row wants and a *baseline* row must not have: a baseline
    asks whether one selection is green, and widening it would make every
    baseline a whole-suite run. See `verdict.collect`.

    The verdict used to be "the exit status was non-zero, and `Ran N` said N was
    not zero". That is wrong in the direction a reader believes, and it shipped:
    a mutation that turns a working import into a failing one leaves `Ran 1
    test`, `errors=1` and a non-zero exit, exactly like a test noticing. The only
    fixture guarding it used a *syntax* error, which takes a different path
    through `unittest.loader` and produces no count at all -- so the check passed
    while the case it named went unasked. `tools/verdict.py` is the answer: classify where
    the result objects still exist.

    A timeout is its own outcome rather than an exception. A generated mutant can
    turn a loop bound into one that never fires, and with no limit here that
    holds a lane for the rest of the run.

    Four limits, and each answers a failure the others cannot see. `each` is
    "this *test* never finishes", and is the one that fires in practice -- it
    names the test, and it costs seconds where the others cost minutes.
    `timeout` is "this *run* never finishes", the backstop for a hang outside
    any test, in a `setUpModule` or an import, where no per-test alarm is armed.
    `memory`, through `verdict.cap`, is "this process never stops allocating";
    and `_Lanes`, through the session started here, is "this mutation spawns
    processes" -- which is none of the others and is the one that reached the
    OOM killer.
    """
    _clear_bytecode(root)
    # Both files land outside the sandbox on purpose: the copy is what the
    # mutation edits, and a report written into it is one `open()` away from
    # being the mutation's to write.
    with tempfile.TemporaryDirectory(prefix="tupferl-verdict-") as box:
        report = Path(box) / "verdict.json"
        noise = Path(box) / "stderr.txt"
        with noise.open("w", encoding="utf-8") as spill:
            probe = subprocess.Popen(
                [
                    sys.executable,
                    "-B",
                    "-c",
                    _probe(),
                    str(report),
                    "1" if failfast else "0",
                    str(memory),
                    str(each),
                    first,
                    "1" if walk else "0",
                    *tests,
                ],
                cwd=root,
                # `-B` covers this process; `PYTHONDONTWRITEBYTECODE` covers the
                # real `age`, `git` and `bash` the suite spawns, which would
                # otherwise leave a `.pyc` in a sandbox that a later mutation
                # reuses. `_BUDGET` covers the *harness* the suite may spawn: a
                # row mutating `tools/` runs tests that start this module again,
                # and without it the inner one sizes itself for the whole host.
                env={
                    **os.environ,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    _BUDGET: str(memory),
                    _PROFILE: _MUTATION_PROFILE,
                },
                # A file rather than a pipe, and this is not a style choice. The
                # suite's `python -m tupferl` grandchildren inherit the write
                # end, so anything that drains a pipe before reaping -- which is
                # what `subprocess.run` does on a timeout -- waits for *them*
                # rather than for the probe. With a file there is nothing to
                # drain, so `_end` can kill the session and be done.
                stdout=subprocess.DEVNULL,
                stderr=spill,
                # Its own session, so this pid is also the group id and every
                # process the suite forks is inside it -- which is what makes
                # both `_Lanes`' count and `_end`'s kill cover the tree rather
                # than the one process. `start_new_session` rather than a
                # `preexec_fn` doing the same thing: `run` drives its lanes from
                # threads, and this one is done in C between fork and exec.
                start_new_session=True,
            )
        _WATCHED.watch(probe.pid, memory)
        try:
            probe.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _end(probe)
            return Verdict("timeout", f"no answer within {timeout:g}s")
        finally:
            # In a `finally` so that no path leaves a group registered: a
            # `KeyboardInterrupt` here would otherwise leave the sampler holding
            # a pid the kernel is free to hand to something else.
            held = _WATCHED.release(probe.pid)
        if held:
            # Before the report is read, not after. A killed lane may well have
            # written one -- the kill lands on whichever process is running, and
            # the probe's own `except BaseException` may have got there first --
            # and reading it would report a verdict about a run that was
            # stopped, which is the "broke counted as an answer" failure this
            # module exists to prevent.
            return Verdict(
                "broke",
                f"this lane's processes held {held >> 20} MiB between them, over the "
                f"{memory >> 20} MiB share it was given, so the whole session was killed",
            )

        try:
            written = json.loads(report.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # The probe was killed before it could write anything at all. Rare
            # and always worth the tail -- but a process killed by a signal
            # writes no stderr either, and that row used to print with no reason
            # at all. It is exactly the case a host OOM-kill produces, which is
            # the failure this file now has a limit for, so it must not be the
            # one that says nothing.
            return Verdict("broke", _tail(noise) or _signalled(probe.returncode))
        if not written["loaded"]:
            # The recorded traceback first. `verdict.main` writes it deliberately
            # and `_tail` is whatever happened to reach stderr, so preferring the
            # tail let a stray import-time warning outrank the reason the probe
            # took the trouble to record.
            return Verdict("broke", str(written.get("why", "")).strip() or _tail(noise))

    if written["broke"]:
        return Verdict("broke", str(written["broke"][0]))
    if written["noticed"]:
        # Required, like every sibling key on these lines. `_probe` reads
        # `verdict.py` out of *this* tree, so the two are always the same
        # revision -- there is no older probe to guard against, and a `.get`
        # here would turn a real protocol break into a silently empty ordering.
        remembered = written["killers"]
        reasons = written["reasons"]
        return Verdict(
            "caught",
            str(written["noticed"][0]),
            str(remembered[0]) if remembered else "",
            written["times"] or None,
            str(reasons[0]) if reasons else "",
        )
    if not written["ran"]:
        return Verdict("broke", "the targets held no tests")
    # A survivor ran its whole selection, so its timings are the complete ones --
    # as are a baseline shard's, which is where most of them come from.
    return Verdict("survived", times=written["times"] or None)


def _tail(noise: Path) -> str:
    """The last line the run managed to say. Empty when it said nothing."""
    try:
        spoken = noise.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return spoken[-1].strip() if spoken else ""


@contextmanager
def _sandboxes(count: int) -> Iterator[queue.Queue[Path]]:
    """``count`` borrowable copies of the working tree, lent out one at a time.

    A queue rather than one copy per mutation: a copy is 1.9 MB of 87 files and
    measures about a millisecond, while a test run costs seconds -- but there is
    still no reason to make five hundred of them for a table of five hundred rows.
    Borrow, mutate, restore, return.

    **Every** borrower is a pool task, which is what keeps this deadlock-free. An
    earlier version handed the baseline a copy taken on the *main* thread and
    never returned it, so with a single lane the baseline held the only sandbox
    and every mutation waited on an empty queue for ever. A task always returns
    what it borrowed in a `finally`; the main thread must never take one.

    Copied rather than ``git worktree add``, which would check out *HEAD* and so
    quietly test committed code -- the mutation is meant to apply to the tree as
    it stands, uncommitted changes and all, which is the whole situation this is
    used in.
    """
    with tempfile.TemporaryDirectory(prefix="tupferl-mutate-") as holder:
        available: queue.Queue[Path] = queue.Queue()
        for index in range(count):
            root = Path(holder) / f"tree{index}"
            shutil.copytree(Path.cwd(), root, ignore=_SKIP, symlinks=True)
            available.put(root)
        yield available


def _borrow(
    available: queue.Queue[Path], tests: Sequence[str], timeout: float, memory: int, each: float
) -> Verdict:
    """Run ``tests`` unmutated in a borrowed sandbox. One shard of the baseline.

    Never `failfast`: a red baseline is a thing you want the whole of, and a
    green one never stops early anyway, so there is nothing to buy.
    """
    root = available.get()
    try:
        return _run(tests, root, timeout=timeout, memory=memory, each=each)
    finally:
        available.put(root)


def _applied(original: str, mutation: Mutation) -> str:
    """The file with one mutation in it.

    Two ways, because the two kinds of row promise different things. A
    hand-written `old` is unique in the file -- `check` refused it otherwise --
    so `replace` cannot land anywhere else. A generated one usually is *not*
    unique (`if not path.exists():` appears many times), so it carries the
    offsets it applies at and the text is spliced there. `check` has already
    confirmed those offsets still hold the text the row quotes.
    """
    if mutation.span is None:
        return original.replace(mutation.old, mutation.new)
    start, end = mutation.span
    return original[:start] + mutation.new + original[end:]


#: How many recently-successful tests `Learned` keeps in front of a row.
#:
#: Small on purpose. Measured across five sweeps, a row's killer is the *same
#: test* as the previous row's 27-42% of the time and in the same module 85-95%,
#: so nearly all of the signal is in the most recent one or two -- and every test
#: kept here is one every later row pays for before reaching its own selection.
#: Eight leaves room for a few interleaved lanes without the head becoming a
#: second, unbudgeted `prefix()`.
LEARNED = 8


class Learned:
    """Whatever caught the last row, tried first on the next one.

    bzip2's move-to-front stage, and it works here for the same reason it works
    there: after the Burrows-Wheeler transform symbols come in runs, and a
    sweep's rows arrive sorted by file and line, so consecutive mutants sit in
    the same function and are usually caught by the same test.

    **This is the one ordering mechanism that learns during the run.**
    `Killers.known` is exact per-mutant memory keyed on
    `sha256(path, operator, old, new)`, so it misses by construction on the sweep
    that matters most -- `--base main` generates rows from *changed* lines, whose
    text is new. `Killers.prefix()` is computed once before the table starts,
    from the previous run's cache, and cannot know that the row now running is in
    the same function as the one before it. Neither looks at adjacency; this only
    looks at adjacency.

    **Consulted in `_attempt`, not in `ahead_of`.** `run` submits every row to
    the pool up front, so a row's `first` is fixed before any verdict exists;
    what a lane knows when it actually picks the row up is the whole point.

    **It reorders, never selects.** The learned tests go in front of
    `Mutation.first`, and `Mutation.tests` is untouched -- the asymmetry
    `Killers` already documents. A wrong guess costs one test; it can never make
    a `caught` that nothing verified. Nor can it smuggle in an unbaselined
    verdict: a killer no shard covered is caught by `_unbaselined` whatever put
    it in front.

    Ids are learned from verdicts of *this* run, so unlike `Killers.known` they
    demonstrably load and need no validation pass.
    """

    def __init__(self, keep: int = LEARNED) -> None:
        self.keep = keep
        #: Newest first. A list rather than a `deque` because `ahead` reads it
        #: whole under the lock and the length is single digits.
        self.recent: list[str] = []
        # `run` drives its lanes from threads, so both ends of this are shared.
        self._lock = threading.Lock()

    def saw(self, killer: str) -> None:
        """Move ``killer`` to the front, dropping the oldest past `keep`."""
        if not killer:
            return
        with self._lock:
            if killer in self.recent:
                self.recent.remove(killer)
            self.recent.insert(0, killer)
            del self.recent[self.keep :]

    def ahead(self, row: Mutation) -> str:
        """The learned tests this row can reach, newest first.

        Cut to what the row's selection covers, for the reason `ahead_of` gives:
        a test in a module that does not import the mutated file cannot see the
        mutation, so running it first is pure cost. An empty selection is
        `WHOLE_SUITE` and reaches everything.

        Already-remembered tests are dropped rather than repeated -- `first` is
        run in order, and naming a test twice buys nothing and costs a run.
        """
        with self._lock:
            recent = list(self.recent)
        if not recent:
            return ""
        already = set(row.first.split())
        reachable = row.tests.split()
        return " ".join(
            test
            for test in recent
            if test not in already
            and (not reachable or any(run_tests.selects(test, only) for only in reachable))
        )


class Work:
    """Which row each lane runs next: the next one nobody has taken yet.

    One cursor, so with N lanes each lane walks a *stride* of N. That is what a
    `ThreadPoolExecutor` handed the whole table already did; this exists as a
    class rather than as submission order only so a lane has an identity to
    print and so the handout has one place to be measured.

    **Four cleverer dispatches were built and measured here and every one of
    them lost** -- equal segments, guided chunks, work stealing, and pulling a
    survivor's neighbours forward. Their numbers are in CLAUDE.md's dead ends.
    The short version is that each was an attempt to guess *during* a run
    something a previous run already knew: which rows are expensive, and which
    test kills each one. `Killers` records both across runs, so there is nothing
    left for a scheduler to infer -- the table arrives ordered slowest-first per
    file (`slowest_first`) and each row already carries its own killer on
    `first`.

    That is also why the stride's poor locality stopped costing anything.
    `Learned` is move-to-front over *adjacency*, which is a proxy for "the next
    row is caught by whatever caught this one", and a stride hands no lane two
    consecutive rows -- measured, the contiguous dispatches scored a 70.7%
    move-to-front hit rate against the stride's 37.2%. Winning that proxy was
    worth up to 11% of wall clock and never more. A recorded killer is the
    answer the proxy was approximating, exactly and per row, so `Learned` is now
    what a *cold* row falls back on rather than the mechanism the run rests on.
    """

    def __init__(self, rows: int) -> None:
        self._rows = rows
        self._next = 0
        # `run` drives its lanes from threads, so the cursor is shared. Measured
        # at 209ns uncontended and 265ns with 32 lanes on it, which is 0.85ms
        # across a whole 3199-row sweep -- nothing worth a lock-free spelling.
        self._lock = threading.Lock()

    def take(self) -> int | None:
        """The next row nobody has taken, or `None` when the table is done."""
        with self._lock:
            if self._next >= self._rows:
                return None
            got = self._next
            self._next += 1
            return got


def _attempt(
    mutation: Mutation,
    available: queue.Queue[Path],
    failfast: bool,
    timeout: float,
    memory: int,
    each: float,
    walk: bool = True,
    learned: Learned | None = None,
) -> Verdict:
    """Apply one mutation in a borrowed sandbox and report what the suite said."""
    root = available.get()
    try:
        source = root / mutation.path
        original = source.read_text(encoding="utf-8")
        source.write_text(_applied(original, mutation), encoding="utf-8")
        try:
            # `first` handed over as its own argument, never merged into the
            # selection: an empty selection is `WHOLE_SUITE` and means "run
            # everything", so anything pushed onto that list turns it into
            # "run only this". See `verdict.collect`.
            # Asked *here*, on the lane, rather than when the row was queued:
            # `run` submits the whole table to the pool at once, so at submit
            # time no verdict exists yet and there is nothing to have learned.
            ahead = learned.ahead(mutation) if learned is not None else ""
            # **The exact killer goes in front of the learned front, not
            # behind it.** `Killers.ahead_of` already drops the cheap prefix
            # for a row whose killer is known -- "exact beats general, the
            # prefix would only be work before the answer" -- and `Learned` is
            # general in exactly the same way: it is what caught the *previous*
            # rows, which is a proxy for what catches this one, and here the
            # thing being proxied is already in hand.
            #
            # It was the other way round until measured, so up to
            # `LEARNED` - 1 tests ran ahead of the one test known to catch the
            # row, on 1105 of this table's 1309 rows.
            #
            # `Learned` still follows, rather than being skipped: a recorded
            # killer can be stale -- the code moved and the test no longer sees
            # it -- and then the learned front is the next best guess before
            # the full selection. That costs nothing when the killer is right,
            # because the killer has already answered.
            first = (
                f"{mutation.first} {ahead}".strip()
                if mutation.exact
                else f"{ahead} {mutation.first}".strip()
            )
            began = time.monotonic()
            verdict = _run(
                mutation.tests.split(),
                root,
                failfast=failfast,
                timeout=timeout,
                memory=memory,
                each=each,
                first=first if ahead else mutation.first,
                walk=walk,
            )
            verdict = verdict._replace(spent=time.monotonic() - began)
            if learned is not None and verdict.outcome == "caught":
                learned.saw(verdict.killer)
            return verdict
        finally:
            # Into the sandbox, not the working tree. Only so the next mutation
            # to borrow this copy starts from clean source.
            source.write_text(original, encoding="utf-8")
    finally:
        available.put(root)


#: What one lane is measured to occupy, as opposed to what `MEMORY` lets it
#: reach. One whole-suite run peaks at 317 MB resident; a gibibyte is three
#: times that, and resident is the number the OOM killer counts.
_LANE = 1 << 30


def _visible_memory() -> int:
    """Memory this process may actually use, container limits included.

    Not `SC_PHYS_PAGES` alone, which reports the host's and ignores a cgroup --
    so in a 2 GiB container on a 62 GiB host it answers 62 and the container is
    OOM-killed with every per-lane cap respected. `tools/cpus.py`'s
    `usable_cpus` makes exactly this argument for CPUs; this is the same mistake
    with the same shape, one resource over.

    Left here rather than beside `usable_cpus` because it has one caller.
    `usable_cpus` got its own module when it got a second, and that is the
    threshold this repository uses.
    """
    limits = []
    for where in CGROUPS:
        try:
            said = Path(where).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        # cgroup v2 writes `max` for "no limit"; v1 writes a number so large it
        # means the same thing, and comparing against the host total is the only
        # way to tell that from a real limit.
        if said.isdigit():
            limits.append(int(said))
    # A lane's share, when this harness is itself running inside one. Same
    # question as the cgroup file above -- "how much may *this* process use" --
    # and the same answer shape, which is why it is a limit here rather than a
    # branch somewhere in `run`.
    inherited = os.environ.get(_BUDGET, "")
    if inherited.isdigit() and int(inherited) > 0:
        limits.append(int(inherited))
    with suppress(AttributeError, OSError, ValueError):  # not POSIX
        limits.append(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    return min(limits) if limits else _BLIND_LANES * _LANE


#: Where the kernel publishes what it can still hand out. A module constant so a
#: test can point it at a file of its own and drive the real parser against real
#: kernel text, rather than patching the function that reads it -- which would
#: leave the parsing, the one part with anything to get wrong, unexercised.
MEMINFO = Path("/proc/meminfo")

#: Where a cgroup publishes the memory ceiling it has carved out, v2 first.
#:
#: A module constant for the reason `MEMINFO` is one: `_visible_memory` and
#: `_confined` both read these, and until this existed the tuple was spelled
#: twice -- so a path corrected in one place stayed wrong in the other, and
#: neither could be pointed at a file a test had written. Fourteen of
#: `_visible_memory`'s fifteen mutants survived the whole-tree sweep, and the
#: cgroup read is where most of them sat.
CGROUPS = (
    "/sys/fs/cgroup/memory.max",
    "/sys/fs/cgroup/memory/memory.limit_in_bytes",
)


def _unclaimed() -> int:
    """What the kernel says can be handed out right now, or 0 if it will not say.

    **The measurement that replaces a guess.** `_budget` used to halve visible
    memory unless `dedicated()` fired, and the halving is a guess about people:
    it assumes someone else wants half, whether or not anyone is there. It is
    wrong in both directions. On an idle cloud container it left 8 GiB of 16
    unused and, because `_share` gives up *lanes* once each ceiling would fall
    under `_FLOOR`, that cost more than half the parallelism -- measured, 3 lanes
    where 7 fit, and a 12-row table at 11.2s against 7.6s. On a laptop with an
    editor and a browser already holding ten of sixteen gigabytes, the same rule
    hands out eight that are not there.

    `MemAvailable` answers the question actually being asked: how much can be
    allocated without pushing the machine into swap, *accounting for what every
    other process is already using*. So a busy machine yields a small budget and
    an empty one a large budget, with nothing to configure and no claim about
    whose machine it is.

    Linux only -- there is no `/proc/meminfo` on macOS, and `vm_stat` is a
    subprocess this cannot justify spawning to size a pool. 0 means "no answer",
    and `_budget` keeps the old rule for that case; the `macos` CI leg is what
    proves that path stays reachable, the same argument `tupferl/config.py`'s
    `tomli` fallback rests on.

    `MemAvailable` and not `MemFree`: free memory excludes the page cache, which
    the kernel will reclaim on demand, so it understates by gigabytes on any
    machine that has read files. Measured here: 15,286 MiB free against 15,494
    available, and the gap is this repository's own working set.
    """
    try:
        said = MEMINFO.read_text(encoding="utf-8")
    except OSError:
        return 0
    for line in said.splitlines():
        name, _, rest = line.partition(":")
        if name == "MemAvailable":
            words = rest.split()
            # `kB` in the file means KiB, which is the kernel's own spelling and
            # not a unit conversion anyone should have to guess at.
            if len(words) == 2 and words[0].isdigit() and words[1] == "kB":
                return int(words[0]) * 1024
    return 0


def _confined() -> int:
    """The cgroup's memory limit, or 0 when the host's RAM is what bounds us.

    A limit counts only when it is *below* what the host reports: cgroup v2
    writes `max` for "no limit" and v1 writes a sentinel near 2**63, and on this
    machine that sentinel is what `memory.limit_in_bytes` holds. Comparing
    against the host total is the only way to tell a real limit from either.
    """
    host = 0
    with suppress(AttributeError, OSError, ValueError):  # not POSIX
        host = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    if not host:
        return 0
    for where in CGROUPS:
        try:
            said = Path(where).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if said.isdigit() and 0 < int(said) < host:
            return int(said)
    return 0


def dedicated() -> str:
    """Why this machine is this run's alone, or "" when it is shared.

    Two signals, and both are facts rather than guesses about intent:

    - **A cgroup limit is in force.** The kernel has already carved out this
      process's share, so halving it again double-counts the same reservation --
      the container has no "other half" to leave for anybody, because nobody
      else is in it.
    - **`CI` is set.** A CI runner is by construction not shared with a person
      waiting for their editor to respond. Every CI system sets it, and
      `tests/profiles.py` already keys off it for the same reason.

    Returned as a *reason* rather than a bool so the line the run prints can say
    which one applied. A lane count nobody can account for is what sent this
    author reading `_share` in the first place.
    """
    if os.environ.get("CI"):
        return "CI is set"
    if _confined():
        return "a cgroup limit is in force"
    return ""


def _why() -> str:
    """Which rule set the budget, for the line the run prints.

    Said out loud for the same reason `--limit` says what it dropped: three
    lanes on a four-core machine reads as a slow tool rather than a bounded one,
    and this author read `_share` twice before finding that the number came from
    memory rather than cores.
    """
    if os.environ.get(_TOTAL, "").isdigit():
        return _TOTAL
    if free := _unclaimed():
        return f"{free >> 20} MiB unclaimed, less {_SPARE >> 20} MiB spare"
    return f"dedicated: {dedicated()}" if dedicated() else "shared machine, so half of it"


def _budget() -> int:
    """What this run may spend in total.

    **Half of visible memory when the machine is shared with a person**, because
    the other half is theirs: mutation testing is a background chore and has no
    claim on a box somebody is working in.

    That halving was unconditional, and on a dedicated machine it is not thrift
    but waste. Measured on the container this was found in -- 16 GiB, four cores,
    nothing else running -- it gave a budget of 8037 MiB, and `_share` then
    allowed **three** lanes where the cores wanted eight. The ceiling is what
    stops a runaway, within seconds; the budget only decides how many honest
    lanes fit, and on a machine with no one else on it the honest answer is
    nearly all of them.

    So a dedicated machine keeps everything but `_SPARE`, and `TUPFERL_MUTATE_TOTAL`
    beats both -- because the one thing better than a good guess about who owns
    the machine is being told.

    **It is an environment variable and not a flag**, which this docstring and
    the line `_why` prints both called `--budget` until someone typed that and
    got "unrecognized arguments".

    A first attempt at explaining *why* it is a variable said `_run` passes it
    down so a nested harness inherits the same ceiling. That is wrong twice
    over: `_run` passes `_BUDGET`, not this, and `_TOTAL` short-circuits
    `_visible_memory` outright -- so a nested harness that inherited it would
    size itself for the **outer** total and ignore exactly the per-lane share
    `_BUDGET` exists to impose. The honest reason is smaller: it is a knob for
    the operator of the machine rather than for one invocation, and it is read
    in one place.
    """
    said = os.environ.get(_TOTAL, "")
    if said.isdigit() and int(said) > 0:
        return int(said)
    visible = _visible_memory()
    if free := _unclaimed():
        # `min` with `visible`, never `free` alone: inside a container
        # `/proc/meminfo` reports the **host's** numbers, so a 2 GiB cgroup on a
        # 62 GiB host would read as 60 available and be OOM-killed with every
        # per-lane cap respected. That is the exact mistake `_visible_memory`
        # was written for, and reading a second source of truth is how it would
        # come back.
        #
        # `_SPARE` still comes off the top: `MemAvailable` is a reading taken at
        # one instant, and the person whose machine this is may open something a
        # second later.
        return max(_FLOOR, min(visible, free) - _SPARE)
    if dedicated():
        # Never below the floor: a very small dedicated box would otherwise be
        # handed a budget under one lane's ceiling and get fewer lanes than the
        # shared rule would have given it, which is the opposite of the point.
        return max(_FLOOR, visible - _SPARE)
    return visible // 2


def _affordable() -> int:
    """How many lanes fit in memory, which is a different question from cores.

    `verdict.cap` bounds one lane; it does not bound their product, and the
    product is what the host feels. Sixteen lanes at 4 GiB is 64 GiB on a 62 GiB
    machine, so a table can still drive it into swap with every individual cap
    respected -- and a *generated* table is the shape that does it, because it
    walks a file in order and its runaway rows are therefore adjacent. When this
    crash was diagnosed, three of four lanes were running away simultaneously,
    all on the same source line.

    Divided by `_LANE` -- what a lane is measured to *use* -- and not by
    `MEMORY`, which is the ceiling on what one may reach before being killed.
    Dividing by the ceiling was the first shape and it assumed every lane is
    simultaneously pathological: an ordinary 16 GiB laptop dropped from sixteen
    lanes to two, and a 7 GiB CI runner to one, to bound a case that the ceiling
    already truncates within seconds. The ceiling stops a runaway; this number
    decides how many honest lanes fit.

    It is half the answer. `_share` is the other half, and the reason this one
    is no longer wrong on its own: what the machine feels is lanes *times*
    ceiling, and nothing here can see the ceiling.
    """
    return max(1, _budget() // _LANE)


class Share(NamedTuple):
    """How many lanes run at once, and what each one may occupy."""

    lanes: int
    memory: int


def _share(wanted: int, memory: int, pinned: bool = False) -> Share:
    """Pick both together, so that their product is something the machine has.

    `verdict.cap` bounds one lane and `_affordable` counts lanes, and for one
    release each was defensible while the pair was not: sixteen lanes at a 4 GiB
    ceiling is 64 GiB on a 63 GiB machine, so a table could drive the host into
    the OOM killer with every individual limit respected. That is woswoar#232, and it
    happened three times.

    The order of concessions is the argument:

    - lanes first, from `_affordable`, because a lane's *measured* use is the
      honest cost of running one and CPU is what the wall clock is made of;
    - then the ceiling, lowered to the share that many lanes leaves, because a
      ceiling is headroom for a pathological row rather than something an honest
      one spends;
    - and only when that share would fall under `_FLOOR` -- below which honest
      runs start being killed -- are lanes given up instead.

    So a big machine keeps its sixteen lanes and simply stops promising each of
    them four gigabytes it cannot deliver, and a small one loses lanes rather
    than reliability.

    ``pinned`` is an explicit `--workers`, and it keeps the lane count it asked
    for. The ceiling still shrinks around it, which is the part worth having; it
    is the *count* that a caller has reasons to fix that this cannot see.
    `tests.test_mutate.TestItRunsThemInParallel` is the case that made this
    concrete there -- it pins four lanes to assert that mutations overlap at all, and
    on a machine with too little memory to afford four it would otherwise assert
    the machine rather than the mechanism, which is what its own comment says it
    is pinned to avoid. The bound that still holds for a pinned run is `_Lanes`,
    which measures rather than predicts.

    ``memory <= 0`` is `--memory 0`, "no cap", and it passes straight through.
    There is no product to bound once one factor is infinite, and quietly
    imposing one would be the flag lying.
    """
    if memory <= 0:
        return Share(max(1, wanted), memory)
    budget = _budget()
    # What the ceilings may add up to, which is more than there is: see
    # `_COMMIT`. `_affordable()` below is deliberately left on the unscaled
    # budget -- it counts lanes by measured use, which already prices peaks as
    # independent.
    allowed = int(budget * _COMMIT)
    # An explicit `--memory` under the floor is the caller's call, not a mistake
    # to correct: they may be reproducing a small machine on purpose. It still
    # sets how many lanes fit.
    floor = min(memory, _FLOOR)
    lanes = max(1, wanted if pinned else min(wanted, _affordable(), allowed // floor))
    return Share(lanes, min(memory, max(floor, allowed // lanes)))


def run(
    mutations: Iterable[Mutation],
    baseline: bool = True,
    workers: int | None = None,
    *,
    strict: bool = True,
    walk: bool = True,
    failfast: bool = False,
    timeout: float = TIMEOUT,
    memory: int = MEMORY,
    each: float = EACH_TEST,
    summarise: bool = True,
    scope: str = "nothing above",
    landed: Callable[[Result], None] | None = None,
) -> Report:
    """Apply each mutation in its own copy of the tree; report what each answered.

    Prints one line per mutation, in the order the table gives them and not the
    order they finish, so that the output is the same every run and can be pasted
    into a pull request as it stands.

    With ``baseline``, also confirms the untouched tree is green -- without which
    "caught" means nothing, because a suite that is already failing catches
    everything. Its targets are sharded and queued alongside the mutations rather
    than run as one serial pass, because a single pass over the union of every
    target was measured at two thirds of the wall clock.

    ``scope`` names what a red baseline voids, because this function does not
    always own the whole of "above". A nested pass -- one batch of
    a `sweep` -- prints its rows inside a larger run whose earlier verdicts are
    still good, and "nothing above means anything" reads there as voiding those
    too. The alternative was a second `print` in the caller correcting this one,
    which is two sites that must stay adjacent, in order, and in agreement about
    wording, with nothing enforcing any of the three.

    ``strict`` decides what an unanswerable row does. For a hand-written table it
    should stop the run: a row that breaks collection is a mistake in the table,
    and the other rows will still be there once it is fixed. For a generated one
    it must not, because a single non-viable mutant out of two hundred would
    throw away every answer already paid for.
    """
    table = list(mutations)
    if not table:
        return Report([])
    asked = memory
    for mutation in table:
        check(mutation)

    shards = baseline_shards(table)
    # Twice the usable cores, which is what `tools/run_tests.py` measured for this
    # same subprocess-wait-bound work (jobs=8 beat jobs=4 by ~9%, jobs=16
    # regressed). An earlier `cpu // 2` here gave two lanes on a four-core runner
    # and was 1.76x slower than this for eight runs. A sandbox is ~1 ms, so a lane
    # is nearly free.
    # Bounded by the work there is and by the cores there are, and by nothing
    # else. A hardcoded `_LANES = 16` used to sit here too, with no measurement
    # behind "the most lanes worth running, whatever the machine reports" -- on a
    # 32-core machine it was the *only* binding term, and lifting it to 32 was
    # worth 30%: 214s against 303s over the 1309 rows of `--only tupferl/`,
    # two interleaved pairs.
    wanted = workers or min(len(table) + len(shards), usable_cpus() * 2)
    lanes, memory = _share(wanted, memory, pinned=workers is not None)
    if lanes != wanted or memory != asked:
        # Said out loud, for the reason `--limit` says what it dropped: a run
        # that quietly took fewer lanes than the machine has cores reads as a
        # slow tool rather than as a bounded one, and a ceiling lowered in
        # silence is how the row that reports `ran out of memory` becomes
        # inexplicable.
        print(
            paint.paint(
                f"{lanes} lane(s) at {memory >> 20} MiB each, from {_budget() >> 20} MiB "
                f"of usable memory ({_why()}), committing "
                # Said, never silent. Over-committing is a judgement about
                # peaks not coinciding, and a reader has to be able to see it
                # was made -- especially on the run that does get killed.
                f"{100 * lanes * memory / max(_budget(), 1):.0f}% "
                f"-- see tools.mutate._share.",
                paint.QUIET,
            )
        )

    # A fresh high-water mark, so this run reports its own. `_WATCHED` is a
    # module-level singleton and a process may call `run` more than once -- a
    # spec file calling `verify` twice is the shape that does it.
    _WATCHED.forget()
    # Around the baseline as well as the rows, because that is the wall clock a
    # reader waited through. A rate computed over the rows alone would flatter
    # every run by however long its suite takes untouched.
    began = time.monotonic()

    results: list[Result] = []
    #: `results[n]` came from `table[places[n]]`. Two lists rather than a list of
    #: pairs, so nothing downstream has to unpack a shape it did not ask for.
    places: list[int] = []
    timings: dict[str, float] = {}
    red = False
    #: One `Learned` shared by every lane, which under a stride is the shape
    #: that works: a lane sees roughly every Nth row, so a private front would
    #: learn a thinned-out version of the same signal. The same list object in
    #: every slot rather than a second code path, so `_attempt` is unchanged.
    learning = [Learned()] * lanes
    work = Work(len(table))
    total = len(table)
    width = len(str(total))
    lane_width = len(str(max(lanes - 1, 1)))
    #: The indent an unanswered row's reason sits at, so it lines up under the
    #: label rather than under the counter. Derived rather than a literal,
    #: because the counter's width is the table's.
    reason_at = " " * (2 * width + lane_width + 6)
    done = 0
    stop = threading.Event()
    #: Held across recording *and* printing, so two lanes finishing together
    #: cannot interleave a row's two lines, and `done` counts every row once.
    speaking = threading.Lock()

    def deliver(index: int, lane: int, verdict: Verdict) -> None:
        """Record and announce one finished row, from the lane that ran it."""
        nonlocal done
        mutation = table[index]
        with speaking:
            done += 1
            timings.update(verdict.times or {})
            results.append(Result(mutation, verdict))
            # The row's own position, kept beside the result rather than
            # recovered from it afterwards. Sorting by `id(mutation)` would work
            # only while no two rows are the same object, which is a property of
            # the generator rather than of anything here.
            places.append(index)
            if landed is not None:
                landed(results[-1])
            known = MEANING[verdict.outcome]
            counter = paint.paint(f"[{done:>{width}}/{total}]", paint.QUIET)
            where = paint.paint(f"L{lane:>{lane_width}}", paint.QUIET)
            # Padded first, painted second. `f"{painted:9}"` counts the escape
            # bytes as columns, so a nine-wide field becomes four and every
            # coloured row sits five characters left of every plain one. The
            # argument is in `tools/paint.py`; this is the table it was found in.
            head = paint.paint(f"{known.headline:9}", known.colour)
            # `flush`, and not as a nicety. Every documented way of running a
            # sweep redirects to a log, and a stream that is not a terminal is
            # *block* buffered -- so without this a progress counter would arrive
            # in 8 KiB steps of about a hundred rows, which is not a progress
            # counter. Measured here: a sweep's log sat empty for five minutes
            # because its header was 250 bytes and nothing had flushed it.
            print(f"{counter} {where} {head} {mutation.label}", flush=True)
            if not verdict.answered:
                # Not indented under the row by accident: an unanswered row must
                # not be skimmable as one of the two real verdicts.
                print(paint.paint(f"{reason_at}-- {verdict.detail}", known.colour), flush=True)

    def _first_look(shard: str) -> Verdict:
        """One baseline shard, in a borrowed sandbox like any other lane task."""
        return _borrow(available, shard.split(), timeout, memory, each)

    def lane_walk(lane: int) -> None:
        """One lane: take the next row, run it, say what it found, repeat."""
        while not stop.is_set():
            index = work.take()
            if index is None:
                return
            verdict = _attempt(
                table[index], available, failfast, timeout, memory, each, walk, learning[lane]
            )
            deliver(index, lane, verdict)
            if strict and not verdict.answered:
                # Every lane stops at its next take. The rows already in flight
                # are paid for either way, which is what the cancelling version
                # of this also settled for.
                stop.set()

    def announce(checked: Future[Verdict]) -> None:
        """Say what a baseline shard found, the moment it is known.

        A callback rather than a blocking wait, because waiting is what cost the
        overlap. The shards go into the pool *first*, so FIFO gives them workers
        before the lane walkers; the walkers then fill every remaining worker at
        once, and each shard hands its worker to another walker as it finishes.
        Nothing idles, and the warning still arrives as early as it possibly can
        -- which for a red baseline is what matters, since the rows streaming
        under it are all void.

        Measured, and this is why it is a callback: resolving these before
        starting any lane left 24 of 32 lanes idle for the whole baseline and
        put the run at 536s against the stride's 351s. That number was reported
        as a cost of the dispatch. It was not.
        """
        nonlocal red
        looked = checked.result()
        with speaking:
            # Before any early return: a shard that ran a whole selection with
            # nothing failing has measured every test in it, which is where most
            # of what `Killers` orders by comes from. A red one still measured
            # whatever ran before it went red.
            timings.update(looked.times or {})
            if looked.outcome == "survived" or red:
                return
            # `survived` is the untouched suite passing, which is the one place
            # the mutation vocabulary reads backwards. A shard that broke or
            # timed out is red too, and its reason is the only clue to why.
            red = True
            print(
                paint.paint(
                    f"  BASELINE NOT GREEN ({looked.outcome}) -- the suite does not "
                    f"pass untouched, so {scope} means anything: {looked.detail}",
                    paint.BAD + paint.HEAD,
                ),
                flush=True,
            )
            # The traceback, not just the name. A red baseline is the one verdict
            # that cannot be diagnosed by re-running the row, and the shard it
            # came from is rarely reproducible by hand -- `first` is a shard of
            # its own, the sandbox is a copy, and the lanes are concurrent.
            if looked.why:
                print(indent(looked.why.rstrip(), "  | "), flush=True)

    with _sandboxes(lanes) as available, ThreadPoolExecutor(max_workers=lanes) as pool:
        # Submitted before the walkers, and never waited on here. There are
        # exactly as many sandboxes as workers and every borrower is a pool
        # task, so shards and rows can share the pool without starving it.
        checking = [pool.submit(_first_look, shard) for shard in shards] if baseline else []
        for checked in checking:
            checked.add_done_callback(announce)
        for walker in [pool.submit(lane_walk, lane) for lane in range(lanes)]:
            walker.result()

    # Completion order on the console, table order on disk. A report whose rows
    # arrive in whatever order the lanes finished cannot be diffed against the
    # next one, and `sort_survivors` walks `results` in order to decide which
    # occurrence of a shared key is the one already recorded.
    # Keyed on the position alone: a tie would fall through to comparing two
    # `Result`s, which is not an ordering anyone defined. Positions are unique,
    # so this never happens -- and that is exactly when it is cheap to say so.
    ordered = sorted(zip(places, results, strict=True), key=lambda pair: pair[0])
    results = [result for _, result in ordered]

    if stop.is_set():
        # The earliest unanswered row in *table* order, not whichever lane
        # tripped first. Which row wins a race is not something to report.
        broke = next(result for result in results if not result.verdict.answered)
        raise SystemExit(
            f"{broke.mutation.label}: this mutation broke collection rather than being "
            f"noticed, so neither 'caught' nor 'SURVIVED' would mean anything "
            f"about {broke.mutation.tests}. {broke.verdict.detail}"
        )

    if not red and (loose := _unbaselined(results, shards)):
        # After the pool, in a sandbox of its own. These are tests the baseline
        # never ran, standing behind a `caught` -- so until they are green on the
        # untouched tree those rows claim something nothing checked. One shard
        # for all of them; on a table whose selections are good this never runs.
        print(
            paint.paint(
                f"\n{len(loose)} test(s) caught a row without being baselined; checking them...",
                paint.ODD,
            )
        )
        with _sandboxes(1) as spare, ThreadPoolExecutor(max_workers=1) as pool:
            loose_verdict = pool.submit(_borrow, spare, loose, timeout, memory, each).result()
        if loose_verdict.outcome != "survived":
            # Only these rows, never the whole run: every other verdict rests on
            # a shard that *was* green, and voiding those would throw away
            # answers this found nothing wrong with. `broke` rather than a
            # correction, because what is known is that the answer cannot be
            # trusted -- not what the right answer is.
            why = f"caught by a test that also fails untouched: {loose_verdict.detail}"
            results = [
                Result(result.mutation, Verdict("broke", why))
                if result.verdict.killer in set(loose)
                else result
                for result in results
            ]
            print(
                paint.paint(
                    f"  NOT GREEN ({loose_verdict.outcome}) -- "
                    f"rows they caught are reported broke.",
                    paint.BAD,
                )
            )

    pace = Pace(time.monotonic() - began, lanes, memory)
    # Only when this call owns the output. With ``summarise`` off the caller is
    # doing the reporting and gets `pace` on the report to do it with, which is
    # what keeps the block last instead of buried above the survivor list.
    if summarise:
        _report_stats(results, pace=pace, red=red)
    else:
        # The caller is doing the reporting and will print the block, but the
        # headroom line is a *guarantee of `run`* rather than of the block --
        # `tests.test_mutate.TestWhatTheHeaviestLaneHeld` pins it, because with
        # `forget` working and no lane sampled, a run that never reports is
        # indistinguishable from a correct one. Reported here and skipped in the
        # block, so it is printed once either way.
        _report_headroom(memory)

    if not red and summarise:
        _summarise(results)
    # `widened=walk`, never a bare `True`: the flag's whole job is to say
    # whether a survivor here has been run against everything, and with `walk`
    # off it has not. Hard-coding it would make the one report that must not
    # claim the guarantee the one that claims it loudest.
    report = Report(results, red, widened=walk, times=timings or None, pace=pace)
    _RUNS.append(report)
    return report


#: How much of its ceiling one lane process may reach before the line saying so
#: is shouted rather than muttered. Not the stated 2x margin `_FLOOR` claims:
#: this machine runs at ~92%, so shouting below 2x would shout on every run and
#: train a reader to skip the line. 90% is "one more test away", which is the
#: point at which somebody should look.
_TIGHT = 0.90


def _report_stats(
    results: Sequence[Result], *, pace: Pace | None, red: bool, headroom: bool = True
) -> None:
    """The block a reader looks at first, and the four numbers that must be in it.

    **`broke` and `timeout` get their own lines, always, including at zero.**
    They were the one category a sweep could not settle here, and the reason is
    worse than a survivor's version of it: such a row is never `caught`, so the
    line it appears to guard is guarded by nothing *while the summary shows it
    in neither of the two numbers a reader looks at*. A block reporting only
    caught and survived would rebuild exactly that hole with better typography.

    **The score names its denominator.** `caught / (caught + survived)` silently
    drops the unanswered rows, which is the flattering direction -- the one
    every bug in this class has erred in. Saying "of N answered" beside "M
    answered nothing" makes the flattering reading unavailable.

    **With a red baseline there is no score at all.** A failing suite notices
    every mutation, so the harness credits every row: a table of 51 came back 51
    for 51 twice here before anyone read the line rather than the rows. A
    percentage is far more seductive than a wall of verdicts, so this refuses to
    compute one rather than printing a flattering number under a warning. Make
    tools fail loudly rather than substituting a default.

    **The sum is silent when it is right and loud when it is wrong.** A tick
    that cannot be false is the decoration CLAUDE.md's test bar is about, so
    there is none; a discrepancy gets a line, because rows going missing between
    the table and the report is exactly the failure nothing else here would see.

    **`rows/s` is reported beside the lane count, and per lane.** A rate without
    the lane count beside it is not comparable to anything -- measured here, the
    same table ran at 1.84 rows/s over 32 lanes and 1.49 over 16, and neither
    figure means anything alone.
    """
    counts = Counter(result.verdict.outcome for result in results)
    total = len(results)
    if not total or pace is None:
        # Silent rather than zeroed. A block of noughts under a report read back
        # from disk is a measurement reported as a result, which is the shape
        # `_report_headroom` exists to correct.
        return
    rate = total / pace.seconds if pace.seconds > 0 else 0.0
    print(
        paint.paint(
            f"\n{total} row(s) in {pace.seconds:.0f}s -- {rate:.2f}/s over "
            f"{pace.lanes} lane(s) ({rate / pace.lanes:.3f}/s/lane)",
            paint.HEAD,
        )
    )
    for outcome in ("caught", "survived", "broke", "timeout"):
        known = MEANING[outcome]
        # Every one of the four, at zero as well, and painted the way the row
        # itself was. A category that disappears when empty is one a reader
        # stops expecting to see.
        print(f"  {paint.paint(f'{known.headline:9}', known.colour)} {counts[outcome]:>6}")
    named = sum(counts[outcome] for outcome in ("caught", "survived", "broke", "timeout"))
    if named != total:
        print(
            paint.paint(
                f"  {named} row(s) accounted for of {total} -- {total - named} went missing "
                f"between the table and this report.",
                paint.BAD + paint.HEAD,
            )
        )
    if red:
        print(
            paint.paint(
                "  no score: the baseline was red, so every row above is void -- a suite "
                "that fails untouched notices every mutation.",
                paint.BAD + paint.HEAD,
            )
        )
        if headroom:
            _report_headroom(pace.ceiling)
        return
    answered = counts["caught"] + counts["survived"]
    if not answered:
        return
    unanswered = counts["broke"] + counts["timeout"]
    caught = counts["caught"]
    said = f"  {caught} caught of {answered} answered -- {caught / answered:.1%}"
    if unanswered:
        said += f"; {unanswered} row(s) answered nothing"
    print(paint.paint(said + ".", paint.QUIET))
    if headroom:
        _report_headroom(pace.ceiling)


def _report_headroom(ceiling: int) -> None:
    """Say how close the heaviest lane process came to its ceiling.

    **Because the ceiling was a number nobody had checked here.** `_FLOOR`
    claimed a whole-suite probe peaks at 838 MiB of address space and that its
    2x margin therefore held; measured on this machine it is ~1.85 GiB against a
    2053 MiB ceiling, a margin of about 1.1x. Nothing printed either figure, so
    the only way to discover it was to go looking -- and the stale one was
    exactly the number a reader would use to argue the floor could come *down*,
    which is the change that kills every lane.

    Sampled, so it understates: `_SAMPLE` is a second and the figure is the
    *current* size read from `/proc/<pid>/stat` rather than the kernel's own
    `VmPeak` high-water. Measured against `VmPeak` over one sweep, the
    understatement was **3%** -- worth naming, and not worth a second file read
    per process per second to remove.

    Silent when nothing was sampled, which is a run whose lanes each finished
    inside one interval. Saying "0 MiB of 2053" there would be a measurement
    reported as a result, which is the shape this function exists to correct.
    """
    widest = _WATCHED.widest()
    if not widest or ceiling <= 0:
        return
    share = widest / ceiling
    said = (
        f"heaviest lane process held {widest >> 20} MiB of its {ceiling >> 20} MiB "
        f"ceiling ({share:.0%}, sampled, ~3% under)"
    )
    print(paint.paint(said, paint.ODD if share >= _TIGHT else paint.QUIET))


def _summarise(
    results: Sequence[Result],
    accepted: dict[str, Accepted] | None = None,
    complete: bool = True,
) -> None:
    """The part a pull request quotes when the news is bad.

    ``accepted`` splits the survivors into ones somebody has already read and
    ones nobody has. The accepted ones are *counted and not listed*: the count
    is what stops the file becoming a way to hide them, and the list is what
    would bury the rows that are new.
    """
    sorted_out = sort_survivors(results, accepted or {}, complete)
    if accepted is not None:
        _report_known(sorted_out)
    # One list of rows-to-read, then split by whether the row answered anything.
    # Both paragraphs below are about the *unread* rows once there is a record,
    # so taking them from one place is what keeps an accepted `BROKE` out of the
    # list as reliably as an accepted survivor.
    fresh = (
        sorted_out.fresh
        if accepted is not None
        else [result for result in results if not MEANING[result.verdict.outcome].clean]
    )
    survivors = [result for result in fresh if result.verdict.answered]
    unanswered = [result for result in fresh if not result.verdict.answered]
    if survivors:
        # `MEANING["survived"].colour`, not `paint.BAD`: this paragraph is about
        # an outcome, and the table owns what an outcome looks like. A literal
        # here is a second copy that agrees today.
        red = MEANING["survived"].colour
        print(
            paint.paint(
                f"\n{len(survivors)} survived. "
                "A test that cannot see the fix removed is decoration:",
                red + paint.HEAD,
            )
        )
        for result in survivors:
            # The label painted and the selection not. They are one line, but
            # the label is the thing to act on and `tests` is how the row got
            # there -- and a survivor list is read by someone deciding which
            # three of forty rows to look at first.
            print(
                f"  - {paint.paint(result.mutation.label, red)} "
                f"{paint.paint(f'({result.mutation.tests})', paint.QUIET)}"
            )
        print("Suspect the fixture before the mutation -- see CLAUDE.md rule 3.")
    if unanswered:
        # Counted separately and never as survivors: these rows asked nothing, and
        # rolling them into either verdict is the error this module exists to
        # avoid, one level up.
        print(
            paint.paint(
                f"\n{len(unanswered)} asked nothing, "
                "so the table is that much smaller than it looks:",
                paint.ODD + paint.HEAD,
            )
        )
        for result in unanswered:
            # Each row in its own outcome's colour rather than the group's:
            # `broke` and `timeout` are different answers, and this is the list
            # where a reader decides which of them to chase.
            colour = MEANING[result.verdict.outcome].colour
            print(f"  - {paint.paint(result.mutation.label, colour)}: {result.verdict.detail}")


def _accept(sorted_out: Survivors, accepted: dict[str, Accepted]) -> None:
    """Write this run's unread survivors into the record, and drop stale rows.

    `--accept` is a *deliberate* act, and it is why this is a flag rather than
    something a run does when it feels like it: recording a survivor is saying
    somebody read it and decided, and a tool that did that on its own would be
    deciding on their behalf.

    The reason it writes is a placeholder naming the file and function, because
    a reason nobody wrote is not a reason -- the row is there to be edited, and
    a reviewer seeing `TODO` in the diff is the point rather than an oversight.
    """
    gone = set(sorted_out.stale)
    rows = {key: row for key, row in accepted.items() if key not in gone}
    for result in sorted_out.fresh:
        key = _key(result.mutation)
        if (already := rows.get(key)) is not None:
            # Another row of a shape already read. The count rises rather than
            # the reason being rewritten: what changed is how many there are.
            rows[key] = already._replace(seen=already.seen + 1)
        else:
            rows[key] = Accepted(f"TODO: why is {result.mutation.label} acceptable?", 1)
    KNOWN.write_text(
        json.dumps(
            {key: {"why": row.why, "seen": row.seen} for key, row in rows.items()},
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        paint.paint(
            f"recorded {len(sorted_out.fresh)} new and dropped {len(sorted_out.stale)} stale; "
            # Both numbers, because they differ: on the first whole-tree sweep
            # 557 survivors shared 432 keys, and a report of either alone reads
            # as the other.
            f"{KNOWN} now holds {len(rows)} key(s) covering "
            f"{sum(row.seen for row in rows.values())} survivor(s). "
            "Every new row says TODO on purpose.",
            paint.ODD,
        )
    )


def _status(report: Report, sorted_out: Survivors) -> int:
    """Red or green, once the record has had its say.

    **It asks "is anything new", not "is anything alive".** That is the whole
    point of the record: a sweep whose every survivor somebody has already read
    and written a reason for has nothing to report, and a job that goes red
    every week is one nobody looks at by the third week.

    `Report.clean` is left alone -- it is a fact about the run, and `verify`
    still uses it for hand-written tables, where a survivor is never expected
    and there is no record to consult.

    There is no second test for `BROKE` and `TIMEOUT` here any more. There was
    one, from when the record held answered survivors only and an unanswered row
    had nowhere to be written down; now `sort_survivors` sorts every row that is
    not `caught`, so an unread one is in `fresh` and a read one is excused, on
    the same terms and through the same table. Keeping the old arm as well would
    have meant a recorded `BROKE` row still turning the run red -- a reason
    written and then ignored, which is how a record stops being read.
    """
    return 1 if sorted_out.fresh or report.baseline_red else 0


def _report_known(sorted_out: Survivors) -> None:
    """Say how many survivors were already understood, and what is stale.

    **Printed whenever there is a baseline**, because a baseline whose size is
    invisible is one nobody re-reads: the number going up unnoticed is how a
    record stops meaning "understood" and starts meaning "ignored". Silent at
    zero, which is every hand-written spec file -- there is no baseline there
    to be invisible, and a line saying so on every `verify()` run is noise
    that would train the eye past the line that matters.
    """
    if sorted_out.accepted:
        print(
            paint.paint(
                f"{len(sorted_out.accepted)} survivor(s) already recorded in {KNOWN} "
                f"-- counted, not listed.",
                paint.QUIET,
            )
        )
    if sorted_out.stale:
        # Loud, because a stale entry is a claim about code that no longer
        # exists -- and the row it used to cover may have been replaced by one
        # nobody has read.
        print(
            paint.paint(
                f"{len(sorted_out.stale)} entr(y/ies) in {KNOWN} match nothing this run "
                f"generated; the code they describe has moved or gone.",
                paint.ODD,
            )
        )


def verify(mutations: Iterable[Mutation], baseline: bool = True, workers: int | None = None) -> int:
    """`run`, reduced to the number of survivors. The shape spec files call.

    Strict, because a spec file is hand-written: a row that cannot be answered is
    a mistake in the table, and stopping is what gets it fixed.
    """
    report = run(mutations, baseline, workers)
    if report.baseline_red:
        return len(report.results)
    return sum(1 for result in report.results if result.verdict.outcome == "survived")


#: What a row runs when selection could not name a target: everything. Empty
#: rather than `"tests"`, because a package name is not discovery -- `unittest`
#: imports the package, finds no tests in it, and reports a green run of nothing.
#: Not a fallback of convenience either: see `mutants.targets_for`, and the row
#: says out loud when it happens.
WHOLE_SUITE = ""


def baseline_shards(table: Sequence[Mutation]) -> list[str]:
    """What the untouched tree must be green on before any verdict counts.

    One shard per distinct selection, plus one holding every remembered `first`.
    A cached killer can name a test outside its row's selection, so it needs
    covering too -- one shard for all of them, never one each: a shard per
    remembered test is the sharding explosion that cost 372s -> 730s, in a new
    disguise.

    **Not the whole suite, though every row now walks it.** That was tried and
    measured, and it fails three ways. It costs a full-suite run per table, which
    took the preflight from about two minutes to six. It is *recursive* here:
    the suite contains `test_mutate`, `test_run_tests`, `test_verdict` and three
    more, so every baseline starts a second harness inside a memory-capped
    sandbox -- a hazard this module's own docstring records for the rare row that
    mutates `tools/`, made universal. And it does not finish: measured, the
    whole suite in a sandbox exceeds `TIMEOUT` and comes back
    `BASELINE NOT GREEN (timeout)`, which voids every verdict above it.

    What the walk needs instead is `_unbaselined`: a row can be caught by a test
    no shard here covered, and only *those* tests need checking, only when one
    turns up. See it for why that is the same guarantee for a fraction of the
    cost.

    A function rather than a constant so `--baseline-only` and `run` cannot
    drift: that flag exists to ask, in one shard's time, the question a sweep
    will ask later, and it is worth nothing if it asks a different one. It
    already went stale once here.
    """
    shards = sorted({mutation.tests for mutation in table})
    if ahead := " ".join(sorted({name for row in table for name in row.first.split()})):
        shards.append(ahead)
    return shards


def _unbaselined(results: Sequence[Result], shards: Sequence[str]) -> list[str]:
    """Killers that no baseline shard covered, so nothing proved them green.

    The hole the walk opens. A row that nothing in its selection notices keeps
    going through the rest of the suite, so it can be caught by a test no shard
    ran untouched -- and on a tree that is already red that claim is free, since
    `failfast` stops at the first red test whatever it was about. Both real
    survivors of one sweep came back credited to a shell-hook test that had never
    heard of the file under mutation (woswoar#268). That is the false `caught`
    this module exists to make impossible.

    Checking *these tests* rather than the whole suite is what keeps the price
    honest: it is the same guarantee, bought at the size of the exception rather
    than the size of the suite, and on a table whose selections are good it is
    empty and costs nothing at all.

    `run_tests.selects` rather than comparing module names, for the reason
    `Killers.ahead_of` gives: a selection naming a class never matched at all,
    and `WHOLE_SUITE` -- the empty selection -- covers everything by meaning
    "run the lot".
    """
    if any(not shard for shard in shards):
        return []
    reachable = [only for shard in shards for only in shard.split()]
    return sorted(
        {
            result.verdict.killer
            for result in results
            if result.verdict.outcome == "caught"
            and result.verdict.killer
            and not any(run_tests.selects(result.verdict.killer, only) for only in reachable)
        }
    )


#: Where the remembered killers live by default, under the directory the
#: `.gitignore` already keeps out of the tree. Machine-specific and stale the
#: moment a test is renamed, which is why nothing here trusts it: see `Killers`.
KILLERS = Path("sweeps/killers.json")

#: How long the cheap-first prefix may take before a row falls through to its
#: own selection. Half a second, which is where the curve turns: measured on
#: milestone 3's table, 40 tests cost 0.33s and cover 57% of caught rows, and
#: the next ten cost 4.1s for 28 points more -- eight times the price per point.
#: A budget rather than a count, because what matters is the seconds every row
#: pays up front, and tests do not all cost the same.
PREFIX = 0.5


#: Survivors somebody has looked at, keyed the way `Killers` keys a killer.
#:
#: **Committed, unlike everything else a sweep writes.** `sweeps/` is ignored
#: because a report is transient; this is the opposite -- it is the record of
#: which survivors have been read and what was decided about them, and it is
#: reviewed in a pull request like any other claim about the tree.
#:
#: The problem it exists for: a whole-tree sweep found 557 survivors, and
#: triaging them in prose does not survive to the next sweep. The following
#: Sunday produces the same 557 rows with nothing to say which were already
#: understood, so either somebody reads all of them again or nobody reads any of
#: them. Both have happened.
KNOWN = Path("known-survivors.json")


class Accepted(NamedTuple):
    """What was decided about one kind of survivor, and how many there were."""

    why: str
    #: **How many rows of this shape somebody read.** `_key` is content, never
    #: position -- that is what lets a row keep its disposition when the code
    #: around it moves, and it is also why two identical mutations in one file
    #: share a key. Measured on the first whole-tree sweep: 557 survivors
    #: collapsed to 432 keys, so 125 rows would have been absorbed by a sibling
    #: nobody read.
    #:
    #: With a count, the 126th occurrence of a shape is still new. Without one,
    #: accepting `if x:` becoming `if True:` once in a file accepts every such
    #: line in it, for ever -- which is the failure this whole record exists to
    #: prevent, arriving through its own key.
    seen: int


def known_survivors(where: Path = KNOWN) -> dict[str, Accepted]:
    """Every accepted survivor's key, why it was accepted, and how many.

    Missing or unreadable is an empty answer rather than an error: a run that
    cannot read the file must report *more* than it should, never less. The
    failure this guards against is a record that silently swallows everything
    because a JSON comma went missing in a merge.
    """
    try:
        rows = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(rows, dict):
        return {}
    found: dict[str, Accepted] = {}
    for key, value in rows.items():
        if not isinstance(value, dict):
            continue
        why, seen = value.get("why"), value.get("seen")
        if isinstance(why, str) and isinstance(seen, int) and seen > 0:
            found[str(key)] = Accepted(why, seen)
    return found


class Survivors(NamedTuple):
    """This run's survivors, split by whether anybody has read them before."""

    fresh: list[Result]
    #: Accepted, with the reason each was accepted for.
    accepted: list[tuple[Result, str]]
    #: Keys in the file that this run's table never produced. Reported so the
    #: file cannot quietly accumulate rows for code that no longer exists --
    #: which is how a baseline stops describing the tree it claims to.
    stale: list[str]


def sort_survivors(
    results: Sequence[Result], accepted: dict[str, Accepted], complete: bool = True
) -> Survivors:
    """Split every row that is not `caught` into read and unread.

    **Not caught, rather than `survived`.** `MEANING` spends "survived" narrowly,
    on the rows that were *answered* and not noticed; `BROKE` and `TIMEOUT` are
    unanswered, and for a long time they had nowhere to be recorded. That made
    them the one category a sweep could not settle: 33 of them came back every
    run with nothing to say which had been read, which is precisely the problem
    this record was built for -- and worse than the survivors' version of it,
    because a `BROKE` row is never `caught`, so the line it appears to guard is
    guarded by nothing while the summary shows it in neither of the two numbers
    a reader looks at.

    A few of them cannot be answered at all and never will be: two mutations
    force `verdict.collect` down `loader.discover(".")` and run the whole suite
    inside a memory-capped sandbox, and `run_tests`'s `if args.worker:` becomes
    a fork bomb. Those want a written reason exactly as an equivalent mutant
    does. In the field's own vocabulary all of these *are* survivors -- the
    mutant was not killed -- which is why the file keeps its name.

    **Counted, not merely matched.** A key covers as many rows as were read, and
    the next one of that shape is fresh -- see `Accepted.seen` for the 125 rows
    that would otherwise have been absorbed by a sibling.

    **`complete` is what makes `stale` mean anything.** "This key matches nothing
    the run generated" is evidence the code has gone only if the run generated
    everything -- and a `--base` run generates rows for the changed lines alone,
    so it "fails to generate" every key belonging to a file the diff did not
    touch. Measured on this change's own sweep: 206 of the record's 210 entries
    were reported stale by a two-file diff.

    That was not merely a misleading line. `_accept` **drops** what `stale`
    names, so `python -m tools.mutate --base main --accept` -- the command
    CLAUDE.md gives for recording a run's survivors -- would have deleted 206
    reviewed reasons and left the file claiming four. A record whose documented
    use destroys it is worse than no record, and nothing in the output said so:
    the count simply came back smaller.

    With no evidence, say nothing. An entry kept one sweep too long is a line in
    a file; an entry dropped is an argument somebody has to make again.
    """
    fresh: list[Result] = []
    seen: list[tuple[Result, str]] = []
    left = {key: row.seen for key, row in accepted.items()}
    for result in results:
        if MEANING[result.verdict.outcome].clean:
            continue
        key = _key(result.mutation)
        if left.get(key, 0) > 0:
            left[key] -= 1
            seen.append((result, accepted[key].why))
        else:
            fresh.append(result)
    reached = {_key(result.mutation) for result in results}
    stale = sorted(set(accepted) - reached) if complete else []
    return Survivors(fresh, seen, stale)


def _resume_key(mutation: Mutation) -> tuple[str, int, int, str] | None:
    """What identifies a row *within one table*, for deciding whether it ran.

    **Not `_key`, and the difference is the whole of #46.** That issue proposed
    keying resume on `_key` -- content, no position -- because it already exists
    and `Killers` uses it. Measured on this tree's `--all` table before writing
    the fix: `_key` gives 2547 distinct values for 3103 rows, so **556 rows
    share a key with another**. Resume keyed on it would read those as already
    answered when they had never run, and the report would claim verdicts it
    never had -- which is the failure the issue itself calls strictly worse than
    losing them.

    Position is right here for the same reason it is wrong in `Killers`. A
    resume compares a table against a report of *that same table*, minutes or
    hours old, from a tree nobody edited in between -- so a line number is
    stable, and it is the only thing that separates two rows spelled
    identically. `generate` dedupes on `(span, new)`, which is what makes
    `(path, span, new)` unique: measured, 3103 distinct for 3103 rows.

    `None` for a row with no span -- a hand-written one -- and `sweep` reads
    that as "not recorded", so it runs again. Re-running a row is a cost;
    skipping one that never ran is a wrong answer.

    **Both coordinates, and the mutants that swap them are equivalent.** A sweep
    reports `span[0]` becoming `span[1]` and the reverse as survivors, and they
    are: `generate` dedupes on `(span, new)`, so telling them apart needs two
    rows sharing a start (or an end) and a `new` while differing in the other
    coordinate -- an outer and an inner node beginning at the same offset and
    rewritten to the same text. Nothing in this tree is that, and a fixture
    built to be it would be testing the tuple rather than the resume. Written
    here rather than as a row in `known-survivors.json` because that record
    keys on `(path, operator, old, new)` and these three collide with unrelated
    `0`/`1` literals elsewhere in this file -- absorbed, not read.
    """
    if mutation.span is None:
        return None
    return (mutation.path, mutation.span[0], mutation.span[1], mutation.new)


def _key(mutation: Mutation) -> str:
    """What identifies a mutation across runs.

    Content, never position. `label` and `span` both carry a line number, and a
    line number is invalidated by any edit *above* it -- which is every edit, so
    a position-keyed cache would be empty exactly when it was most wanted. What
    stays the same is the file, the operator, and the text going in and out.

    Hashed rather than concatenated because `old` and `new` are whole
    statements: the keys would otherwise be kilobytes and the file unreadable.

    **The outcome is deliberately not in it**, though the record now covers
    `BROKE` and `TIMEOUT` as well as survivors -- so a reason written for a row
    that survived also excuses the same row once it starts raising, and the
    reason is then about the wrong thing. The alternative is worse: a row near
    the alarm flips between `BROKE` and `TIMEOUT` by itself -- seven of
    `watch.main`'s did, from a fixture racing the harness -- and keying on the
    outcome would make each flip a fresh unread row and a red sweep, which is
    the "goes red every week so nobody looks" failure the record exists to
    prevent. A reason can go stale in *meaning* while its key stays valid; that
    is true of survivors too, and the answer to it is a reviewer reading the
    file, not a finer key.
    """
    parts = "\0".join((mutation.path, mutation.operator, mutation.old, mutation.new))
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()[:16]


class Killers:
    """Which test caught each mutation last time, so it can run first this time.

    A mutation is caught by a *set* of tests, and `failfast` stops at whichever
    the selection reaches first. That order is alphabetical and has nothing to
    do with cost, so a mutant in a pure function was found only after the
    CLI-driven classes above it had run: 20.48s to learn something a 0.30s test
    knew (tupferl#5).

    Choosing one global order over all mutants is Min-Sum Set Cover -- NP-hard,
    greedy within a factor of 4, and it needs the *full* kill-set per mutant,
    which costs more to collect than it saves. Per mutant there is no problem to
    solve: run the test that worked last time.

    **The remembered test is put in front of the usual selection, never in place
    of it.** That asymmetry is the whole safety argument. A cache that is right
    saves the rest of the suite; a cache that is wrong costs one extra test and
    the run continues exactly as it would have. Substituting instead would turn
    every stale entry into a `caught` that nothing verified -- flattering the
    tests, which is the direction every bug in this class has erred.

    **It goes on `Mutation.first`, not into `Mutation.tests`.** Folding it into
    `tests` was the first shape and it *doubled* the wall clock: `run` shards the
    baseline check by distinct `tests` string, so giving every row its own killer
    gave every row its own shard, and one baseline run of `tupferl/sync.py`'s
    selection became 42 of them -- 42 x 27s, which is the whole regression and
    nothing to do with the ordering it was meant to fix. Measured at 372s against
    730s before the field existed.
    """

    def __init__(self, where: Path | None, budget: float = PREFIX) -> None:
        self.where = where
        self.budget = budget
        #: What the last `ahead_of` decided, for a caller that wants to say so.
        self.head: list[str] = []
        self.dropped = 0
        self.known: dict[str, str] = {}
        self.cost: dict[str, float] = {}
        #: What each *row* cost last time, by `_key`. `cost` above is per test
        #: and answers "which tests are cheap and catch a lot"; this is per
        #: mutation and answers "which rows should run first". Two different
        #: questions that happen to share a file.
        self.seconds: dict[str, float] = {}
        if where is not None and where.is_file():
            try:
                saved = json.loads(where.read_text(encoding="utf-8"))
                # A flat mapping is the older shape, from before costs were
                # recorded. Read rather than discarded: the killers in it are
                # still good, and the costs refill on the next run.
                rows = saved.get("killers", saved) if isinstance(saved, dict) else {}
                self.known = {str(k): str(v) for k, v in rows.items() if isinstance(v, str) and v}
                found = saved.get("costs", {}) if isinstance(saved, dict) else {}
                self.cost = {str(k): float(v) for k, v in found.items()}
                spent = saved.get("seconds", {}) if isinstance(saved, dict) else {}
                self.seconds = {str(k): float(v) for k, v in spent.items()}
            except (OSError, ValueError, AttributeError, TypeError):
                # A half-written or hand-edited file is not worth a failure: the
                # worst an empty cache does is run at yesterday's speed.
                self.known, self.cost, self.seconds = {}, {}, {}

    def prefix(self) -> list[str]:
        """Cheap tests that between them catch a lot, cheapest yield first.

        Greedy on *rows newly caught per second*, which is the 4-approximation
        for Min-Sum Set Cover (Feige, Lovász, Tetali) -- and the best any
        polynomial algorithm gets unless P=NP. It is computed here from what the
        cache already holds rather than from a checked-in list, so it cannot rot
        against a suite that has moved.

        This is what a row with no remembered killer runs first. The measured
        shape on milestone 3's table: the first seven tests cost under a
        millisecond each and cover 15% of everything, all of them pure-logic
        tests -- the decision table, the report, the commit message. A `sync.py`
        row's full selection is 22s by comparison.

        Under-counts on purpose. `failfast` stops at the first test to notice, so
        only one killer per row is ever observed and a test gets no credit for
        rows something else reached first. Real coverage is at least this.
        """
        rows: dict[str, set[str]] = {}
        for key, test in self.known.items():
            if test in self.cost:
                rows.setdefault(test, set()).add(key)
        covered: set[str] = set()
        chosen: list[str] = []
        spent = 0.0
        while spent < self.budget:
            best, yield_ = "", 0.0
            for test, caught in rows.items():
                fresh = len(caught - covered)
                # A floor on the divisor: a test too fast to measure would
                # otherwise divide by zero, and those are exactly the ones worth
                # having first.
                rate = fresh / max(self.cost[test], 0.001)
                if fresh and rate > yield_:
                    best, yield_ = test, rate
            if not best or spent + self.cost[best] > self.budget:
                break
            chosen.append(best)
            covered |= rows[best]
            spent += self.cost[best]
        return chosen

    def ahead_of(self, table: Sequence[Mutation]) -> list[Mutation]:
        """The same table, with each remembered killer moved to the front.

        Only ids that still load are used, and they are resolved once for the
        whole table rather than per mutant. That is not thrift: an id that no
        longer exists makes `unittest`'s loader record an error, which
        `tools/verdict.py` correctly classifies as `broke` -- so one renamed
        test would turn every mutant that remembered it into a non-answer, and
        the sweep would report a wall of `BROKE` rows for a rename.
        """
        head = self.prefix()
        wanted = {self.known[_key(row)] for row in table if _key(row) in self.known} | set(head)
        usable = _loadable(wanted)
        if dropped := len(wanted) - len(usable):
            print(f"{dropped} remembered test(s) no longer load, so their rows run as usual.")
        head = [test for test in head if test in usable]
        if head:
            spent = sum(self.cost.get(test, 0.0) for test in head)
            print(
                f"{len(head)} cheap test(s), {spent:.2f}s, run first where nothing is remembered."
            )

        ahead = []
        for row in table:
            killer = self.known.get(_key(row), "")
            if killer and killer in usable:
                # Exact beats general: this test is known to catch *this* row, so
                # the prefix would only be work before the answer. `exact` says
                # so to `_attempt`, which owes the same precedence against
                # `Learned` and for the same reason.
                ahead.append(row._replace(first=killer, exact=True))
                continue
            # Nothing remembered -- a new row, or one whose killer stopped
            # working. Cut to what this row can reach: a test in a module that
            # does not import the mutated file cannot see the mutation, so
            # running it is pure cost.
            #
            # `run_tests.selects` rather than comparing module names, which was
            # the first version and dropped the prefix in the two places it was
            # most wanted. An empty selection is `WHOLE_SUITE` -- what a file
            # nothing imports gets -- so its rows run *everything*, ~51s each,
            # and the prefix was cut to nothing for exactly them. And a selection
            # naming a class rather than a module never matched at all.
            reachable = row.tests.split()
            mine = [
                test
                for test in head
                if not reachable or any(run_tests.selects(test, only) for only in reachable)
            ]
            ahead.append(row._replace(first=" ".join(mine)) if mine else row)
        return ahead

    def learn(self, report: Report) -> None:
        """Remember what caught each mutation, and forget what stopped catching it.

        Costs come from `Report.times`, which `run` fills from every row it
        collected *and* every baseline shard. The shards are the richest source
        by far: they alone run a whole selection with nothing failing, so they
        measure every test in it rather than the handful before the first
        failure.
        """
        self.cost.update(report.times or {})
        for result in report.results:
            if result.verdict.spent > 0:
                # Every outcome, not only the answered ones. A `timeout` row
                # costs the full `--timeout` and a `broke` row is expensive
                # too, and what this orders by is price rather than verdict --
                # those are exactly the rows a run most wants to start early.
                self.seconds[_key(result.mutation)] = result.verdict.spent
            if result.verdict.outcome == "caught" and result.verdict.killer:
                self.known[_key(result.mutation)] = result.verdict.killer
            elif result.verdict.answered:
                # It survived. Whatever used to catch it does not any more, so
                # keeping the entry would put a test that cannot help at the
                # front of every future run of this row.
                self.known.pop(_key(result.mutation), None)

    def save(self) -> None:
        if self.where is None:
            return
        self.where.parent.mkdir(parents=True, exist_ok=True)
        self.where.write_text(
            json.dumps(
                {"killers": self.known, "costs": self.cost, "seconds": self.seconds},
                indent=1,
                sort_keys=True,
            ),
            encoding="utf-8",
        )


def _loadable(ids: Iterable[str]) -> set[str]:
    """Those of `ids` that `unittest` can still turn into a test.

    Asked of the loader rather than by checking that the file exists: a renamed
    *method* leaves its module in place, and that is the common way a remembered
    id goes stale.
    """
    found = set()
    for name in ids:
        loader = unittest.TestLoader()
        try:
            loader.loadTestsFromName(name)
        except Exception:
            # Deliberately every exception: a module that no longer imports can
            # raise anything at all on the way, and each one means the same
            # thing here -- this id cannot be put in front of a run.
            continue
        if not loader.errors:
            found.add(name)
    return found


def generated(args: argparse.Namespace) -> list[Mutation]:
    """The table the diff implies, printed about before any of it runs."""
    root = Path.cwd()
    touched = mutants.every_line(root) if args.all else mutants.changed_lines(args.base, root)
    if args.only:
        touched = {
            path: lines
            for path, lines in touched.items()
            if any(wanted in path for wanted in args.only)
        }
    if not touched:
        raise SystemExit(
            "no mutable files at all under tupferl/ or tools/."
            if args.all
            else f"nothing mutable changed against {args.base}. Only tupferl/**.py and "
            f"tools/**.py are generated from; a change to tests/ is not a fix to test."
        )

    index = mutants.importers(root)
    table: list[Mutation] = []
    for path in sorted(touched):
        tests = mutants.targets_for(path, root, index) or WHOLE_SUITE
        table.extend(
            mutants.generate(
                (root / path).read_text(encoding="utf-8"),
                path,
                touched[path],
                tests=tests,
                operators=args.operator or None,
                skip=args.skip_operator or None,
            )
        )
        if tests == WHOLE_SUITE:
            print(
                paint.paint(
                    f"note: nothing imports {path}, so its rows run the whole suite.", paint.ODD
                )
            )

    counted = sum(len(lines) for lines in touched.values())
    print(
        paint.paint(
            f"{len(touched)} file(s), {counted} {'' if args.all else 'changed '}lines "
            f"-> {len(table)} mutants",
            paint.HEAD,
        )
    )
    kept, dropped = mutants.cap(table, args.limit)
    if dropped:
        # Said out loud, because a silent cap reads as "everything was covered"
        # and the count would look right either way -- CLAUDE.md is explicit.
        share: dict[str, int] = {}
        for row in dropped:
            share[row.path] = share.get(row.path, 0) + 1
        listed = ", ".join(f"{path} {count}" for path, count in sorted(share.items()))
        print(paint.paint(f"--limit {args.limit}: {len(dropped)} not run ({listed}).", paint.ODD))
        print(
            paint.paint(
                "Counts below are out of what ran, not out of what the diff implies.", paint.ODD
            )
        )
    # Ordered here, at the source: this is where path order came from, and both
    # `sweep` and the plain path take their rows from it. After `cap`, which
    # re-sorts what it kept back into path order -- so the round-robin it uses
    # to *choose* rows never reaches the order they run in.
    return [row for rows in by_size(kept).values() for row in rows]


def by_size(table: Sequence[Mutation]) -> dict[str, list[Mutation]]:
    """The table grouped by file, smallest file first, each file's rows together.

    **One rule, two callers, because the two paths disagreed.** `sweep` sorted
    this way and the plain `--base` path did not: `generated` builds its table
    with `for path in sorted(touched)`, and a `ThreadPoolExecutor` runs futures
    in submission order, so *alphabetical* order became execution order by
    accident of assembly. Nobody chose it.

    What that cost is measured in tupferl#49: a 132-row sweep across five files
    was stopped after 32 minutes and 59 rows, and every one of them was in
    `mutate.py` because `mutate` sorts first. `paint.py` was a module the same
    diff had just added, and not one of its 19 rows had been looked at. Worse,
    the first file was the *expensive* one -- 39 of those 59 rows survived, and
    a survivor only earns the name after the full walk -- so path order spent
    the whole budget on the slowest file and produced no coverage of the rest.

    Smallest first, so a run that is stopped has answered whole files. **Each
    file's rows stay contiguous**, and that is load-bearing once, not twice.
    This said it was also what let `sweep` count a file down to zero before
    writing its `--json`; **that stopped being true at #46**, which made the
    write per *row* and the resume key positional-per-row, and the claim sat
    here for releases afterwards being quoted as a reason. `sweep` uses the
    grouping to build a flat list and to print a file count, and for nothing
    else. What is left:

    - `Learned` (tupferl#43) is move-to-front, and its docstring rests on rows
      arriving sorted by file and line so that consecutive mutants sit in the
      same function. Interleaving *rows* across files was this issue's first
      proposal and is a measured dead end -- replayed over 906 recorded rows the
      move-to-front hit rate falls from 72.8% to 27.3%, and nothing fails: same
      verdicts, no counter, just a slower sweep. See CLAUDE.md.

    Composes with `Killers.ahead_of`, which despite the name does not reorder
    rows at all -- it maps each one to itself with `first` set, so table order
    survives it. The issue warned they might conflict; they do not.

    A plain `dict`, whose insertion order the caller reads back: `sweep` wants
    the grouping for its countdown and `generated` wants the flat rows, and one
    of them building its own would be the second spelling this replaces.
    """
    grouped: dict[str, list[Mutation]] = {}
    for row in table:
        grouped.setdefault(row.path, []).append(row)
    return {path: grouped[path] for path in sorted(grouped, key=lambda name: len(grouped[name]))}


def slowest_first(table: Sequence[Mutation], seconds: Mapping[str, float]) -> list[Mutation]:
    """The same table, each file's rows ordered by what they cost last time.

    Longest-processing-time-first, which is the classic greedy for makespan and
    is within 4/3 of optimal. It matters here because row costs are wildly
    uneven: measured on this tree, survivors are ~12% of the rows and ~49% of
    the lane-seconds, at ~71s each against ~7.3s for a caught row. Left in line
    order they arrive whenever they arrive, and a handful reached near the end
    is what every lane then waits on -- observed repeatedly, with the last ten
    completions of a 32-lane sweep spread over seven lanes.

    **Within each file, never across** -- and that restriction is more
    conservative than it needs to be. It was chosen believing `sweep` counted a
    file down to zero before writing its `--json`; it does not, and has not
    since #46 made that write per row. See `by_size`, where the stale claim
    was. What actually wants contiguity is `Learned`, and only for rows with no
    recorded killer -- a timed row already carries its exact killer on `first`,
    which is the answer adjacency was approximating.

    So a *global* sort is available for the timed rows, keeping only the cold
    ones grouped by file, and it would reach the residual tail this cannot: the
    largest file runs last under `by_size`, so its survivors are dispatched near
    the end however well its own rows are ordered (measured: the median
    survivor moves 1198 -> 1043 of 1309, but the last one is last in every
    run). What still argues against it is #49's smallest-file-first, so an
    interrupted run has answered whole files -- weaker than when it was written,
    since a per-row resume no longer loses a partial file. Not attempted.

    That restriction costs much less than it looks, because of where the tail
    actually forms. `by_size` puts the **largest** file last, so a sweep's final
    stretch is one file's rows -- exactly the stretch this reorders.

    **A row nobody has timed takes its file's median**, so cold rows sort into
    the middle as a block and, the sort being stable, keep their line order
    within it. Three things follow, and all three are wanted: a brand-new file
    is left exactly as it arrived; a `--base` diff, whose rows are new text by
    construction and so almost entirely cold, is barely reordered at all; and
    `Learned`, which is move-to-front over adjacency, keeps the contiguity it
    rests on for precisely the rows that still need it. A row with a recorded
    cost does not need it -- `Killers.ahead_of` has already put its own killer
    on `first`.

    Keyed by `_key`, so 556 of this tree's 3103 rows share a key with another
    and therefore share a recorded cost. That is fine and is the same argument
    `Killers` makes about `known`: a wrong estimate costs ordering quality and
    can never cost an answer. It is why this is not `_resume_key`, which is
    unique but positional, and so empty after any edit above a row.
    """
    if not seconds:
        return list(table)
    by_path: dict[str, list[Mutation]] = {}
    for row in table:
        by_path.setdefault(row.path, []).append(row)
    known = [seconds[key] for row in table if (key := _key(row)) in seconds]
    overall = median(known) if known else 0.0

    ordered: list[Mutation] = []
    cold = 0
    for rows in by_path.values():
        here = [seconds[key] for row in rows if (key := _key(row)) in seconds]
        # This file's own median, not the tree's: `gitrepo.py`'s rows each drive
        # a real `git` subprocess and `merge.py`'s do not, so a global figure
        # would place every cold row of the cheap file ahead of the expensive
        # file's timed ones.
        middle = median(here) if here else overall
        cold += len(rows) - len(here)
        ordered.extend(sorted(rows, key=lambda row: -seconds.get(_key(row), middle)))
    timed = len(table) - cold
    if timed:
        print(
            paint.paint(
                f"{timed} of {len(table)} row(s) ordered slowest-first from the last sweep; "
                f"{cold} never timed, kept in line order.",
                paint.QUIET,
            )
        )
    return ordered


def _bytes(said: str) -> int:
    """A byte count for `--memory`, refusing the two values that fail silently.

    `0` means no cap, spelled the way `--limit 0` next to it already means it.
    A negative number is refused rather than accepted: `RLIM_INFINITY` is `-1`
    on Linux, so `--memory -1` used to read as "no limit" through a flag whose
    whole purpose is to impose one -- a typo that removes the guard and says
    nothing.
    """
    try:
        value = int(said)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a byte count: {said}") from None
    if value < 0:
        raise argparse.ArgumentTypeError(
            f"negative memory limit: {said}. Use 0 for no cap; -1 is not infinity here."
        )
    return value


def _marker(where: Path) -> Path:
    """The file whose only meaning is that the run is over.

    ``--json`` cannot carry that meaning. Under `--batch` and `--all`, `_persist`
    writes after every file, so the report exists long before the run ends --
    `tools/watch.py --done` read its arrival as a finish and announced one
    **nine minutes** early, exiting 0 while the sweep still had eleven files to
    go. That is this repository's own
    watchdog being told the wrong thing by this module, which is woswoar#275.

    A sibling name rather than a suffix swap: `Path("r.json").with_suffix(...)`
    has to reason about which suffix it is replacing, and `r.json.done` beside
    `r.json` is what a reader sorting a directory expects to see.
    """
    return where.parent / (where.name + ".done")


def _pidfile(where: Path) -> Path:
    """Where a run records its own process id, beside its report.

    Written because the caller could not reliably record it. A sweep is started
    detached -- that is the whole reason `tools/watch.py` exists -- and the
    obvious `python -m tools.mutate ... & echo $! > sweep.pid` does not survive
    every shell: in one session it produced no file at all, and the pid had to
    be recovered from the process table on fifteen consecutive runs, with a
    pattern filter carefully written not to match the filtering command. That is
    the `pgrep -f` hole `watch.py` was built to close, reappearing in the step
    that feeds it.

    The run knows its own pid and cannot get it wrong, so it writes it.
    """
    return where.parent / (where.name + ".pid")


def _persist(report: Report, where: Path, announce: bool = True) -> None:
    """The run's answers, as `tools/reached.py` reads them.

    Written because a survivor list is not the end of the analysis. Crossing
    these outcomes with a coverage map is what separates "no test reaches this"
    from "a test reaches it and asserts nothing", and that cannot be done from
    the printed table -- which is prose, in table order, with no line numbers.

    `line` is parsed back out of the label rather than carried on `Mutation`,
    because a hand-written row has no position at all, and the label is the one
    place both kinds already agree on how to say where they are.
    """
    rows = []
    for result in report.results:
        found = re.match(r"\S+?:(\d+) ", result.mutation.label)
        rows.append(
            {
                "label": result.mutation.label,
                "path": result.mutation.path,
                "line": int(found.group(1)) if found else None,
                "tests": result.mutation.tests,
                "operator": result.mutation.operator,
                "outcome": result.verdict.outcome,
                "detail": result.verdict.detail,
                "killer": result.verdict.killer,
                # Enough to rebuild the row, not just to read about it, so a
                # resumed sweep can re-run a recorded row rather than only read
                # its verdict back. Cheap to keep and impossible to reconstruct.
                "old": result.mutation.old,
                "new": result.mutation.new,
                "span": list(result.mutation.span) if result.mutation.span else None,
                # Beside `killer`, which is the other half of what `Killers`
                # learns from a row. Without it a *resumed* sweep could never
                # time a row at all: the resume skips what is already recorded,
                # so a table that kept crashing would stay permanently cold and
                # `slowest_first` would have nothing to order it by -- on
                # exactly the long whole-tree sweeps that crash. Three decimals
                # because this orders rows spanning 0.3s to a 300s timeout.
                "seconds": round(result.verdict.spent, 3),
            }
        )
    # **Written aside and renamed, never in place.** `os.replace` is atomic on
    # POSIX, so a crash during a write leaves the previous complete report
    # rather than a truncated one -- and `_recorded` reads a truncated report as
    # *nothing*, which costs the whole run.
    #
    # Optional before #46 and required after it. At 19 writes over a 2.4-hour
    # sweep the process spent about 0.01% of its life mid-write; at one write a
    # row that is 3124 x 37.6 ms, **1.4%**. A recovery mechanism whose own
    # recovery file has a 1-in-70 chance of being the casualty is not one, and
    # the failure is silent: the next run simply starts over.
    beside = where.with_name(where.name + ".tmp")
    beside.write_text(
        json.dumps(
            {"baseline_red": report.baseline_red, "widened": report.widened, "results": rows},
            indent=1,
        ),
        encoding="utf-8",
    )
    os.replace(beside, where)
    if announce:
        # Silent for the per-row writes `sweep` makes, which since #46 happen
        # 3103 times in a whole-tree run. The line is worth reading once, at the
        # end; printed after every row it is the loudest thing in the log and
        # says the same thing each time.
        print(paint.paint(f"\nwrote {len(rows)} row(s) to {where}", paint.QUIET))


def _run_spec(mutations: Sequence[Mutation], args: argparse.Namespace) -> int:
    """A `MUTATIONS` table from a spec file, run the way the caller asked for.

    This used to be `run(mutations)` -- no arguments at all -- so every flag on
    the command line was accepted by `argparse` and then silently dropped:
    `--workers`, `--memory`, `--timeout`, `--each-test`, `--no-baseline`,
    and `--json`. Asking for one lane got two; asking for a report
    got no file, which reads as the run having failed to write one rather than as
    the flag never having been consulted. A flag that silently does nothing is
    the failure this project refuses everywhere else -- `sync` rejects `--ours`
    outright rather than ignore it -- and the tool that checks the tests is a bad
    place to keep an exception.

    `strict` stays on, which `_run_generated` turns off: a spec file is written
    by hand, so a row that cannot be answered is a mistake in the table, and
    stopping is what gets it fixed.

    CLAUDE.md's promise that a survivor has been run against the whole suite is
    kept by `run` itself now: every row walks outward past its selection, so a
    hand-written table gets the same guarantee this path could never offer when
    it was a second pass the spec path did not call.
    """
    report = run(
        mutations,
        baseline=not args.no_baseline,
        workers=args.workers,
        timeout=args.timeout,
        memory=args.memory,
        each=args.each_test,
    )
    if args.json:
        _persist(report, args.json)
        _marker(args.json).touch()
    return 0 if report.clean else 1


def _run_generated(
    rows: Sequence[Mutation],
    args: argparse.Namespace,
    landed: Callable[[Result], None] | None = None,
) -> Report:
    """One batch of generated rows. Both the whole table and `sweep` use this.

    `strict=False`: a generated row that breaks collection is not a mistake
    anyone made, and discarding two hundred paid-for answers because of it would
    be. The row is reported and the run goes on.

    `failfast=True`: worth having for a generated table and not for a hand
    table, because `caught` is the expected outcome for most generated rows and
    without it each one runs the rest of its target after the answer is known.
    An average, not a bound -- `unittest` runs classes alphabetically, so a
    mutant caught only by the last of them still pays for nearly all.

    No ``scope``: it existed because `sweep` called this once per *file*, and a
    batch had to say that a red baseline voided only its own rows. tupferl#7
    replaced the batches with one pool, so there is one baseline and one scope
    again, and `run`'s default is right.
    """
    return run(
        rows,
        baseline=not args.no_baseline,
        workers=args.workers,
        strict=False,
        summarise=False,
        failfast=True,
        timeout=args.timeout,
        memory=args.memory,
        each=args.each_test,
        landed=landed,
    )


def _recorded(where: Path | None) -> list[Result]:
    """Every row a previous `--json` report holds, rebuilt.

    Rebuilt rather than merely counted, because `sweep` has to do three things
    with them and only one is "skip". They must stay in the file it rewrites --
    the first version returned labels only, so a resumed run persisted just the
    batches it had run and *deleted* the answers it had decided to skip, which
    is the recovery mechanism eating the thing it recovers. And they must reach
    `_summarise` and the exit status, or a resume whose new batches are all
    caught exits 0 while recorded survivors go unmentioned.

    A half-written file resumes as nothing: re-running everything is the safe
    reading of a crash mid-write.
    """
    if where is None or not where.is_file():
        return []
    try:
        saved = json.loads(where.read_text(encoding="utf-8"))
        return [
            Result(
                Mutation(
                    row["label"],
                    row["path"],
                    row["old"],
                    row["new"],
                    row["tests"],
                    span=(row["span"][0], row["span"][1]) if row.get("span") else None,
                    operator=row.get("operator", ""),
                ),
                Verdict(
                    row["outcome"],
                    row.get("detail", ""),
                    row.get("killer", ""),
                    spent=float(row.get("seconds", 0.0)),
                ),
            )
            for row in saved.get("results", [])
        ]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def sweep(table: Sequence[Mutation], args: argparse.Namespace) -> Report:
    """Run a large table in one pool, writing answers out as each file finishes.

    One table of three thousand rows reports nothing until it ends, and the run
    it was written for took 151 minutes and was killed twice by an out-of-memory
    machine before the guard in woswoar#223 existed. A crash then cost the afternoon.
    Recorded per file, a crash costs one file, and re-running with the same
    `--json` skips what is already recorded -- there is no separate flag.

    **Scheduled per row, recorded per file**, and separating those two is the
    whole of tupferl#7. This ran a *pool per file* until then, so a batch could
    not return until its slowest row did: one hung mutant held a lane for the
    full `--timeout` while the other two idled, and the two hangs in milestone
    3's table sat in different files, so they were serialised -- ~600s of a 913s
    run, most of it with the machine two thirds empty. Batching was never about
    parallelism; it is about when the report is safe to write.

    It also cost the baseline. Each batch checked its own shard, and with one
    selection per file that shard had no second shard to run beside it: nine
    files meant nine serialised suite runs. Pooled with everything else they
    overlap.

    A `--json` written mid-run says `baseline_red: false` until the run ends,
    because the baseline shards are collected after the rows -- see `run`. That
    is the exposure the per-batch version had too, one batch later, and it is
    bounded the same way: a resumed run re-checks the baseline for whatever files
    it still has to do.
    """
    collected = _recorded(args.json)
    # Keyed by row, not by file, and paired with the per-row write below -- the
    # two halves of #46 have to move together. Recorded per *file*, a crash
    # inside one lost every row of it however many had been answered: 673 of the
    # 3103-row table for `tools/mutate.py`, 23%, about half an hour at the 2.76s
    # a row that sweep measures. Written per row but resumed per file, the next
    # run would call a partly-recorded file complete and silently drop the rows
    # that never ran -- a report claiming answers it never had, which is worse
    # than losing them.
    #
    # `_resume_key`, never `_key`: a label is not unique (78 are duplicated
    # here) and neither is `_key` (556 rows share one). See its docstring.
    done = {key for result in collected if (key := _resume_key(result.mutation)) is not None}

    # One walk deciding both, so the "skip" predicate cannot drift from the
    # "run" one. Counted from the table rather than from the report, so a
    # `--json` reused across an edit says nothing about a file the table no
    # longer has rather than naming one the run is not doing anyway.
    todo: list[Mutation] = []
    skipped: dict[str, int] = {}
    for row in table:
        if _resume_key(row) in done:
            skipped[row.path] = skipped.get(row.path, 0) + 1
        else:
            todo.append(row)

    by_file = by_size(todo)
    for path, count in sorted(skipped.items()):
        # The count, not just the name. A file is no longer all-or-nothing, so
        # "skipping" without a number cannot distinguish a file fully recorded
        # from one that got three rows in before the crash -- and the whole
        # point of the change is that those are now different.
        print(paint.paint(f"{path}: {count} row(s) already recorded, skipping", paint.QUIET))
    if not by_file:
        # `widened=True` on every report this function builds, recorded rows
        # included. `sweep` is only ever reached from `main`, which always walks;
        # a rebuilt `Report` that took the field's default would write
        # `widened: false` onto rows that did walk, which is the flag lying in
        # exactly the direction it exists to prevent. It is rebuilt four times
        # here, so this is four chances to forget.
        #
        # Asserted rather than derived only in this arm, where there is no run to
        # ask: every row was read back from a recorded report, and nothing ran.
        return Report(collected, widened=True)

    # Smallest file first, and its rows contiguous -- `by_size`, which the plain
    # `--base` path now shares. Contiguity is what `Work` hands each lane as a
    # segment, so it is load-bearing here in a way it was not when the pool took
    # whatever was next: a segment that straddled files would learn nothing.
    order = list(by_file)
    rows = [row for rows_here in by_file.values() for row in rows_here]
    print(
        paint.paint(f"\n{len(rows)} mutant(s) across {len(order)} file(s), in one pool", paint.HEAD)
    )

    fresh: list[Result] = []

    def finished(result: Result) -> None:
        fresh.append(result)
        if args.json:
            # Every row, not every file. #46 asked for this measured before the
            # change landed, and offered a write-every-N fallback if it showed.
            # Measured on a full-size report -- 3124 rows, 1.45 MB -- the median
            # of 11 writes is 37.6 ms, against the 2.76 s a row the issue took
            # from a real sweep: 117 s over a 2.4-hour run, **1.36%**. Below the
            # >10% a machine drifts by over minutes, so there is no throttle
            # here and no window of loss to size.
            #
            # It is 4.5 GB of writes to keep 1.45 MB of state, because each
            # write is the whole report: O(rows^2) in table size. At 3124 rows
            # that buys a crash costing one row instead of 673 for 1.36%; at ten
            # thousand it would be ~4%, and somewhere past that the fallback the
            # issue offered starts to earn its knob. Nothing to do today, but it
            # is the term that grows.
            #
            # `True`, not `report.widened`: this runs *during* `_run_generated`
            # below, so that name is not bound yet and reading it here is a
            # `NameError` on every `--batch` sweep that persists mid-run. `main`
            # is the only caller and always walks.
            _persist(Report([*collected, *fresh], widened=True), args.json, announce=False)

    report = _run_generated(rows, args, landed=finished)
    collected.extend(report.results)
    if args.json:
        _persist(Report(collected, report.baseline_red, widened=report.widened), args.json)
    if report.baseline_red:
        print(
            paint.paint(
                f"\nthe baseline was red, so none of the {len(collected)} row(s) means anything.",
                paint.BAD + paint.HEAD,
            )
        )
    # `times` carried through, not dropped. Re-wrapping the report without them
    # is what made the cheap prefix silently learn nothing: the run measured
    # every test and the number reached `Killers` as an empty dict.
    return Report(
        collected,
        report.baseline_red,
        widened=report.widened,
        times=report.times,
        pace=report.pace,
    )


def _baseline_is_green(table: list[Mutation], args: argparse.Namespace) -> bool:
    """Run just this table's baseline shards, and say whether they all passed.

    `baseline_shards` rather than a second spelling of it, so this cannot answer
    a different question from the sweep it is meant to predict. ``table`` is no
    longer read: every row walks the whole suite, so the shard set does not
    depend on which rows are in it. Kept in the signature because the caller has
    the table and a future shard set may want it again -- and because dropping
    it would make this function's *name* the only thing tying it to the run.
    """
    shards = baseline_shards(table)
    # The same sizing `run` does, so the question is asked under the conditions
    # the sweep will ask it under -- which is the whole point of asking early.
    wanted = args.workers if args.workers is not None else _affordable()
    lanes, memory = _share(wanted, args.memory, pinned=args.workers is not None)
    green = True
    with _sandboxes(lanes) as available, ThreadPoolExecutor(max_workers=lanes) as pool:
        checks = [
            pool.submit(_borrow, available, shard.split(), args.timeout, memory, args.each_test)
            for shard in shards
        ]
        for shard, future in zip(shards, checks, strict=True):
            verdict = future.result()
            name = shard if len(shard) < 70 else f"{shard[:67]}..."
            if verdict.outcome == "survived":
                print(f"  {paint.paint('green  ', paint.GOOD)} {paint.paint(name, paint.QUIET)}")
                continue
            green = False
            print(paint.paint(f"  RED ({verdict.outcome})  {name}: {verdict.detail}", paint.BAD))
            if verdict.why:
                print(indent(verdict.why.rstrip(), "  | "))
    print(
        paint.paint(
            f"\n{len(shards)} baseline shard(s), {'all green' if green else 'NOT green'}.",
            paint.HEAD + (paint.GOOD if green else paint.BAD),
        )
    )
    return green


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.mutate",
        description="Run a table of mutations -- from a spec file, or from a diff.",
    )
    parser.add_argument(
        "script", nargs="?", help="a Python file defining MUTATIONS: list[Mutation]"
    )
    parser.add_argument(
        "--base", help="generate mutants for every line changed against this revision"
    )
    parser.add_argument(
        "--list", action="store_true", help="print the generated table and run nothing"
    )
    parser.add_argument(
        "--only", action="append", default=[], metavar="PATH", help="restrict to matching paths"
    )
    parser.add_argument(
        "--operator", action="append", default=[], metavar="NAME", help="use only this operator"
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="every line of every mutable file, rather than a diff",
    )
    parser.add_argument(
        "--batch",
        action="store_true",
        help="run a file at a time, writing --json as each lands (implied by --all)",
    )
    parser.add_argument(
        "--skip-operator",
        action="append",
        default=[],
        metavar="NAME",
        help="run every operator but this one",
    )
    parser.add_argument("--limit", type=int, default=LIMIT, help="cap the table (0 for no cap)")
    parser.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds per mutation")
    parser.add_argument(
        "--each-test",
        type=float,
        default=EACH_TEST,
        metavar="SECONDS",
        help=f"seconds one test may take, 0 to disable (default {EACH_TEST:g})",
    )
    parser.add_argument(
        "--memory",
        type=_bytes,
        default=MEMORY,
        help="bytes of address space one mutation may occupy (0 for no cap)",
    )
    parser.add_argument("--workers", type=int, help="lanes to run in parallel")
    parser.add_argument("--no-baseline", action="store_true", help="skip the untouched-suite check")
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="run just the untouched-suite check for this table, and stop",
    )
    parser.add_argument(
        "--json",
        type=Path,
        metavar="PATH",
        help="write the outcomes here, for `python -m tools.reached`",
    )
    parser.add_argument(
        "--killers",
        type=Path,
        default=KILLERS,
        metavar="PATH",
        help=f"remember which test caught each mutation (default {KILLERS})",
    )
    parser.add_argument(
        "--no-killers",
        action="store_true",
        help="ignore and do not update the remembered killers",
    )
    parser.add_argument(
        "--accept",
        action="store_true",
        help=(
            f"record this run's survivors in {KNOWN} so later runs report only new ones "
            "(only a whole-tree --all run may also drop entries)"
        ),
    )
    parser.add_argument(
        "--prefix",
        type=float,
        default=PREFIX,
        metavar="SECONDS",
        help=f"budget for the cheap-tests-first prefix, 0 to disable (default {PREFIX:g})",
    )
    args = parser.parse_args(argv)

    if args.all and args.base:
        parser.error("--all is every line; --base is what changed. Not both.")
    if args.all:
        # Downstream only asks "generated or a spec file?", and `--all` is the
        # generated path with a wider net rather than a third kind of run.
        args.base = "--all"
        if args.limit == LIMIT:
            # The default cap is sized for a diff. Left alone it turned the
            # documented `--all` into 200 of 4451 rows -- and, because the cap
            # spreads across files, into batches of seven, so batching,
            # incremental `--json` and resume all did nothing on the one command
            # line anybody would type. An explicit `--limit` is still honoured.
            args.limit = 0
    if bool(args.script) == bool(args.base):
        parser.error("give a spec file, --base or --all")

    if args.base:
        table = generated(args)
        killers = Killers(None if args.no_killers else args.killers, budget=args.prefix)
        if args.list:
            for row in table:
                print(f"  {paint.paint(f'{row.operator:16}', paint.QUIET)} {row.label}")
            return 0
        # After `--list`, which is about the table rather than about how it will
        # be run, and before the first row.
        table = killers.ahead_of(table)
        # After `ahead_of`, which maps each row to itself with `first` set and
        # so leaves table order alone, and before anything runs. `sweep`
        # re-groups with `by_size`, which appends in iteration order, so a
        # within-file reorder survives that regrouping intact.
        table = slowest_first(table, killers.seconds)
        if args.baseline_only:
            # Before the prefix is announced and before any sandbox is built: a
            # red baseline voids every row, so being able to ask *only* that
            # question, in the time one shard takes rather than one sweep, is the
            # difference between a minute and a re-run. Two full sweeps were paid
            # for here to learn what this prints -- and the second was launched
            # on a theory the first could not have confirmed.
            return 0 if _baseline_is_green(table, args) else 1
        if killers.dropped:
            print(
                paint.paint(
                    f"{killers.dropped} remembered test(s) no longer load; "
                    f"those rows run as usual.",
                    paint.ODD,
                )
            )
        if killers.head:
            spent = sum(killers.cost.get(test, 0.0) for test in killers.head)
            print(
                paint.paint(
                    f"{len(killers.head)} cheap test(s), {spent:.2f}s, run first "
                    f"where nothing is remembered.",
                    paint.QUIET,
                )
            )
        if args.json:
            # Before the first row, so a watcher started alongside this one has
            # something to read straight away. Its own pid, not a caller's guess.
            _pidfile(args.json).write_text(f"{os.getpid()}\n", encoding="utf-8")
            # Cleared before the first row, not merely written after the last.
            # A resumed sweep points `--json` at a part-written report, and a
            # marker left by the run that was interrupted would tell a watcher
            # that *this* one had finished before it began. After the `--list`
            # return on purpose: listing a table is not a run, and must not
            # retract a marker an earlier complete run earned.
            _marker(args.json).unlink(missing_ok=True)
        report = sweep(table, args) if args.all or args.batch else _run_generated(table, args)
        accepted = known_survivors()
        # Each term is a way the table can be a subset of what the record covers,
        # and `stale` is only evidence when it is not: `--all` is the whole tree
        # where `--base` is a diff, and the three filters narrow even that. A
        # `--limit` that bites is the one case this does not see; it caps per
        # file under `--all`, so it would have to be reached deliberately.
        complete = args.all and not (args.only or args.operator or args.skip_operator)
        _summarise(report.results, accepted, complete)
        sorted_out = sort_survivors(report.results, accepted, complete)
        if args.accept:
            _accept(sorted_out, accepted)
        if report.baseline_red:
            # Its verdicts are meaningless by definition, so its killers are
            # too -- and a killer recorded from a red tree is a test that fails
            # untouched, which is exactly what must never be put in front of a
            # later run. This is the supply line for the false `caught` the
            # baseline shard above guards against; both ends are closed.
            print(
                paint.paint(
                    "the baseline was red, so nothing was remembered from this run.", paint.BAD
                )
            )
        else:
            killers.learn(report)
            killers.save()
        # Last, and after `_summarise`. The numbers are what a reader looks at
        # first, so they go where the eye lands at the end of a scroll rather
        # than above a hundred and sixty survivors.
        _report_stats(report.results, pace=report.pace, red=report.baseline_red, headroom=False)
        if args.json:
            _persist(report, args.json)
            # Last: the marker means the whole run is over, and nothing after
            # this point can still change a verdict.
            _marker(args.json).touch()
            # The pid names a process that no longer exists, and a stale one is
            # exactly the false-liveness `watch.py` refuses to answer with.
            _pidfile(args.json).unlink(missing_ok=True)
        return _status(report, sorted_out)

    already = len(_RUNS)
    namespace = runpy.run_path(args.script)
    # Every table the script ran, not just the last: a spec calling `verify`
    # twice would otherwise have its exit status decided by the second, so a
    # first table full of survivors followed by a clean one exits zero. That is
    # woswoar#213's own symptom -- a run reported as the opposite of what it was --
    # reintroduced by the fix for it.
    mine = _RUNS[already:]
    ran_itself = bool(mine)
    mutations = namespace.get("MUTATIONS")

    if mutations and ran_itself:
        # Both shapes in one file. Re-running the table would cost the same
        # minutes for the same answer and print it twice with nothing to say
        # which pass a reader is looking at, so take the one that already ran.
        print(
            f"{args.script} defines MUTATIONS *and* calls verify(); the results above "
            f"are the run it did itself. Delete one of the two.",
            file=sys.stderr,
        )
    if ran_itself:
        return 0 if all(report.clean for report in mine) else 1
    if mutations:
        return _run_spec(mutations, args)
    raise SystemExit(
        f"{args.script} defines no MUTATIONS and never called verify(), so there was "
        f"nothing to run. The shape this takes is `MUTATIONS = [Mutation(...), ...]` "
        f"at module level."
    )


if __name__ == "__main__":
    # Deliberately not `main()`. `python -m tools.mutate` runs this file as
    # `__main__`, and the spec it then executes does `from tools.mutate import
    # verify` -- which imports the file a *second* time, as a different module
    # object with its own `_RUNS`. The local `main` would look at a list the
    # spec's `verify` never appended to, and report a script that had just run a
    # green table as one that defined nothing. Which is woswoar#213 again, by a
    # different route.
    from tools.mutate import main as _main

    raise SystemExit(_main())
