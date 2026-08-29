"""Tests for the mutant generator.

Everything here except `TestReadingARealDiff` is pure -- source text in,
mutations out -- so it runs in milliseconds and needs no sandbox, no subprocess
and no suite. That is the whole reason `tools/mutants.py` is a separate module
from the runner.

The generator can be wrong in two directions and they are not equally bad. A
mutant it fails to produce is coverage nobody asked for. A mutant whose *span*
is wrong edits a different part of the file than its label claims, still parses,
and reports `caught` or `SURVIVED` about something nobody chose -- which is the
class of error `tools/mutate.py` exists to prevent, arriving one module earlier.
So the span assertions here are exact, never "it contains".

Ported from `martinus/woswoar` (Apache-2.0). Unlike `test_reached.py` and
`test_watch.py`, this one really did need adapting, which is the half of issue
#4's warning that held up: `tools/mutants.py` differs by 150 lines, the module
paths in every fixture had to be renamed, `support.git` takes its environment
explicitly here, and **five assertions name real modules** -- the sibling match,
the index-not-short-circuited pair, the helper closure, and the synthetic
package `__init__`. Those four were re-pointed at this project's layout rather
than renamed, and each is noted where it sits. Everything else was mechanical.

Claims about woswoar's own history are kept and attributed, because they are
the argument for why a check is shaped as it is; claims about *this*
repository's layout were re-derived from it.
"""

from __future__ import annotations

import ast
import collections
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from tools import mutants
from tools.mutants import Mutation

from . import support
from .support import requires_git

REPO_ROOT = Path(__file__).resolve().parent.parent


def mutate(
    body: str, lines: set[int] | None = None, operators: list[str] | None = None
) -> list[Mutation]:
    """Every mutant for a snippet, on every line unless told otherwise.

    **Bounded, because `line_starts` is under here and it can loop for ever.**
    Every arm of its `while` advances `at`, so a mutation that stops one
    advancing spins, and one that advances backwards grows its list without
    limit. `TestLineEndingsThatAreNotNewline` arms the same bound on itself for
    the tests that call `mutants.line_starts` directly -- but the sweep's killer
    for `mutants.py:170` is `TestWhatIsNeverMutated`, which reaches the same line
    through here, and that row stayed `BROKE` through a fix that only armed the
    class the sweep had named.

    That is the point worth carrying: **the killer a sweep reports is one route
    to the line, not all of them.** Two routes, two bounds, and this is the one
    every generating test in the file shares.
    """
    source = textwrap.dedent(body).lstrip()
    if lines is None:
        lines = set(range(1, len(source.splitlines()) + 1))
    with support.deadline(support.PATIENCE, f"generating mutants never finished for {body!r:.60}"):
        return mutants.generate(source, "tupferl/thing.py", lines, operators=operators)


class TestReadingAHunkHeader(unittest.TestCase):
    """`git diff --unified=0`, parsed without a repository in sight."""

    def test_a_single_line_hunk(self) -> None:
        diff = "+++ b/tupferl/x.py\n@@ -13 +13 @@\n"
        self.assertEqual(mutants.parse_hunks(diff), {"tupferl/x.py": {13}})

    def test_a_counted_hunk(self) -> None:
        diff = "+++ b/tupferl/x.py\n@@ -15,2 +15,3 @@\n"
        self.assertEqual(mutants.parse_hunks(diff), {"tupferl/x.py": {15, 16, 17}})

    def test_an_insertion_names_the_new_lines_only(self) -> None:
        diff = "+++ b/tupferl/x.py\n@@ -58,0 +60,6 @@\n"
        self.assertEqual(mutants.parse_hunks(diff), {"tupferl/x.py": set(range(60, 66))})

    def test_a_pure_deletion_contributes_nothing(self) -> None:
        """`+9,0` is a removal: there is no new code at line 9 to mutate.

        Taking it as one line -- which "omitted means 1" would, if the zero were
        not checked -- generates mutants for whatever now sits at that number,
        labelled as if the diff had put it there.
        """
        diff = "+++ b/tupferl/x.py\n@@ -10,3 +9,0 @@\n"
        self.assertEqual(mutants.parse_hunks(diff), {})

    def test_a_deleted_file_contributes_nothing(self) -> None:
        """And it needs no special case: git spells its hunk `+0,0`.

        The deleted file comes *second* so that a parser which failed to notice
        it would attribute the hunk to `y.py` -- which is the only way this can
        be a test at all. It passes because the count is zero, not because
        `/dev/null` is recognised.
        """
        diff = "+++ b/tupferl/y.py\n@@ -1 +1 @@\n+++ /dev/null\n@@ -1,5 +0,0 @@\n"
        self.assertEqual(mutants.parse_hunks(diff), {"tupferl/y.py": {1}})

    def test_several_files_in_one_diff(self) -> None:
        diff = "+++ b/tupferl/x.py\n@@ -1 +1 @@\n+++ b/tools/y.py\n@@ -4,2 +4,2 @@\n"
        self.assertEqual(mutants.parse_hunks(diff), {"tupferl/x.py": {1}, "tools/y.py": {4, 5}})

    def test_a_quoted_path(self) -> None:
        """git C-quotes any path with an odd byte in it, `b/` inside the quotes."""
        diff = '+++ "b/tupferl/odd\\tname.py"\n@@ -1 +1 @@\n'
        self.assertEqual(mutants.parse_hunks(diff), {"tupferl/odd\tname.py": {1}})


class TestWhatMayBeMutated(unittest.TestCase):
    def test_the_product_and_the_tools_are_in(self) -> None:
        self.assertTrue(mutants.mutable("tupferl/sync.py"))
        self.assertTrue(mutants.mutable("tools/mutate.py"))

    def test_the_tests_are_not(self) -> None:
        """Breaking a test proves nothing about the fix, and the run would
        cheerfully report the assertion it had just deleted."""
        self.assertFalse(mutants.mutable("tests/test_sync.py"))

    def test_other_files_are_not(self) -> None:
        self.assertFalse(mutants.mutable("tupferl/shell/tupferl.bash"))
        self.assertFalse(mutants.mutable("docs/architecture.md"))
        self.assertFalse(mutants.mutable("setup.py"))

    def test_an_unmutable_prefix_wins_over_a_mutable_one(self) -> None:
        """`UNMUTABLE` is empty today, and `str.startswith(())` is always
        `False`, so the clause reading it cannot change any answer the other
        three tests here ask for -- deleting it leaves every one of them green.
        That is the shape `mutable`'s own comment argues against two lines
        above it, where the `tests/` clause was removed for being unreachable.

        The difference is that this one is *meant* to be empty: it is the lock
        `UNMUTABLE`'s docstring describes, waiting for the first tool under
        `tools/` that writes outside a directory it made itself. So it is given
        a value here rather than deleted, which is what makes the clause
        provable without waiting for that day.
        """
        self.assertTrue(mutants.mutable("tools/mutate.py"))
        with mock.patch.object(mutants, "UNMUTABLE", ("tools/mutate.py",)):
            self.assertFalse(mutants.mutable("tools/mutate.py"))
            # And nothing else is caught by it.
            self.assertTrue(mutants.mutable("tools/mutants.py"))


class TestTheOperators(unittest.TestCase):
    """One fixture where each fires, one where it must not."""

    def prose(self, body: str, operators: list[str]) -> list[str]:
        return [row.label.split(" -- ", 1)[1] for row in mutate(body, operators=operators)]

    def test_every_operator_has_a_test_here(self) -> None:
        """The completeness guard, in the shape `test_architecture.py` uses.

        An operator added without a fixture would otherwise ship untested, and
        the generator's own output is the last place a gap is visible.
        """
        named = {operator.name for operator in mutants.OPERATORS}
        self.assertEqual(set(_FIRES), named)
        # Both tables, not just the first. An operator could otherwise ship with
        # a fires-fixture and no must-not-fire fixture, which is half a test.
        self.assertEqual(set(_QUIET), named)

    def test_each_operator_fires_on_its_own_fixture(self) -> None:
        for name, (body, expected) in _FIRES.items():
            with self.subTest(operator=name):
                got = self.prose(body, operators=[name])
                self.assertEqual(got, expected)

    def test_every_operator_rewrites_the_code_and_not_only_its_label(self) -> None:
        """The prose and the *edit*, which are two claims and were one test.

        `_FIRES` above pins what each operator says. Nothing pinned what it
        writes -- so an operator whose clone was never modified produced the
        right sentence over unchanged source, and the sweep reported the
        assignment that does the rewriting as a survivor in six places at once:
        `connector`'s `clone.op`, `order`'s two `clone.func`/`clone.keywords`
        pairs, `slice`'s `clone.slice`, and `negate`'s comparison swap.

        A mutation that changes nothing is not caught by any suite: it is the
        original program. So this is the one assertion that separates "the
        generator described an edit" from "the generator made one".
        """
        for name, (body, prose) in _FIRES.items():
            with self.subTest(operator=name):
                rows = mutate(body, operators=[name])
                # The count first, or an operator that produced *nothing* would
                # satisfy every assertion in the loop below by never entering it
                # -- the precondition that was never established, from §2.
                self.assertEqual(len(prose), len(rows), f"{name} produced the wrong rows")
                for row in rows:
                    # Compared through `ast`, not as text. `ast.unparse` adds
                    # parentheses, so a clone nobody modified comes back as
                    # `(a and b)` against `a and b` -- different strings, the
                    # same program. Six survivors hid behind exactly that.
                    self.assertNotEqual(
                        ast.unparse(ast.parse(row.old)),
                        ast.unparse(ast.parse(row.new)),
                        f"{name} produced an edit that changes nothing: {row.new!r}",
                    )

    def test_the_order_operator_rewrites_both_of_its_shapes(self) -> None:
        """`order` has two branches and `_FIRES` reaches one.

        Its fixture is `sorted(days)`, which has no keywords -- so clearing them
        is a no-op there, and `list(days)` comes out right either way. And the
        `min`/`max`/`any`/`all` swap is a different branch that fixture never
        enters at all. Both assignments came back survivors while the operator
        looked covered.
        """

        # Normalised through `ast`, because `ast.unparse` wraps what it emits:
        # the rows really read `(list(days))`, and comparing raw text here would
        # be asserting on the unparser's brackets rather than on the edit.
        def rewrites(body: str) -> set[str]:
            return {ast.unparse(ast.parse(row.new)) for row in mutate(body, operators=["order"])}

        kept = rewrites("x = sorted(days, key=len)\n")
        self.assertIn("list(days)", kept, f"sorted's keywords survived the swap to list: {kept}")
        self.assertEqual({"all(items)"}, rewrites("x = any(items)\n"))

    def test_no_operator_fires_on_a_fixture_it_should_not(self) -> None:
        for name, body in _QUIET.items():
            with self.subTest(operator=name):
                self.assertEqual(self.prose(body, operators=[name]), [])


