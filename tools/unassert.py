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

    One scanner for all three readers below, because "where does a string end"
    is the part that is easy to get subtly wrong and there should be exactly one
    answer to it in this file.
    """
    depth, quote, i = 0, "", start
    while i < len(text):
        char = text[i]
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


def bracket(expr: str) -> str:
    """`expr`, parenthesised only if `not` would otherwise bind too tightly.

    `not a.b(c)` is the same as `not (a.b(c))` and reads better; `not a == b` is
    *not* the same as `not (a == b)`, so anything with a top-level space keeps
    its brackets. Conservative in the safe direction: an unnecessary pair of
    parentheses is noise, a missing pair is a changed assertion.
    """
    for i, char, depth in _scan(expr):
        if char == " " and depth == 0 and i:
            return f"({expr})"
    return expr


#: name -> (how many arguments it takes, how to spell the assertion).
#: A trailing extra argument is `unittest`'s message and becomes `assert x, msg`.
FORMS: dict[str, tuple[int, Callable[[list[str]], str]]] = {
    "assertEqual": (2, lambda a: f"{a[1]} == {a[0]}"),
    "assertNotEqual": (2, lambda a: f"{a[1]} != {a[0]}"),
    "assertIs": (2, lambda a: f"{a[1]} is {a[0]}"),
    "assertIsNot": (2, lambda a: f"{a[1]} is not {a[0]}"),
    "assertIn": (2, lambda a: f"{a[0]} in {a[1]}"),
    "assertNotIn": (2, lambda a: f"{a[0]} not in {a[1]}"),
    "assertTrue": (1, lambda a: a[0]),
    "assertFalse": (1, lambda a: f"not {bracket(a[0])}"),
    "assertIsNone": (1, lambda a: f"{a[0]} is None"),
    "assertIsNotNone": (1, lambda a: f"{a[0]} is not None"),
    "assertLess": (2, lambda a: f"{a[0]} < {a[1]}"),
    "assertGreater": (2, lambda a: f"{a[0]} > {a[1]}"),
    "assertLessEqual": (2, lambda a: f"{a[0]} <= {a[1]}"),
    "assertGreaterEqual": (2, lambda a: f"{a[0]} >= {a[1]}"),
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
        found = CALL.search(text)
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
            args = [flatten(a) for a in split_args(text[open_at + 1 : end])]
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
