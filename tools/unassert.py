"""Rewrite `self.assertX(...)` calls as plain `assert` statements.

The mechanical half of a `unittest`-to-pytest conversion, and it exists because
cluster B3 had **506 of them across five modules** -- past what is honest to
call hand-editing, and CLAUDE.md forbids sed over the real tree for reasons that
are all still true. Clusters B4a, B4b, B5 and B6 have more.

**Why this is not the sed the rules forbid**, which is the whole argument for
its shape:

- **It finds a call by scanning balanced parentheses, not by matching a
  regex.** `self.assertEqual(f(a, b), c)` has a nested call and a comma inside
  it; no regular expression can split that correctly, and one that looks like it
  does is the dangerous kind. `close` and `split_args` track bracket depth and
  string state, including triple quotes and backslash escapes.
- **It refuses what it does not recognise and says so**, rather than guessing.
  An unknown method, or a known one with the wrong number of arguments, is left
  exactly as it was and reported. What is left still reads `self.assert...`,
  which in a converted class is an `AttributeError` at run time -- so the file
  cannot silently half-convert, and that property is what `test_unassert.py`
  asserts rather than describing.
- **It is not the evidence.** The caller reads the whole diff and runs the
  suite. Every defect B3 found came from a step this tool cannot check: a
  fixture parameter attached to the wrong test, a rename reaching inside the
  fixture class, prose matched as if it were code. The tool being right is
  necessary and nowhere near sufficient.

**Argument order is `actual == expected`**, which is what B1 and B2 settled on
and the order pytest's own assertion rewriting reads best in. `assertEqual` and
`assertIs` are symmetric so this is presentation only; `assertIn`,
`assertGreater` and `assertLess` are **not**, and their operands keep their
positions.

**That is an assumption about the file, not a fact, and B5 is the cluster where
it was false.** The flip reads `assertEqual(expected, actual)` -- the repository's
own convention -- and emits `actual == expected`. `tests/test_reached.py` and
`tests/test_watch.py` were ported from `martinus/woswoar`, which writes
`assertEqual(actual, expected)`, so the output was yoda: `assert 1 == split.total`.
Nothing is *wrong* -- `==` is symmetric and no assertion changed meaning -- and
`ruff --fix` (SIM300) put 27 of them back in `test_reached.py` alone. But it
does not put all of them back: a dict, set or list literal on the left is not a
SIM300 constant, so four survived and had to be flipped by hand. **Check which
convention the file uses before reading the diff**, and expect a ruff pass and a
hand pass rather than one.

`assertRaises` is deliberately absent from `FORMS`. It is a context manager
rather than a call, its replacement takes a different shape
(`pytest.raises(...) as caught`, then `caught.value`), and a wrong guess there
changes what a test asserts. It is reported and left for a person.

**This tool dies with Phase C**, along with `tools/verdict_unittest.py`. It is
here rather than in a scratch directory because CLAUDE.md §7 says so in as many
words: a note that "lives on one machine, under one tool, for one person" is
worse than not having written it. The realistic alternative was not that B4a
reuses a file in `/tmp` -- it is that B4a writes this again, with a fresh set of
the same four mistakes.
"""

from __future__ import annotations

import argparse
import ast
import re
from collections.abc import Callable, Iterator
from pathlib import Path

#: A call this tool might rewrite. The `(` is part of the match so that the scan
#: below starts at a known bracket.
CALL = re.compile(r"self\.(assert[A-Za-z]+)\(")

#: What a refused call is renamed to while the rest of the file is scanned, so
#: the same call is not found again on the next pass. Put back before writing.
PARKED = "\x00SELF\x00."


def _ends(text: str, start: int) -> tuple[str, int]:
    """The quote that opens at `start`, if any, and where it ends."""
    for quote in ('"""', "'''", '"', "'"):
        if text.startswith(quote, start):
            return quote, start + len(quote)
    return "", start


