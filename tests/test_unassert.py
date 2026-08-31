"""`tools/unassert.py`, and the one property that makes it safe to run.

**The property is not "it converts correctly".** A converter that got every
assertion right and one wrong would be worse than no converter, because the
wrong one is silent: a test that still passes while asserting something weaker
is exactly what CLAUDE.md §2 is about. What is asserted here instead is that it
**cannot silently half-convert** -- anything it does not recognise comes back
byte-for-byte and is reported, so the file it produces either runs or fails
loudly, and never quietly asserts less than it did.

The correctness half is checked by the caller running the suite, and B3's own
record is the evidence that this is the right division: all 506 of its
conversions were faithful, and every defect the review found came from a step
this tool cannot see.
"""

from __future__ import annotations

import pytest

from tools import unassert


def only(text: str) -> str:
    """`text` converted, asserting nothing was refused."""
    done, refused = unassert.convert(text)
    assert refused == [], refused
    return done


class TestWhatItRefuses:
    """The safety property, from both sides."""

    def test_an_unknown_method_comes_back_untouched(self) -> None:
        """Byte-for-byte, and reported. A converter that dropped or half-edited
        what it could not parse would leave a file nobody could review."""
        was = "        self.assertAlmostEqual(a, b)\n"
        done, refused = unassert.convert(was)
        assert done == was
        assert refused == ["assertAlmostEqual: no rule for it"]

    def test_a_known_method_with_the_wrong_arity_is_refused(self) -> None:
        """`assertRaises` as a context manager is the real instance: one
        argument where `FORMS` has none, so it is left for a person rather than
        guessed at."""
        was = "        with self.assertRaises(ValueError):\n"
        done, refused = unassert.convert(was)
        assert done == was
        assert refused == ["assertRaises: takes 2, found 1"] or refused == [
            "assertRaises: no rule for it"
        ]

    def test_what_is_refused_still_reads_self_assert(self) -> None:
        """Which is the whole point: in a converted class that is an
        `AttributeError` at run time, so a half-conversion cannot pass."""
        done, _ = unassert.convert("self.assertAlmostEqual(a, b)")
        assert "self.assert" in done

    def test_a_refusal_does_not_stop_the_rest(self) -> None:
        """The refused call is parked, not returned early -- otherwise one
        unknown method at the top of a file would silently convert nothing
        below it while reporting success."""
        done, refused = unassert.convert("self.assertAlmostEqual(a, b)\nself.assertEqual(1, x)\n")
        assert "assert x == 1" in done
        assert "self.assertAlmostEqual(a, b)" in done
        assert len(refused) == 1

    def test_nothing_to_do_is_not_an_error(self) -> None:
        """The precondition for reading the count above: a file with no
        assertions comes back unchanged with nothing reported."""
        assert unassert.convert("x = 1\n") == ("x = 1\n", [])


class TestItFindsTheWholeCall:
    """The reason this is not a regular expression."""

    def test_a_nested_call_with_its_own_comma(self) -> None:
        """`assertEqual(f(a, b), c)` has three top-level-looking commas and one
        real one. A regex splitting on `,` gets this wrong and looks right."""
        assert only("self.assertEqual(f(a, b), c)") == "assert c == f(a, b)"

    def test_a_comma_inside_a_string_is_not_an_argument_boundary(self) -> None:
        assert only('self.assertIn("a, b", text)') == 'assert "a, b" in text'

    def test_a_bracket_inside_a_string_does_not_move_the_depth(self) -> None:
        """The failure this prevents is worse than a wrong split: a mis-tracked
        depth ends the call at the wrong `)` and truncates the file."""
        assert only('self.assertEqual(")", closer)') == 'assert closer == ")"'

    def test_an_escaped_quote_does_not_end_the_string(self) -> None:
        assert only(r'self.assertEqual("a\"b", x)') == r'assert x == "a\"b"'

    def test_a_call_spread_over_several_lines_is_joined(self) -> None:
        """`ruff format` re-wraps afterwards; what matters is that the
        continuation lines are not left behind as text."""
        was = "self.assertEqual(\n    expected,\n    actual,\n)"
        assert only(was) == "assert actual == expected"

    def test_an_unbalanced_call_is_refused_rather_than_guessed(self) -> None:
        done, refused = unassert.convert("self.assertEqual(a, b")
        assert done == "self.assertEqual(a, b"
        assert refused == ["assertEqual: unbalanced parentheses"]