#: Fixture -> the prose every operator should produce on it, in order.
_FIRES: dict[str, tuple[str, list[str]]] = {
    "boundary": ("x = a < b\n", ["`<` becomes `<=`"]),
    "negate": ("x = a is None\n", ["`is` becomes `is not`"]),
    "connector": ("x = a and b\n", ["`and` becomes `or`"]),
    "branch": (
        "if ready:\n    go()\n",
        ["the `if` is always taken", "the `if` is never taken"],
    ),
    "drop-not": ("x = not ready\n", ["the `not` is dropped"]),
    "order": (
        "x = sorted(days)\n",
        ["the ordering is reversed", "`sorted` becomes `list`"],
    ),
    "affix": (
        'x = name.endswith(".age")\n',
        ["the `.endswith(...)` filter accepts everything"],
    ),
    "return-constant": (
        "def f():\n    return True\n",
        ["returns `False` instead of `True`"],
    ),
    "drop-call": ("path.mkdir()\n", ["the call to `path.mkdir(...)` never happens"]),
    "drop-assign": ("self.total = 1\n", ["`self.total` is never assigned"]),
    "drop-kwarg": ("path.mkdir(mode=0o700)\n", ["`mode=448` is dropped"]),
    "off-by-one": ("x = items[1]\n", ["`1` becomes `2`", "`1` becomes `0`"]),
    # Two statements: the second pins that a divisor of 1 yields the `2`
    # direction only. `// 0` is a `ZeroDivisionError`, which is BROKE rather
    # than an answer and costs a whole suite run to establish nothing.
    "divisor": (
        "x = total // 60\ny = total // 1\n",
        ["`60` becomes `61`", "`60` becomes `59`", "`1` becomes `2`"],
    ),
    "sign": ("x = -1\n", ["`-1` becomes `1`"]),
    "return-value": (
        "def f():\n    return compute(x)\n",
        ["returns `None` instead of `compute(x)`"],
    ),
    # Both shapes: an expression and an augmented assignment. With only the
    # first, the mutation cutting `AugAssign` out of `arith` survived.
    "arith": (
        "x = a + b\ncount += 1\n",
        ["`+` becomes `-`", "`+=` becomes `-=`"],
    ),
    "slice": ("x = items[1:]\n", ["the slice takes everything"]),
}

#: Fixture -> a shape the operator must leave alone.
_QUIET: dict[str, str] = {
    # A chained comparison has two operators and no single answer.
    "boundary": "x = a < b < c\n",
    "negate": "x = a < b\n",
    "connector": "x = a + b\n",
    # Already constant: forcing `True` to `True` changes nothing, and a row that
    # changes nothing reports about nothing.
    "branch": "if True:\n    go()\n",
    "drop-not": "x = -value\n",
    "order": "x = sorted(days, reverse=True)\n",
    "affix": "x = name.strip()\n",
    "return-constant": "def f():\n    return value\n",
    # The meter is never the thing being compared.
    "drop-call": "progress.tick()\n",
    # A plain name: deleting it leaves a NameError further down, which reports
    # BROKE -- not an answer, and a whole suite run to learn that.
    "drop-assign": "plain = 3\n",
    # `ok=` is a required field, not an optional guarantee: dropping it is a
    # `TypeError`, which is BROKE rather than an answer.
    "drop-kwarg": 'Check(label="x", ok=True)\n',
    # Big enough that +-1 is arbitrary rather than a boundary.
    "off-by-one": "x = items[97]\n",
    # A variable divisor has no literal to move, and a multiplication is not a
    # division however constant its right side -- the second line is why this
    # is a separate operator from `arith` rather than a case inside it.
    #
    # Then the three the guard rejects one clause at a time, because with only
    # the two above, *dropping the whole guard* left this fixture quiet and the
    # mutant survived: `True` is an `int` in Python and would otherwise divide
    # into `2`, `0` is the division that raises rather than answers, and a float
    # divisor moved by one is a change of a different order.
    "divisor": (
        "x = total // count\nx = total * 60\nw = total // True\ny = total // 0\nz = total // 1.5\n"
    ),
    # `-0` is `0`, so the row would be an equivalent mutant by construction; and
    # a negated *name* has no literal whose sign there is to flip.
    #
    # `-True` and `-"a"` are the two the guard rejects, and they are here for the
    # same reason as above -- the first is an `int` by inheritance, the second is
    # not a number at all, and without them the guard could be deleted whole with
    # this fixture still silent.
    "sign": 'x = -0\ny = -value\nz = -True\nw = -"a"\n',
    # `return None` is already what the mutation would produce -- and `return
    # True` is left to `return-constant`, which swaps it. `None` is falsy, so
    # asking both would be very nearly the same question at twice the price.
    # With only the `return None` half, dropping the boolean exclusion survived.
    "return-value": "def f():\n    return None\n\n\ndef g():\n    return True\n",
    # String concatenation has no meaningful `-`.
    "arith": "x = a % b\n",
    # `x[:]` has no bound left to widen.
    "slice": "x = items[:]\n",
}


class TestStringConcatenationIsNotArithmetic(unittest.TestCase):
    """`"a" + "b"` becoming `"a" - "b"` is a `TypeError`, never a verdict.

    A row like that comes back `BROKE`, which is never `caught` -- so the line it
    appeared to guard is guarded by nothing, and the summary counts it in neither
    number a reader looks at. #57 measured 20 such rows in a whole-tree sweep.

    **Literals only.** The check is about what the expression proves on its own,
    not about what an identifier looks like: getting it wrong permissively costs
    one `BROKE` row, and getting it wrong strictly silently stops mutating real
    arithmetic, which is a loss of coverage no output would report.
    """

    def additions(self, source: str) -> list[str]:
        return [row.label for row in mutate(source, operators=["arith"])]

    def test_two_string_literals_are_not_mutated(self) -> None:
        self.assertEqual([], self.additions('x = "a" + "b"\n'))

    def test_one_string_literal_is_enough(self) -> None:
        """`str + int` raises whichever way round it is written, so a string on
        either side settles that the `+` is concatenation."""
        self.assertEqual([], self.additions('x = name + ".done"\n'))
        self.assertEqual([], self.additions('x = ".done" + name\n'))

    def test_an_f_string_counts_as_one(self) -> None:
        self.assertEqual([], self.additions('x = f"{a}" + b\n'))

    def test_ordinary_arithmetic_is_still_mutated(self) -> None:
        """The half that matters more. A guard that swallowed real `+` would
        remove coverage silently, which is the failure this whole module is
        against -- so this is the assertion that would catch it."""
        self.assertEqual(1, len(self.additions("x = a + b\n")))
        self.assertEqual(1, len(self.additions("x = 1 + 2\n")))
        self.assertEqual(1, len(self.additions("count += 1\n")))

    def test_a_number_beside_a_name_is_still_mutated(self) -> None:
        """The shape closest to the one being refused, and the reason the check
        asks about *strings* rather than about literals in general."""
        self.assertEqual(1, len(self.additions("x = at + 1\n")))

    def test_an_attribute_is_not_something_this_can_judge(self) -> None:
        """**#57's own example, and this does not fix it.** `paint.GOOD +
        paint.HEAD` in `tools/watch.py` is two attributes; proving them
        string-valued means resolving a name across a module boundary, which is
        a type checker rather than a guard. Measured on this tree: 9 `+`
        expressions have a provable string operand and 37 do not, the three in
        `watch.py` among the 37.

        Asserted rather than left implicit, so that reading the issue as done
        does not hide the part that is not.
        """
        self.assertEqual(1, len(self.additions("x = paint.GOOD + paint.HEAD\n")))


