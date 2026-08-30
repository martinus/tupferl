"""Plan §5's rule about error messages, as a check rather than as a habit.

    every error message states what happened and what the user can do next.
    One sentence each.

That is a property of *text*, and text is the thing no type checker looks at.
It held here by convention for five milestones and then did not: four messages
had drifted to "what happened" alone, each one a `git` failure reported with
git's own words and nothing after them. A user meeting
`could not stage .bashrc in /home/me/...: fatal: ...` has been told what broke
and left to guess what to try.

**The check is structural, because "is this actionable?" is not decidable and
"does it have two halves?" is.** Every message in this program is written as
`what happened; what to do next.` -- one semicolon, one full stop, one sentence.
Those three are mechanical, and measured against the tree as it stood they
identified *exactly* the four defective messages and no others: 39 of 43 had all
three, and the four that lacked a semicolon were the four with no next step. A
proxy that agreed with the real property on every instance available is the
strongest form this check can take.

What it deliberately does not cover:

- **`why` arguments to `sync.undone`.** That function appends its own clause, so
  the message a user sees is the caller's half plus "-- run `tupferl doctor`,
  then sync again." The composed sentence is what matters and the `raise` that
  builds it is scanned; the fragments handed in are not.
- **Anything not raised as a `TupferlError`.** `manifest.Refused.why` and
  `sync.Outcome.why` are sentences a user reads, printed rather than raised.
  They pass through the messages above -- "skipped {path}: {why}" -- where the
  surrounding line carries the shape. A rule for them would be a different rule.
- **Whether the advice is any *good*.** Nothing mechanical can say that. This
  catches the message that does not try.

**Each message is its own test**, parametrized over the scan, rather than one
test looping over all of them. A failure then names the offending message in
its own nodeid instead of in a `subTest` label, and the list is built once at
import rather than once per test. The list being non-empty is the precondition
`test_the_scan_found_them` states, and it is stated separately because a
parametrize over an empty list is *zero tests that all pass* -- §2's
zero-iteration trap, moved from run time to collection time and no less silent
for it.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

#: The package, found from this file rather than from the working directory.
#: Both would work under `tools/run_tests.py` and under `tools/mutate.py`, which
#: run with a tree root as their cwd -- but a test that reads a *tree* rather
#: than the tree it was loaded from is one that passes when pointed at the wrong
#: one, and CLAUDE.md §8 collects what that costs. `python -m pytest` given this
#: file's absolute path, from another directory, is the case that shows the
#: difference.
PACKAGE = Path(__file__).resolve().parent.parent / "tupferl"

#: The fewest messages the scan may find and still be believed. A walk that
#: matched nothing would satisfy every assertion below vacuously, which is the
#: failure mode CLAUDE.md §8 collects instances of. 43 exist as this is written;
#: the floor sits just under, so it does not need editing every time a command
#: gains a failure -- but a refactor that hid half of them from the scan trips
#: it, which is what it is for.
FLOOR = 40


class Message(NamedTuple):
    """One `raise TupferlError(...)`, as the text a reader would see."""

    module: str
    line: int
    #: The static template, with every `{...}` substitution replaced by `{}`.
    #: The template rather than a rendered message, because what is asserted is
    #: what the *author* wrote: a value interpolated at runtime cannot be made
    #: to say what to do next, and a rule that let one try would be satisfied by
    #: a `git` error that happened to contain a semicolon.
    text: str


def spelled(node: ast.expr) -> list[str]:
    """Every string `node` can evaluate to, or `[]` if that cannot be read.

    A list because one `raise` can carry two messages: `manage.remove` picks
    between an overlay wording and a shared one with a conditional expression,
    and checking only the first would leave the other unguarded.

    Adjacent string literals are concatenated by the parser into a single
    `JoinedStr`, so implicit concatenation across lines -- which nearly every
    message in this program uses -- needs no handling here.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        parts = [part.value if isinstance(part, ast.Constant) else "{}" for part in node.values]
        return ["".join(str(part) for part in parts)]
    if isinstance(node, ast.IfExp):
        return spelled(node.body) + spelled(node.orelse)
    return []


def raised(source: str, module: str) -> list[Message]:
    """Every `raise TupferlError(...)` in `source`.

    Matched on the name at the `raise`, which is how every one of them is
    written. A message built elsewhere and raised through a variable would be
    invisible here -- and would arrive as an unreadable argument, which
    `scan_package` refuses rather than skips.
    """
    found: list[Message] = []
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call)):
            continue
        called = node.exc.func
        name = getattr(called, "id", None) or getattr(called, "attr", None)
        if name != "TupferlError":
            continue
        if not node.exc.args:
            # `TupferlError()` with no message. Nothing to check and nothing to
            # read, so it is reported as a message with no text rather than
            # skipped -- the assertions below then fail on it, which is right.
            found.append(Message(module, node.lineno, ""))
            continue
        for text in spelled(node.exc.args[0]) or [""]:
            found.append(Message(module, node.lineno, text))
    return found


