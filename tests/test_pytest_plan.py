"""`docs/pytest-plan.md`'s status line, asserted against the tree it describes.

**Because "continue the plan" has to be a safe instruction.** The plan is what
says where the conversion has got to, and a fresh session reads it rather than
remembering; a status line that has drifted then sends that session to the wrong
cluster. CLAUDE.md §0's whole argument applies to it -- it is read as authority,
so a sentence that stopped being true actively misleads.

It is not hypothetical. The line said "33 modules ... 25 are still `TestCase`s"
and the tree held 34 and 26, **one day after it was written**: Phase 0 counted
before `tests/test_verdict_unittest.py` existed, and nothing re-counted.

**The count asks the `unittest` loader what it takes back, and nothing else.**
Two cheaper spellings were tried and both are wrong, in the flattering direction
CLAUDE.md §8 collects:

- *grepping for `unittest.TestCase`* is wrong in both directions, measured over
  this tree: it misses `test_overlays`, `test_sync_commits`,
  `test_sync_conflicts` -- which subclass bases living in `tests/support.py` and
  never name `TestCase` themselves -- and `test_sync_properties`, whose class
  Hypothesis builds; and it counts *this* file, which names `unittest.TestCase`
  only to ask about it. 23 against grep's 20.
- *asking `issubclass` of the module's own attributes*, filtered by
  `value.__module__ == mod.__name__`, was what this file did first. It reads a
  mutable attribute, and cluster B2 then edited exactly that attribute: deleting
  `test_sync_properties.py`'s three-line dunder rewrite dropped the count by one
  with no conversion behind it. **A count that a `__module__ = __name__` can
  lower is not a count of work done.**

`loadTestsFromModule` is what `python -m unittest discover` and
`tools/verdict_unittest.py` actually run, so the number means the thing the plan
is about: modules pytest still takes through its `unittest` adapter. It is also
immune to the hazard the `__module__` filter existed for -- a module that
*imports* a base from `tests/support.py` gains no runnable test by doing so, and
those bases carry no `test_` methods to count. Measured: 159ms for all 35.

The companion claim is the one a status line cannot make: that **every** module
is accounted for. A module nobody scheduled is invisible to a count that only
compares two totals, so `test_every_module_is_scheduled_or_permanent` reads the
cluster table and insists each module is either converted, named in a cluster,
or one of the two the plan keeps.

**The plan states modules *left*, not modules converted**, and this file is why:
its first version guarded a "converted" count and failed on its own first run,
because a module born pytest-native -- this one -- raises that number without a
conversion. What is left only ever falls.
"""

from __future__ import annotations

import importlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "pytest-plan.md"

#: The modules pytest still runs through its `unittest` adapter, and why.
#: **This is not work left to do**, which is why it is named here rather than
#: left to look like arrears: `test_sync_properties` exposes a class Hypothesis
#: builds inside `hypothesis.stateful`. The plan keeps `X = Machine.TestCase` as
#: the pytest-idiomatic spelling, so that module is finished and will still be
#: unittest-backed for ever.
#:
#: **It held two entries until Phase C.** `test_verdict_unittest` was the other,
#: kept only so the retired classifier had one module it could still grade; the
#: file was deleted with its subject, so the exception went with it rather than
#: being converted. A `PERMANENT` that shrinks by a *deletion* is the one way
#: this dict can fall without a conversion behind it -- worth saying, because
#: the plan's own number falls too and reads like progress.
PERMANENT = {
    "test_sync_properties": "the class is Hypothesis's, built in `hypothesis.stateful`",
}


def modules() -> list[str]:
    """Every test module in `tests/`, by stem."""
    return sorted(path.stem for path in (ROOT / "tests").glob("test_*.py"))


def still_unittest() -> list[str]:
    """Those `unittest`'s own loader still takes runnable tests back from.

    Asked of the loader rather than read off the source or off a class
    attribute, for the two reasons in this module's docstring: both cheaper
    spellings answer a different question, and one of them answers it wrongly
    the moment a conversion touches `__module__`.
    """
    found = []
    for stem in modules():
        mod = importlib.import_module(f"tests.{stem}")
        if unittest.TestLoader().loadTestsFromModule(mod).countTestCases():
            found.append(stem)
    return found