class TestWhatIsNeverMutated(unittest.TestCase):
    def test_a_docstring_is_left_alone(self) -> None:
        self.assertEqual(mutate('def f():\n    """Words."""\n'), [])

    def test_an_f_string_interior_yields_nothing_at_all(self) -> None:
        """Zero, and the assertion is deliberately not "a correct span".

        On 3.10 and 3.11 the nodes inside a `JoinedStr` carry the *enclosing
        string's* position, so a span computed from one splices bytes somewhere
        else in the file -- and the result usually still parses. The point is not
        that the span would be wrong; it is that it cannot be trusted at all.
        """
        self.assertEqual(mutate('x = f"{a < b}"\n'), [])

    def test_an_annotation_is_left_alone(self) -> None:
        """Every module here has `from __future__ import annotations`, so these
        are never evaluated: a guaranteed equivalent mutant."""
        # The literal inside the annotation must be untouched while the one in
        # the value is not -- a fixture that only had the annotation could not
        # tell "suppressed" from "the operator never fires here".
        rows = mutate("x: Literal[1] = 2\n", operators=["off-by-one"])
        self.assertEqual([row.old for row in rows], ["2", "2"])

    def test_a_type_checking_block_is_left_alone(self) -> None:
        self.assertEqual(mutate("if TYPE_CHECKING:\n    import x\n"), [])

    def test_the_main_guard_is_left_alone(self) -> None:
        self.assertEqual(mutate('if __name__ == "__main__":\n    go()\n'), [])

    def test_an_assert_is_left_alone(self) -> None:
        self.assertEqual(mutate("assert a is None\n"), [])

    def test_a_pragma_line_is_left_alone(self) -> None:
        self.assertEqual(mutate("x = a < b  # pragma: no mutate\n"), [])

    def test_a_pragma_on_the_very_first_line_still_counts(self) -> None:
        """`range(1, lines + 1)`, and line 1 is the end of it a fixture misses.

        Every other pragma test puts the marker further down a file that opens
        with a docstring or an import, so a scan starting at line 2 answers them
        all correctly. The `off-by-one` row on that range survived on exactly
        that gap.
        """
        self.assertEqual(mutate("x = a < b  # pragma: no mutate\n"), [])

    def test_a_pragma_covers_the_whole_construct_not_just_its_first_line(self) -> None:
        """The pragma reads as covering what it sits in, so it must.

        Checked by span rather than by start line. A multi-line `if` was mutated
        regardless while its author could see the pragma sitting there -- silent,
        and in the direction that reads as success, because the run then reports
        `SURVIVED` about code that had been declared out of scope.
        """
        source = (
            "def g(a, b):\n    if (a\n            and b):  # pragma: no mutate\n        return 1\n"
        )
        rows = mutants.generate(source, "w/t.py", {2, 3}, tests="t")
        self.assertEqual([row.label for row in rows], [])

    def test_a_form_feed_above_a_pragma_does_not_move_it(self) -> None:
        """The pragma is numbered the way `ast` numbers, not the way `str` does.

        `str.splitlines` breaks on form feed and the tokenizer does not, so this
        source put the suppression one line below the code it names: the marked
        comparison was mutated and the innocent line above it was skipped. The
        module already owned `line_starts` for exactly this; the pragma was the
        last place not using it. `dedent` eats a form feed, so no helper here.
        """
        source = "def f(a, b):\n    x = a < b\n\x0c\n    y = a < b  # pragma: no mutate\n"
        rows = mutants.generate(source, "w/t.py", {1, 2, 3, 4}, tests="t")
        self.assertEqual([row.label for row in rows], ["w/t.py:2 in f() -- `<` becomes `<=`"])

    def test_a_while_is_never_forced_true(self) -> None:
        """A hang is not a caught mutant: it burns a lane and the timeout to
        report `TIMEOUT`, which is not an answer."""
        self.assertEqual(mutate("while ready:\n    go()\n", operators=["branch"]), [])

    def test_only_the_changed_lines(self) -> None:
        body = "x = a < b\ny = c < d\n"
        self.assertEqual([row.label for row in mutate(body, lines={2})].pop().count(":2"), 1)
        self.assertEqual(len(mutate(body, lines={2})), 1)