def _scan(text: str, start: int = 0) -> Iterator[tuple[int, str, int]]:
    """Walk `text` from `start`, yielding (index, char, depth) outside strings.

    One scanner for all five readers below -- `called`, `close`, `split_args`,
    `flatten` and `convert`'s comment check -- because "where does a string end"
    is the part that is easy to get subtly wrong and there should be exactly one
    answer to it in this file.

    **Comments are skipped, and leaving them in was this scanner's own version
    of the bug `flatten` was fixed for.** A `#` comment holding an odd number of
    quote characters -- "the suite's", which is how English is written -- opened
    a string that never closed, and from there every judgement about inside and
    outside was inverted. Measured on `tests/test_verdict_unittest.py`:
    **12,379 characters of real string content** were reported as code, which is
    how the finder came to match a `self.assertEqual(1, 2)` written inside a
    triple-quoted probe module.

    The `#` itself *is* yielded, then the rest of its line is skipped. `close`
    and `split_args` ignore it, which is what makes a bracket or a comma inside
    a comment stop counting; `convert` reads it as a reason to refuse, because a
    comment cannot survive being flattened onto one line.

    **Skipped with a flag and `i += 1`, the way a string already is, rather than
    by `find`ing the newline.** `str.find` answers `-1` for "not there", and a
    comment on the last line of a file with no trailing newline is exactly that
    case -- so any mutation mishandling the sentinel assigned `i = -1` and the
    loop ran backwards for ever. The mutation sweep found it as a `BROKE` row on
    a fixture whose comment has no newline after it, which is CLAUDE.md's
    `RLIM_INFINITY` lesson in a second spelling: a sentinel is not a number.
    Here `i` only ever increases, so a hang is not reachable by any mutation of
    this arm.
    """
    depth, quote, comment, i = 0, "", False, start
    while i < len(text):
        char = text[i]
        if comment:
            comment = char != "\n"
            i += 1
            continue
        if quote:
            if char == "\\":
                i += 2
                continue
            if text.startswith(quote, i):
                i += len(quote)
                quote = ""
                continue
            i += 1
            continue
        opened, after = _ends(text, i)
        if opened:
            quote, i = opened, after
            continue
        if char == "#":
            yield i, char, depth
            comment = True
            # survivor: off-by-one -- equivalent: with `comment` already set, not
            #   advancing means the next iteration re-reads this same `#` in the
            #   arm above, which sets `comment` to the same value and advances by
            #   one. One wasted iteration, identical output. The other five
            #   mutations of these two lines are caught.
            i += 1
            continue
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        yield i, char, depth
        i += 1


def close(text: str, open_at: int) -> int:
    """Index of the `)` matching the `(` at `open_at`.

    Raises `ValueError` on an unbalanced expression rather than returning a
    plausible-looking index, because every caller is about to slice on it.
    """
    for i, char, depth in _scan(text, open_at):
        if char in ")]}" and depth == 0:
            return i
    raise ValueError(f"unbalanced from {open_at}")


def split_args(text: str) -> list[str]:
    """`text` split at top-level commas, respecting brackets and strings."""
    out: list[str] = []
    start = 0
    for i, char, depth in _scan(text):
        if char == "," and depth == 0:
            out.append(text[start:i].strip())
            start = i + 1
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def flatten(expr: str) -> str:
    """`expr` on one line, collapsing runs of whitespace **outside strings only**.

    A multi-line argument has to become one line, or the rewritten `assert`
    carries the old call's indentation into the middle of an expression. The
    obvious spelling -- `" ".join(expr.split())` -- was what this did, and it
    reached inside string literals: `assertIn("host  .gitconfig", out)` came back
    asserting `"host .gitconfig"`, one space, against output that has two.

    That is the silent kind. The rewritten line reads correctly, `ruff` and
    `mypy` are happy, and the test fails later for a reason that looks like a
    bug in the code under test -- which is how it was found, three files into
    cluster B4a. Nothing about the tool's refusal machinery could have caught
    it: the call *was* recognised, and it was rewritten into a different claim.
    """
    out: list[str] = []
    seen = 0
    space = False
    for i, char, _ in _scan(expr):
        if i > seen:
            # `_scan` skips over a string literal without yielding, so the gap
            # in the indices *is* the literal. Kept exactly as it was written.
            out.append(expr[seen:i])
            space = False
        seen = i + 1
        if char.isspace():
            if not space:
                out.append(" ")
                space = True
            continue
        out.append(char)
        space = False
    out.append(expr[seen:])
    return "".join(out).strip()


def called(text: str) -> re.Match[str] | None:
    """The first `self.assertX(` in `text` that is **not inside a string**.

    `CALL.search` alone was the fourth reader of this file that did not know a
    literal from code, and the one the module docstring above claims does:
    "it finds a call by scanning balanced parentheses, not by matching a regex"
    was true of `close`, `split_args` and `flatten`, and never of the finder.

    Measured across `tests/`, converting each module in memory and comparing its
    string constants before and against after: it would have rewritten **48
    literals in five modules** -- 3 in `test_mutate.py`, 6 in `test_run_tests.py`,
    4 each in `test_verdict.py` and `test_verdict_unittest.py`, 31 in this
    tool's own tests. Every one is the source of a *probe module*, written as a
    literal precisely so that the harness can drive a `unittest`-style test; the
    modules holding them are what cluster B6 converts.
    """
    for i, char, _ in _scan(text):
        if char == "s" and (found := CALL.match(text, i)):
            return found
    return None


#: Node kinds that bind looser than the operators `FORMS` splices, so an
#: argument whose own top level is one of them has to keep its brackets.
#: `Compare` is the one that matters most and is easiest to miss:
#: `assertEqual(a == b, c)` spliced bare is `c == a == b`, a *chained*
#: comparison and a different assertion.
LOOSE = (ast.BoolOp, ast.IfExp, ast.Lambda, ast.NamedExpr, ast.Compare, ast.Starred)