class TestWhitespaceInsideAStringSurvives:
    """The one defect this tool has actually shipped, and the shape of it.

    Joining a multi-line call onto one line was `" ".join(arg.split())`, which
    does not know a string literal from an expression. `assertIn("host  .gitconfig",
    out)` came back asserting one space against output that has two -- a
    *different claim*, spelled correctly, passing `ruff` and `mypy`, and failing
    later as though the code under test were wrong. It survived the whole
    refusal machinery because the call was recognised: nothing was refused, the
    rewrite was simply not equivalent.

    Found three files into cluster B4a by a test going red. One literal in nine
    converted modules had two spaces in it.
    """

    def test_two_spaces_in_a_literal_are_not_collapsed(self) -> None:
        was = 'self.assertIn("host  .gitconfig", out)'
        assert only(was) == 'assert "host  .gitconfig" in out'

    def test_a_newline_inside_a_triple_quoted_literal_survives(self) -> None:
        """The same hazard at its largest: a literal that *is* several lines."""
        was = 'self.assertEqual("""a\n  b""", x)'
        assert only(was) == 'assert x == """a\n  b"""'

    def test_the_expression_around_it_is_still_flattened(self) -> None:
        """The other half. A rewrite that stopped collapsing anything would pass
        the two tests above and leave the call's own indentation inside the
        `assert`, which is what the flattening is for."""
        was = 'self.assertEqual(\n    "a  b",\n    f(\n        x,\n    ),\n)'
        assert only(was) == 'assert f( x, ) == "a  b"'

    def test_a_literal_at_the_very_end_of_an_argument_is_kept(self) -> None:
        """The tail after the last character `_scan` yields, which is the branch
        an off-by-one in `flatten` would drop silently."""
        assert only('self.assertTrue(x == "a  b")') == 'assert x == "a  b"'


#: One case per entry in `unassert.FORMS`. Module-level so that
#: `test_every_form_has_a_case` reads the same table the parametrize runs over
#: rather than a second copy of it.
CASES = [
    ("self.assertEqual(1, x)", "assert x == 1"),
    ("self.assertNotEqual(1, x)", "assert x != 1"),
    ("self.assertIs(True, x)", "assert x is True"),
    ("self.assertIsNot(True, x)", "assert x is not True"),
    ("self.assertIn(a, b)", "assert a in b"),
    ("self.assertNotIn(a, b)", "assert a not in b"),
    ("self.assertTrue(x)", "assert x"),
    ("self.assertFalse(x)", "assert not x"),
    ("self.assertIsNone(x)", "assert x is None"),
    ("self.assertIsNotNone(x)", "assert x is not None"),
    ("self.assertLess(a, b)", "assert a < b"),
    ("self.assertGreater(a, b)", "assert a > b"),
    ("self.assertLessEqual(a, b)", "assert a <= b"),
    ("self.assertGreaterEqual(a, b)", "assert a >= b"),
    ("self.assertIsInstance(x, int)", "assert isinstance(x, int)"),
]


class TestTheSpellings:
    """One per entry in `FORMS`, because a wrong operand order is silent.

    The asymmetric ones are the point: `assertEqual` reversed is the same test,
    `assertIn` reversed is a different one.
    """

    @pytest.mark.parametrize(("was", "want"), CASES)
    def test_each_one(self, was: str, want: str) -> None:
        assert only(was) == want

    def test_every_form_has_a_case(self) -> None:
        """The precondition. A method added to `FORMS` and not to the table
        above would be unexercised, and the table is a literal so nothing else
        would notice."""
        covered = {was.split("(")[0].removeprefix("self.") for was, _ in CASES}
        assert covered == set(unassert.FORMS), covered ^ set(unassert.FORMS)

    def test_a_message_is_carried_across(self) -> None:
        assert only('self.assertEqual(1, x, "why")') == 'assert x == 1, "why"'

    def test_a_message_on_a_one_argument_form_is_carried_too(self) -> None:
        assert only('self.assertTrue(x, "why")') == 'assert x, "why"'


