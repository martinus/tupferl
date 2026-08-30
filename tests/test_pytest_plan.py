"""`docs/pytest-plan.md`'s status line, asserted against the tree it describes.

**Because "continue the plan" has to be a safe instruction.** The plan is what
says where the conversion has got to, and a fresh session reads it rather than
remembering; a status line that has drifted then sends that session to the wrong
cluster. CLAUDE.md §0's whole argument applies to it -- it is read as authority,
so a sentence that stopped being true actively misleads.

It is not hypothetical. The line said "33 modules ... 25 are still `TestCase`s"
and the tree held 34 and 26, **one day after it was written**: Phase 0 counted
before `tests/test_verdict_unittest.py` existed, and nothing re-counted.

**The count asks `unittest`, never the text.** Grepping for `unittest.TestCase`
is the obvious spelling and it is wrong here by four modules: `test_overlays`,
`test_sync_commits`, `test_sync_conflicts` and `test_sync_properties` subclass
bases that live in `tests/support.py` and never name `TestCase` themselves, so a
grep reports them converted when they are not -- in the flattering direction,
which is the one CLAUDE.md §8 collects. Importing and asking `issubclass` is
exact, and costs 176ms for all 34 modules.

The companion claim is the one a status line cannot make: that **every** module
is accounted for. A module nobody scheduled is invisible to a count that only
compares two totals, so `test_every_module_is_scheduled_or_excused` reads the
cluster table and insists each module is either converted, named in a future
cluster, or the one the plan excludes on purpose.

**The plan states modules *left*, not modules converted**, and this file is why:
its first version guarded a "converted" count and failed on its own first run,
because a module born pytest-native -- this one -- raises that number without a
conversion. What is left only ever falls, and is what the plan is about.
"""

from __future__ import annotations

import importlib
import inspect
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "docs" / "pytest-plan.md"

#: The one module Phase B never converts. It dies with its subject in Phase C,
#: and `tools/verdict_unittest.py` needs a `unittest`-style module to grade
#: while it lives -- `tests/test_mutate.py`'s `EITHER_LAYER` row points at it.
EXCUSED = "test_verdict_unittest"


def modules() -> list[str]:
    """Every test module in `tests/`, by stem."""
    return sorted(path.stem for path in (ROOT / "tests").glob("test_*.py"))


def still_unittest() -> list[str]:
    """Those that still define a `unittest.TestCase`, asked of `unittest`.

    `v.__module__ == mod.__name__` because a module that imports a base class
    from `tests/support.py` would otherwise be reported as defining it -- the
    import is not the definition, and half this suite imports `support`.
    """
    found = []
    for stem in modules():
        mod = importlib.import_module(f"tests.{stem}")
        if any(
            inspect.isclass(value)
            and issubclass(value, unittest.TestCase)
            and value.__module__ == mod.__name__
            for value in vars(mod).values()
        ):
            found.append(stem)
    return found


def claimed() -> tuple[int, int]:
    """The two numbers the plan's status line states: total, and left to do.

    **Left to do, not converted**, and the difference is what this test found on
    its own first run: a module born pytest-native -- this one -- raises a
    "converted" count without any conversion having happened. Modules still
    holding a `TestCase` is the quantity the plan is about, and it only falls.
    """
    said = re.search(
        r"\*\*Of (\d+) test modules, (\d+)\s*\n?\s*still hold",
        PLAN.read_text(encoding="utf-8"),
    )
    assert said, "the status line no longer has the shape this test reads"
    return int(said.group(1)), int(said.group(2))


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

    def test_it_names_the_right_number_left_to_convert(self) -> None:
        _, left = claimed()
        still = still_unittest()
        assert left == len(still), (
            f"the plan says {left} modules still hold TestCases; {len(still)} do: {still}"
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

    def test_every_module_is_scheduled_or_excused(self) -> None:
        """Converted, named in a cluster, or the one module the plan keeps."""
        loose = [stem for stem in still_unittest() if stem not in scheduled() and stem != EXCUSED]
        assert loose == [], f"these modules are in no cluster and are not converted: {loose}"

    def test_the_excused_module_is_the_one_the_plan_says(self) -> None:
        """It is excused here and argued for in the plan's B6 row, so the two
        must not drift apart -- and if it were ever converted, this file would
        be the only thing still claiming it was not."""
        assert EXCUSED in still_unittest(), f"{EXCUSED} was converted; the plan says it is not"
        assert EXCUSED not in scheduled(), f"{EXCUSED} is in a cluster and also excused"
