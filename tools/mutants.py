"""What to change, and where the diff says it matters. `tools/mutate.py` runs it.

Ported from `martinus/woswoar` (Apache-2.0), where the evidence in these
docstrings was collected. Claims naming a file or a measurement say where it was
measured; `woswoar#48` is an issue in that repository, not in this one.

A hand-written table only asks the questions its author thought to ask, and
writing it is the work that remains after woswoar#48 removed the loop around it.
This reads `git diff`, generates mutants for the lines the change actually touched,
and hands them to the runner. The author stops writing the table and starts
reading the survivors.

Split from `mutate` along the line `tools/sandbox.py` and `compare.verdict`
already drew: the *generator* is pure -- `(source, changed lines) -> mutations`
-- so `generate`, the operators, `Offsets` and `label` are testable in
milliseconds with no sandbox, no subprocess and no suite. That is the split, and
it is worth stating exactly rather than generously: `changed_lines` forks `git`,
`check` reads the file it is asked about, and `importers` and `targets_for`
glob and read `tests/`. Four functions touch the world, not one. What none of
them does is run a suite or copy a tree, which is the cost the split exists to
keep out of these tests.

**Spans.** A generated mutant is routinely not unique in its file: `if not
path.exists():` appears many times. So a row carries the character offsets it
applies at, and `check` asserts the text *at* those offsets rather than counting
occurrences.

Be clear about what that assertion can and cannot do. `generate` derives `old`
*from* the span it just computed, so for a freshly generated row the check is
true by construction: it catches the file changing between `--list` and the run,
and nothing else. It is **not** a guard against a bad offset. Only tests are,
and there are two traps for them to cover -- `col_offset` is a UTF-8 *byte*
offset into its line, and a project whose CLI prints `✔` and `✘` -- which
`tupferl doctor` does -- has such lines, and lines must be counted the way the tokenizer
counts them rather than the way `str.splitlines` does. See `line_starts`.

**What no operator here can generate.** Some of the weak fixtures CLAUDE.md §2
lists map onto an operator exactly: `order` produces the reversed walk, `affix`
the suffix filter that accepts everything. No count is given, because the
mapping is not one-to-one and the count that used to stand here was wrong in
both directions.

The class that is definitely out of reach is *scope widening*: a search widened
from one block of a file to the whole of it changes which region it covers, and
no local AST rewrite produces that. That is why the spec-file
mode is not going away, and why "every generated mutant was caught" must not be
read as "the tests are good".
"""

from __future__ import annotations

import ast
import bisect
import copy
import io
import re
import subprocess
import tokenize
from collections.abc import Callable, Iterator, Mapping, Sequence
from difflib import SequenceMatcher
from itertools import groupby
from pathlib import Path
from typing import NamedTuple


class Mutation(NamedTuple):
    """One edit that some test is supposed to notice."""

    #: What the mutation does, in the words of whoever might reintroduce it.
    #: Printed, so make it a sentence a reader of the PR would understand.
    label: str
    path: str
    #: For a hand-written row, must appear exactly once in the file: ambiguity is
    #: an error rather than a guess, because replacing the wrong one of two
    #: matches quietly tests nothing. For a generated row `span` pins it instead.
    old: str
    new: str
    #: Whitespace-separated unittest targets, as `python -m unittest` takes them.
    tests: str
    #: Say so when the replacement is meant to *contain* the original -- an
    #: inserted call, an early return in front of code that stays. Otherwise
    #: `check` refuses that shape, because it is overwhelmingly a mistake.
    additive: bool = False
    #: Character offsets, when the text to replace is not unique. `old` must equal
    #: `text[span[0]:span[1]]`, which is a stronger claim than "appears once".
    span: tuple[int, int] | None = None
    #: Which operator produced this. Empty for a hand-written row.
    operator: str = ""
    #: Tests to run *before* `tests`: the one a previous sweep recorded as
    #: catching this mutation, or -- when none was -- a cheap high-yield prefix.
    #: Separate from `tests` and not folded into it,
    #: which was the first shape and cost twice the wall clock: `run` shards the
    #: baseline check by distinct `tests` string, so a killer prepended there
    #: made every row its own shard -- 1 baseline run became 42, each a full
    #: suite. See `mutate.Killers`.
    #:
    #: **A sequence, where `tests` beside it is one space-joined string, and the
    #: asymmetry is the point.** `tests` holds a *selection* -- dotted module and
    #: class paths built by `targets_for`, which cannot contain a space. This
    #: holds pytest *nodeids*, and a parametrized one can:
    #: `tests/test_errors.py::test_the_shape[tupferl/manage.py:41]` is ordinary,
    #: and `[not fine]` is what a two-word parameter gives. Space-joined, such an
    #: id is shredded into halves that select nothing -- and selecting nothing is
    #: not an error to pytest, so a baseline shard built from one comes back
    #: green having run no test at all. That is the failure this type prevents,
    #: and it fails in the flattering direction.
    #:
    #: The type carries the intent and cannot enforce it: `str` *is* a
    #: `Sequence[str]`, so the annotation accepts the one value it exists to
    #: forbid. `check` closes that half -- see the guard at the top of it.
    first: Sequence[str] = ()
    #: Whether `first` is the test recorded as catching *this* row, rather than
    #: the general cheap-yield prefix. The two are both "run these first" and
    #: are otherwise indistinguishable once written into one string, but they
    #: deserve opposite treatment against `mutate.Learned`: an exact killer
    #: belongs in front of it, a general prefix behind it. `mutate.Killers`
    #: sets this; nothing else does.
    exact: bool = False


#: Nodes that carry a source position. `ast.AST` does not -- only statements and
#: expressions do -- and every span in this module comes from one of these.
Positioned = ast.expr | ast.stmt


class Edit(NamedTuple):
    """One operator's answer about one node."""

    node: Positioned
    new: str
    #: The clause that goes into the printed label, after `-- `.
    prose: str


#: A line carrying this is left alone. `# pragma: no cover` means something else
#: and is deliberately not honoured: in the project this came from, half its
#: uses marked code that needs a hung subprocess to reach, which is exactly the
#: kind of guard worth mutating if you ever do reach it.
NO_MUTATE = re.compile(r"#\s*pragma:\s*no\s+mutate\b")

#: Only these are worth mutating. Never `tests/**`: breaking a test proves
#: nothing about the fix, and the run would report the assertion it removed.
MUTABLE = ("tupferl/", "tools/")

#: Never generated for, whatever `MUTABLE` says. Empty today, and kept because
#: of what filled it in woswoar: a script under `tools/` built a sandbox and
#: then *wrote a real store into it*, so a mutant that broke the one line
#: pointing it at that sandbox wrote into whatever the ambient environment
#: named -- which, for a sweep run from a developer's shell, was their live
#: installation. It did (woswoar#245).
#:
#: The rule that follows for this project: anything added under `tools/` that
#: writes outside a directory it created itself belongs in here, and belongs in
#: here *as well as* guarding itself. One mutation can break either lock.
UNMUTABLE: tuple[str, ...] = ()


def mutable(path: str) -> bool:
    # No `not path.startswith("tests/")`: `MUTABLE` cannot match a `tests/` path,
    # so the clause could never change the answer. Removed for the reason this
    # module gives twice elsewhere -- a guard nothing can reach is a guard
    # nobody can trust, and the test named for that clause was passing off the
    # tuple above rather than off the clause it appeared to be testing.
    return path.endswith(".py") and path.startswith(MUTABLE) and not path.startswith(UNMUTABLE)


#: A disposition written beside the code it excuses:
#: `# survivor: <operator>[, <operator>] -- <reason>`. Same shape as `NO_MUTATE`
#: one constant up, and read out of the same files for the same reason.
TAGGED = re.compile(r"#\s*survivor:\s*([\w\s,-]+?)\s*--\s*(\S.*?)\s*$")