def claimed() -> tuple[int, int]:
    """The two numbers the plan's status line states: total, and left to do.

    **Left to do, not converted**, and the difference is what this test found on
    its own first run: a module born pytest-native -- this one -- raises a
    "converted" count without any conversion having happened. Modules still run
    through the `unittest` adapter is the quantity the plan is about, and it only
    falls.
    """
    said = re.search(
        r"\*\*Of (\d+) test modules, (\d+)\s*\n?\s*still",
        PLAN.read_text(encoding="utf-8"),
    )
    assert said, "the status line no longer has the shape this test reads"
    return int(said.group(1)), int(said.group(2))


def status_line() -> str:
    """The status paragraph alone -- the sentence a reader acts on.

    Bounded by the paragraph that follows it rather than by the section heading:
    everything between them is *about* the status line rather than part of it,
    and a claim that may be made anywhere in that region is a claim that need
    not be made in the line itself.
    """
    text = PLAN.read_text(encoding="utf-8")
    start, end = text.find("Status: **Phases"), text.find("Both numbers are asserted")
    assert 0 <= start < end, "the status paragraph no longer has the shape this test reads"
    return text[start:end]


def scheduled() -> set[str]:
    """Every module the cluster table names, across all seven clusters."""
    text = PLAN.read_text(encoding="utf-8")
    table = text[text.index("| PR | modules |") : text.index("**Size:** 7 PRs")]
    found: set[str] = set()
    for line in table.splitlines():
        if line.startswith("| B"):
            found |= set(re.findall(r"`(test_\w+)`", line.split("|")[2]))
    return found


class TestTheStatusLineIsTrue:
    """The three numbers, against what the tree actually holds."""

    def test_the_shape_it_reads_is_still_there(self) -> None:
        """Stated on its own, because every assertion below rests on a regex
        over prose. A rewording that broke the match would otherwise make this
        file pass by finding nothing -- which is the failure it exists to
        prevent, one level up."""
        total, left = claimed()
        assert total > 0
        assert 0 <= left <= total, "the plan's own two numbers do not fit together"

    def test_it_names_the_right_number_of_modules(self) -> None:
        total, _ = claimed()
        assert total == len(modules()), (
            f"the plan says {total} test modules; the tree has {len(modules())}"
        )

    def test_it_names_the_right_number_still_run_as_unittest(self) -> None:
        _, left = claimed()
        still = still_unittest()
        assert left == len(still), (
            f"the plan says {left} modules still run as unittest; {len(still)} do: {still}"
        )


class TestEveryModuleIsAccountedFor:
    """The claim a pair of totals cannot make: nothing is unscheduled.

    Two totals agreeing says the counting is right, not that the *plan* covers
    the tree. A module added and never put in a cluster keeps both totals
    consistent and is converted by nobody.
    """

    def test_the_cluster_table_is_read_at_all(self) -> None:
        """The precondition. A table this could not find would satisfy the test
        below by scheduling nothing against nothing."""
        assert len(scheduled()) > 20, scheduled()

    def test_every_module_is_scheduled_or_permanent(self) -> None:
        """Converted, named in a cluster, or one of the two the plan keeps."""
        loose = [
            stem for stem in still_unittest() if stem not in scheduled() and stem not in PERMANENT
        ]
        assert loose == [], f"these modules are in no cluster and are not converted: {loose}"

    def test_the_permanent_ones_really_are_unittest_backed(self) -> None:
        """If one were ever converted, this file would be the only thing left
        claiming it was not -- and the plan's number would then be one too high
        with nothing to say so."""
        still = still_unittest()
        for stem in PERMANENT:
            assert stem in still, f"{stem} no longer runs as unittest; the plan says it does"

    def test_the_status_line_names_both_of_them(self) -> None:
        """The half a count cannot carry. A permanent exception that only this
        file knows about reads, in the plan, as a module somebody forgot.

        Scoped to the status paragraph and not to the document, which was the
        first spelling and could not fail: the paragraph *below* the status line
        names `test_sync_properties` while explaining how the guard's predicate
        was chosen, so a status line that had dropped it was still green.
        """
        said = status_line()
        for stem in PERMANENT:
            assert stem in said, f"the status line does not name {stem}"
