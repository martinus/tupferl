"""The lint CLAUDE.md §1 asks for: the gate must actually gate.

The two ways a required check goes wrong both end in a green tick, which is why
this is a test and not a convention:

- a job is added to the workflow and not to the gate's `needs`, so it can fail
  while the required check passes;
- `if: always()` is dropped, so the gate is *skipped* when a dependency fails --
  and a skipped required check counts as satisfied.

Parsed by hand rather than with a YAML library. That is a real cost: a hand
parser can fail to find anything and then assert nothing, which is exactly the
vacuous pass this file exists to prevent. So every test below establishes its
own precondition first -- the job set is non-empty and contains names known to
be there -- and `test_the_parser_finds_the_jobs` states it on its own.

**The per-job tests are parametrized over `FOUND`, which is read once at
import.** That makes the precondition load-bearing in a second way: a parser
that found nothing would collect *no* per-job cases at all, so those tests would
not fail, they would vanish -- and a suite that silently lost two tests reports
the same green as one that ran them.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
MUTATION = ROOT / ".github" / "workflows" / "mutation.yml"

#: The name of the one required status check. Written here as well as in the
#: workflow because branch protection names it too, and a rename that only
#: happened in one of the three is the failure this file cannot otherwise see.
GATE = "gate"

#: The preflight from CLAUDE.md §7, which CI must run and in this order. Kept as
#: the source of truth in the file people read before pushing.
PREFLIGHT = (
    "ruff check .",
    "ruff format --check .",
    "mypy tupferl tests tools",
    "python -m tools.run_tests",
)


def workflow() -> str:
    return CI.read_text(encoding="utf-8")


def settings(text: str) -> str:
    """`text` with its comment lines removed.

    **Every assertion about `ci.yml` reads this and not the raw file**, and it
    was written because one of them could not fail. The gate explains itself in
    a comment that quotes the setting it is explaining --

        # - `if: always()`, because a job whose dependency failed is *skipped*

    -- so `test_it_runs_even_when_a_dependency_failed` was satisfied by the
    prose. Measured: deleting the real `if: always()` line left all 33 tests in
    this file green. That is the same trap
    `TestTheScheduledSweepSaysWhatItSwept` records for `mutation.yml`, where two
    of four hand-made edits survived for the same reason, and the same one
    `tests/test_release.py`'s `settings()` exists for.

    Whole comment *lines* rather than `mutation.yml`'s cut-at-the-first-`#`,
    because the two files differ and the rule has to be exact rather than
    approximate: `ci.yml` has no trailing comments at all -- checked -- while it
    does have `#` inside shell blocks that a first-`#` cut would leave half a
    line of.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def jobs() -> dict[str, str]:
    """Top-level job names mapped to their block of the file.

    Only what follows the `jobs:` line, and only until the next top-level key:
    `on:` and `concurrency:` have two-space children too, and a parser that
    collected those would report `push` as a job.

    Read from `settings(...)`, so every block below holds what the workflow
    *does* and not what it says about itself. See `settings`.
    """
    text = settings(workflow())
    start = re.search(r"^jobs:$", text, re.MULTILINE)
    assert start, "no `jobs:` key in the workflow"
    body = text[start.end() :]
    end = re.search(r"^\S", body, re.MULTILINE)
    body = body[: end.start()] if end else body

    found: dict[str, str] = {}
    name: str | None = None
    for line in body.splitlines():
        header = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if header:
            name = header.group(1)
            found[name] = ""
        elif name is not None:
            found[name] += line + "\n"
    return found


#: The parse, done once so that the per-job tests below are parametrized over
#: the same job set `test_the_parser_finds_the_jobs` vouches for.
FOUND = jobs()