class Tags:
    """Every `# survivor:` tag in one file, by the line it guards and operator.

    **An index, not a search.** The first version joined the comment block above
    a line into one string and ran one regex over it, which meant a *second* tag
    in that block was swallowed into the first one's reason -- so its rows stayed
    unread for ever and `--accept` stacked an identical `TODO` under them on
    every run. Reproduced: three runs, three copies, and the row still
    unexcused. That is the absorption failure the old hash record was replaced
    for, one layer down.

    **Built from `tokenize`, not from raw lines.** It is the exact answer to
    "is this a comment" -- a `# survivor:` inside a string literal is not one --
    and measured on `tools/mutate.py` it costs 5.5ms against `line_starts`' 9.0,
    so the precise answer is also the cheaper one. Nothing in this tree has such
    a string today; the point is that nothing has to keep checking.

    A tag on a comment-only line guards the next line that carries code, so a
    reason may sit above a long statement rather than trailing it. A tag after
    code guards that line. A block may hold several tags, and a tag may wrap:
    a line that begins a new `# survivor:` starts one, any other comment line
    continues the one before it.
    """

    def __init__(self, source: str) -> None:
        self._at: dict[int, dict[str, tuple[int, str]]] = {}
        lines = source.split("\n")
        self._opens = self._statements(source, len(lines))
        for guards, tag_line, text in self._blocks(source, lines):
            guards = self.statement(guards)
            found = TAGGED.search(text)
            # survivor: branch -- the tag block held no `# survivor:` at all -- an ordinary comment
            #   above a statement. Skipping it and indexing nothing are the same answer, since
            #   `excuse` asks by operator and this block names none.
            if not found:
                continue
            for word in found.group(1).split(","):
                if operator := word.strip():
                    self._at.setdefault(guards, {}).setdefault(operator, (tag_line, found.group(2)))

    @staticmethod
    def _statements(source: str, count: int) -> dict[int, int]:
        """`{line: the line its logical statement starts on}`, counting from 0.

        **A tag guards a statement, not a physical line**, and until this
        existed only the prose said so. `--accept` computed its insertion point
        from a mutation's span, which for anything inside brackets is a
        *continuation* line -- so a tag landed in the middle of a comprehension,
        split it, and left `ruff format --check` wanting to reflow the file.
        That is `--accept` handing back a tree that fails the preflight, on a
        flag whose whole purpose is to be run and reviewed.

        Both sides normalise through this, which is what keeps them agreeing:
        the writer inserts above the statement, and the reader looks the
        mutation's line up as the statement it belongs to.
        """
        opens: dict[int, int] = {}
        try:
            first = None
            # **`NEWLINE` ends a logical line; `NL` does not.** Inside brackets
            # every line break is an `NL`, so resetting on it made the map an
            # identity and the fix inert -- the first version did exactly that
            # and put a tag back inside the comprehension it was written for.
            skip = (tokenize.NL, tokenize.COMMENT, tokenize.INDENT, tokenize.DEDENT)
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.NEWLINE:
                    first = None
                    continue
                if token.type in skip or token.type == tokenize.ENDMARKER:
                    continue
                if first is None:
                    first = token.start[0] - 1
                for line in range(first, token.end[0]):
                    opens.setdefault(line, first)
        except (SyntaxError, tokenize.TokenError, IndentationError, ValueError):
            return {}
        return {line: opens.get(line, line) for line in range(count)}

    def statement(self, line: int) -> int:
        """The line `line`'s statement begins on -- itself, unless it continues one."""
        return self._opens.get(line, line)

    @staticmethod
    def _comments(source: str) -> dict[int, int]:
        """`{line index: column}` for every real comment, from `tokenize`."""
        found: dict[int, int] = {}
        try:
            for token in tokenize.generate_tokens(io.StringIO(source).readline):
                if token.type == tokenize.COMMENT:
                    found[token.start[0] - 1] = token.start[1]
        except (SyntaxError, tokenize.TokenError, IndentationError, ValueError):
            # A file this cannot read excuses nothing, which is the safe
            # direction: every row in it is reported rather than hidden.
            # survivor: return-value -- the `except` arm for a file `tokenize` cannot read. Every
            #   mutable file in this tree parses, and one that did not would fail the preflight long
            #   before a sweep. Returning the empty map excuses nothing, which is the safe direction
            #   the docstring above states.
            return {}
        return found

    #: How each line of a file reads to `_blocks`.
    _CODE, _BLANK, _COMMENT, _TRAILING = "code", "blank", "comment", "trailing"

    def _kinds(self, comments: Mapping[int, int], lines: Sequence[str]) -> list[str]:
        """One label per line, so the grouping below needs no index arithmetic."""
        found = []
        for at, line in enumerate(lines):
            col = comments.get(at)
            if col is None:
                found.append(self._CODE if line.strip() else self._BLANK)
            else:
                found.append(self._TRAILING if line[:col].strip() else self._COMMENT)
        return found

    def _blocks(self, source: str, lines: Sequence[str]) -> Iterator[tuple[int, int, str]]:
        """`(line guarded, line the tag is on, tag text)` for each tag.

        **No `while`, and that is deliberate.** The first version walked the file
        with two hand-rolled counters, and a mutation dropping either increment
        spun for ever -- seven rows came back `BROKE` on the sweep that followed,
        one of them `ran out of memory` because the loop appended while it spun.
        `BROKE` is never `caught`, so those lines were guarded by nothing. A
        `groupby` over labelled lines says the same thing with no counter to
        mutate, which removes the whole class rather than bounding the tests
        that trip over it.
        """
        comments = self._comments(source)
        kinds = self._kinds(comments, lines)
        for at, kind in enumerate(kinds):
            if kind == self._TRAILING:
                # After code: the tag guards the line it trails.
                # survivor: slice -- the slice starts at the `#`, and widening it to the whole line
                #   only feeds `TAGGED.search` more text before the same match -- the pattern is
                #   anchored on `# survivor:`, not on the start of the string.
                yield at, at, lines[at][comments[at] :]
        for commented, group in groupby(range(len(lines)), key=lambda n: kinds[n] == self._COMMENT):
            # survivor: branch -- `groupby` alternates, so the false half is every run of non-
            #   comment lines. Taking it would look for tags among code, where `_comments` has
            #   already established there are none.
            if not commented:
                continue
            block = list(group)
            # The line directly below the block, and **a blank line ends it**.
            # A comment separated from the code by one is a section header or a
            # note about what came before at least as often as it is about what
            # follows, and the safe direction for a record of dispositions is to
            # excuse nothing rather than the wrong thing. A block at the end of
            # a file guards nothing either.
            guards = block[-1] + 1
            # survivor: boundary, branch, connector -- equivalent, and every way round: dropping
            #   either half indexes the tag at a blank line or past the end of the file, and no
            #   mutation sits on either -- so the entry is never queried and the answer is
            #   unchanged. Kept because "a tag guards a statement" is the claim, and a guard that
            #   says so is worth more than one fixture could show.
            if guards >= len(kinds) or kinds[guards] != self._CODE:
                continue
            yield from self._split(guards, [(n, lines[n][comments[n] :]) for n in block])

    @staticmethod
    def _split(guards: int, block: Sequence[tuple[int, str]]) -> Iterator[tuple[int, int, str]]:
        """One block's comment lines, cut into tags.

        A line beginning a new `# survivor:` starts one; any other comment line
        continues the one before it, which is what lets a reason wrap. Several
        tags in a block is the case the first version could not represent at
        all -- it joined the block and ran one regex, so the second tag was
        swallowed into the first one's reason and its rows stayed unread.
        """
        started: int | None = None
        text = ""
        # survivor: off-by-one -- the sentinel that flushes the last tag of a block. Any negative
        #   line does it, because the only thing read from it is `line < 0`.
        for line, comment in [*block, (-1, "")]:
            # survivor: boundary, off-by-one -- the same sentinel, from the other side. `-1` is not
            #   a line any file has, so the comparison is a marker rather than a bound.
            if line < 0 or TAGGED.search(comment):
                # survivor: branch -- guards the flush against a block whose first comment line is
                #   not a tag -- a plain comment above a tagged statement. Without it the `None`
                #   start would be yielded and `excuse` would key a tag on nothing.
                if started is not None:
                    yield guards, started, text
                started, text = line, comment
            # survivor: branch -- the continuation arm, and the same guard: a comment line before
            #   the first `# survivor:` in a block belongs to no tag and is dropped.
            elif started is not None:
                text = f"{text.rstrip()} {comment.lstrip('#').strip()}"

    def excuse(self, line: int, operator: str) -> tuple[int, str] | None:
        """The tag guarding `line` for `operator`: where it sits, and why."""
        return self._at.get(line, {}).get(operator)

    def operators(self, line: int) -> set[str]:
        """Every operator already excused at `line`, so a writer need not repeat one."""
        return set(self._at.get(line, {}))


def line_starts(source: str) -> list[int]:
    """Where each line begins, counting lines the way the *tokenizer* does.

    Deliberately not `str.splitlines`, and this is the second trap in this
    module rather than a preference. `str.splitlines` also breaks on form feed,
    vertical tab, the file/group/record separators and U+2028/9; CPython's
    tokenizer treats a form feed as ordinary whitespace. A single `\f` -- the
    Emacs page separator, and legal Python -- therefore shifts every line number
    below it by one for `splitlines` while `ast` keeps counting from the real
    one, so every span below it lands on the wrong line.

    That produces the worst outcome this module has: an edit that still parses,
    in a different place from the one the row's label names, reported as `caught`
    or `SURVIVED` about a line nobody touched.
    """
    starts = [0]
    at = 0
    while at < len(source):
        if source[at] == "\r":
            at += 2 if source[at + 1 : at + 2] == "\n" else 1
            starts.append(at)
        elif source[at] == "\n":
            at += 1
            starts.append(at)
        else:
            at += 1
    return starts


