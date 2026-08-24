"""Plan §7.2 properties 1 and 2, written before the sync engine's example tests.

Property 1 -- *one-sided change wins* -- is `merge(base, a, base) == a` and
`merge(base, base, b) == b`. The sync engine never calls the merge for a
one-sided change (it copies, which is cheaper and cannot introduce a marker), so
this is a property of the primitive rather than of a code path a user reaches.
It is worth having anyway: it is the assumption the *engine's* short-circuit
rests on, and if it stopped holding the engine would be quietly wrong in the
direction nobody checks.

Property 2 -- *a non-overlapping merge keeps every changed line from both sides
and adds no other* -- is asserted here as exact equality against an expectation
built line by line, which is stronger. "Contains every changed line" is satisfied
by an output that also contains three copies of it and a conflict marker.

**The fixture's shape is the part to read.** Both sides edit the middle line of
three-line regions, and the regions are disjoint, so every pair of changed lines
has at least two unchanged lines between it -- `git merge-file` merges hunks that
touch, and adjacent single-line changes are one hunk and therefore a conflict.
Each region also carries its own index in every line, so no two regions are
textually identical: with identical regions git's diff may attribute a change to
the wrong one, and two changes meant for different places land on top of each
other. Both of those produce a *conflict* rather than a wrong answer, so without
this the test would fail intermittently and read as a bug in the merge.
"""

from __future__ import annotations

import unittest

from hypothesis import assume, given
from hypothesis import strategies as st

from tests import profiles  # noqa: F401  -- registers and loads the profile
from tupferl import merge

#: One line of a file, without the two bytes that would make the fixture mean
#: something else.
#:
#: `\n` and `\r`, because newlines are added by the fixture: a generated one
#: would silently change how many lines a region has, and the whole construction
#: rests on that count.
#:
#: `\x00`, because a file containing one is not text and has no 3-way merge --
#: `merge.is_text` reports the whole file as one conflict, which is the honest
#: answer and not what properties 1 and 2 are about. Excluded here rather than
#: `assume`d away so the reason is stated once; the excluded case is covered by
#: `test_merge.TestBinaryFilesHaveNoMerge`, which is what stops this exclusion
#: from being a hole. Hypothesis found it on this file's first run.
TEXT = st.text(
    alphabet=st.characters(blacklist_characters="\n\r\x00", blacklist_categories=("Cs",)),
    max_size=12,
)

#: Which side edits a region, or neither. Weighted towards editing, because a
#: run where both sides changed nothing tests that the merge returns the base
#: and nothing else.
SIDES = st.sampled_from(["ours", "theirs", "neither"])


def region(index: int, middle: str) -> list[str]:
    """Three lines, the middle one editable, all three naming their region.

    The index in every line is what keeps regions textually distinct; see the
    module docstring for what happens without it.
    """
    return [f"{index}: top", f"{index}: {middle}", f"{index}: bottom"]


def joined(lines: list[str]) -> bytes:
    return ("".join(line + "\n" for line in lines)).encode("utf-8")


class TestOneSidedChangeWins(unittest.TestCase):
    """Property 1."""

    @given(base=st.lists(TEXT, max_size=8), edit=st.lists(TEXT, max_size=8))
    def test_only_ours_changed(self, base: list[str], edit: list[str]) -> None:
        got = merge.three_way(".bashrc", joined(base), joined(edit), joined(base))
        self.assertEqual(0, got.conflicts)
        self.assertEqual(joined(edit), got.data)

    @given(base=st.lists(TEXT, max_size=8), edit=st.lists(TEXT, max_size=8))
    def test_only_theirs_changed(self, base: list[str], edit: list[str]) -> None:
        got = merge.three_way(".bashrc", joined(base), joined(base), joined(edit))
        self.assertEqual(0, got.conflicts)
        self.assertEqual(joined(edit), got.data)


class TestANonOverlappingMergeKeepsBothSides(unittest.TestCase):
    """Property 2."""

    @given(
        plan=st.lists(st.tuples(TEXT, TEXT, SIDES), min_size=1, max_size=6),
    )
    def test_every_changed_line_survives_and_nothing_else_appears(
        self, plan: list[tuple[str, str, str]]
    ) -> None:
        """Each region is edited by at most one side, so the merge is decidable.

        The expectation is built from the same plan the two inputs are built
        from, but built *forwards* -- pick the side that edited each region --
        rather than by re-deriving what the merge should do. A test containing a
        copy of the code it checks cannot fail (CLAUDE.md §2), and there is no
        merge algorithm in here to copy.
        """
        base: list[str] = []
        ours: list[str] = []
        theirs: list[str] = []
        want: list[str] = []
        for index, (original, edited, side) in enumerate(plan):
            # An "edit" that changes nothing is not a second side of the merge,
            # it is the unedited region -- and it would make this test pass for
            # a merge that dropped one side entirely.
            assume(original != edited)
            base.extend(region(index, original))
            ours.extend(region(index, edited if side == "ours" else original))
            theirs.extend(region(index, edited if side == "theirs" else original))
            want.extend(region(index, original if side == "neither" else edited))

        got = merge.three_way(".bashrc", joined(base), joined(ours), joined(theirs))
        # The merged text in the failure message: a conflict here means the
        # fixture's regions overlapped, and the markers say which two.
        self.assertEqual(0, got.conflicts, got.data)
        self.assertEqual(joined(want), got.data)


if __name__ == "__main__":
    unittest.main()