class TestTheParserWorks:
    """Stated first, because everything below is vacuous without it."""

    def test_the_parser_finds_the_jobs(self) -> None:
        assert GATE in FOUND
        assert "lint" in FOUND
        assert "test" in FOUND
        assert len(FOUND) >= 4

    def test_it_does_not_mistake_a_trigger_for_a_job(self) -> None:
        """`on:` has `push:` under it at the same indentation as a job name."""
        assert "push" not in FOUND
        assert "pull_request" not in FOUND

    def test_the_comment_stripping_leaves_the_settings_alone(self) -> None:
        """`settings`' own precondition, and it needs both halves.

        Strip too much and every assertion below passes by finding nothing;
        strip too little and they pass by finding a comment. The second is what
        actually happened here -- see `settings`.
        """
        assert "runs-on:" in FOUND[GATE], "stripping removed the settings"
        assert "load-bearing" not in FOUND[GATE], "comments are still being read"

    @pytest.mark.parametrize("job", sorted(FOUND))
    def test_every_job_has_a_runner(self, job: str) -> None:
        assert "runs-on:" in FOUND[job]

    @pytest.mark.parametrize("job", sorted(FOUND))
    def test_every_job_has_a_timeout(self, job: str) -> None:
        """Without one a job gets GitHub's six-hour default, and the gate
        `needs:` every job -- so one wedged leg holds the pull request open with
        nothing in the log to read, because a running job's log is a 404.

        That is not hypothetical: the `macos` leg hung for twenty minutes on a
        `tcsetattr` waiting for output nobody was reading, while every other leg
        finished in under a minute. A timeout would have said so in ten.
        """
        assert "timeout-minutes:" in FOUND[job]


def needs() -> set[str]:
    """The names in the gate's single-line `needs:` list."""
    listed = re.search(r"^    needs: \[([^\]]*)\]", FOUND[GATE], re.MULTILINE)
    assert listed is not None, "the gate has no single-line `needs:` list"
    return {name.strip() for name in listed.group(1).split(",") if name.strip()}


class TestTheGate:
    def test_it_needs_every_other_job(self) -> None:
        """The one that rots. A leg added above and not here can fail while the
        required check passes."""
        assert needs() == set(FOUND) - {GATE}

    def test_it_needs_something(self) -> None:
        """A gate with an empty `needs` satisfies the test above only if the
        workflow has no other jobs, which would itself be the bug."""
        assert len(needs()) >= 4

    def test_it_runs_even_when_a_dependency_failed(self) -> None:
        """Without `if: always()` the gate is skipped rather than failed, and a
        skipped required check counts as satisfied."""
        assert "if: always()" in FOUND[GATE]

    def test_it_checks_each_dependency_s_result(self) -> None:
        """`needs` alone is not enough: `always()` means the gate runs whatever
        happened, so it has to look at what happened."""
        assert "toJSON(needs)" in FOUND[GATE]
        assert '.value.result != "success"' in FOUND[GATE]

    def test_it_refuses_to_pass_with_no_dependencies(self) -> None:
        """The guard inside the gate itself, for the case this file cannot see:
        a `needs` list emptied in a branch nobody ran the tests on."""
        assert "the gate has no dependencies" in FOUND[GATE]


class TestThePreflightMatchesCI:
    @pytest.mark.parametrize("command", PREFLIGHT)
    def test_ci_runs_every_preflight_command(self, command: str) -> None:
        """A preflight that has drifted from CI is worse than none, because it
        is trusted: a contributor runs it, sees green, and is surprised."""
        assert f"- run: {command}\n" in FOUND["lint"]

    @pytest.mark.parametrize("command", PREFLIGHT)
    def test_claude_md_quotes_the_same_commands(self, command: str) -> None:
        # The file wraps the line with a backslash, so the check is on the
        # pieces rather than on one string.
        assert command in (ROOT / "CLAUDE.md").read_text(encoding="utf-8")

    def test_the_order_is_the_same(self) -> None:
        """Cheapest first. A contributor who runs the preflight and gets a type
        error after a five-minute suite has been told nothing they could not
        have learned in two seconds."""
        lint = FOUND["lint"]
        at = [lint.index(f"- run: {command}\n") for command in PREFLIGHT]
        assert at == sorted(at)