class Offsets:
    """Character offsets for the (line, column) pairs `ast` reports.

    Exists for one reason, and it is not convenience. `col_offset` is a **UTF-8
    byte** offset into its line; `str` indexing is by character. Every line up to
    the first non-ASCII character in a file behaves identically under both, so
    the bug this prevents is invisible in any fixture written in plain English
    and appears only on the files that print things.
    """

    def __init__(self, source: str) -> None:
        self._source = source
        self._starts = line_starts(source)

    def lines(self) -> int:
        """How many lines the source has, counted the way `ast` counts them."""
        return len(self._starts)

    def line(self, lineno: int) -> str:
        start = self._starts[lineno - 1]
        end = self._starts[lineno] if lineno < len(self._starts) else len(self._source)
        return self._source[start:end]

    def line_of(self, offset: int) -> int:
        """Which line an offset falls on, counting from 0.

        The exact inverse of `at`, and written here beside it so the `- 1` on
        the bisect lives in one place: it was spelled twice in `tools/mutate.py`
        and the `- 1` is the whole correctness of it.
        """
        return bisect.bisect_right(self._starts, offset) - 1

    def at(self, lineno: int, col: int) -> int:
        line = self.line(lineno)
        return self._starts[lineno - 1] + len(line.encode("utf-8")[:col].decode("utf-8"))

    def span(self, node: Positioned) -> tuple[int, int]:
        assert node.end_lineno is not None and node.end_col_offset is not None
        return (
            self.at(node.lineno, node.col_offset),
            self.at(node.end_lineno, node.end_col_offset),
        )


def _trimmed(text: str, width: int) -> str:
    """`text` cut to `width` with an ellipsis. Prose for a label, never a rewrite."""
    # survivor: arith, boundary, off-by-one, slice -- prose for a label, and the docstring says so.
    #   Every mutant here moves where a sentence is cut for display -- a label a reader skims, never
    #   a decision. `_near_miss` below is the only thing that reads a label back, and it reads the
    #   *text to replace* rather than this.
    return text if len(text) <= width else text[: width - 1] + "\N{HORIZONTAL ELLIPSIS}"


def _rewritten(node: ast.AST, change: Callable[[ast.AST], None]) -> str:
    """`node` with `change` applied to a copy, unparsed and spliceable.

    An expression is parenthesised without exception, which removes every
    precedence question in one rule: a node's own span never includes
    surrounding parentheses, so a parenthesised replacement is legal in every
    position these operators fire in. A statement is not, because it cannot be
    -- `ast.unparse` emits no leading indentation and a statement's span begins
    after its indent, so splicing at that offset lines up.

    Which of the two is decided *here*, from the node, rather than by the caller
    choosing between two helpers. It was two helpers, and the choice was the
    caller's; the failure that shape invites is an expression spliced without
    parentheses, which is a precedence bug that still parses -- exactly what the
    parenthesise-always rule exists to abolish.

    Unparsing the *copy* rather than the module is what keeps the other ten
    thousand lines of the file byte-identical -- a reformatted sandbox would make
    the `additive` check meaningless and a survivor unreadable.
    """
    clone = copy.deepcopy(node)
    change(clone)
    text = ast.unparse(clone)
    return f"({text})" if isinstance(clone, ast.expr) else text


#: Comparison flips, split into two operators because they answer different
#: questions. `boundary` is the off-by-one -- the class a two-item fixture
#: cannot see. It is the class CLAUDE.md §2's weak-fixture list opens with: a
#: fixture whose winner leads on *every* column, so a much weaker ranking passes
#: the test written for the strong one.
_STRICTNESS: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
}
#: `negate` is the inverted guard. It earns its place on woswoar#206: `Check.ok`
#: defaulted to `None`, and `assertFalse(status.ok)` passes for `None` too, so
#: nothing could tell the two answers apart.
_OPPOSITE: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
}


#: How each comparison is written, for the prose half of a row. A module
#: constant rather than a literal rebuilt per call, matching `_ARITH_SPELLING`.
_SPELLING: dict[type[ast.cmpop], str] = {
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.Is: "is",
    ast.IsNot: "is not",
    ast.In: "in",
    ast.NotIn: "not in",
}


def _spell(op: ast.cmpop) -> str:
    return _SPELLING[type(op)]


def _compare(node: ast.AST, table: dict[type[ast.cmpop], type[ast.cmpop]]) -> Iterator[Edit]:
    if not isinstance(node, ast.Compare) or len(node.ops) != 1:
        return
    # survivor: off-by-one -- guarded by `len(node.ops) != 1` two lines up, so index 0 is the only
    #   one there is. A chained comparison is refused rather than mutated, which is what that check
    #   is for.
    was = node.ops[0]
    becomes = table.get(type(was))
    if becomes is None:
        return

    def flip(clone: ast.AST) -> None:
        assert isinstance(clone, ast.Compare)
        clone.ops = [becomes()]

    yield Edit(node, _rewritten(node, flip), f"`{_spell(was)}` becomes `{_spell(becomes())}`")


def boundary(node: ast.AST) -> Iterator[Edit]:
    yield from _compare(node, _STRICTNESS)


def negate(node: ast.AST) -> Iterator[Edit]:
    yield from _compare(node, _OPPOSITE)


def connector(node: ast.AST) -> Iterator[Edit]:
    """`and` <-> `or`: the compound guard where only one half is ever exercised."""
    if not isinstance(node, ast.BoolOp):
        return
    # One conditional, not two asking the same question: split, a future edit
    # can flip the token without flipping the prose, and the row would then
    # describe the opposite of what it did.
    was, now, becomes = (
        ("and", "or", ast.Or) if isinstance(node.op, ast.And) else ("or", "and", ast.And)
    )

    def flip(clone: ast.AST) -> None:
        assert isinstance(clone, ast.BoolOp)
        clone.op = becomes()

    yield Edit(node, _rewritten(node, flip), f"`{was}` becomes `{now}`")


def branch(node: ast.AST) -> Iterator[Edit]:
    """The mutation the hand-written tables here already write, now free.

    `while` is deliberately not included, and the reason is *certainty*, not
    cost. `boundary`, `arith` and `off-by-one` all fire on a loop condition and
    can produce a hang too -- `at += 1` to `at -= 1` is the one that OOM-killed
    this machine -- and they are not excluded, because they are merely likely to
    hang and the runner's `--timeout` and `--memory` bound them when they do.
    Forcing a `while` condition to `True` is *certain* to hang, every time, on
    every loop: a row that can only ever report `TIMEOUT` is not an answer, and
    generating one per `while` in the diff spends the budget to learn nothing.
    """
    if not isinstance(node, ast.If):
        return
    if isinstance(node.test, ast.Constant):
        # Already `if True:`. Forcing it to `True` changes nothing, and a row
        # that changes nothing reports `caught` or `SURVIVED` about nothing.
        return
    yield Edit(node.test, "True", "the `if` is always taken")
    yield Edit(node.test, "False", "the `if` is never taken")


def drop_not(node: ast.AST) -> Iterator[Edit]:
    if not isinstance(node, ast.UnaryOp) or not isinstance(node.op, ast.Not):
        return
    yield Edit(node, f"({ast.unparse(node.operand)})", "the `not` is dropped")