class TestItLooksOnlyAtCode:
    """The finder was the one reader that did not know a literal from code.

    The module docstring claims "it finds a call by scanning balanced
    parentheses, not by matching a regex". That was true of `close`,
    `split_args` and `flatten`, and never of `CALL.search`. Found by review,
    after the same class of bug had already shipped once in `flatten`.
    """

    def test_a_call_written_inside_a_string_is_left_alone(self) -> None:
        """The measured case: `tests/` holds five modules whose *probe sources*
        are triple-quoted `unittest` test modules, and they are what cluster B6
        converts. Rewriting one changes what the harness is being driven with.
        """
        quotes = chr(34) * 3
        was = (
            f"PROBE = {quotes}\nclass T(unittest.TestCase):\n    self.assertEqual(1, 2)\n{quotes}\n"
        )
        assert unassert.convert(was) == (was, [])

    def test_an_apostrophe_in_a_comment_does_not_open_a_string(self) -> None:
        """The root of it, and the reason the fix is in `_scan` rather than in
        the finder: a comment holding an odd number of quotes opened a string
        that never closed, and every judgement after it was inverted.

        Measured on `tests/test_verdict_unittest.py`: 12,379 characters of real
        string content read as code. Written here as the smallest fixture that
        reproduces it -- a comment, then a literal holding a call.
        """
        quotes = chr(39) * 3
        held = f"{quotes}self.assertEqual(1, 2){quotes}"
        was = f"# the suite's own\nP = {held}\nself.assertTrue(x)\n"
        done, refused = unassert.convert(was)
        assert refused == []
        assert held in done, done
        assert done.endswith("assert x\n"), done

    def test_a_comment_inside_the_call_is_refused_rather_than_flattened(self) -> None:
        """A comment cannot survive being joined onto one line, and dropping it
        would delete something a person wrote. So it is reported and left --
        which is the tool's whole safety property, reached by a new route."""
        was = "self.assertEqual(\n    expected,  # why\n    actual,\n)"
        done, refused = unassert.convert(was)
        assert done == was
        assert refused == ["assertEqual: a comment inside the call"]

    def test_a_comma_in_a_comment_does_not_split_the_arguments(self) -> None:
        """The same fix, seen through `split_args`. Before it, the comment's
        comma made this a three-argument call and the arity check refused it --
        for the wrong reason, which is a refusal that would have gone away the
        moment somebody deleted a comma from a comment."""
        was = "self.assertEqual(a, b)  # one, two, three"
        done, refused = unassert.convert(was)
        assert refused == []
        assert done == "assert b == a  # one, two, three"


class TestPrecedenceAroundEveryOperator:
    """`bracket` encoded the right rule and was wired to one of fifteen forms.

    `assertFalse` had it; the other fourteen spliced raw argument text around
    `==`, `in`, `<` and friends with nothing checking what it bound like. The
    rule is `ast`'s now, so it answers for all of them at once.
    """

    def test_a_comparison_argument_does_not_become_a_chained_comparison(self) -> None:
        """The worst of them, because the result is valid Python that means
        something else: `assertEqual(a == b, c)` spliced bare reads
        `c == a == b`, which is `c == a and a == b`."""
        assert only("self.assertEqual(a == b, c)") == "assert c == (a == b)"

    def test_an_or_argument_keeps_its_brackets(self) -> None:
        """`x == a or b` parses as `(x == a) or b`, which is true whenever `b`
        is -- an assertion that has stopped being able to fail."""
        assert only("self.assertEqual(a or b, x)") == "assert x == (a or b)"

    def test_a_conditional_argument_keeps_them_too(self) -> None:
        assert only("self.assertIn(a if c else d, b)") == "assert (a if c else d) in b"

    def test_an_ordinary_argument_gains_nothing(self) -> None:
        """The other half: brackets everywhere would be noise, and noise is what
        makes a reader stop seeing the ones that matter."""
        assert only("self.assertEqual(f(a, b), c.d)") == "assert c.d == f(a, b)"

    def test_a_call_form_needs_none_because_its_own_brackets_delimit(self) -> None:
        assert only("self.assertIsInstance(a or b, int)") == "assert isinstance(a or b, int)"

    def test_an_argument_that_will_not_parse_keeps_its_brackets(self) -> None:
        """The arm `ast` cannot answer for, driven directly because no whole
        call reaches it: every argument of a call that parses is an expression
        that parses. It is here because "cannot tell" and "needs none" are
        different answers and only one of them is safe."""
        assert unassert.bracket("a b") == "(a b)"
        assert unassert.bracket("=") == "(=)"


class TestWhereNotBinds:
    """`assertFalse` is the one rewrite that can change meaning by precedence."""

    def test_a_plain_call_needs_no_brackets(self) -> None:
        assert only("self.assertFalse(where.exists())") == "assert not where.exists()"

    def test_a_comparison_keeps_them(self) -> None:
        """`not a == b` is `(not a) == b`, which is a different assertion --
        and one that would often still pass."""
        assert only("self.assertFalse(a == b)") == "assert not (a == b)"

    def test_a_membership_test_keeps_them(self) -> None:
        assert only("self.assertFalse(a in b)") == "assert not (a in b)"

    def test_a_subscript_with_no_top_level_space_needs_none(self) -> None:
        assert only("self.assertFalse(found['x'])") == "assert not found['x']"