class TestLineEndingsThatAreNotNewline(unittest.TestCase):
    """`\r\n` and a bare `\r` end a line for CPython, and so for every span here.

    `line_starts` was written for the form feed -- a character `str.splitlines`
    breaks on and the tokenizer does not -- and it handles the opposite case in
    the same loop: characters the tokenizer *does* break on. That half arrived
    with no fixture, and the tool said so about itself: of the 17 rows that
    survived `--base main --only tools/mutants.py`, 15 were in this branch, and
    the reason was not a subtle one. No source anywhere in the suite contained a
    `\r`, so `if source[at] == "\r"` was never once entered.

    The exactness matters for the same reason the form feed did. A step of 1
    across `\r\n` puts every span below it one character early, which is an edit
    that still parses, in a place the row's label does not name.

    Every test here runs under a deadline. `line_starts` is a
    `while at < len(source)` loop in which every arm advances `at`, so a mutation
    that stops one advancing spins for ever, and one that advances *backwards*
    grows `starts` without limit as well. Four rows came back `BROKE` that way on
    the whole-tree sweep -- two of them "ran out of memory" -- and `BROKE` is
    never `caught`, so the exactness this class exists to pin was pinned by
    nothing.

    Armed in `setUp` rather than around `starts_agree_with_ast`, which was the
    first attempt: three of the four rows are killed by tests that do not go
    through that helper, and they stayed `BROKE`. A bound on one helper covers
    the tests that call it and reads as though it covered the class.
    """

    def setUp(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(support.deadline(support.PATIENCE, "line_starts never finished"))

    def starts_agree_with_ast(self, source: str) -> list[int]:
        """`ast` is the authority; this asserts against it rather than a constant.

        Written this way because the constant is what a reader checks by hand and
        gets wrong. `get_source_segment` slices with `ast`'s own line accounting,
        so if `line_starts` disagrees the segments come back shifted.
        """
        tree = ast.parse(source)
        for node in tree.body:
            self.assertIsNotNone(ast.get_source_segment(source, node))
        return mutants.line_starts(source)

    def test_crlf_counts_as_one_line_ending(self) -> None:
        source = "a = 1\r\nb = 2\r\n"
        # 7, not 6: the `\n` after the `\r` starts no line of its own.
        self.assertEqual(self.starts_agree_with_ast(source), [0, 7, 14])

    def test_a_bare_cr_ends_a_line_too(self) -> None:
        """The `else 1` arm. A lone `\r` is a terminator for CPython, not junk."""
        source = "a = 1\rb = 2\r"
        self.assertEqual(self.starts_agree_with_ast(source), [0, 6, 12])

    def test_the_three_endings_mixed(self) -> None:
        """One source reaching every arm, because real files are converted badly."""
        source = "a = 1\r\nb = 2\rc = 3\n"
        self.assertEqual(self.starts_agree_with_ast(source), [0, 7, 13, 19])

    def test_a_leading_newline_is_not_skipped(self) -> None:
        """`at` starts at 0, and index 0 is a line ending on a file that opens blank."""
        source = "\na = 1\n"
        self.assertEqual(self.starts_agree_with_ast(source), [0, 1, 7])

    def test_a_file_that_does_not_end_in_a_newline(self) -> None:
        """The last line has no start after it, so its end is the end of the file.

        Every other fixture in this file ends in `\n`, which is why the bound in
        `Offsets._line` survived mutation: `lineno < len(starts)` and
        `lineno <= len(starts)` only differ when the mutated node sits on the
        final line and nothing follows it. Git warns about such a file and
        happily stores one.
        """
        source = "first = 1\nx = a < b"
        self.assertEqual(mutants.line_starts(source), [0, 10])
        row = mutants.generate(source, "w/t.py", {2}, tests="t").pop()
        start, end = row.span or (0, 0)
        self.assertEqual(source[start:end], "a < b")

    def test_a_span_below_a_crlf_lands_on_its_own_text(self) -> None:
        """The end-to-end claim: one character early is a different edit.

        `\r\n` above the mutated line, and a comparison below it, so a step of 1
        shifts the span into the line ending rather than onto `a < b`.
        """
        source = "first = 1\r\nx = a < b\n"
        row = mutants.generate(source, "w/t.py", {2}, tests="t").pop()
        start, end = row.span or (0, 0)
        self.assertEqual(source[start:end], "a < b")
        self.assertEqual(row.old, "a < b")


class TestDroppingAKeywordArgument(unittest.TestCase):
    """The operator written for `mkdir(..., mode=0o700)`.

    Deliberately narrow. A blanket "drop any keyword" was measured first in
    woswoar, over the 490 keyword arguments its package had then: the commonest
    are *required* parameters passed by name, and dropping one raises
    `TypeError` -- `BROKE` rather than an answer, at the price of a whole suite
    run. `_DROPPABLE` inverts the rule so that a keyword nobody argued for is
    never dropped.

    The number is woswoar's and is labelled as such. It read "281 in `tupferl/`"
    after the port, which was woswoar's figure with the package renamed; counted
    by AST, this package has 149 that `_DROPPABLE` could reach (150 nodes,
    one of which is a `**kwargs` and so not droppable).
    """

    def dropped(self, body: str) -> list[str]:
        rows = mutants.generate(body, "w/t.py", {1}, tests="t", operators=["drop-kwarg"])
        return [row.label.split(" -- ")[-1] for row in rows]

    def test_a_mode_is_droppable(self) -> None:
        """The case it exists for: the directory is still made, still returns,
        and every test that only asks whether the path exists still passes."""
        self.assertEqual(
            self.dropped("path.mkdir(parents=True, mode=0o700)\n"), ["`mode=448` is dropped"]
        )

    def test_check_true_is_droppable(self) -> None:
        self.assertEqual(
            self.dropped("subprocess.run(argv, check=True)\n"), ["`check=True` is dropped"]
        )

    def test_check_false_is_not(self) -> None:
        """It is `subprocess.run`'s own default, so removing it changes nothing
        and the row could only ever be an unkillable survivor."""
        self.assertEqual(self.dropped("subprocess.run(argv, check=False)\n"), [])

    def test_a_keyword_nobody_argued_for_is_left_alone(self) -> None:
        """`ok=` is a required `Check` field: dropping it is a `TypeError`."""
        self.assertEqual(self.dropped('Check(label="x", ok=True)\n'), [])

    def test_a_star_star_spread_is_not_a_keyword_to_drop(self) -> None:
        self.assertEqual(self.dropped("f(**options)\n"), [])

    def test_the_replacement_actually_omits_it(self) -> None:
        """The prose could be right while the edit is not."""
        rows = mutants.generate(
            "p.mkdir(mode=0o700, parents=True)\n",
            "w/t.py",
            {1},
            tests="t",
            operators=["drop-kwarg"],
        )
        self.assertNotIn("mode", rows[0].new)
        self.assertIn("parents", rows[0].new, "only the named keyword goes")

    def test_each_droppable_keyword_is_reachable(self) -> None:
        """Otherwise a name can sit in `_DROPPABLE` spelled wrongly for ever."""
        for name in sorted(mutants._DROPPABLE):
            with self.subTest(keyword=name):
                value = "True" if name in ("check", "exist_ok", "follow_symlinks") else "0"
                self.assertEqual(len(self.dropped(f"f(a, {name}={value})\n")), 1)

    def test_encoding_is_not_droppable(self) -> None:
        """It was, and woswoar's first whole-package sweep with this operator
        said so: 41 unkillable rows against 3 caught. No sweep of this package
        has measured it, so the number is kept and attributed rather than
        restated as if it were local.

        Dropping `encoding="utf-8"` is a real defect -- under `LC_ALL=C` the same
        read raises `UnicodeDecodeError` -- but this suite runs in a UTF-8 locale
        everywhere, so `read_text()` and `read_text(encoding="utf-8")` are the
        same call almost everywhere in it. A row that can only ever survive is
        not a question worth asking every run.

        "Almost" in woswoar, since woswoar#229, where one suite drove a round
        trip in a child that prefers ASCII and made two `encoding=` arguments
        answerable. This project has no such suite, so here the operator is off
        for the simpler reason above and nothing is bought by turning it on.
        """
        self.assertEqual(self.dropped('p.read_text(encoding="utf-8")\n'), [])


class TestARowThatMatchesNothingSaysWhatIsClose(unittest.TestCase):
    """The refusal is right and used to end the search there.

    A hand-written row that matches nothing is almost always quoting the file's
    past: an edit moved the line by a word after the row was written. The author
    then greps for a string they believe is present, which is the one search
    that cannot succeed. Five rows in one session ended that way.
    """

    def refusal(self, old: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mod.py"
            path.write_text("def clamp(value: int) -> int:\n    return value\n", encoding="utf-8")
            with self.assertRaises(SystemExit) as refused:
                mutants.check(Mutation("x", str(path), old, "pass", "t"))
        return str(refused.exception)

    def test_it_offers_the_closest_line(self) -> None:
        said = self.refusal("def clamp(value: int) -> int :")
        self.assertIn("closest line", said)
        self.assertIn("def clamp(value: int) -> int:", said)

    def test_it_offers_nothing_when_nothing_is_close(self) -> None:
        """A suggestion that is not the intended line is worse than none."""
        self.assertNotIn("closest line", self.refusal("import antigravity"))

    def test_a_multi_line_row_gets_no_guess(self) -> None:
        """There is no "nearest line" to a span, and picking one of its lines
        would point at the half that still matches.

        The first line here is deliberately *close* to a real one. With
        something unlike the file, the distance check refuses it anyway and this
        test passes whether or not the span check exists -- which is exactly
        what it did until a mutation said so.
        """
        self.assertNotIn(
            "closest line", self.refusal("def clamp(value: int) -> int :\n    return value\n")
        )


class TestSkippingAnOperator(unittest.TestCase):
    """`--skip-operator`, the escape hatch a pragma cannot serve.

    A pragma suppresses a *line*; this suppresses a *kind*, which is what an
    equivalent mutant produced by one operator over and over needs.
    """

    def test_it_subtracts_from_the_default_set(self) -> None:
        every = mutants.generate("x = a < b\n", "w/t.py", {1}, tests="t")
        fewer = mutants.generate("x = a < b\n", "w/t.py", {1}, tests="t", skip=["boundary"])
        self.assertTrue(any(row.operator == "boundary" for row in every))
        self.assertFalse(any(row.operator == "boundary" for row in fewer))
        self.assertLess(len(fewer), len(every))

    def test_an_unknown_name_is_refused(self) -> None:
        with self.assertRaises(SystemExit) as refused:
            mutants.generate("x = a < b\n", "w/t.py", {1}, skip=["boundry"])
        self.assertIn("no such operator: boundry", str(refused.exception))

    def test_skipping_everything_is_refused_rather_than_silent(self) -> None:
        """An empty selection generates nothing and would exit 0 -- a run that
        asked nothing reading as a run that found nothing."""
        with self.assertRaises(SystemExit) as refused:
            mutants.generate(
                "x = a < b\n", "w/t.py", {1}, operators=["boundary"], skip=["boundary"]
            )
        self.assertIn("every operator was skipped", str(refused.exception))


class TestEveryLine(unittest.TestCase):
    """What `--all` means, enumerated rather than diffed against the first commit."""

    def test_it_finds_the_mutable_files_and_all_their_lines(self) -> None:
        found = mutants.every_line(REPO_ROOT)
        self.assertIn("tools/mutants.py", found)
        self.assertIn("tupferl/sync.py", found)
        body = (REPO_ROOT / "tupferl/sync.py").read_text(encoding="utf-8").splitlines()
        self.assertEqual(max(found["tupferl/sync.py"]), len(body))
        self.assertEqual(min(found["tupferl/sync.py"]), 1)

    def test_it_holds_nothing_that_is_not_mutable(self) -> None:
        for path in mutants.every_line(REPO_ROOT):
            self.assertTrue(mutants.mutable(path), path)

    def test_tests_are_not_swept(self) -> None:
        self.assertFalse(any(p.startswith("tests/") for p in mutants.every_line(REPO_ROOT)))


class TestWhatTheWholeTreeWalkTakesIn(unittest.TestCase):
    """`every_line`'s two filters, neither reachable from the tree as it is."""

    def tree(self) -> Path:
        box = Path(tempfile.mkdtemp(prefix="tupferl-walk-"))
        self.addCleanup(shutil.rmtree, box, True)
        (box / "tupferl").mkdir()
        (box / "tupferl" / "real.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
        (box / "tupferl" / "__init__.py").write_text("", encoding="utf-8")
        (box / "tupferl" / "skipped.py").write_text("c = 3\n", encoding="utf-8")
        return box

    def test_an_empty_module_contributes_no_lines(self) -> None:
        """`if body:`. An empty `__init__.py` has nothing to mutate, and an
        entry with an empty line set reads downstream as a file that was
        covered -- it appears in the count of files and contributes no rows."""
        found = mutants.every_line(self.tree())
        self.assertEqual({1, 2}, found["tupferl/real.py"])
        self.assertNotIn("tupferl/__init__.py", found)

    def test_an_unmutable_path_is_skipped(self) -> None:
        """The guard whose own comment says it was once thought unreachable.
        `UNMUTABLE` is empty in this repository, so filling it is the only way
        to drive the clause rather than the tuple above it."""
        with mock.patch.object(mutants, "UNMUTABLE", ("tupferl/skipped.py",)):
            found = mutants.every_line(self.tree())
        self.assertIn("tupferl/real.py", found)
        self.assertNotIn("tupferl/skipped.py", found)


class TestChoosingOperators(unittest.TestCase):
    """A run that asked nothing must not read as a run that found nothing."""

    def test_an_unknown_operator_is_refused(self) -> None:
        """`--operator boundry` used to generate zero rows and exit 0.

        Which is indistinguishable, in a pull request, from a change nothing
        could mutate -- the same failure `--limit` avoids by saying out loud how
        many rows it dropped.
        """
        with self.assertRaises(SystemExit) as refused:
            mutants.generate("x = a < b\n", "w/t.py", {1}, operators=["boundry"])
        self.assertIn("no such operator: boundry", str(refused.exception))
        self.assertIn("boundary", str(refused.exception))

    def test_a_known_operator_still_selects(self) -> None:
        rows = mutants.generate("x = a < b\n", "w/t.py", {1}, operators=["boundary"])
        self.assertEqual([row.label for row in rows], ["w/t.py:1 -- `<` becomes `<=`"])


class TestTheLabelNamesAScope(unittest.TestCase):
    """`in C()` named a call that does not exist, on the string a reviewer reads."""

    def test_a_class_body_is_not_written_as_a_call(self) -> None:
        rows = mutants.generate("class C:\n    LIMIT = 1\n", "w/t.py", {2}, tests="t")
        self.assertEqual(rows[0].label, "w/t.py:2 in C -- `1` becomes `2`")

    def test_a_method_still_is(self) -> None:
        source = "class C:\n    def m(self, a, b):\n        return a < b\n"
        rows = mutants.generate(source, "w/t.py", {3}, tests="t")
        self.assertEqual(rows[0].label, "w/t.py:3 in C.m() -- `<` becomes `<=`")

    def test_a_function_in_a_function_is_not_written_as_two_calls(self) -> None:
        """`outer().inner()` is what baking the parentheses into the name gives."""
        source = "def outer():\n    def inner(a, b):\n        return a < b\n    return inner\n"
        rows = mutants.generate(source, "w/t.py", {3}, tests="t")
        self.assertEqual(rows[0].label, "w/t.py:3 in outer.inner() -- `<` becomes `<=`")


class TestTheSpanIsExact(unittest.TestCase):
    def test_the_span_holds_the_text_the_row_quotes(self) -> None:
        source = "x = a < b\n"
        row = mutate(source).pop()
        start, end = row.span or (0, 0)
        self.assertEqual(source[start:end], row.old)
        self.assertEqual(row.old, "a < b")

    def test_a_line_with_non_ascii_before_it(self) -> None:
        """`col_offset` is a UTF-8 *byte* offset; `str` slicing is by character.

        In woswoar two modules carried `·`, `✔` and `²` on code lines, which is
        where the operator was written. Here the only non-ASCII on a code line
        is `tupferl/doctor.py`'s `MARKS = {True: "\u2714", ...}`, and no mutable
        node sits after it on that line -- so this is a guard against a shape
        this package could easily grow rather than one it has today, and saying
        otherwise (as the ported line did) claims a live case that is not there.
        Every fixture written in plain English passes under either arithmetic,
        which is what makes the bug invisible without this test.
        """
        # On the *same line*, before the column. A fixture with the non-ASCII on
        # an earlier line passes under either arithmetic -- the line starts are
        # counted in characters and are right either way -- so the first version
        # of this test proved nothing, and the mutation that removes the
        # conversion survived it.
        source = 'x = "✔ ✘ ²" if a < b else "·"\n'
        row = mutate(source, lines={1}).pop()
        start, end = row.span or (0, 0)
        self.assertEqual(source[start:end], "a < b")
        self.assertEqual(row.old, "a < b")

    def test_a_span_on_a_later_line(self) -> None:
        """`lineno` indexes a list, and a one-line fixture cannot see it slip.

        `_lines[lineno - 1]` against `_lines[lineno - 2]` picks the same string
        whenever the file has one line, and the same *offset* whenever every
        earlier line is the same length. Both mutations survived a fixture set
        that had drifted to single-line sources -- including this class's own
        UTF-8 case, which had to become one line to test the byte conversion.
        So: three lines, deliberately of different lengths, mutating the third.

        The line *above* the mutated one carries a multi-byte character inside
        the first four bytes, which is the only arrangement that can tell the two
        apart: `line.encode()[:col]` gives four characters on any ASCII line, so
        a slip onto a neighbouring plain-English line is invisible.
        """
        source = "a = 1\n# ·x\nx = a < b\n"
        row = mutate(source, lines={3}).pop()
        start, end = row.span or (0, 0)
        self.assertEqual(source[start:end], "a < b")
        self.assertEqual(source.index("a < b", source.index("x =")), start)

    def test_a_form_feed_does_not_shift_the_lines_below_it(self) -> None:
        """`str.splitlines` and the tokenizer disagree, and only one is right.

        `splitlines` breaks on form feed, vertical tab, the file/group/record
        separators and U+2028/9; CPython treats a form feed as whitespace. A
        single `\\f` -- the Emacs page separator, and legal Python -- made
        `splitlines` see seven lines where `ast` saw six, so every span below it
        landed one line early. The mutant then edited `limit` on line 4 while its
        label named `return True` on line 5, and the result still parsed.

        The span assertion cannot be `source[start:end] == row.old`: `generate`
        derives `old` from the span, so that identity holds however wrong the
        offsets are. It has to be the text this test names independently.
        """
        source = (
            "def f(values, limit):\n"
            "    total = sum(values)\n"
            "\x0c\n"
            "    if total < limit:\n"
            "        return True\n"
            "    return False\n"
        )
        # Not through `mutate`: `textwrap.dedent` counts `\x0c` as whitespace and
        # rewrites the line, so the helper would hand the generator a different
        # string from the one asserted against here.
        rows = mutants.generate(source, "tupferl/thing.py", {5}, operators=["return-constant"])
        self.assertEqual(len(rows), 1)
        start, end = rows[0].span or (0, 0)
        self.assertEqual(source[start:end], "True")
        self.assertEqual(start, source.index("True"))

    def test_a_repeated_line_gets_distinct_spans(self) -> None:
        """The reason spans exist: `str.replace` cannot tell these apart."""
        source = (
            "def f():\n    if not ready:\n        return 1\n\n\n"
            "def g():\n    if not ready:\n        return 2\n"
        )
        rows = mutate(source, operators=["drop-not"])
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0].span, rows[1].span)
        for row in rows:
            start, end = row.span or (0, 0)
            self.assertEqual(source[start:end], row.old)


class TestTheLabel(unittest.TestCase):
    def test_it_names_the_place_and_the_change(self) -> None:
        # Pinned to one operator: `return-value` also fires on this line, and a
        # `.pop()` off an unpinned list asserts whichever sorted last.
        row = mutate(
            "class C:\n    def m(self):\n        return a < b\n", operators=["boundary"]
        ).pop()
        self.assertEqual(row.label, "tupferl/thing.py:3 in C.m() -- `<` becomes `<=`")

    def test_at_module_scope_there_is_no_function(self) -> None:
        self.assertEqual(
            mutate("x = a < b\n").pop().label, "tupferl/thing.py:1 -- `<` becomes `<=`"
        )


class TestTheCap(unittest.TestCase):
    def rows(self, path: str, count: int) -> list[Mutation]:
        return [Mutation(f"{path}:{n}", path, "a", "b", "t", span=(n, n + 1)) for n in range(count)]

    def test_it_spreads_across_files_rather_than_taking_a_prefix(self) -> None:
        """`[:limit]` would cover the first file exhaustively and the largest not
        at all -- and the printed count would look right either way."""
        table = self.rows("a.py", 10) + self.rows("z.py", 10)
        kept, dropped = mutants.cap(table, 6)
        self.assertEqual(len(kept), 6)
        self.assertEqual({row.path for row in kept}, {"a.py", "z.py"})
        self.assertEqual(len(dropped), 14)

    def test_everything_is_accounted_for(self) -> None:
        table = self.rows("a.py", 5) + self.rows("z.py", 7)
        kept, dropped = mutants.cap(table, 4)
        self.assertEqual(len(kept) + len(dropped), len(table))

    def test_no_cap_keeps_everything(self) -> None:
        table = self.rows("a.py", 5)
        self.assertEqual(mutants.cap(table, 0), (table, []))


class TestThePackageInitIsIndexedAsThePackage(unittest.TestCase):
    """`tests/__init__.py` is `tests`, not `tests.__init__`, which nothing imports.

    Driven against a built tree rather than the real one, and that is the point:
    the repository's own `tests/__init__.py` is empty, so against the real tree
    both spellings give the same answer and a mutation reverting this survives
    for want of anything to distinguish them. `module_of` already carries this
    exact special case for `tupferl/__init__.py`, argued at length; this is the
    other half of the same mapping, and the failure it prevents is silent --
    anything ever added to `tests/__init__.py` drops out of the closure.
    """

    def test_a_helper_in_the_package_init_still_carries_its_imports(self) -> None:
        with tempfile.TemporaryDirectory() as box:
            root = Path(box)
            (root / "tests").mkdir()
            (root / "tests" / "__init__.py").write_text(
                "from tupferl import copies\n", encoding="utf-8"
            )
            # `from . import helper` is what a test module writes, and it is the
            # package -- `tests` -- that the import index has to be keyed on.
            (root / "tests" / "test_a.py").write_text("from . import helper\n", encoding="utf-8")
            index = mutants.importers(root)
        self.assertEqual(index.get("tupferl.copies"), {"tests.test_a"})


class TestFollowingAHelperOneLevel(unittest.TestCase):
    """`importers` links a helper under `tests/` back to the tests that import
    it -- `tests/support.py` imports `paths`, so every module importing
    `support` reaches `paths` too.

    The filter that decides what counts as a helper -- `module == "tests" or
    module.startswith("tests.")` -- is *not* asserted here, and the reason is
    worth stating rather than hiding: a first attempt did assert it, and the
    assertion was vacuous. `uses_helper` is only ever read against `helpers`,
    which holds the non-`test_` files under `tests/`, so an entry keyed by a
    product module is looked up and never found. Both mutations of that line
    are recorded as equivalent instead, with that argument.
    """

    def tree(self) -> Path:
        box = Path(tempfile.mkdtemp(prefix="tupferl-importers-"))
        self.addCleanup(shutil.rmtree, box, True)
        (box / "tupferl").mkdir()
        (box / "tests").mkdir()
        (box / "tupferl" / "widget.py").write_text("x = 1\n", encoding="utf-8")
        (box / "tests" / "__init__.py").write_text("", encoding="utf-8")
        (box / "tests" / "support.py").write_text(
            "from tupferl import widget  # noqa: F401\n", encoding="utf-8"
        )
        (box / "tests" / "test_through_helper.py").write_text(
            "from tests import support  # noqa: F401\n", encoding="utf-8"
        )
        (box / "tests" / "test_direct.py").write_text(
            "from tupferl import widget  # noqa: F401\n", encoding="utf-8"
        )
        return box

    def test_a_test_reaches_what_its_helper_imports(self) -> None:
        """The closure, and the reason the index exists at all: a mutation in
        `widget` has to run the test that only reaches it through `support`."""
        box = self.tree()
        found = mutants.targets_for("tupferl/widget.py", box, mutants.importers(box)).split()
        self.assertIn("tests.test_through_helper", found)
        self.assertIn("tests.test_direct", found)


class TestChoosingTheTests(unittest.TestCase):
    """Driven against the real repository, because that is the map it describes."""

    index: dict[str, set[str]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.index = mutants.importers(REPO_ROOT)

    def targets(self, path: str) -> set[str]:
        return set(mutants.targets_for(path, REPO_ROOT, self.index).split())

    def test_the_name_match(self) -> None:
        self.assertIn("tests.test_sync", self.targets("tupferl/sync.py"))

    def test_siblings_of_the_name_match(self) -> None:
        """`sync.py` here, where woswoar's copy used `importer.py`: one of the
        five assertions in this file that name real modules, so they had to be
        re-pointed rather than renamed."""
        self.assertLessEqual(
            {"tests.test_sync_cli", "tests.test_sync_commits"},
            self.targets("tupferl/sync.py"),
        )

    def test_a_module_with_no_test_of_its_own_is_found_by_import(self) -> None:
        """`tupferl/copies.py` has no `test_copies.py`; `tests/test_sync.py`
        imports it. A name heuristic alone would report it as untested.

        The fifth of the five the header counts, and the one added last.
        woswoar's line said `gitrepo.py`, and the rename made that false here --
        `tests/test_gitrepo.py` exists, so the assertion still passed while the
        case it is named for went unexercised. `copies.py` is this project's
        module with the shape: no name match at all, resolved only through the
        import index.
        """
        self.assertIn("tests.test_sync", self.targets("tupferl/copies.py"))

    def test_the_name_match_does_not_short_circuit_the_index(self) -> None:
        """Measured on woswoar#216: taking `test_install` alone because the name
        matched reported nine mutants as survivors that `tests.test_doctor`
        catches.

        `config.py` is this project's instance of the same shape --
        `tests.test_config` matches by name, and `tests.test_doctor` reaches it
        only through the index.
        """
        found = self.targets("tupferl/config.py")
        self.assertIn("tests.test_config", found)
        self.assertIn("tests.test_doctor", found)

    def test_a_helper_carries_its_imports_to_the_tests_that_use_it(self) -> None:
        """Through `tests/support.py`, which every suite imports relatively.

        `tupferl/manifest.py` is this project's case that tells the two answers
        apart: `tests/test_merge.py` imports `support` and nothing that reaches
        `manifest` directly, so it appears here only if the closure runs. An
        assertion about a module the test imports itself cannot -- it is
        satisfied whether or not the helper mechanism works at all, which in
        woswoar is how that mechanism came to be inert without any test
        noticing. Checked, not assumed: `test_merge.py` has no `manifest` or
        `paths` import of its own.
        """
        self.assertIn("tests.test_merge", self.targets("tupferl/manifest.py"))

    def test_every_mutable_file_resolves_or_says_it_cannot(self) -> None:
        """The completeness pass. An empty answer is allowed -- it means "run
        everything" -- but it must be a small, named set, or the selection has
        quietly stopped working and every row would cost a whole suite."""
        homeless = set()
        for source in sorted(REPO_ROOT.glob("tupferl/**/*.py")):
            path = source.relative_to(REPO_ROOT).as_posix()
            if mutants.mutable(path) and not self.targets(path):
                homeless.add(path)
        self.assertEqual(homeless, set(), "these no longer resolve to any test module")


@requires_git
class TestReadingARealDiff(unittest.TestCase):
    """A real `git init`, because that is what the repository does elsewhere.

    Plan §7.1 forbids mocking git and the rest of this suite obeys it --
    `tests/test_gitrepo.py` and every two-machine test drive the real binary.
    Asserting on a canned diff string is already covered above; this is about
    the argv.

    `requires_git` from `tests/support.py` rather than a local `shutil.which`,
    for the reason stated where it is defined: a check that has to grow later
    must not be updatable in one file and forgotten in the other. Without it
    `check=True` turns "this machine has no git" into an error rather than a
    skip. The cost is real and worth naming -- importing `support` enrols this
    module in the import index for everything `support` pulls in, so a mutation
    in `manifest` or `paths` now also runs this file. That is a fraction of a
    second a row, and these tests are among the cheapest in the suite.
    """

    def git(self, *argv: str) -> str:
        """`support.git`, bound to this fixture's root and sandbox environment.

        The one place this port is not mechanical. woswoar's helper is
        `git(root, *argv)` and hardens its configuration internally; tupferl's
        takes the environment explicitly, because `tests/support.py` builds a
        sandbox from *nothing* rather than from `os.environ` -- see its module
        docstring for why that direction is the safe one for a dotfiles
        manager. So the identity and the branch name come from `seed_home`,
        which is where every other fixture here gets them.
        """
        return support.git(list(argv), self.root, self.env)

    def write(self, name: str, body: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        home = Path(self._tmp.name).resolve()
        support.seed_home(home)
        self.env = support.sandbox_env(home)
        # The repository sits *below* the sandbox home rather than at it. A
        # `git add -A` at the home itself would take in `seed_home`'s own
        # `.gitconfig` and `.local/`, so the diff under test would carry files
        # the fixture created rather than the ones it wrote on purpose.
        self.root = home / "work"
        self.root.mkdir()

        self.git("init", "--initial-branch=main", "-q")
        self.write("tupferl/thing.py", "one = 1\ntwo = 2\nthree = 3\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "base")

    def test_it_sees_the_branch_and_the_working_tree_but_not_the_base(self) -> None:
        """The single most testable decision here: `--merge-base`, not two dots.

        A two-dot diff would include whatever landed on `main` after this branch
        started and generate mutants for it, labelled as if they belonged to the
        change under review.
        """
        self.git("checkout", "-qb", "work")
        self.write("tupferl/thing.py", "one = 1\ntwo = 22\nthree = 3\n")
        self.git("commit", "-qam", "on the branch")

        self.git("checkout", "-q", "main")
        self.write("tupferl/later.py", "later = 1\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "landed on main afterwards")

        self.git("checkout", "-q", "work")
        # Uncommitted on top, which is the situation the tool is used in.
        self.write("tupferl/thing.py", "one = 1\ntwo = 22\nthree = 33\n")

        found = mutants.changed_lines("main", self.root)
        self.assertEqual(found, {"tupferl/thing.py": {2, 3}})
        self.assertNotIn("tupferl/later.py", found)

    def test_a_base_that_does_not_exist_is_refused(self) -> None:
        """**A typo'd `--base` must not read as "nothing changed".**

        `_git` raises on a non-zero status, and without that check a failed
        `git diff` hands back its empty stdout -- so `--base mian` produces an
        empty table, the header says "0 file(s), 0 changed lines -> 0 mutants",
        and the sweep reports every row caught because there are none. That is
        this repository's flagship failure shape, reached by one keystroke.

        `rev-parse --verify` fires first so the message names the ref rather
        than the diff invocation it would otherwise fail inside.
        """
        with self.assertRaises(SystemExit) as raised:
            mutants.changed_lines("no-such-ref", self.root)
        self.assertIn("no-such-ref", str(raised.exception))

    def test_it_refuses_to_run_from_below_the_repository_root(self) -> None:
        """Paths in a diff are relative to the top level, so a run from a
        subdirectory reads every one of them against the wrong place -- and
        `mutable()` then rejects the lot, giving an empty table rather than an
        error."""
        below = self.root / "tupferl"
        with self.assertRaises(SystemExit) as raised:
            mutants.changed_lines("main", below)
        self.assertIn("repository root", str(raised.exception))

    def test_an_untracked_file_counts_entirely(self) -> None:
        """`git diff` cannot see it, and "no mutants for the new module" reads
        exactly like "the new module is covered"."""
        self.write("tupferl/fresh.py", "a = 1\nb = 2\n")
        self.assertEqual(mutants.changed_lines("main", self.root)["tupferl/fresh.py"], {1, 2})

    def test_a_change_outside_the_mutable_paths_is_ignored(self) -> None:
        self.write("tests/test_thing.py", "x = 1\n")
        self.write("docs/notes.md", "words\n")
        self.assertEqual(mutants.changed_lines("main", self.root), {})

    def test_a_file_that_is_not_python_under_a_mutable_path_is_ignored(self) -> None:
        """The test above cannot reach the `mutable(name)` guard: git's own
        pathspec already drops `tests/` and `docs/`, so the walk never sees
        those names. A `.md` *inside* `tupferl/` passes the pathspec and is
        stopped only here -- and without the guard the walk reads it and
        generates mutants for prose.
        """
        self.write("tupferl/NOTES.md", "words\n")
        self.assertEqual({}, mutants.changed_lines("main", self.root))

    def test_a_path_named_unmutable_is_skipped_even_though_it_qualifies(self) -> None:
        """`UNMUTABLE` is empty today, so nothing in the tree can exercise it --
        and the comment beside `every_line`'s copy of this guard records what
        that cost: it argued the check could never be false, which was true
        while `mutable` had two clauses and false the moment it grew a third.
        The excluded directory stayed in `--all` after being excluded, and
        nobody visited the line.

        Filling it here is the only way to drive the third clause. What it
        protects against is real: a script under `tools/` that built a sandbox
        and wrote a store into it, where one broken line sent the write to the
        developer's live installation (woswoar#245).
        """
        self.write("tupferl/keep.py", "a = 1\n")
        self.write("tupferl/danger.py", "b = 2\n")
        with mock.patch.object(mutants, "UNMUTABLE", ("tupferl/danger.py",)):
            found = mutants.changed_lines("main", self.root)
        self.assertIn("tupferl/keep.py", found)
        self.assertNotIn("tupferl/danger.py", found)


class TestABoundedCallStillReturns(unittest.TestCase):
    """Two pure functions that a one-line mutation turns into infinite loops.

    Everything else in this module is pure and sub-millisecond, and that is its
    virtue. These two spawn a child, which is the price of asking a question no
    in-process assertion can: *does this return at all?*

    A `support.deadline` would be cheaper and is the wrong instrument here. It
    arms `SIGALRM`, which is exactly what `tools/verdict.py` uses for its
    per-test bound, so a test that installed its own handler would displace the
    harness's for the rest of the run -- trading six unguarded lines for a
    silently disarmed alarm across the whole suite.

    Measured on this branch: ten mutants of `line_starts` and `cap` came back
    `BROKE` for want of these, and `BROKE` is never `caught`.

    **`cap`'s four convert; `line_starts`' six do not, and the reason is worth
    recording.** `TestChoosingTheTests.setUpClass` builds the real import index,
    which parses every file in the repository -- so a `line_starts` that never
    advances hangs *this module's own fixture* before any test in it runs. The
    per-test alarm cannot help: `setUpClass` is not a test, so the row comes
    back `TIMEOUT` at 300s rather than `BROKE` at 30. Verified by running that
    mutant against this selection. The test below is still correct and still the
    right shape -- it simply cannot be reached in the one run where it would
    matter, which is the harness-inside-the-harness limit issue #4 named.

    **The name buys the ordering.** `unittest` takes classes in `dir(module)`
    order, which is alphabetical, so this sorts before `TestCappingTheTable` --
    which calls `cap` in-process with no bound and, under the mutants that stop
    its loop terminating, hangs. With `failfast` (which every mutant run uses)
    the bounded call here fails first and the run ends before the unbounded one
    is reached. Measured: six of `TestCappingTheTable`'s eight tests never
    return under `len(kept) <= limit`.
    """

    #: `support.PATIENCE`, which *is* `bounded(5.0)` and whose docstring already
    #: covers this use -- "any call whose subject could loop for ever". Named
    #: here so the class reads, but never a second copy of the number.
    #:
    #: Five rather than the twenty it was: the honest call is a subprocess spawn
    #: and a few hundred microseconds of work, so twenty was four thousand times
    #: the wait it bounds -- and the bound is paid per test, under a sweep, on a
    #: machine already running thirty-eight lanes.
    BOUND = support.PATIENCE

    #: What the child may allocate. The mutants under test do not merely spin,
    #: they spin *while appending*, so an unbounded child takes memory from the
    #: whole machine for as long as `BOUND` allows -- and a sweep runs
    #: thirty-eight of these beside each other. Capped, the same mutant dies with
    #: `MemoryError` in milliseconds and the test fails on its own assertion
    #: rather than on a clock. Measured: `mutants.py:170` came back `TIMEOUT` at
    #: 300s without this, on a line that two other bounds already covered.
    #:
    #: Applied by calling `verdict.cap`, not by a second `setrlimit` here. That
    #: function owns every part of this that is easy to get wrong: it sets
    #: `RLIMIT_DATA` as well as `RLIMIT_AS` -- the one macOS is likelier to
    #: honour, so a hand-rolled copy is weakest on the very leg it apologises
    #: for -- it swallows a platform's refusal, and since the fix in this branch
    #: it lowers the hard half too. Where the platform declines, `BOUND` is
    #: still in force; the ceiling only makes the failure faster.
    CEILING = 512 << 20

    def returns(self, body: str) -> None:
        """Run `body` against the real module in a child, and insist it ends."""
        script = textwrap.dedent(
            """
            import sys
            sys.path.insert(0, {root!r})
            from tools.verdict import cap
            cap({ceiling})
            from tools import mutants
            from tools.mutants import Mutation
            {body}
            print("done")
            """
        ).format(
            root=str(REPO_ROOT),
            ceiling=self.CEILING,
            body=textwrap.dedent(body).strip(),
        )
        done = subprocess.run(
            [sys.executable, "-B", "-c", script],
            capture_output=True,
            text=True,
            timeout=self.BOUND,
        )
        self.assertEqual(0, done.returncode, done.stderr[-600:])
        self.assertEqual("done", done.stdout.strip())

    def test_line_starts_advances(self) -> None:
        """`at -= ...` never advances: a negative index wraps in Python rather
        than raising, so the `while` condition stays true -- and it loops while
        *appending*, which is the mutant `verdict.cap`'s docstring names as the
        reason that guard exists at all."""
        self.returns("mutants.line_starts('a' + chr(10) + 'bb' + chr(10) + 'ccc' + chr(10))")

    def test_cap_drains_its_queues(self) -> None:
        """The round-robin `while len(kept) < limit and any(queues.values())`.
        Four separate mutations stop it terminating."""
        self.returns(
            "rows = [Mutation('x', 'tupferl/m%d.py' % (i % 3), 'a', 'b', 't') "
            "for i in range(30)]\n"
            "kept, dropped = mutants.cap(rows, 7)\n"
            "assert len(kept) == 7, kept\n"
            "assert len(dropped) == 23, dropped"
        )


class TestWhichTestsARowRunsAgainst(unittest.TestCase):
    """`targets_for`: the named module, unioned with everything that imports it.

    Five of its seven mutants survived, all on lines the suite executes. It is
    a heuristic and allowed to be one -- the walk means a missed killer costs a
    longer run, never a wrong verdict -- but `.startswith(...)` accepting
    everything would silently change what `WHOLE_SUITE` means, and an empty
    answer is what makes a row run the whole suite.

    A tree of its own, because the answer depends on which files exist beside
    the one being asked about.
    """

    def tree(self, *tests: str) -> Path:
        box = Path(tempfile.mkdtemp(prefix="tupferl-targets-"))
        self.addCleanup(shutil.rmtree, box, True)
        (box / "tupferl").mkdir()
        (box / "tests").mkdir()
        (box / "tupferl" / "widget.py").write_text("x = 1\n", encoding="utf-8")
        for name in tests:
            (box / "tests" / name).write_text("import unittest\n", encoding="utf-8")
        return box

    def test_the_module_named_after_the_file_is_found(self) -> None:
        box = self.tree("test_widget.py")
        self.assertEqual("tests.test_widget", mutants.targets_for("tupferl/widget.py", box, {}))

    def test_an_aspect_module_is_found_too(self) -> None:
        """`test_<stem>_<aspect>.py` is the convention CLAUDE.md records, and
        `sync` has four such modules -- a rule that missed them would run the
        sync engine's rows against a fraction of their tests."""
        box = self.tree("test_widget.py", "test_widget_properties.py")
        found = mutants.targets_for("tupferl/widget.py", box, {}).split()
        self.assertEqual(["tests.test_widget", "tests.test_widget_properties"], found)

    def test_a_merely_similar_name_is_not_a_match(self) -> None:
        """The half that stops `startswith` from accepting everything.
        `test_widgets.py` is a different module, and matching it would make the
        selection quietly wrong for every file whose name is a prefix of
        another -- `copies` and `config`, `manage` and `manifest`."""
        box = self.tree("test_widget.py", "test_widgetry.py", "test_widgets.py")
        self.assertEqual("tests.test_widget", mutants.targets_for("tupferl/widget.py", box, {}))

    def test_what_imports_it_is_unioned_in(self) -> None:
        """Both halves. The named module alone misses a test that drives this
        code through something else, and the import closure alone misses the
        module named for it when nothing imports it directly."""
        box = self.tree("test_widget.py")
        index = {"tupferl.widget": {"tests.test_elsewhere"}}
        found = mutants.targets_for("tupferl/widget.py", box, index).split()
        self.assertEqual(["tests.test_elsewhere", "tests.test_widget"], found)

    def test_nothing_at_all_is_an_empty_answer(self) -> None:
        """Empty is what `mutate.WHOLE_SUITE` reads as "run everything". The
        unsafe answer would be to call it "no tests" and skip the row, which
        reads as coverage nobody has."""
        self.assertEqual("", mutants.targets_for("tupferl/widget.py", self.tree(), {}))


class TestCappingTheTable(unittest.TestCase):
    """`cap` is what `--limit` runs on, and the sweep found it almost unguarded:
    16 survivors and 4 BROKE across its fourteen lines, the largest cluster in
    either module.

    Its docstring names the reason it is not `[:limit]` -- that would "cover the
    alphabetically first file exhaustively and the largest one not at all, and
    the printed summary would not show it, because the count would be right
    either way". A property the count cannot reveal is exactly the kind that
    needs a test rather than a glance, and it had none.

    Every test here runs under a deadline, because `cap`'s round-robin is a
    `while` whose exit depends on the very comparison the sweep mutates: turning
    `len(kept) < limit` into `<=` leaves the outer loop true while the inner one
    appends nothing, so the queues never drain. That is a hang, and the harness
    files a hang as `BROKE` -- never `caught` -- so the loop bound was guarded by
    nothing. Armed for the whole test rather than around one call: every method
    below calls `cap`, and the mutation hangs whichever runs first.
    """

    def setUp(self) -> None:
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(support.deadline(support.PATIENCE, "cap never finished"))

    def rows(self, path: str, many: int) -> list[Mutation]:
        return [Mutation(f"{path}#{i}", path, "a", "b", "t", span=(i, i + 1)) for i in range(many)]

    def test_no_limit_keeps_everything(self) -> None:
        rows = self.rows("tupferl/a.py", 5)
        for limit in (0, -1):
            with self.subTest(limit=limit):
                kept, dropped = mutants.cap(rows, limit)
                self.assertEqual(rows, kept)
                self.assertEqual([], dropped)

    def test_a_limit_of_one_keeps_one(self) -> None:
        """`limit <= 0` is "no limit", and 1 is the smallest real one. Read as
        `<= 1`, a `--limit 1` returns the whole table -- so the run the flag was
        meant to keep short is the longest one there is, and the printed header
        says the honest number while the sweep ignores it.

        Every other test here uses 0, -1, 4 or 5, so the boundary sat between
        two cases and was covered by neither.
        """
        kept, dropped = mutants.cap(self.rows("tupferl/a.py", 5), 1)
        self.assertEqual(1, len(kept))
        self.assertEqual(4, len(dropped))

    def test_a_table_under_the_limit_is_untouched(self) -> None:
        """`len(mutations) <= limit`, where `<=` becoming `<` sends an
        exactly-sized table through the round-robin instead of returning it.

        The files are given out of order deliberately. With a single file the
        round-robin's output is identical to its input, so `<` survives -- which
        is what the first draft of this test measured. The reorder is only
        observable when more than one file is in play.
        """
        rows = [row for name in "ba" for row in self.rows(f"tupferl/{name}.py", 2)]
        for limit in (4, 5):
            with self.subTest(limit=limit):
                kept, dropped = mutants.cap(rows, limit)
                self.assertEqual(rows, kept, "an under-limit table was reordered")
                self.assertEqual([], dropped)

    def test_it_takes_from_every_file_rather_than_draining_the_first(self) -> None:
        """The whole argument for the function. Three files of ten, capped at
        six: two from each. `[:limit]` gives six from `a.py` and none from the
        others, and the *count* is six either way -- which is why this asserts
        the split and not the total.
        """
        rows = [row for name in "abc" for row in self.rows(f"tupferl/{name}.py", 10)]
        kept, dropped = mutants.cap(rows, 6)

        self.assertEqual(6, len(kept))
        self.assertEqual(24, len(dropped))
        taken = collections.Counter(row.path for row in kept)
        self.assertEqual({"tupferl/a.py": 2, "tupferl/b.py": 2, "tupferl/c.py": 2}, dict(taken))

    def test_an_uneven_cap_still_spreads_before_it_doubles_up(self) -> None:
        """Five across three files is 2/2/1, never 3/1/1: the round-robin takes
        one from each in turn. This is what `and` becoming `or` at the inner
        guard, and `<` becoming `<=`, each break in a different direction."""
        rows = [row for name in "abc" for row in self.rows(f"tupferl/{name}.py", 10)]
        kept, _ = mutants.cap(rows, 5)
        taken = sorted(collections.Counter(row.path for row in kept).values())
        self.assertEqual([1, 2, 2], taken)

    def test_a_file_running_dry_does_not_end_the_round(self) -> None:
        """`any(queues.values())`, which every other fixture here leaves
        unguarded because they all hold the *same* number of rows per file --
        CLAUDE.md §2's "two symmetric inputs", in its local spelling. With
        `all`, the loop stops the moment the smallest file drains:

        | | kept | dropped |
        |---|---|---|
        | `any` | 5 | 6 |
        | `all` | 2 | 9 |

        so `--limit 5` would quietly sweep two rows and report the right count
        for the wrong table.
        """
        rows = self.rows("tupferl/a.py", 1) + self.rows("tupferl/b.py", 10)
        kept, dropped = mutants.cap(rows, 5)
        self.assertEqual(5, len(kept), "the round ended when the first file ran dry")
        self.assertEqual(6, len(dropped))

    def test_the_visiting_order_does_not_depend_on_the_input_order(self) -> None:
        """`sorted(queues)`. A dict preserves insertion order, so without the
        sort the file that happened to appear first would be favoured -- and two
        machines building the same table from a different walk would cap it
        differently."""
        forward = [row for name in "abc" for row in self.rows(f"tupferl/{name}.py", 4)]
        backward = [row for name in "cba" for row in self.rows(f"tupferl/{name}.py", 4)]
        self.assertEqual(mutants.cap(forward, 4)[0], mutants.cap(backward, 4)[0])

        # And *which* order, not merely that the two agree. `sorted(queues,
        # reverse=True)` is a mutant the `order` operator really generates, and
        # it is just as input-independent as the right answer -- so the
        # assertion above holds against it. With a limit of 2 the round-robin
        # takes one row each from the first two files it visits, and which two
        # those are is the whole question.
        kept, _ = mutants.cap(forward, 2)
        self.assertEqual(["tupferl/a.py", "tupferl/b.py"], [row.path for row in kept])

    def test_what_is_kept_comes_back_in_file_and_span_order(self) -> None:
        """`kept.sort(key=(path, span or (0, 0)))`. The round-robin builds the
        list interleaved -- a.py, b.py, a.py, b.py -- and running a sweep in that
        order would jump between files for no reason a reader could see."""
        rows = [row for name in "ba" for row in self.rows(f"tupferl/{name}.py", 3)]
        kept, _ = mutants.cap(rows, 4)
        self.assertEqual(sorted(kept, key=lambda row: (row.path, row.span)), kept)

    def test_a_row_with_no_span_sorts_first_rather_than_raising(self) -> None:
        """`span or (0, 0)` -- a hand-written row has no span, and `None` is not
        comparable with a tuple. The fallback is what stops a mixed table
        raising `TypeError` in the middle of a sweep."""
        rows = [
            Mutation("hand-written", "tupferl/a.py", "a", "b", "t"),
            *self.rows("tupferl/a.py", 3),
        ]
        kept, _ = mutants.cap(rows, 2)
        self.assertEqual("hand-written", kept[0].label)

    def test_nothing_is_lost_between_the_two_halves(self) -> None:
        """Every row comes back exactly once, in one list or the other."""
        rows = [row for name in "abc" for row in self.rows(f"tupferl/{name}.py", 7)]
        kept, dropped = mutants.cap(rows, 8)
        self.assertEqual(sorted(rows), sorted(kept + dropped))


if __name__ == "__main__":
    unittest.main()