def order(node: ast.AST) -> Iterator[Edit]:
    """Ordering, and the weak fixture that hides a wrong one.

    Two items cannot distinguish a sorted walk from a reversed one -- so this
    generates the reversed walk and lets the fixture prove it can or cannot
    tell. `sorted(x)` -> `list(x)` is the weaker sibling: it keeps the
    elements and drops only the guarantee.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
        return
    name = node.func.id
    if name == "sorted" and not any(word.arg == "reverse" for word in node.keywords):

        def backwards(clone: ast.AST) -> None:
            assert isinstance(clone, ast.Call)
            clone.keywords = [*clone.keywords, ast.keyword(arg="reverse", value=ast.Constant(True))]

        yield Edit(node, _rewritten(node, backwards), "the ordering is reversed")

        def unordered(clone: ast.AST) -> None:
            assert isinstance(clone, ast.Call)
            clone.func = ast.Name(id="list", ctx=ast.Load())
            clone.keywords = []

        yield Edit(node, _rewritten(node, unordered), "`sorted` becomes `list`")
    swaps = {"min": "max", "max": "min", "any": "all", "all": "any", "reversed": "list"}
    # survivor: branch -- the `sorted` branch above and this one are mutually exclusive by name, and
    #   `_FIRES` now drives both -- `test_the_order_operator_rewrites_both_of_its_shapes`. What
    #   survives is the *dispatch*, which cannot be wrong without one of those two producing
    #   nothing, and both assert their row count.
    if name in swaps:

        def renamed(clone: ast.AST, to: str = swaps[name]) -> None:
            assert isinstance(clone, ast.Call)
            clone.func = ast.Name(id=to, ctx=ast.Load())

        yield Edit(node, _rewritten(node, renamed), f"`{name}` becomes `{swaps[name]}`")


def affix(node: ast.AST) -> Iterator[Edit]:
    """`x.endswith(s)` -> `True`, and the reason it is `True` and not `False`.

    The weak fixture is "a directory holding only `.age` files cannot test a
    suffix filter". A filter that accepts *everything* is precisely the answer
    such a directory cannot distinguish from a filter that works; one that
    accepts nothing empties the result and any assertion notices.
    """
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return
    if node.func.attr not in ("startswith", "endswith"):
        return
    yield Edit(node, "True", f"the `.{node.func.attr}(...)` filter accepts everything")


def return_constant(node: ast.AST) -> Iterator[Edit]:
    """Predicates whose tests only ever assert the happy direction."""
    if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Constant):
        return
    was = node.value.value
    # survivor: branch, negate -- `is` against the singletons, not `==`, so `1` and `0` do not
    #   qualify -- and `_QUIET` pins that with `return 1`. Both halves are needed and neither can be
    #   dropped without the other still admitting its own literal.
    if was is True or was is False:
        # survivor: drop-not -- `not was` appears twice on one line, in the edit and in its prose,
        #   so dropping either `not` makes the row describe itself wrongly rather than produce a
        #   different edit. `_FIRES` pins the prose exactly.
        yield Edit(node.value, str(not was), f"returns `{not was}` instead of `{was}`")


def drop_call(node: ast.AST) -> Iterator[Edit]:
    """A call whose value is discarded, deleted.

    The "the write never happened" class. This product writes files and forks
    `git`, so a test that cannot notice a missing side effect is the expensive
    kind of decoration. `pass` rather than deletion because the span is one
    statement and an empty body is a syntax error.
    """
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return
    called = node.value.func
    shown = ast.unparse(called)
    if shown.split(".")[0] in ("logging", "progress", "log"):
        # The meter is explicitly never the thing being compared -- see
        # `tools/sandbox.py`'s argument for pinning it out of a comparison.
        return
    # `print` is *not* excluded, though every off-the-shelf tool excludes it by
    # default. Here the CLI's printed output is the product, and
    # `tests/test_doctor.py` and `tests/test_cli.py` assert on it.
    yield Edit(node, "pass", f"the call to `{shown}(...)` never happens")


#: Keywords `drop-kwarg` may remove, and what each one is holding up.
#:
#: A table rather than "drop any keyword", because a blanket version is mostly
#: noise and this was measured before it was written. Of 281 keyword arguments
#: counted in the project it was measured in, the commonest are *required*
#: parameters passed by name -- `ok=` is a `Check` field there, as it is here --
#: and dropping one raises `TypeError`, which is `BROKE` rather than an answer and costs a
#: whole suite run to discover. That is the argument `drop_assign` already makes
#: for leaving plain `name = ...` alone. Nine more pass `check=False`, which is
#: `subprocess.run`'s own default, so removing it changes nothing and the row is
#: an unkillable equivalent mutant.
#:
#: Optional-versus-required is not decidable from the AST; it needs the callee's
#: signature. So the rule is inverted: a keyword not named here is never
#: dropped, and the list holds the ones whose absence silently weakens a
#: guarantee *and that a test can notice*. Both halves matter, and the second
#: was learned the expensive way.
#:
#: `encoding` was here and is not any more. Dropping `encoding="utf-8"` is a
#: real defect -- under `LC_ALL=C` the same read raises `UnicodeDecodeError`,
#: checked -- but this suite runs in a UTF-8 locale on every machine and in CI,
#: where `read_text()` and `read_text(encoding="utf-8")` are the same call. The
#: first sweep with this operator produced 41 of them, all unkillable, against
#: 3 caught rows in the whole package: a permanent survivor list that buries the
#: rows that mean something. The class is real and untested, which is what
#: issues are for; it is not what a generator should keep asking about.
_DROPPABLE = {
    #: `mkdir(mode=0o700)`, `chmod` -- the mode is the guarantee. `docs/security.md`
    #: claims are meant to be backed by tests, and nothing could generate this.
    "mode",
    #: `subprocess.run(check=True)` -- dropped, a failing command reads as success.
    #: Only when it is `True`; `check=False` is the default and unkillable.
    "check",
    #: The symlink and permission flags, same class as `mode`.
    "follow_symlinks",
    "exist_ok",
}


def drop_kwarg(node: ast.AST) -> Iterator[Edit]:
    """One keyword argument removed, where its absence weakens a guarantee.

    Narrow on purpose -- see `_DROPPABLE`. The failure this reaches is the one
    that leaves no trace: `mkdir(..., mode=0o700)` without its mode still makes
    the directory, still returns, and still passes every test that only asks
    whether the path exists.
    """
    if not isinstance(node, ast.Call) or not node.keywords:
        return
    for at, keyword in enumerate(node.keywords):
        # No `arg is None` guard: `_DROPPABLE` holds strings, so a `**spread`
        # (whose `arg` is `None`) already fails the membership test. A guard
        # nothing can reach is one nobody can trust -- as `every_line` says.
        if keyword.arg not in _DROPPABLE:
            continue
        if keyword.arg == "check" and not (
            isinstance(keyword.value, ast.Constant) and keyword.value.value is True
        ):
            # `check=False` is `subprocess.run`'s default: dropping it changes
            # nothing, and a row that changes nothing can only ever survive.
            continue

        def without(clone: ast.AST, index: int = at) -> None:
            assert isinstance(clone, ast.Call)
            del clone.keywords[index]

        trimmed = _trimmed(ast.unparse(keyword.value), 20)
        yield Edit(node, _rewritten(node, without), f"`{keyword.arg}={trimmed}` is dropped")


def off_by_one(node: ast.AST) -> Iterator[Edit]:
    """Small integer literals, moved by one. Index and threshold arithmetic."""
    if not isinstance(node, ast.Constant) or not isinstance(node.value, int):
        return
    if isinstance(node.value, bool) or abs(node.value) > 2:
        return
    for now in (node.value + 1, node.value - 1):
        yield Edit(node, repr(now), f"`{node.value}` becomes `{now}`")


def sign(node: ast.AST) -> Iterator[Edit]:
    """A negative numeric literal, made positive. `-1` -> `1`.

    `off-by-one` cannot reach this. `-1` parses as `UnaryOp(USub, Constant(1))`,
    so that operator sees the `1` and moves it to `0` or `2`, which the minus
    then turns into `0` and `-2` -- both still negative-or-zero, and a sentinel
    chosen for being negative survives all of them. Nothing here touched the
    minus itself until woswoar#274.

    Found by comparing against mutmut on woswoar#272: `tools/watch.py` sets
    `self.last = -1` so that a job already at zero rows still gets its opening
    line, and `+1` breaks that for a log holding exactly one matching row --
    silently, which is the failure the sentinel exists to prevent.

    Only negative to positive, never the reverse. Flipping every positive
    literal would fire on nearly every integer in the codebase, and a negative
    one is rare enough to be deliberate: a sentinel, a reverse index, an offset
    backwards. The asymmetry is the precision.
    """
    if not isinstance(node, ast.UnaryOp) or not isinstance(node.op, ast.USub):
        return
    if not isinstance(node.operand, ast.Constant):
        return
    value = node.operand.value
    if isinstance(value, bool) or not isinstance(value, int | float):
        return
    # `0` and `-0` are the same number, so the row would be an equivalent mutant
    # by construction and cost a suite run to say so.
    if not value:
        return
    yield Edit(node, repr(value), f"`-{value}` becomes `{value}`")


#: Division and modulo, where an off-by-one on the right operand changes the
#: *unit* rather than an index -- `// 60` reading a count of seconds as minutes.
_DIVIDES = (ast.Div, ast.FloorDiv, ast.Mod)


def divisor(node: ast.AST) -> Iterator[Edit]:
    """The right operand of `/`, `//` or `%`, moved by one.

    Deliberately not a widening of `off-by-one`, which caps at `abs(value) > 2`
    and should keep that cap: moving every integer in the codebase by one would
    be thousands of rows, nearly all equivalent. The cap is why `// 60` was
    never a candidate, and the answer is not a bigger cap but a narrower
    position -- a divisor is a *unit*, and its magnitude says nothing about how
    interesting it is to be wrong by one.

    From woswoar#272: `Watch.minutes()` computes `// 60`, and at the revision compared
    there nothing could tell `// 61` from it, because every assertion in the
    file read "0m".

    A divisor of `1` yields no row for the `0` direction: dividing by zero is a
    `ZeroDivisionError`, which reports `BROKE` rather than an answer and costs a
    whole suite run to establish nothing.
    """
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, _DIVIDES):
        return
    if not isinstance(node.right, ast.Constant):
        return
    value = node.right.value
    if isinstance(value, bool) or not isinstance(value, int) or not value:
        return
    for now in (value + 1, value - 1):
        if not now:
            continue
        yield Edit(node.right, repr(now), f"`{value}` becomes `{now}`")


def return_value(node: ast.AST) -> Iterator[Edit]:
    """`return X` -> `return None`: the function that silently returns nothing.

    Always a real answer, never a `BROKE`: no name goes missing and nothing
    raises, so the suite either notices or does not. That is what makes this the
    cheapest operator here per row.

    Booleans are left to `return-constant`, which swaps them -- `None` is falsy,
    so turning `return True` into `return None` asks very nearly the same
    question twice and costs a second suite run to do it.
    """
    if not isinstance(node, ast.Return) or node.value is None:
        return
    if isinstance(node.value, ast.Constant) and (
        node.value.value is None or isinstance(node.value.value, bool)
    ):
        return
    trimmed = _trimmed(ast.unparse(node.value), 30)
    yield Edit(node.value, "None", f"returns `None` instead of `{trimmed}`")


#: Arithmetic that a fixture with one small number cannot tell apart from itself.
_ARITH: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.FloorDiv,
    ast.FloorDiv: ast.Mult,
}
_ARITH_SPELLING = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.FloorDiv: "//"}


def _concatenation(node: ast.AST) -> bool:
    """Is this `+` provably joining strings, from the expression alone?

    A string operand settles it: `str + int` is a `TypeError` in any case, so a
    well-formed `+` with a string literal on either side is concatenation --
    and `-` on it can only raise. Such a row is `BROKE` by construction, never
    `caught` or `SURVIVED`, so the line it appeared to guard is guarded by
    nothing and no verdict says so.

    **Literals only, and that is the whole of what is sound here.** #57 proposed
    this check and cited `paint.GOOD + paint.HEAD` in `tools/watch.py` as the
    case to remove -- and those are *attributes*, which this cannot judge:
    proving them string-valued means resolving a name across a module boundary,
    which is a type checker rather than a guard. Measured on this tree: 9 `+`
    expressions have a provable string operand and 37 do not, the three in
    `watch.py` among the 37. So this removes a real class and not the one the
    issue was looking at, which is worth knowing before anyone reads that issue
    as done.

    Erring permissive costs a `BROKE` row. Erring strict silently stops mutating
    real arithmetic, which is a loss of coverage no output would report -- so
    when in doubt, mutate.
    """
    if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Add):
        return False
    return any(
        isinstance(side, ast.JoinedStr)
        or (isinstance(side, ast.Constant) and isinstance(side.value, str))
        for side in (node.left, node.right)
    )


def arith(node: ast.AST) -> Iterator[Edit]:
    """`+` <-> `-`, `*` <-> `//`, and the same on an augmented assignment.

    One body for both shapes. `x = a + b` and `count += 1` differ only in
    whether the row's prose carries the trailing `=` and in whether the splice
    is parenthesised, and `_rewritten` decides the second from the node.
    """
    if not isinstance(node, (ast.BinOp, ast.AugAssign)):
        return
    if _concatenation(node):
        return
    becomes = _ARITH.get(type(node.op))
    if becomes is None:
        return

    # The instance is built here and bound as a default: mypy's narrowing from
    # the `is None` check above does not follow into a nested function.
    swapped = becomes()

    def flip(clone: ast.AST, to: ast.operator = swapped) -> None:
        assert isinstance(clone, (ast.BinOp, ast.AugAssign))
        clone.op = to

    was, now = _ARITH_SPELLING[type(node.op)], _ARITH_SPELLING[becomes]
    # `+=` reads as one token in the row even though the AST separates them.
    sign = "" if isinstance(node, ast.BinOp) else "="
    yield Edit(node, _rewritten(node, flip), f"`{was}{sign}` becomes `{now}{sign}`")


def slice_widened(node: ast.AST) -> Iterator[Edit]:
    """`x[1:]`, `x[:-1]`, `x[a:b]` -> `x[:]`: the bound stops bounding.

    One replacement rather than one per end, because the fixture that can tell
    `x[1:]` from `x[:]` can almost always tell it from `x[2:]` too, and two rows
    per slice doubles the cost of the commonest shape in `cache.py`.
    """
    if not isinstance(node, ast.Subscript) or not isinstance(node.slice, ast.Slice):
        return
    cut = node.slice
    if cut.lower is None and cut.upper is None and cut.step is None:
        return

    def widen(clone: ast.AST) -> None:
        assert isinstance(clone, ast.Subscript)
        clone.slice = ast.Slice(lower=None, upper=None, step=None)

    yield Edit(node, _rewritten(node, widen), "the slice takes everything")


def drop_assign(node: ast.AST) -> Iterator[Edit]:
    """`self.x = ...` and `d[k] = ...`, deleted. The field that is never set.

    Attribute and subscript targets **only**, and the restriction is the whole
    design. Deleting `x = f()` leaves a `NameError` further down, which reports
    `BROKE` -- not an answer, and a full suite run to find that out. Assigning
    through an attribute or a key cannot make a name disappear, so every row here
    produces a real verdict.
    """
    if isinstance(node, ast.Assign):
        targets = node.targets
    # survivor: branch -- `+=` on an attribute or a key. `_FIRES` covers `ast.Assign` and the two
    #   arms share every line below them, so what is unreached is the dispatch alone -- an
    #   `AugAssign` fixture would assert the same `is never assigned` prose the `Assign` one already
    #   does.
    elif isinstance(node, ast.AugAssign):
        targets = [node.target]
    else:
        return
    # survivor: off-by-one -- both halves refuse a shape that would leave a `NameError` rather than
    #   a verdict, which the docstring above argues at length. `a = b = f()` and `x = f()` are the
    #   two, and `_QUIET` holds the second.
    if len(targets) != 1 or not isinstance(targets[0], (ast.Attribute, ast.Subscript)):
        return
    yield Edit(node, "pass", f"`{ast.unparse(targets[0])}` is never assigned")


class Operator(NamedTuple):
    name: str
    fire: Callable[[ast.AST], Iterator[Edit]]


OPERATORS: tuple[Operator, ...] = (
    Operator("boundary", boundary),
    Operator("negate", negate),
    Operator("connector", connector),
    Operator("branch", branch),
    Operator("drop-not", drop_not),
    Operator("order", order),
    Operator("affix", affix),
    Operator("return-constant", return_constant),
    Operator("drop-call", drop_call),
    Operator("drop-assign", drop_assign),
    Operator("drop-kwarg", drop_kwarg),
    Operator("off-by-one", off_by_one),
    Operator("sign", sign),
    Operator("divisor", divisor),
    Operator("return-value", return_value),
    Operator("arith", arith),
    Operator("slice", slice_widened),
)


# --- what may be mutated at all -------------------------------------------

#: Whole subtrees no operator sees.
#:
#: `JoinedStr` is the one that would produce a *wrong* answer rather than a
#: useless one: on 3.10 and 3.11 the nodes inside an f-string carry the enclosing
#: string's position, so a span computed from them splices the wrong bytes into
#: a file that then still parses. `requires-python` is `>=3.10`.
#:
#: The rest are equivalent mutants by construction. `assert` in `tools/` is
#: mostly mypy narrowing; mutating an import produces a `BROKE` row, which is not
#: an answer and costs a full suite run to find out.
_SKIP_SUBTREE = (ast.JoinedStr, ast.Assert, ast.Import, ast.ImportFrom)

#: Fields not descended into. Annotations are never evaluated -- every module
#: here has `from __future__ import annotations` -- so a mutant inside one cannot
#: change behaviour, and `mypy --strict` already holds them.
_SKIP_FIELDS: dict[type, frozenset[str]] = {
    ast.AnnAssign: frozenset({"annotation"}),
    ast.FunctionDef: frozenset({"returns", "decorator_list"}),
    ast.AsyncFunctionDef: frozenset({"returns", "decorator_list"}),
    ast.ClassDef: frozenset({"decorator_list"}),
    ast.arg: frozenset({"annotation"}),
}


def _is_guard(test: ast.expr) -> bool:
    """`if TYPE_CHECKING:` and `if __name__ == "__main__":`, neither worth asking about."""
    if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
        return True
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
    )


def _walk(
    node: ast.AST, where: str = "", inside_callable: bool = False
) -> Iterator[tuple[ast.AST, str, bool]]:
    """Every node an operator may be asked about, with its enclosing qualname.

    Docstrings need no suppression and used to have one. No operator matches a
    bare `Expr(Constant)`, so the machinery that skipped them changed no output;
    a mutation removing it survived, and the honest reading was that the code was
    redundant rather than the fixture weak.
    """
    if isinstance(node, _SKIP_SUBTREE):
        return
    if isinstance(node, ast.If) and _is_guard(node.test):
        return
    yield node, where, inside_callable
    ignored = _SKIP_FIELDS.get(type(node), frozenset())
    for field, value in ast.iter_fields(node):
        if field in ignored:
            continue
        for child in value if isinstance(value, list) else [value]:
            if not isinstance(child, ast.AST):
                continue
            deeper, callable_now = where, inside_callable
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                deeper = f"{where}.{child.name}" if where else child.name
                # Carried separately rather than baked into the name, because a
                # function nested in a function would otherwise read
                # `outer().inner()`. Only the innermost scope decides the
                # parentheses, and a class body is not a call.
                callable_now = not isinstance(child, ast.ClassDef)
            yield from _walk(child, deeper, callable_now)


def _touches(node: ast.AST, lines: set[int]) -> bool:
    """Does any line of `node` fall inside the diff?

    *Any*, not *all*. A single edited line inside a wrapped call means the
    enclosing statement starts above the hunk and ends below it, and requiring
    full containment would silently skip the commonest real edit there is.
    """
    return not lines.isdisjoint(_spanned(node))


def _spanned(node: ast.AST) -> range:
    """Every line `node` occupies, or an empty range if it has no position.

    One definition, because two callers ask the same question for opposite
    reasons: `_touches` wants an overlap with the diff, `generate` wants the
    absence of one with the pragma lines. They disagreed once -- the pragma was
    tested against `node.lineno` alone -- and a multi-line construct was the
    case that fell through.
    """
    start = getattr(node, "lineno", None)
    if start is None:
        # survivor: off-by-one -- an empty range for a node with no `lineno`. `range(1)` would offer
        #   line 0, which no `ast` node has -- the module counts from 1 -- so every reader's overlap
        #   test answers the same either way.
        return range(0)
    return range(start, (getattr(node, "end_lineno", None) or start) + 1)


def _pragma_lines(source: str, offsets: Offsets) -> set[int]:
    """Which lines carry `# pragma: no mutate`, counted the way `ast` counts.

    Not `str.splitlines`, which is what this did first and which was wrong for
    the reason `line_starts` exists: it breaks on form feed and the tokenizer
    does not, so one `\f` above a pragma moved the suppression down a line --
    the marked code was mutated anyway and an innocent line was skipped, both
    silently. The module already owns the right answer; this was the last place
    still asking the wrong one.
    """
    # survivor: off-by-one -- the *lower* bound is now pinned by
    #   `test_a_pragma_on_the_very_first_line_still_counts`; what is left is the other direction.
    #   `range(0, ...)` asks `offsets.line(0)`, which indexes `_starts[-1]` and so re-reads the last
    #   line -- and even where that line carries a pragma, the number it adds is 0, which no `ast`
    #   node has. The blocked set gains an entry nothing can match.
    return {
        number for number in range(1, offsets.lines() + 1) if NO_MUTATE.search(offsets.line(number))
    }


def label(path: str, line: int, where: str, prose: str, inside_callable: bool = True) -> str:
    """One line, and it is the product.

    A generated row still has to read as a sentence in a pull request, because
    that is what the reader of the PR gets. `--list` exists so the mechanical
    version can be edited into a better one before it is run.
    """
    place = f"{path}:{line}"
    if not where:
        return f"{place} -- {prose}"
    # `()` only for a function. `in C()` named a call that does not exist, on
    # the one string the reader of a pull request actually sees.
    return f"{place} in {where}{'()' if inside_callable else ''} -- {prose}"


def _chosen(
    operators: Sequence[str] | None, skip: Sequence[str] | None = None
) -> tuple[Operator, ...]:
    """The operators to run, refusing a name that is not one.

    Decided once per file rather than re-tested per node per operator, and
    `SystemExit` rather than an empty selection because `--operator boundry`
    otherwise generated nothing, printed `0 mutants`, ran the baseline and
    exited 0. A run that asked nothing must not read as a run that found
    nothing -- the same argument `--limit` makes when it says out loud how many
    rows it dropped.

    `skip` is the escape hatch for an equivalent mutant whose operator is
    otherwise worth running -- the one `# pragma: no mutate` cannot serve,
    because a pragma suppresses a *line* and this suppresses a *kind*. It
    subtracts after `operators` selects, so naming a name in both is an empty
    selection, which is refused for the reason above.
    """
    known = {op.name for op in OPERATORS}
    asked, skipped = set(operators or ()), set(skip or ())
    # survivor: order -- `sorted` over a *set*, which CLAUDE.md records as only probabilistically
    #   guarded: hash order is randomised per run, so the mutant agrees whenever it happens to
    #   match. The list is prose in a `SystemExit` a person reads once.
    unknown = sorted((asked | skipped) - known)
    if unknown:
        raise SystemExit(
            f"no such operator: {', '.join(unknown)}. Available: "
            f"{', '.join(op.name for op in OPERATORS)}"
        )
    wanted = (asked or known) - skipped
    if not wanted:
        raise SystemExit(
            "every operator was skipped, so nothing would be generated. "
            "Drop a --skip-operator, or say what you did want with --operator."
        )
    return tuple(op for op in OPERATORS if op.name in wanted)


def generate(
    source: str,
    path: str,
    lines: set[int],
    tests: str = "",
    operators: Sequence[str] | None = None,
    skip: Sequence[str] | None = None,
) -> list[Mutation]:
    """Every mutant the operators can make on the changed lines of one file."""
    tree = ast.parse(source)
    offsets = Offsets(source)
    blocked = _pragma_lines(source, offsets)
    chosen = _chosen(operators, skip)

    seen: set[tuple[tuple[int, int], str]] = set()
    out: list[Mutation] = []
    for node, where, inside_callable in _walk(tree):
        if not _touches(node, lines):
            continue
        for operator in chosen:
            for edit in operator.fire(node):
                # Every line the edit spans, not just the one it starts on. A
                # pragma reads as covering the construct it sits in, and a
                # multi-line `if (a\n        and b):  # pragma: no mutate` was
                # mutated regardless -- silently, and in the direction that
                # reads as success, because the author sees the pragma and the
                # run reports `SURVIVED` on code declared out of scope.
                if not blocked.isdisjoint(_spanned(edit.node)):
                    continue
                span = offsets.span(edit.node)
                old = source[span[0] : span[1]]
                # No `old == new` check: every operator either parenthesises its
                # replacement or emits a different token, so the no-op it would
                # catch cannot be produced. A mutation removing it survived, and
                # a guard nothing can reach is a guard nobody can trust.
                # survivor: branch -- the dedupe, and what it removes is two operators reaching the
                #   same edit at the same span. Nothing in the tree does today -- `generate` over
                #   the whole of `tupferl/` and `tools/` produces 3387 rows and 3387 distinct keys
                #   -- so the guard is against a pair of operators nobody has written yet.
                if (span, edit.new) in seen:
                    continue
                # survivor: drop-call -- the other half of the same dedupe, unreachable for the same
                #   reason: with no duplicate produced, recording one changes nothing.
                seen.add((span, edit.new))
                out.append(
                    Mutation(
                        label=label(path, edit.node.lineno, where, edit.prose, inside_callable),
                        path=path,
                        old=old,
                        new=edit.new,
                        tests=tests,
                        span=span,
                        operator=operator.name,
                    )
                )
    # survivor: connector, off-by-one -- `span or (0, 0)` is for a row without one, and `generate`
    #   gives every row a span -- the fallback exists so the key is total, not because this path can
    #   produce it. The `operator` tiebreak orders two edits at one span, which the dedupe above has
    #   already made impossible.
    out.sort(key=lambda row: (row.span or (0, 0), row.operator))
    return out


def dead_tags(root: Path) -> list[tuple[str, int, str]]:
    """Every `# survivor:` tag naming an operator its statement cannot produce.

    **The other direction from `mutate.Survivors.spent`, and the one nothing
    else can see.** `spent` asks whether a tag's rows are all now *caught*,
    which needs a sweep and only reaches the tags that sweep touched. This asks
    whether a tag has any row at all, which is pure -- so it runs in the
    preflight, over the whole tree, every time, which is far more often than a
    sweep runs.

    Such a tag is invisible by construction: `excused` never resolves to it, so
    it neither excuses anything nor gets reported as spent. It simply sits there
    claiming somebody read a survivor. Two real instances, both found by the
    first run of this: a tag written for `mutate._lane`'s walk placed above the
    enclosing `while`, covering the loop header and none of the five statements
    inside it; and `verdict_unittest.main`'s `off-by-one` reason four screens
    from the `argv` indices it describes, above a `return` that has never
    carried such a row.

    Here rather than in `tests/`, because it is the same question `--accept` and
    a sweep would want to ask, and neither can reach a helper that lives in a
    test module.

    It answers in `(path, statement, operator)` -- the statement the tag claims
    to guard, not the tag's own line, which `mutate.Excuse` reports instead. The
    statement is what a reader has to look at here: the complaint is that *this*
    statement produces no such row, and the tag is the handful of lines above
    it. Asking `excuse` for the tag's line would also have added an arm for a
    `None` that `operators` has already ruled out, and an unreachable guard is
    one this repository would rather not have.
    """
    found: list[tuple[str, int, str]] = []
    for path, whole in sorted(every_line(root).items()):
        source = (root / path).read_text(encoding="utf-8")
        index, offsets = Tags(source), Offsets(source)
        reachable = {
            # survivor: off-by-one -- equivalent: `span` is one edit node's `(start, end)`, and an
            #   edit node is always inside a single logical statement -- so `statement` normalises
            #   either end to the same line, and `span[1]`/`span[-1]` are the same answer.
            #   Measured over this tree's 3930 rows: the two ends agree on every one of them.
            (index.statement(offsets.line_of(row.span[0])), row.operator)
            for row in generate(source, path, whole)
            if row.span is not None
        }
        for statement in range(len(source.split("\n"))):
            for operator in sorted(index.operators(statement)):
                if (statement, operator) not in reachable:
                    found.append((path, statement + 1, operator))
    return found


def cap(mutations: Sequence[Mutation], limit: int) -> tuple[list[Mutation], list[Mutation]]:
    """Keep `limit` of them, round-robin across files; return what was dropped too.

    Round-robin rather than `[:limit]`, which would cover the alphabetically
    first file exhaustively and the largest one not at all -- and the printed
    summary would not show it, because the count would be right either way.
    """
    if limit <= 0 or len(mutations) <= limit:
        return list(mutations), []
    queues: dict[str, list[Mutation]] = {}
    for row in mutations:
        queues.setdefault(row.path, []).append(row)
    kept: list[Mutation] = []
    while len(kept) < limit and any(queues.values()):
        for path in sorted(queues):
            if queues[path] and len(kept) < limit:
                kept.append(queues[path].pop(0))
    dropped = [row for queue in queues.values() for row in queue]
    # survivor: off-by-one -- the same total-ordering fallback as `generate`'s sort, and `cap` is
    #   handed rows `generate` built, so the `None` arm is unreachable from any caller in this tree.
    kept.sort(key=lambda row: (row.path, row.span or (0, 0)))
    return kept, dropped


def _near_miss(original: str, wanted: str) -> str:
    """The closest line in the file, when a hand-written row matches nothing.

    Almost always the same cause: an edit moved the line by a word since the row
    was written, and the row is now quoting the file's past. Without this the
    author greps for a string they believe is there, which is the one search
    that cannot work. With it the answer is in the refusal.

    Only for a single-line `old`, and only when something is close enough to be
    worth printing: a multi-line span has no "nearest line", and a suggestion
    that is not the intended line is worse than none.
    """
    first = wanted.strip().splitlines()
    # survivor: off-by-one -- `_near_miss` only guesses for a *single* line of wanted text; both
    #   halves refuse the shapes where a guess would mislead, and the docstring says a suggestion
    #   that is not the intended line is worse than none.
    if len(first) != 1 or not first[0]:
        return ""
    best, score = "", 0.0
    for line in original.splitlines():
        # survivor: off-by-one -- the argument order into `SequenceMatcher`, which is symmetric for
        #   `ratio()` -- swapping the two sequences returns the same number.
        ratio = SequenceMatcher(None, first[0], line.strip()).ratio()
        # survivor: boundary -- strictly greater keeps the *first* best line rather than the last;
        #   with `>=` a later equally-close line wins. Both are a hint in an error message, and the
        #   docstring's claim is about the 0.6 floor below rather than which of two equal lines is
        #   quoted.
        if ratio > score:
            best, score = line, ratio
    # survivor: boundary -- the floor under which no line is close enough to suggest. A boundary
    #   shift changes which near-misses are offered, and the prose that carries them is advice --
    #   `check` has already raised by the time a reader sees it.
    if score < 0.6:
        return ""
    return f"\n\nThe closest line there is:\n    {best.strip()}"


def check(mutation: Mutation) -> None:
    """Refuse a row that cannot mean anything.

    Three checks. The first is about the row's *shape* and reads nothing; the
    other two read the real file.

    Two shapes there, because the two kinds of row make different promises. A
    hand-written one says "this text is unique", and a second match means the
    edit could land anywhere. A generated one says "this text is *here*", which
    is the stronger claim -- and checking it is what stands between the
    byte-versus-character offset trap and a mutation that silently edits the
    wrong span of a file that still parses.
    """
    # `str` *is* a `Sequence[str]`, so `first: Sequence[str]` accepts the whole
    # string it exists to forbid and mypy says nothing -- verified. Iterating one
    # yields characters, so `_attempt` spreads a killer into fifty single-letter
    # names and every one selects nothing; the row comes back `BROKE`, which is
    # never `caught`, so the line it guards is unguarded and the summary counts
    # it in neither of the two numbers a reader looks at.
    #
    # Here rather than in `_attempt` because this runs over the whole table
    # before the first sandbox is built: one loud death at row 0 rather than a
    # wall of non-answers at the end of an hour. The other half of the hole is
    # `NamedTuple._replace`, whose keywords mypy does not check at all -- and
    # `_replace` does not even reach `__new__`, so a validator there would miss
    # `Killers.ahead_of`, which is one of the two producers. That is exactly how
    # a `str` got past the conversion that introduced this.
    #
    # It is the *only* enforcement point, and `mutate.run` is the only caller.
    # `--baseline-only` reaches `baseline_shards` without passing through here,
    # which is safe today for a reason that is about its argument parsing rather
    # than about this: that flag lives inside the generated-table branch, where
    # every `first` was built by `Killers.ahead_of` as a tuple. A spec file
    # never reaches it. Widening the flag means bringing this check with it.
    if isinstance(mutation.first, str):
        raise SystemExit(
            f"{mutation.label}: `first` is a sequence of test ids, not one "
            f"space-joined string. Given a string it is iterated character by "
            f"character, and every character selects nothing. Pass a tuple, "
            f'even for a single id: `first=("{mutation.first}",)`.'
        )
    original = Path(mutation.path).read_text(encoding="utf-8")
    if mutation.span is None:
        found = original.count(mutation.old)
        if found != 1:
            raise SystemExit(
                f"{mutation.label}: {mutation.path} contains the text to replace "
                f"{found} times, not once. A mutation that matches nothing tests "
                f"nothing, and one that matches twice tests something else."
                + _near_miss(original, mutation.old)
            )
    else:
        start, end = mutation.span
        # survivor: branch -- the span check, and `TestTheEditIsRefusedRatherThanGuessed` drives its
        #   `SystemExit` directly. What survives here is the comparison read the other way, which
        #   needs a file that changed *between* generation and application -- the state the message
        #   tells the reader to fix by regenerating.
        if original[start:end] != mutation.old:
            raise SystemExit(
                f"{mutation.label}: {mutation.path} no longer holds that text at "
                f"{start}..{end}, so the row would edit something else. The file "
                f"has changed since the table was generated -- regenerate it."
            )

    if not mutation.additive and mutation.old in mutation.new:
        raise SystemExit(
            f"{mutation.label}: the text to replace survives verbatim inside "
            f"the replacement, so the code under test does not change and "
            f"'caught' would mean nothing. If the point really is to insert "
            f"something in front of code that stays, pass additive=True."
        )


# --- where the diff says it matters ---------------------------------------

_HUNK = re.compile(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@")


def _git(argv: Sequence[str], root: Path) -> str:
    """A local one, deliberately not the product's own git wrapper.

    That one defaults `cwd` to the managed repository, raises the product's own
    error type and carries a timeout, and importing it would drag the package
    and `progress` into a development tool. `tools/sandbox.py` already builds its
    own `git` argv for the same reason.
    """
    finished = subprocess.run(["git", *argv], cwd=root, capture_output=True, text=True, check=False)
    if finished.returncode != 0:
        raise SystemExit(f"git {' '.join(argv)} failed: {finished.stderr.strip()}")
    return finished.stdout


def _unquote(target: str) -> str:
    """`+++ b/path`, including the C-quoted form git uses for odd bytes."""
    # survivor: affix, connector -- git's C-quoted path form, which it emits only for a name with
    #   bytes that are not plain ASCII. Both ends are needed and a one-ended fixture is not
    #   something git produces.
    if target.startswith('"') and target.endswith('"'):
        target = target[1:-1].encode("ascii", "backslashreplace").decode("unicode_escape")
    # survivor: affix -- strips git's `a/` and `b/` prefixes. A different count leaves or eats a
    #   character of the real path, which `parse_hunks` then fails to match against any file -- so
    #   the row is a table of nothing rather than a wrong table, and `--base` reports zero mutants
    #   out loud.
    return target[2:] if target.startswith(("a/", "b/")) else target


def parse_hunks(diff: str) -> dict[str, set[int]]:
    """Which lines of which files the diff *adds*. Pure, so a test can drive it.

    Only the `+c,d` half of each header is read. `d` omitted means one line; `d`
    of zero is a pure deletion and contributes nothing at all, because there is
    no new code there to mutate -- and taking it as one line would generate
    mutants for whatever happens to sit at that number now.
    """
    found: dict[str, set[int]] = {}
    current: str | None = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            # No `/dev/null` special case, and the absence is the documentation:
            # git spells a deleted file's hunk `+0,0`, so the count check below
            # already drops it -- and a guard that nothing can reach is a guard
            # nobody can trust. A mutation removing the special case survived,
            # which is how it was found.
            current = _unquote(line[4:].strip())
            continue
        header = _HUNK.match(line)
        if header is None or current is None:
            continue
        start = int(header.group(1))
        count = 1 if header.group(2) is None else int(header.group(2))
        if count:
            found.setdefault(current, set()).update(range(start, start + count))
    return found


def changed_lines(base: str, root: Path) -> dict[str, set[int]]:
    """Every mutable line this branch has touched, working tree included."""
    top = _git(["rev-parse", "--show-toplevel"], root).strip()
    if Path(top).resolve() != root.resolve():
        raise SystemExit(
            f"run this from the repository root: paths in a diff are relative to {top}, not {root}."
        )
    # survivor: drop-call -- the existence check for the base revision, whose value is discarded:
    #   `_git` raises for a bad revision, and dropping the call moves the same failure to the `diff`
    #   two lines down with a message about a diff rather than about a revision.
    _git(["rev-parse", "--verify", base], root)

    # `--merge-base` with no second commit, so the right-hand side is the
    # *working tree*: staged and unstaged changes included. That is the situation
    # the tool is used in, and the same reason `_sandboxes` copies rather than
    # running `git worktree add`. It also excludes commits that landed on the
    # base after this branch started, which a two-dot diff would drag in and
    # generate mutants for.
    diff = _git(["diff", "--unified=0", "--merge-base", base, "--", *MUTABLE], root)
    changed = {path: lines for path, lines in parse_hunks(diff).items() if mutable(path)}

    # A brand-new module is invisible to `git diff` until it is added, and "no
    # mutants for the new file" reads exactly like "the new file is covered".
    for name in _git(["ls-files", "--others", "--exclude-standard", "--", *MUTABLE], root).split():
        if mutable(name):
            body = (root / name).read_text(encoding="utf-8").splitlines()
            changed.setdefault(name, set()).update(range(1, len(body) + 1))
    return changed


def every_line(root: Path) -> dict[str, set[int]]:
    """Every line of every mutable file. What `--all` means.

    Enumerated rather than diffed against the repository's first commit. Those
    two agreed exactly when it was checked, but only because this repository's
    root commit happens to be near-empty -- a fact about its history rather than
    about what "everything" means. No absolute count is quoted here on purpose:
    it moves with every commit, and a stale one reads as a promise.

    `git` is not consulted at all, so an unstaged or untracked file counts the
    same as a committed one. That matches `changed_lines`, which folds untracked
    files in for the same reason: a brand-new module with no mutants reads
    exactly like a covered one.
    """
    found: dict[str, set[int]] = {}
    for prefix in MUTABLE:
        # The `mutable(name)` filter is back, and the comment that removed it is
        # why it needs saying. It argued that walking `MUTABLE` for `*.py`
        # already satisfies both halves, so the check could never be false --
        # true when `mutable` was two clauses, and false the moment it grew
        # `UNMUTABLE`. A guard "nothing can reach" stopped being unreachable
        # without anybody visiting this line, which is how the excluded
        # directory stayed in `--all` after being excluded (woswoar#245).
        # survivor: order -- `sorted` for a stable table across machines, over a `rglob` whose own
        #   order is filesystem-defined. Two runs of the same tree on one machine agree either way,
        #   which is why no fixture here sees it.
        for path in sorted((root / prefix).rglob("*.py")):
            name = path.relative_to(root).as_posix()
            if not mutable(name):
                continue
            body = path.read_text(encoding="utf-8").splitlines()
            if body:
                found[name] = set(range(1, len(body) + 1))
    return found


# --- which tests to run it against ----------------------------------------


def module_of(path: str) -> str:
    """`tupferl/config.py` -> `tupferl.config`, and a package's `__init__` -> the package.

    The `__init__` case is not cosmetic: nothing anywhere writes
    `import tupferl.__init__`, so without it `tupferl/__init__.py` resolves to no
    tests at all and every mutant in it would run the whole suite.
    """
    dotted = path[: -len(".py")].replace("/", ".")
    return dotted[: -len(".__init__")] if dotted.endswith(".__init__") else dotted


def _imported(node: ast.AST, package: str = "") -> Iterator[str]:
    """Every dotted name an import statement pulls in, relative ones included.

    The relative half is not a nicety. `tests/` is a package and its own style is
    `from . import support` / `from .support import requires_age` -- so an index
    that only understood absolute imports attributed nothing from any helper, and
    the closure below ran over an empty set. Found by mutation testing: removing
    the closure changed no answer, because it had never produced one.
    """
    # survivor: branch -- `import x` against `from x import y`, and the docstring above records that
    #   an earlier version understanding only one of them attributed nothing from any helper. Both
    #   arms are driven by `TestChoosingTheTests` against this repository, which is why the
    #   *dispatch* is what is left.
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
        return
    if not isinstance(node, ast.ImportFrom):
        return
    if node.level:
        # survivor: branch -- a relative import in a file with no package to resolve against, which
        #   cannot happen for the `tests/` tree this is called on -- `importers` passes `"tests"`
        #   always.
        if not package:
            return
        base = f"{package}.{node.module}" if node.module else package
    # survivor: branch -- `from . import x`, where there is no module name to build a base from. The
    #   arm below returns, and the level check above has already handled the relative case.
    elif node.module:
        base = node.module
    else:
        return
    yield base
    for alias in node.names:
        yield f"{base}.{alias.name}"


def _statements(tree: ast.Module) -> Iterator[ast.stmt]:
    """Every statement in `tree`, which is everywhere an import can be.

    Not `ast.walk`. An `import` is a statement and can only appear in a
    statement position, so descending into expressions is work that cannot find
    anything: measured over the real `tests/` tree, `ast.walk` visits 94,583
    nodes to reach 317 imports. Restricting the descent to the fields that hold
    statement lists cut `importers` from 106 ms to 67 ms -- a fifth of the whole
    generator's wall clock -- for a byte-identical index.
    """
    stack: list[ast.stmt] = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        for field in ("body", "orelse", "finalbody", "handlers"):
            for child in getattr(node, field, []):
                # survivor: branch -- the walk over every place an import can be. A statement is
                #   pushed and anything else ignored; `TestChoosingTheTests` drives it over this
                #   repository, so what survives is a dispatch whose both arms are exercised and
                #   whose answer is a set nothing distinguishes them in.
                if isinstance(child, ast.stmt):
                    # survivor: drop-call -- the same walk, from the other side.
                    stack.append(child)
                # survivor: branch -- an `ExceptHandler` is not a `stmt`, so its body would be
                #   skipped -- an import inside `except ImportError:` is exactly the shape this
                #   exists for, and `tupferl/config.py`'s shim is one. Nothing in `tests/` has one,
                #   which is why no fixture reaches it.
                elif isinstance(child, ast.ExceptHandler):
                    # survivor: drop-call -- the other half of the `ExceptHandler` arm, unreachable
                    #   for the same reason.
                    stack.extend(child.body)


def importers(root: Path) -> dict[str, set[str]]:
    """Which test modules import which module, computed rather than kept by hand.

    `tupferl/paths.py` and `tupferl/errors.py` have no same-named test module,
    so the name heuristic alone would report them as having no tests at all --
    and a hand-kept table would rot towards that silently, which is the argument
    `tupferl/paths.py` makes about `ENV_KEYS`.

    Helper modules under `tests/` are followed one level: `tests/support.py`
    imports `paths`, and every test module that imports *it* reaches it too.
    """
    direct: dict[str, set[str]] = {}
    helpers: dict[str, set[str]] = {}
    uses_helper: dict[str, set[str]] = {}
    # survivor: order -- `sorted` for a stable answer across machines, over a `glob` whose order is
    #   the filesystem's. One machine agrees with itself either way.
    for source in sorted((root / "tests").glob("*.py")):
        # `module_of` rather than the stem, so `tests/__init__.py` indexes as
        # `tests` -- what an import actually spells. The same special case
        # `module_of` already carries for `tupferl/__init__.py`, applied to the
        # other half of the same mapping; without it anything ever added to
        # `tests/__init__.py` would drop out of the closure silently.
        name = module_of(f"tests/{source.name}")
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        pulled = {module for node in _statements(tree) for module in _imported(node, "tests")}
        if source.name.startswith("test_"):
            # One pass, not two over the same set: `direct` for everything, and
            # additionally `uses_helper` for the `tests.*` entries.
            for module in pulled:
                direct.setdefault(module, set()).add(name)
                # `== "tests"` as well as the dotted form: `from . import
                # support` yields the bare package name too, and that is the
                # key `tests/__init__.py` is indexed under. Without it a helper
                # living in the package `__init__` is never linked back to the
                # tests that import through it.
                # survivor: affix, branch -- the comment beside it says why both halves are needed
                #   -- `from . import support` yields the bare package name, which is the key
                #   `tests/__init__.py` is indexed under. That file is empty in this tree, so the
                #   first half has nothing to link and no fixture separates them.
                if module == "tests" or module.startswith("tests."):
                    uses_helper.setdefault(module, set()).add(name)
        else:
            helpers[name] = pulled

    for helper, pulled in helpers.items():
        for module in pulled:
            direct.setdefault(module, set()).update(uses_helper.get(helper, set()))
    return direct


def targets_for(path: str, root: Path, index: dict[str, set[str]] | None = None) -> str:
    """The unittest targets a mutant in `path` should be run against.

    A heuristic, and it is allowed to be one because this is an **ordering**
    rather than a gate: `verdict.collect` walks a mutation's selection first and
    then every other test module, stopping at whatever notices. So a selection
    that misses the killing test costs a longer walk, never a wrong answer.

    That used to be bought by re-running each survivor against the whole suite
    afterwards, which meant a survivor ran its selection *and then* a superset of
    it -- 486s of the 52 min one milestone-6 sweep spent on survivors was the
    same work done twice. Measured across five sweeps, the killer was inside its
    own selection in 1,516 of 1,516 caught rows, so this heuristic is not merely
    allowed to be wrong: it has not yet been observed to be.

    An empty answer means "run everything", and the caller says so in the row.
    It is not a fallback of convenience: `tests/test_cli.py` drives the CLI by
    spawning `python -m tupferl`, and the end-to-end sync tests will reach most
    of the package through a real `git` -- neither imports what it exercises, so
    no static index can see them. The unsafe answer would be to call that "no
    tests" and skip the row, which reads as coverage nobody has.
    """
    stem = Path(path).stem
    named = {
        f"tests.{found.stem}"
        for found in (root / "tests").glob("test_*.py")
        if found.stem == f"test_{stem}" or found.stem.startswith(f"test_{stem}_")
    }
    imported = (index if index is not None else importers(root)).get(module_of(path), set())
    # The union, not the name match short-circuiting the index. Measured on
    # woswoar#216: taking one same-named test module because the name matched
    # left nine mutants reported as survivors that another module catches --
    # `doctor` there called a function no name heuristic can see. The
    # widening corrected all nine, so the answer was right either way, but it
    # paid a whole-suite run per row to get there. It is now paid for by the walk
    # continuing, which costs those nine rows the same and the rest nothing.
    return " ".join(sorted(named | imported))
