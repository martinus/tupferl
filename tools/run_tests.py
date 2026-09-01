"""Run the suite in parallel, and refuse to call a partial run green.

Ported from `martinus/woswoar` (Apache-2.0), where the figures quoted below were
measured. They are kept because they are the argument for the shape, and
attributed because they were not measured here.

A suite that drives real `git` spends most of its wall clock waiting on
subprocesses rather than on CPU -- measured there at about 88% for the dominant
module, 5400 git invocations for one serial run. That is latency, so it splits
across processes almost linearly: 21s serially, 6.7s on a four-core runner. This
project's suite is the same shape by construction: the plan forbids mocking git
(plan §7.1), so every sync test is subprocess-bound too.

Splitting is by *scope*, which is the node a test hangs off:
``tests/test_sync.py::TestX`` for a test inside a class, and the file itself for
one that is not. A scope is what a class- or module-scoped fixture is shared
across -- and `setUpClass` is that same sentence in the vocabulary this suite is
still written in -- so splitting one would run its fixture twice. Scopes are
then packed into batches, a few per worker rather than one process per scope --
71 interpreter starts cost about 5.6s of import CPU between them, which is most
of what parallelism had just bought.

**Taking the key from `item.parent` rather than from the class name also answers
Phase B ahead of time** (docs/pytest-plan.md). A test written as a plain
function has its module for a parent, so it packs by module with nothing here to
change -- but a *module*-scoped fixture then runs once per batch that touches
its module rather than once per run, so it has to stay cheap or idempotent.
`tests/support.py`'s cached two-machine template already is; a new one that is
not would be paid for silently.

**A scope is a packing key and never a selector**, and the difference is not a
nicety. `tests/test_sync.py::TestX` happens to select exactly the tests it holds;
`tests/test_sync.py` -- the scope a test outside any class packs under -- selects
the *whole file*, the classes in it included. So a batch is handed the ids of
its scopes rather than their names: ids partition, which is the property `pack`
assumes and the property the accounting check below is stated in. Handing over
names instead ran a class twice for any module holding a bare function beside
one, and only the duplicate check at the bottom of `main` ever saw it.

What this adds over `python -m pytest` is an accounting check. A parallel runner
has a failure mode a serial one does not: a batch that dies before it reports,
leaving a run that is green because nothing ran. So every id handed to a batch
must come back in that batch's report, by name:

    ids discovered == ids reported

Two honest limits on that. It is a closed loop -- both sides come from the same
collect, so a file renamed out of pytest's `python_files` patterns shrinks both
and stays green, exactly as it would under plain pytest. And when a batch dies
the ids come back as never-run, which is the point, but the diagnosis of *why*
is in that batch's own stderr rather than in the summary.

There are two splits, and they compose. Batches divide a suite over the
processes of one machine, which is what the paragraphs above are about.
`--shard I/N` divides it over N machines, each of which then batches its own
share. The second exists for macOS: in woswoar the dominant module was 17,651
process spawns, and each cost about four times there what it costs on Linux --
uniformly so across `git add`, `commit`, `push`, `fetch` and `clone`, which is
what says it is the spawning rather than any one operation. That one suite was
115s of the macOS job's 145s while taking 38s on Linux. Nothing about the
accounting changes -- each shard checks that the ids *it* was given all reported
-- and `pack` does both splits, so the machines are balanced by the rule that
already balanced the processes.

`pytest -n auto --dist loadscope` would cover the batching in one more dev
dependency. It is not used because it covers only the batching: nothing in it
reconciles the ids discovered against the ids reported by name (a dead worker is
*reported*, and reporting is not reconciling), there is no `--shard I/N` for
splitting one suite over N machines, and there is no `--no-skips`. That is one
dependency against three behaviours, and the third is the reason this file
exists. **What would justify re-opening it** is xdist growing the accounting
check, at which point the other two are small enough to argue about separately.

Two vocabularies meet here, and `dotted` is the only place they do. Inside, a
test is a pytest nodeid (``tests/test_sync.py::TestX::test_y``) because that is
what a collect hands back and what a worker's command line has to say. On the
command line, `--only` and `--exclude` take the dotted module-and-class spelling
they were written in before pytest, so ci.yml's six `--exclude` values and
CLAUDE.md's `--only` examples keep working verbatim.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from tools import paint, settings
from tools.cpus import usable_cpus
from tools.settings import SETTINGS

#: Through `settings` rather than computed again, so that `settings._root` really
#: is the only thing that knows where a project is -- which is what
#: `tools/README.md`'s extraction checklist claims and what a second copy here
#: made false. This feeds pytest's ``--rootdir``, discovery's default and a
#: subprocess `cwd`, so an installed harness whose root moved would otherwise
#: leave this one walking `site-packages`.
ROOT = settings.ROOT

#: What every `pytest.main` call below passes, and why each of the three is
#: there. ``-q`` because sixty-four batches of per-test lines is not a log
#: anybody reads; ``--no-header`` for the same reason, once per batch;
#: ``-p no:cacheprovider`` because the alternative is that many workers writing
#: into one `.pytest_cache` at once, in a tree the suite also copies.
#:
#: Plugin autoload is deliberately *not* disabled, which is the opposite of what
#: `tools/mutate.py` does to its probes. A sweep has to be reproducible across
#: machines; this is the developer's own suite runner, and a plugin they
#: installed is theirs to have. Nothing a plugin can do escapes the accounting
#: check, because discovery runs under the same plugins as the batches do.
#:
#: **Measured, so the decision has a price beside it**: turning autoload off
#: takes a worker from 0.20 s to 0.17 s here -- 30 ms x 128 batches, about 3.8 s
#: of CPU and 0.1-0.15 s of wall clock. Only 6 of 128 batches hold a scope that
#: wants the one plugin installed (hypothesis), so a version that passed
#: `-p hypothesispytest` to those and disabled autoload elsewhere would buy that
#: back. It is not worth the machinery at this size; the number is here so a
#: later reader is arguing with a measurement rather than with a preference.
QUIET = ("-q", "--no-header", "-p", "no:cacheprovider")

#: The statuses that mean the hooks are the whole answer. Anything else and the
#: reports cannot be believed without a second reason -- see `_unexplained`.
#:
#: The same three are `verdict.ANSWERED`, and `tools/mutate.py` keeps a third
#: copy as `_OK`/`_NONE`. Deliberately not shared, for the reason `_stated`
#: gives: `tools/verdict.py` is read as *source text* into a sandbox and may
#: import nothing from `tools`. When pytest changes this set, all three move.
ANSWERED = frozenset(
    {
        int(pytest.ExitCode.OK),
        int(pytest.ExitCode.TESTS_FAILED),
        int(pytest.ExitCode.NO_TESTS_COLLECTED),
    }
)

#: The statuses a module that would not import accounts for, and they differ by
#: caller -- which is the whole point of there being two.
#:
#: A broken module ends a run ``INTERRUPTED``. Naming a *scope inside* one ends
#: it ``USAGE_ERROR`` instead: pytest reports the import failure and then says
#: "found no collectors for tests/x.py::TestY". Only a batch can reach that,
#: because only a batch names scopes; discovery names the root and nothing else.
#:
#: **Letting discovery excuse every status was a hole, and it is measured rather
#: than imagined.** A tree holding a broken module *and* a `conftest.py` hook
#: that raises collects one item, records one unloadable module, and exits
#: ``INTERNAL_ERROR``. The broken module then excused the internal error,
#: discovery handed back a scope map truncated where the hook died, and
#: `--only` on an unrelated module filtered the one piece of red evidence away
#: -- a green run over a collect that blew up half way, which is the failure
#: this file exists to refuse, one level up from where it refuses it.
BY_A_BROKEN_MODULE = frozenset({int(pytest.ExitCode.INTERRUPTED)})
BY_ONE_A_BATCH_NAMED = BY_A_BROKEN_MODULE | {int(pytest.ExitCode.USAGE_ERROR)}


def dotted(node: str) -> str:
    """A nodeid spelled the way `--only` and `--exclude` spell it.

    ``tests/test_sync.py::TestX`` is ``tests.test_sync.TestX``. The *scope* is
    translated rather than the pattern, which is what keeps `selects` anchored
    at a dot: a pattern turned into a nodeid prefix would have to re-derive that
    anchoring against two separators, ``/`` and ``::``, and get both right.

    A name with no ``.py`` in it -- a directory, or a nodeid this tool did not
    build -- comes back with its slashes turned into dots and nothing else,
    which is what someone naming one would have typed.

    `tools/mutate.py`'s `_dotted` is this same translation with
    `mutants.module_of` in place of the `replace`, which additionally collapses
    a package's ``__init__``. That case cannot arise here -- ``__init__.py``
    matches none of pytest's `python_files` patterns, so no scope is ever one --
    and the two are kept apart rather than shared because importing `mutants`
    into the runner would put it in every worker for one line.
    """
    path, _, rest = node.partition("::")
    return ".".join(
        [path.removesuffix(".py").replace("/", "."), *(p for p in rest.split("::") if p)]
    )


def _named(code: int) -> str:
    """An exit status as pytest's own name for it, with the number kept.

    The number alone sends a reader to pytest's source; the name alone is not
    what a shell reports. A status pytest does not define is possible -- a
    plugin may return its own -- and says so rather than raising here.

    `verdict._named` is the same four lines, not shared for the reason `_stated`
    gives, and the two render deliberately differently: this one keeps the
    number because it goes into a CI annotation that a reader meets without
    pytest's source to hand, where verdict's goes into a `broke` row of a table
    that is already wide. Neither is authoritative over the other; they are two
    audiences. Said here because the pair is otherwise indistinguishable from an
    oversight -- and `tools/cpus.py` records what that costs when nobody says
    it: two copies of one number, diverged, neither chosen against the other.
    """
    try:
        return f"{pytest.ExitCode(code).name} ({code})"
    except ValueError:  # pragma: no cover - no plugin here returns one
        # survivor: return-value -- unreachable: every status this file reads comes back from
        #   `pytest.main`, which returns a member of its own `ExitCode`. The arm is reached only
        #   by a plugin returning a number of its own, and nothing here loads one.
        return f"status {code}"


def _stated(longrepr: object) -> str:
    """The one line of a rendered collection failure worth putting in a summary.

    A collection failure renders as a small traceback whose last line is the
    exception, prefixed ``E   `` the way it is printed. The *first* line is the
    file and line number, which is what a reader would reach for and is the less
    useful half -- "test_x.py:1: in <module>" says nothing about what went
    wrong. Under `unittest` this line read ``Failed to import test module:
    tests.test_x``, which named the module twice and the cause not at all.

    The same four lines are in `tools/verdict.py`, deliberately not shared: that
    module is read as *source text* into a sandbox and may import nothing from
    `tools`, and a `run_tests` that imported it would put every `verdict.py`
    mutation into this file's blast radius as well.
    """
    spoken = [line for line in str(longrepr).splitlines() if line.strip()]
    if not spoken:
        return "collection failed and said nothing"
    # The marker is stripped only when it really is one -- `removeprefix("E")`
    # turns a line beginning "Errno" into one beginning "rrno", quietly, in the
    # one message a reader has to act on.
    head, _, rest = spoken[-1].partition(" ")
    return (rest if head == "E" else spoken[-1]).strip()


def _why(longrepr: object) -> str:
    """The reason a test was skipped, without the marker pytest prints it with.

    A skip arrives as the three-tuple ``(file, line, "Skipped: age is not
    installed")``. The prefix is cut so the summary reads as it did under
    `unittest`, whose `result.skipped` carried the bare reason -- and
    `--no-skips` exists to make exactly that reason loud, so a prefix glued onto
    the front of every one of them is noise on the line a reader acts on.
    """
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ")
    # Nothing pytest produces reaches this line, and it is tested anyway --
    # driven directly, because a defensive branch nothing exercises is one an
    # edit reverts silently, and the sweep reports all three of its mutations.
    return str(longrepr)


def _unexplained(code: int, unloadable: dict[str, str], excused: frozenset[int]) -> str:
    """Why a finished `pytest.main` cannot be believed, or ``""`` if it can.

    **Statuses 3 and 4 fire no hook at all**, so the status is the only evidence
    they happened: a selection naming a module the tree no longer has is a usage
    error, and read through the hooks alone it is indistinguishable from a batch
    that legitimately held no tests. That is the shape CLAUDE.md §8 is about, so
    it is checked rather than inferred.

    `excused` is the set of statuses an entry in `unloadable` accounts for, and
    **the caller chooses it** -- `BY_A_BROKEN_MODULE` for discovery,
    `BY_ONE_A_BATCH_NAMED` for a batch, which additionally reaches
    ``USAGE_ERROR`` because it names scopes. Both constants carry the
    measurement, and the reason this is not one wider set is the hole recorded
    at the second of them: a broken module must not excuse a collect that blew
    up for an unrelated reason.

    ``NO_TESTS_COLLECTED`` is not an error here. The parent refuses an empty
    selection long before this, with a message that says which filter emptied
    it; a *batch* that collects nothing has already failed the accounting check
    by name, which is the better diagnosis of the two.
    """
    if code in ANSWERED:
        # survivor: return-value -- equivalent: both callers spell this
        #   `if why := _unexplained(...)`, and `None` is exactly as falsy as `""`. The empty
        #   string is the honest value for something documented as returning a reason, not a
        #   distinction either caller can see.
        return ""
    if unloadable and code in excused:
        # survivor: return-value -- equivalent, for the reason on the arm above.
        return ""
    return f"pytest exited {_named(code)}"


def _settled(ids: list[str]) -> list[str]:
    """`ids` without repeats, in the order they first arrived.

    A test using `subTest` reports once per failing subcase and every one of
    them carries the owning test's nodeid, so a class with a twenty-case loop in
    it would otherwise be named twenty times in the summary.

    **`ran` is deliberately not put through this.** Its repeats are what
    `main`'s duplicate check exists to see, and deduplicating them here would
    silence the guard in the file it guards.
    """
    return list(dict.fromkeys(ids))


def _note_unloadable(store: dict[str, str], report: pytest.CollectReport) -> bool:
    """Record a failed collect, if that is what this is, and say whether it was.

    **The key is the wire protocol**, which is why it is written once. `run_batch`
    writes this dict to JSON, `main` reads it back and prints
    ``could not import {name}``, and `--only` matches on it -- so two spellings
    would mean a batch report the parent mis-keys, with one broken module named
    two ways in `main`'s single `unloadable`. Both plugins below need exactly
    this, and `_Collector` needs one thing more, which is what the return value
    is for.

    Dotted rather than a nodeid because it is a name a person types after
    `--only`, and because "could not import tests.test_x" is the sentence.
    """
    if report.outcome != "failed":
        return False
    store[dotted(report.nodeid) or "collection"] = _stated(report.longrepr)
    return True


class Found(NamedTuple):
    """What discovery says: the scopes to run, and the modules that would not
    import at all."""

    scopes: dict[str, list[str]]
    #: Module name, dotted -- so `--only` and `--exclude` reach these too --
    #: mapped to the one line of the failure that says what went wrong.
    unloadable: dict[str, str]


class Refused(Exception):
    """Discovery stopped for a reason that is not a module failing to import.

    Carried as an exception rather than as another entry in `unloadable`,
    because the line `main` prints for that dict is "could not import <name>"
    and this is not a name anyone can import.

    A class of its own rather than the `ValueError` `shard_of` raises one screen
    down, which is otherwise the same "raise with the text the caller should
    print" shape: `except ValueError` around `discover()` would also swallow a
    `ValueError` escaping a plugin's collect, and swallowing an unexplained
    collect is the one thing this exception exists to prevent.
    """


class _Collector:
    """One `--collect-only` pass, remembered by scope.

    The scope key comes from `item.parent.nodeid` rather than from the item's
    own id with the last segment cut off: pytest already knows what a test
    hangs off, and a string cut would have to know that a parametrized id ends
    in ``[...]`` and that a plain function's parent is its module.
    """

    def __init__(self) -> None:
        self.scopes: dict[str, list[str]] = {}
        self.unloadable: dict[str, str] = {}
        #: The full rendering of each failure, for stderr. Kept beside the
        #: one-line form rather than derived from it, because the summary wants
        #: the cause on one line and a human wants the traceback.
        self.tracebacks: list[str] = []

    def pytest_itemcollected(self, item: pytest.Item) -> None:
        parent = item.parent.nodeid if item.parent is not None else item.nodeid
        self.scopes.setdefault(parent, []).append(item.nodeid)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """A module that will not import, reported before anything of it runs.

        The parameter must be named ``report``: pluggy validates hookimpl
        argument *names* against the hook specification and raises
        `PluginValidationError` at registration for anything else.
        """
        if _note_unloadable(self.unloadable, report):
            self.tracebacks.append(f"{report.nodeid}\n{report.longrepr}")


def discover(root: Path = ROOT) -> Found:
    """Map each scope to the ids it holds, in collection order.

    A module that will not import is *not* one of them, and telling those two
    apart is the whole of woswoar#221: counted as a test that failed, it sends a
    reader looking for an assertion, and counted as a test that ran it satisfies
    the accounting check. pytest keeps them apart for free -- a failed collect
    produces a `CollectReport` and no items at all, where `unittest` substituted
    a synthetic `_FailedTest` that **is** a `TestCase` and had to be filtered
    back out by name.

    Each failure's own traceback goes to stderr, where a human reads it in
    context and where every other traceback from this tool goes -- under
    `unittest` it existed only in `loader.errors` and the summary carried its
    first line, which named the module and not the cause.

    **The captured collect output is dumped only when the run cannot be
    explained**, and that restriction is the whole reason the tracebacks are
    kept separately. ``--collect-only -q`` prints one line per collected test:
    dumping the buffer for an ordinary broken module put 148 KB and 1598 node
    lines on stderr around one four-line traceback. When the *status* is
    unexplained there is nothing better to offer, so it goes out then.

    `root` is a parameter so this can be pointed at a throwaway tree. It has one
    caller in anger and one in the tests, and the alternative was writing a
    deliberately broken module into `tests/` while the suite that finds it is
    running in 64 parallel workers.
    """
    found = _Collector()
    said = io.StringIO()
    with contextlib.redirect_stdout(said):
        code = int(
            pytest.main(
                ["--collect-only", *QUIET, "--rootdir", str(root), str(root)], plugins=[found]
            )
        )
    if why := _unexplained(code, found.unloadable, BY_A_BROKEN_MODULE):
        print(said.getvalue(), file=sys.stderr)
        raise Refused(f"{why} while collecting {root}")
    for traceback in found.tracebacks:
        print(traceback, file=sys.stderr)
    return Found(found.scopes, found.unloadable)


def selects(name: str, only: str) -> bool:
    """Does `only` select the scope `name`?

    Both in the dotted spelling -- see `dotted`, which is what puts a scope into
    it. Anchored at a dot rather than matching any substring: a job that pins a
    fatal-skip policy with `--only tests.test_sync` must not drag a later
    `tests/test_sync_chunks.py` under that policy without anyone choosing it.
    """
    return name == only or name.startswith(only + ".")


def pack(scopes: dict[str, list[str]], bins: int) -> list[list[str]]:
    """Spread scopes over batches, largest first onto the lightest batch.

    Test count is a rough proxy for duration -- measured, packing by true
    duration would save about 5% -- but it is free, and it keeps a fat scope
    from being scheduled last, which is what actually sets the wall clock.

    Never more batches than scopes: an empty batch would start an interpreter
    to run nothing. That clamp is also why no filter is needed afterwards --
    with at most one batch per scope, `min(weights)` always finds a batch that
    is still empty, so none is left behind.

    **With no scopes there are no batches**, which is the only answer any
    caller wants. It used to floor at one, and `main` then had to undo that: a
    worker spawned with no names, refused by argparse, and reported as
    `::error::batch died without reporting` above the import failure that was
    the actual answer. Returning `[]` is not a refusal -- whether a run with
    nothing selected is an error stays `main`'s question, and it answers it
    before getting here -- it is just the right count of batches for no work.
    """
    batches: list[list[str]] = [[] for _ in range(min(bins, len(scopes)))]
    # survivor: off-by-one -- equivalent: `weights.index(min(weights))` picks the lightest bin, and
    #   adding the same constant to every bin leaves that comparison exactly where it was. The zero
    #   is the natural spelling of "nothing packed yet", not an arithmetic the packing depends on.
    weights = [0] * len(batches)
    for name in sorted(scopes, key=lambda n: (-len(scopes[n]), n)):
        light = weights.index(min(weights))
        batches[light].append(name)
        weights[light] += len(scopes[name])
    return batches


def shard_of(spec: str) -> tuple[int, int]:
    """Parse ``I/N`` into a zero-based index and a count.

    Raises `ValueError` with the text a caller should print. Separate from
    `main` so the parsing has a test that does not need a suite to run.
    """
    index, sep, total = spec.partition("/")
    if not sep or not index.strip().isdigit() or not total.strip().isdigit():
        raise ValueError(f"--shard wants I/N, got {spec!r}")
    got, count = int(index), int(total)
    # survivor: off-by-one -- equivalent because the second term already covers it: with `count` at
    #   0, `1 <= got <= 0` is false for every `got`, so `not ...` raises regardless of what the
    #   first term says. `count < 1` is the sentence a reader wants -- "N at least 1" -- rather than
    #   a check the range test needs.
    if count < 1 or not 1 <= got <= count:
        raise ValueError(f"--shard {spec} is out of range: I must be 1..N and N at least 1")
    return got - 1, count


class _Recorder:
    """What one batch's pytest run did, in the five keys the parent reads.

    `verdict.Watcher` answers pytest's reports at `pytest_runtest_makereport`
    and this answers them at `pytest_runtest_logreport`, which looks like a
    disagreement and is not. That one needs `call.excinfo` to tell a memory cap
    or an alarm from a test saying no, and `excinfo` exists only at
    `makereport`; this one needs ids and a phase, both of which a report
    carries. The `context` filter differs for the same reason -- see
    `pytest_runtest_logreport` below, where filtering on it would lose a
    subcase-only failure entirely.
    """

    def __init__(self) -> None:
        self.ran: list[str] = []
        self.failures: list[str] = []
        self.errors: list[str] = []
        #: Keyed by nodeid so a `subTest` loop that skips twenty times says so
        #: once. The first reason wins, which is the one nearest the cause.
        self.skipped: dict[str, str] = {}
        self.unloadable: dict[str, str] = {}

    def pytest_runtest_logstart(self, nodeid: str, location: object) -> None:
        """One entry per test that *started*, which is what the accounting
        check subtracts from.

        Counted here rather than from the reports, and this is the half that
        moved: a class whose `setUpClass` raises produces no ``call`` report at
        all, yet pytest starts each of its tests and files a ``setup`` error
        against each real nodeid. Under `unittest` the same failure produced one
        synthetic `setUpClass (module.Class)` id and left every test in the
        class to surface as "never ran" under a name the parent could not act
        on.
        """
        self.ran.append(nodeid)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Sort one report into the bucket it belongs in.

        **A failed `subTest` arrives here as a `SubtestReport` and nowhere
        else** -- the owning test's own ``call`` report reads ``passed``, which
        pyproject.toml records as the pytest 9 behaviour the version floor is
        there for. So the failed arm must *not* filter on ``context``: doing so
        makes a suite whose only failure is a subcase report itself green, with
        pytest's own exit status the only dissenting evidence. `_settled` is
        what keeps the twenty cases of one loop from being named twenty times.

        The failure/error split is by *phase*, where `unittest` split it by
        exception type -- `AssertionError` against everything else. The phase is
        the more useful line: `main` prints them under different labels, and
        what a reader needs to know is whether to look at the test or at what
        set it up. A `RuntimeError` raised in a test body is the test saying no
        just as much as a failed assertion is.

        xfail and xpass are neither: a report carrying ``wasxfail`` is dropped
        here, so an expected failure counts as no pass and no skip. A *strict*
        xpass is not one of those -- pytest files it as an ordinary ``call``
        failure, which is what it is. Nothing in this suite uses either yet;
        the rule is written down so the first use does not decide it silently.
        """
        if hasattr(report, "wasxfail"):
            return
        if report.failed:
            (self.failures if report.when == "call" else self.errors).append(report.nodeid)
        elif report.skipped:
            self.skipped.setdefault(report.nodeid, _why(report.longrepr))

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        """The belt to discovery's brace: a module can import in the parent and
        not here -- a half-written file caught mid-save, an import with a side
        effect that depends on which worker got there first -- and the batch
        must still run the scopes that did load, which pytest does by itself.
        """
        _note_unloadable(self.unloadable, report)


def run_batch(names: list[str], out: Path) -> int:
    """Run the tests named on the command line, and write what happened as JSON.

    Ids rather than the scopes they were packed under -- see the dispatch in
    `main`, where a scope name turned out not to be a selector.

    The file *is* the interface: only ids travel back to the parent, because
    shipping the tracebacks too would print every failure twice. They go to this
    process's stderr instead, where they interleave with the tests' own output
    and a human reads them in context.

    **Everything pytest prints is moved to stderr**, which is where all of it
    went when a `TextTestRunner` wrote it. The parent's stdout is the summary,
    and a hundred-odd batches' progress interleaved into it would bury the
    four lines a reader is actually looking for. `redirect_stdout` rather than
    `os.dup2(2, 1)`: pytest builds its terminal writer from `sys.stdout` inside
    `pytest.main`, so swapping the object is enough -- measured at 0 bytes on
    stdout against 1980 on stderr -- and it leaves this function callable
    in-process without wrecking its caller's streams.
    """
    heard = _Recorder()
    with contextlib.redirect_stdout(sys.stderr):
        code = int(pytest.main([*names, *QUIET, "--rootdir", str(ROOT)], plugins=[heard]))
    if why := _unexplained(code, heard.unloadable, BY_ONE_A_BATCH_NAMED):
        # No report at all, rather than one nobody can stand behind. The
        # parent's "batch died without reporting" path is exactly right for
        # this -- it puts every id in the batch back as never-run -- and it is
        # already the path a segfault takes, so there is no second one to test.
        print(f"::error::{why} over {' '.join(names)}", file=sys.stderr)
        return 1
    out.write_text(
        json.dumps(
            {
                "ran": heard.ran,
                "failures": _settled(heard.failures),
                "errors": _settled(heard.errors),
                # The reason, unlike a traceback, is not printed by the child at
                # this verbosity, so it would otherwise be lost.
                "skipped": [[tid, why] for tid, why in heard.skipped.items()],
                # A new key rather than a new meaning for `errors`: the parent
                # reads this file as a protocol, and an id that is a module
                # rather than a test would be counted as a test that failed.
                "unloadable": heard.unloadable,
            }
        ),
        encoding="utf-8",
    )
    return 1 if heard.failures or heard.errors or heard.unloadable else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=0, help="batches at once (default: 2x CPUs)")
    parser.add_argument(
        "--no-skips",
        action="store_true",
        help="treat a skipped test as a failure, for jobs that install every optional tool",
    )
    parser.add_argument("--only", default="", help="run only this module or class")
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="MODULE_OR_CLASS",
        help="skip this module or class entirely; repeatable, and each pattern must "
        "match. For a suite that cannot run somewhere, so the rest keeps --no-skips",
    )
    parser.add_argument(
        "--shard",
        default="",
        metavar="I/N",
        help="run only shard I of N, for splitting one suite over N machines. "
        "The batches below split a suite over the processes of one machine; this "
        "splits it over the machines, and the two compose",
    )
    parser.add_argument("--worker", nargs="+", help=argparse.SUPPRESS)
    parser.add_argument("--out", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    # survivor: branch -- tools/run_tests.py:298 in main() -- the `if` is never taken --
    #   unmeasurable by construction: forcing this branch off makes every spawned worker ignore
    #   `--worker`, run the whole suite itself and spawn more -- an unbounded fork storm. `_Lanes`
    #   killed the session at 2199 MiB against its 2063 MiB share and took the rest of the table
    #   with it, so the row can never come back anything but BROKE.
    if args.worker:
        assert args.out
        return run_batch(args.worker, Path(args.out))

    try:
        scopes, unloadable = discover()
    except Refused as stopped:
        print(f"::error::{stopped}")
        return 1
    # The filters below speak the dotted spelling and `scopes` is keyed by
    # nodeid, so each comparison translates. `unloadable` is *already* dotted --
    # `_note_unloadable` keys it that way, because its keys are what a person
    # types after `--only` and what `main` prints -- which is why the two lines
    # below look inconsistent and are not.
    if args.only:
        scopes = {name: ids for name, ids in scopes.items() if selects(dotted(name), args.only)}
        # The filter reaches the broken modules too, or `--only tests.test_x` on
        # a module that will not import answers "no test scope matches" -- true,
        # unhelpful, and the opposite of what someone running that command is
        # trying to find out.
        unloadable = {name: why for name, why in unloadable.items() if selects(name, args.only)}
        if not scopes and not unloadable:
            print(f"::error::no test scope matches {args.only!r}")
            return 1
    # One pattern at a time, and each has to match something. Checking only
    # that the *set* shrank would let a renamed class stop being excluded as
    # long as any other pattern still matched -- and the job that needs this
    # passes two, so that is the likely case rather than the exotic one. What it
    # would cost is a red build on the platform the tool is missing from, which
    # is precisely what naming the class was meant to avoid.
    for pattern in args.exclude:
        kept = {name: ids for name, ids in scopes.items() if not selects(dotted(name), pattern)}
        if len(kept) == len(scopes):
            print(f"::error::no test scope matches --exclude {pattern!r}")
            return 1
        scopes = kept
    if not scopes and not unloadable:
        # **Every filter above can end here, and only `--only` said so.** An
        # `--exclude` list that removes the last scope is not a typo -- each
        # pattern matched something, which is all that loop checks -- and the
        # run then packs one *empty* batch, spawns a worker with no names,
        # watches argparse refuse it, prints `::error::batch died without
        # reporting` and **exits 0**: the error is annotated in the log and the
        # job is green. Found by a test written for the refusals above.
        #
        # `unloadable` is what keeps this from refusing the case that matters
        # most: a selection that matches only a module which will not import
        # has no scopes to run and a real answer to give.
        #
        # **Before the `--shard` block, not after.** Placed after it,
        # `count > len(scopes)` fires first on an already-empty selection
        # and reports `--shard 1/2 wants more shards than there are
        # test scopes` -- true, and it names the matrix when the exclude list
        # is what emptied it. The whole argument for hoisting this check
        # out of the individual filters is that the answer should not
        # depend on which one ran; after the shard block it still did, for
        # that one combination. The shard check then only ever sees
        # `0 < len(scopes) < count`, which is the case it was written for.
        print("::error::every test scope was filtered out; nothing would run")
        return 1
    if args.shard:
        try:
            index, count = shard_of(args.shard)
        except ValueError as exc:
            print(f"::error::{exc}")
            return 1
        # `pack` is reused rather than a slice of the sorted names, so the
        # machines get comparable amounts of work for the same reason the
        # processes do -- and so that one balancing rule is tested once.
        #
        # More shards than scopes is fatal rather than an empty green run. An
        # empty shard reports "Ran 0 tests" and exits 0, which is precisely the
        # partial run that is green because nothing happened -- the thing this
        # script exists to refuse. It can only come from a matrix that outgrew
        # the suite, so it is the CI config that is wrong, and silence there
        # would spread to every shard as scopes were removed.
        if count > len(scopes):
            print(f"::error::--shard {args.shard} wants more shards than there are test scopes")
            return 1
        chosen = pack(scopes, count)[index]
        scopes = {name: scopes[name] for name in chosen}
    expected = {tid for ids in scopes.values() for tid in ids}

    # Twice the CPUs, because the work is subprocess wait rather than CPU:
    # measured on four cores, jobs=8 beats jobs=4 by ~9%, and jobs=16 regresses.
    # `tools/cpus.py` says why the count is not `os.cpu_count()`, and why the
    # doubling stays here rather than moving in with it.
    jobs = args.jobs or usable_cpus() * 2
    # More batches than workers, so a batch that runs long is overlapped by the
    # others rather than deciding the wall clock on its own.
    #
    # A selection matching nothing *but* a module which will not import reaches
    # here with no scopes, deliberately -- it has a real answer to give. `pack`
    # returns no batches for it, so no worker is spawned with no names for
    # argparse to refuse and report as `::error::batch died without reporting`
    # above the import failure that is the actual answer. That annotation about
    # a crash that never happened was measured on both runners and long
    # predates the port.
    batches = pack(scopes, jobs * 2)

    with tempfile.TemporaryDirectory(prefix=SETTINGS.tmp("batches-")) as tmp:

        def run(indexed: tuple[int, list[str]]) -> dict[str, Any]:
            index, names = indexed
            out = Path(tmp) / f"{index}.json"
            # **The ids, not the scope names, and that distinction is the whole
            # of a bug this had.** A scope name is a *packing* key; it is not a
            # selector. `tests/test_x.py::TestY` happens to select exactly its
            # own tests, but `tests/test_x.py` -- the scope a test outside any
            # class packs under -- selects the whole file, classes included. So
            # a module holding a bare function *and* a class dispatched that
            # class's tests to two batches and ran them twice. Measured: green
            # for a file of only functions, green for a file of only classes,
            # `::error::1 tests ran more than once` for one file with both.
            #
            # Ids partition by construction, which is what `pack` assumes and
            # what the accounting check is stated in. The argv cost is nothing:
            # 1607 ids over 128 batches is ~13 each, and the largest single
            # scope in this tree is 20 -- about 1.2 KB against a 2 MiB bound.
            chosen = [tid for name in names for tid in scopes[name]]
            proc = subprocess.run(
                [sys.executable, "-m", "tools.run_tests", "--worker", *chosen, "--out", str(out)],
                cwd=ROOT,
            )
            if not out.exists():
                # Killed by the OOM killer, a segfault in a C extension, a crash
                # in a class fixture, a pytest that could not be believed: no
                # report, so nothing it would have said can be believed either.
                # Its ids come out below as never-run.
                print(f"::error::batch died without reporting, exit {proc.returncode}: {names}")
                return {
                    "ran": [],
                    "failures": [],
                    "errors": list(names),
                    "skipped": [],
                    "unloadable": {},
                }
            loaded: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
            return loaded

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            reports = list(pool.map(run, enumerate(batches)))

    for report in reports:
        # survivor: drop-call -- tools/run_tests.py:399 in main() -- the call to
        #   `unloadable.update(...)` never happens -- measured unreachable: `discover` reports a
        #   module it could not import and produces no items for it, so no batch is ever asked to
        #   load one and no batch report carries an `unloadable` entry. Verified by driving a real
        #   tree with a broken module -- exactly one `could not import` line is printed, from the
        #   parent's own collect. The line guards a `discover` that stopped doing that.
        unloadable.update(report["unloadable"])
    ran = [tid for report in reports for tid in report["ran"]]
    failures = [tid for report in reports for tid in report["failures"]]
    errors = [tid for report in reports for tid in report["errors"]]
    skipped = [pair for report in reports for pair in report["skipped"]]

    where = f" (shard {args.shard})" if args.shard else ""
    ran_line = f"\nRan {len(ran)} tests in {len(batches)} batches on {jobs} workers{where}"
    print(paint.paint(ran_line, paint.HEAD))
    for label, ids in (("FAIL", failures), ("ERROR", errors)):
        for tid in ids:
            # the traceback is already above, from the batch
            print(paint.paint(f"  {label}: {tid}", paint.BAD))

    # survivor: order -- tools/run_tests.py:413 in main() -- `sorted` becomes `list` -- equivalent
    #   for what `discover` produces: `unloadable` is filled by a collect that visits modules in
    #   sorted order, so its insertion order already is the sorted order. The *reversal* of this
    #   line is caught by `test_broken_modules_are_named_in_a_settled_order`; only the redundancy
    #   is not.
    for name in sorted(unloadable):
        # Its own line and its own wording: this is not a test that failed, and
        # printing it as one is what sent a reader looking for an assertion.
        print(f"::error::could not import {name}: {unloadable[name]}")
    ok = not (failures or errors or unloadable)
    if missing := expected - set(ran):
        # The accounting check this script exists for.
        print(f"::error::{len(missing)} discovered tests never ran")
        for tid in sorted(missing):
            print(paint.paint(f"  never ran: {tid}", paint.BAD))
        ok = False
    # survivor: branch -- the `if` is never taken -- unreachable, because a batch is handed the
    #   *ids* of its scopes and each id belongs to exactly one scope, which `pack` places in
    #   exactly one batch. **It was reachable once**, and this guard is the only thing that saw
    #   it: handing a worker the scope *name* made `tests/test_x.py` select the classes in that
    #   file as well, so a module with a bare function beside a class ran the class twice. Not a
    #   future batching rule, then -- a past one.
    if duplicated := len(ran) - len(set(ran)):
        # survivor: drop-call -- tools/run_tests.py:425 in main() -- the call to `print(...)` never
        #   happens -- same as `run_tests.py:424` -- the line only runs when a test id was reported
        #   twice, which `pack` makes impossible today.
        print(f"::error::{duplicated} tests ran more than once")
        ok = False
    if skipped:
        for tid, why in skipped:
            print(paint.paint(f"  skipped: {tid} ({why})", paint.ODD))
        if args.no_skips:
            print(f"::error::{len(skipped)} tests were skipped - an optional tool is missing")
            ok = False

    # The last line, and the one a reader looks at first. `::error::` lines are
    # deliberately left plain above: GitHub Actions parses that prefix into an
    # annotation, and wrapping one in escape codes is a guess about a parser
    # nothing here can test.
    print(
        paint.paint(
            f"{'OK' if ok else 'FAILED'} ({len(failures)} failures, {len(errors)} errors, "
            f"{len(skipped)} skipped)",
            paint.HEAD + (paint.GOOD if ok else paint.BAD),
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