def messages() -> list[Message]:
    """Every message the package raises, read out of the source."""
    found: list[Message] = []
    for path in sorted(PACKAGE.glob("*.py")):
        found.extend(raised(path.read_text(encoding="utf-8"), path.name))
    return found


#: Read once, at import, so the two per-message tests below are parametrized
#: over the same list rather than each rebuilding it. `test_the_scan_found_them`
#: is what says this is not empty.
FOUND = messages()


def named(message: Message) -> str:
    """A parametrize id that reads like the `subTest` label this replaced."""
    return f"{message.module}:{message.line}"


class TestEveryErrorSaysWhatToDoNext:
    """Plan §5's two halves and one sentence, over the whole package."""

    def test_the_scan_found_them(self) -> None:
        """The precondition for every assertion below.

        Without it a walk that matched nothing passes the other three, and a
        green run would mean "this file no longer reads the package" rather than
        "the package is fine". Under parametrize it does more than that: an
        empty `FOUND` collects *no* cases at all, so the two tests below would
        not fail, they would cease to exist -- and a suite that lost two tests
        reports the same green as one that ran them. The module count is
        asserted too: one file holding all of them would mean the glob had
        collapsed.
        """
        assert len(FOUND) >= FLOOR, FOUND
        assert len({message.module for message in FOUND}) >= 5

    @pytest.mark.parametrize("message", FOUND, ids=named)
    def test_each_has_two_halves(self, message: Message) -> None:
        """`what happened; what to do next.` -- the semicolon is the seam.

        This is the assertion that four real messages failed. Each of them
        ended at git's own words, which say what broke and never what to try.
        """
        assert "; " in message.text, message.text
        assert message.text.rsplit(";", 1)[1].strip(), message.text

    @pytest.mark.parametrize("message", FOUND, ids=named)
    def test_each_is_one_finished_sentence(self, message: Message) -> None:
        """Plan §5 says one sentence, so: it ends, and it ends once.

        Ending in a full stop also rules out a message that trails off in an
        interpolated value -- a `git` error, a path -- which is the shape "what
        happened" alone always takes.
        """
        assert message.text.endswith("."), message.text
        assert ". " not in message.text, message.text


class TestTheScanCanFail:
    """The other direction, and the reason the class above is worth having.

    Every assertion up there is of the form "no message is bad". CLAUDE.md §2
    calls that a negative assertion whose precondition was never established:
    it is equally satisfied by a package with no messages, a scanner that reads
    the wrong directory, and a predicate that is true of every string. Each
    fixture below is a message the real predicates must reject.
    """

    def check(self, source: str) -> list[str]:
        """The texts `raised` finds in `source`, under the module name `x.py`."""
        return [message.text for message in raised(source, "x.py")]

    def test_a_message_with_no_next_step_has_no_semicolon(self) -> None:
        (text,) = self.check('raise TupferlError(f"could not stage {name}: {why}")')
        assert "; " not in text

    def test_a_message_that_trails_off_in_a_value_has_no_full_stop(self) -> None:
        (text,) = self.check('raise TupferlError(f"could not commit; git said {why}")')
        assert not text.endswith(".")

    def test_two_sentences_are_visible_as_two(self) -> None:
        text = self.check('raise TupferlError("it broke; try again. Sorry.")')[0]
        assert ". " in text

    def test_both_arms_of_a_conditional_message_are_read(self) -> None:
        """`manage.remove`'s shape. Checking one arm and not the other is how a
        message goes unguarded while the line it is on looks covered."""
        texts = self.check('raise TupferlError(f"a; b." if flag else f"c: {value}")')
        assert texts == ["a; b.", "c: {}"]

    def test_a_message_with_no_text_at_all_is_reported(self) -> None:
        """Not skipped. A `raise TupferlError()` that the scan passed over
        silently would be the one error in the program with no message."""
        assert self.check("raise TupferlError()") == [""]

    def test_an_unreadable_message_is_reported_as_empty(self) -> None:
        """A message built elsewhere and raised through a name. It cannot be
        read here, so it comes back empty and fails -- rather than counting as
        a message that passed."""
        assert self.check("raise TupferlError(built_elsewhere)") == [""]

    def test_other_exceptions_are_not_scanned(self) -> None:
        """`OSError` and friends are not the user's to act on -- see
        `tupferl/errors.py`. A scan that swept them in would fail on the first
        `raise ValueError("x")` in the package and say nothing true."""
        assert self.check('raise ValueError("no semicolon here")') == []