def bracket(expr: str) -> str:
    """`expr`, parenthesised only where an operator spliced around it would win.

    `not a.b(c)` is the same as `not (a.b(c))` and reads better; `not a == b` is
    *not* the same as `not (a == b)`. The rule used to be "anything with a
    top-level space", which is right for `not` and was wired to `assertFalse`
    alone -- so the other fourteen forms spliced raw text around `==`, `in`,
    `<` and friends with nothing checking what it bound like.

    Asking `ast` what the argument's own top-level node is answers both, and
    answers `not found['x']` correctly where the space heuristic did so by
    accident. An expression that will not parse keeps its brackets:
    conservative in the safe direction, because an unnecessary pair is noise
    and a missing pair is a changed assertion.
    """
    try:
        top = ast.parse(expr, mode="eval").body
    except SyntaxError:
        return f"({expr})"
    return f"({expr})" if isinstance(top, LOOSE) else expr


#: name -> (how many arguments it takes, how to spell the assertion).
#: A trailing extra argument is `unittest`'s message and becomes `assert x, msg`.
FORMS: dict[str, tuple[int, Callable[[list[str]], str]]] = {
    "assertEqual": (2, lambda a: f"{bracket(a[1])} == {bracket(a[0])}"),
    "assertNotEqual": (2, lambda a: f"{bracket(a[1])} != {bracket(a[0])}"),
    "assertIs": (2, lambda a: f"{bracket(a[1])} is {bracket(a[0])}"),
    "assertIsNot": (2, lambda a: f"{bracket(a[1])} is not {bracket(a[0])}"),
    "assertIn": (2, lambda a: f"{bracket(a[0])} in {bracket(a[1])}"),
    "assertNotIn": (2, lambda a: f"{bracket(a[0])} not in {bracket(a[1])}"),
    # `assertTrue` splices nothing around its argument, so anything binds.
    "assertTrue": (1, lambda a: a[0]),
    "assertFalse": (1, lambda a: f"not {bracket(a[0])}"),
    "assertIsNone": (1, lambda a: f"{bracket(a[0])} is None"),
    "assertIsNotNone": (1, lambda a: f"{bracket(a[0])} is not None"),
    "assertLess": (2, lambda a: f"{bracket(a[0])} < {bracket(a[1])}"),
    "assertGreater": (2, lambda a: f"{bracket(a[0])} > {bracket(a[1])}"),
    "assertLessEqual": (2, lambda a: f"{bracket(a[0])} <= {bracket(a[1])}"),
    "assertGreaterEqual": (2, lambda a: f"{bracket(a[0])} >= {bracket(a[1])}"),
    # A call's arguments are already delimited by its own brackets.
    "assertIsInstance": (2, lambda a: f"isinstance({a[0]}, {a[1]})"),
}


def convert(text: str) -> tuple[str, list[str]]:
    """`text` with every recognised assertion rewritten, and what was refused.

    The refused calls come back **unchanged** in the returned text, still
    spelled `self.assert...`. That is the safety property: a partial conversion
    is visible and does not run.
    """
    refused: list[str] = []
    while True:
        found = called(text)
        if not found:
            return text.replace(PARKED, "self."), refused

        name, at = found.group(1), found.start()
        open_at = found.end() - 1
        why = ""
        try:
            end = close(text, open_at)
        except ValueError:
            why, end = "unbalanced parentheses", -1
        form = FORMS.get(name)
        if not why and form is None:
            why = "no rule for it"
        args: list[str] = []
        if not why:
            inside = text[open_at + 1 : end]
            if any(char == "#" for _, char, _ in _scan(inside)):
                # A comment cannot be flattened onto one line, and dropping it
                # would delete something a person wrote. Left for that person.
                why = "a comment inside the call"
            else:
                args = [flatten(a) for a in split_args(inside)]
                assert form is not None
                if not form[0] <= len(args) <= form[0] + 1:
                    why = f"takes {form[0]}, found {len(args)}"
        if why:
            refused.append(f"{name}: {why}")
            text = text[:at] + text[at:].replace("self.", PARKED, 1)
            continue
        assert form is not None
        arity, build = form
        message = f", {args[arity]}" if len(args) > arity else ""
        text = text[:at] + f"assert {build(args[:arity])}{message}" + text[end + 1 :]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="say what would change")
    args = parser.parse_args(argv)

    for path in args.paths:
        done, refused = convert(path.read_text(encoding="utf-8"))
        if not args.dry_run:
            path.write_text(done, encoding="utf-8")
        left = done.count("self.assert")
        print(f"{path}: {left} left for a person")
        for note in refused:
            print(f"    {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