class Sweep:
    """`mutation.yml` as two readings of the same file: settings, and prose."""

    def __init__(self, raw: str) -> None:
        # **Comments stripped, and this is not tidiness.** The first version of
        # these tests searched the whole file, and two of four hand-made edits
        # survived: `scope.txt` appears in the comment *above* the `path:` list
        # that collects it, and `timeout-minutes: 420` still matches when it has
        # been commented out. Both assertions were matching the prose that
        # explains the setting rather than the setting.
        #
        # No `#` appears inside a value in this file -- not in the `${{ }}`
        # expressions and not in the echoed strings -- so cutting at the first
        # one is exact here rather than approximate.
        self.text = "\n".join(line.split("#", 1)[0] for line in raw.splitlines())
        #: The prose, kept separately so a test can assert a setting is *not*
        #: only described.
        self.prose = raw


@pytest.fixture
def sweep() -> Iterator[Sweep]:
    yield Sweep(MUTATION.read_text(encoding="utf-8"))


class TestTheScheduledSweepSaysWhatItSwept:
    """`mutation.yml` had no test at all, and it is the workflow that runs for
    hours unattended.

    The hazard is CLAUDE.md section 8's: **a report cannot say what it was asked
    for.** `results.json` covering only the 41% of rows that are `tupferl/` is
    indistinguishable from one covering the tree, and a run killed part-way
    writes a report that reads exactly like a complete one -- `--all` implies
    `--batch`, so `_persist` writes after every file and the upload step is
    `if: always()`. The count looks right either way.

    Hand-parsed, like the rest of this file: `pyyaml` is not a declared
    dependency, and adding one to read four lines would be the larger change.
    That cost is real -- a hand parser can find nothing and then assert nothing
    -- so `test_the_file_is_the_one_this_thinks_it_is` states the precondition
    before anything else runs.
    """

    def test_the_file_is_the_one_this_thinks_it_is(self, sweep: Sweep) -> None:
        """The precondition. Every assertion below is a substring search, and a
        renamed file or a rewritten job would make all of them pass by finding
        nothing to disagree with."""
        assert "name: mutation sweep" in sweep.text
        assert "python -m tools.mutate --all" in sweep.text
        assert "upload-artifact" in sweep.text

    def test_the_comment_stripping_leaves_the_settings_alone(self, sweep: Sweep) -> None:
        """The fixture's own precondition. Strip too much and every assertion
        below passes by finding nothing; strip too little and they pass by
        finding a comment. Both happened."""
        assert "timeout-minutes:" in sweep.text, "stripping removed the settings"
        assert "Measured, not guessed" not in sweep.text, "comments are still being read"

    def test_both_ways_of_running_it_record_their_scope(self, sweep: Sweep) -> None:
        """Two branches, and the `--base` one is the *more* important: a report
        from a partial sweep is the one that could be mistaken for a whole
        one."""
        scoped = [line for line in sweep.text.splitlines() if "scope.txt" in line]
        recorded = sum(1 for line in scoped if "tee" in line)
        assert recorded >= 2, f"a branch records nothing: {scoped}"

    def test_the_scope_is_uploaded_beside_the_report(self, sweep: Sweep) -> None:
        """Written and not collected is the same as not written. The artifact is
        the only thing that outlives the runner."""
        after = sweep.text.split("upload-artifact", 1)[1]
        assert "results.json" in after
        assert "scope.txt" in after, "the scope is recorded and then thrown away"

    def test_the_job_still_has_a_ceiling(self, sweep: Sweep) -> None:
        """`ci.yml`'s jobs are checked for this above; this workflow is in
        another file and was never covered. A sweep with no ceiling holds a
        runner until GitHub's own limit, and a running job's log is a 404 -- a
        hang is the one failure with nothing to read."""
        assert re.search(r"timeout-minutes:\s*\d+", sweep.text)
