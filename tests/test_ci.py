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
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"

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


def jobs() -> dict[str, str]:
    """Top-level job names mapped to their block of the file.

    Only what follows the `jobs:` line, and only until the next top-level key:
    `on:` and `concurrency:` have two-space children too, and a parser that
    collected those would report `push` as a job.
    """
    text = workflow()
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


class TestTheParserWorks(unittest.TestCase):
    """Stated first, because everything below is vacuous without it."""

    def test_the_parser_finds_the_jobs(self) -> None:
        found = jobs()
        self.assertIn(GATE, found)
        self.assertIn("lint", found)
        self.assertIn("test", found)
        self.assertGreaterEqual(len(found), 4)

    def test_it_does_not_mistake_a_trigger_for_a_job(self) -> None:
        """`on:` has `push:` under it at the same indentation as a job name."""
        self.assertNotIn("push", jobs())
        self.assertNotIn("pull_request", jobs())

    def test_every_job_has_a_runner(self) -> None:
        for name, block in jobs().items():
            with self.subTest(job=name):
                self.assertIn("runs-on:", block)

    def test_every_job_has_a_timeout(self) -> None:
        """Without one a job gets GitHub's six-hour default, and the gate
        `needs:` every job -- so one wedged leg holds the pull request open with
        nothing in the log to read, because a running job's log is a 404.

        That is not hypothetical: the `macos` leg hung for twenty minutes on a
        `tcsetattr` waiting for output nobody was reading, while every other leg
        finished in under a minute. A timeout would have said so in ten.
        """
        for name, block in jobs().items():
            with self.subTest(job=name):
                self.assertIn("timeout-minutes:", block)


class TestTheGate(unittest.TestCase):
    def needs(self) -> set[str]:
        block = jobs()[GATE]
        listed = re.search(r"^    needs: \[([^\]]*)\]", block, re.MULTILINE)
        self.assertIsNotNone(listed, "the gate has no single-line `needs:` list")
        assert listed is not None
        return {name.strip() for name in listed.group(1).split(",") if name.strip()}

    def test_it_needs_every_other_job(self) -> None:
        """The one that rots. A leg added above and not here can fail while the
        required check passes."""
        others = set(jobs()) - {GATE}
        self.assertEqual(others, self.needs())

    def test_it_needs_something(self) -> None:
        """A gate with an empty `needs` satisfies the test above only if the
        workflow has no other jobs, which would itself be the bug."""
        self.assertGreaterEqual(len(self.needs()), 4)

    def test_it_runs_even_when_a_dependency_failed(self) -> None:
        """Without `if: always()` the gate is skipped rather than failed, and a
        skipped required check counts as satisfied."""
        self.assertIn("if: always()", jobs()[GATE])

    def test_it_checks_each_dependency_s_result(self) -> None:
        """`needs` alone is not enough: `always()` means the gate runs whatever
        happened, so it has to look at what happened."""
        block = jobs()[GATE]
        self.assertIn("toJSON(needs)", block)
        self.assertIn('.value.result != "success"', block)

    def test_it_refuses_to_pass_with_no_dependencies(self) -> None:
        """The guard inside the gate itself, for the case this file cannot see:
        a `needs` list emptied in a branch nobody ran the tests on."""
        self.assertIn("the gate has no dependencies", jobs()[GATE])


class TestThePreflightMatchesCI(unittest.TestCase):
    def test_ci_runs_every_preflight_command(self) -> None:
        """A preflight that has drifted from CI is worse than none, because it
        is trusted: a contributor runs it, sees green, and is surprised."""
        lint = jobs()["lint"]
        for command in PREFLIGHT:
            with self.subTest(command=command):
                self.assertIn(f"- run: {command}\n", lint)

    def test_claude_md_quotes_the_same_commands(self) -> None:
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        for command in PREFLIGHT:
            with self.subTest(command=command):
                # The file wraps the line with a backslash, so the check is on
                # the pieces rather than on one string.
                self.assertIn(command, text)

    def test_the_order_is_the_same(self) -> None:
        """Cheapest first. A contributor who runs the preflight and gets a type
        error after a five-minute suite has been told nothing they could not
        have learned in two seconds."""
        lint = jobs()["lint"]
        found = [lint.index(f"- run: {command}\n") for command in PREFLIGHT]
        self.assertEqual(sorted(found), found)


if __name__ == "__main__":
    unittest.main()
