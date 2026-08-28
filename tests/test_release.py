"""The release workflow's guards, asserted the way `tests/test_ci.py` asserts
the gate's.

Uploading to PyPI is the one irreversible act in this repository: a version
number, once taken, can never be reused, and a wrong 1.0.0 is 1.0.1 for ever.
Every guard that matters therefore runs *before* the upload -- and a guard that
was deleted, or that stopped being reached, produces a release that looks
exactly like a correct one. That is the same shape as a required check being
skipped rather than run, and it wants the same answer: a test.

Parsed by hand, like `test_ci.py`, and with the same cost: a hand parser that
finds nothing asserts nothing. So `TestTheParserFindsTheJobs` states the
precondition on its own, and every other test here begins from a job set that is
known to be non-empty.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"

#: Every job the workflow has. Written here as well as in the file so that a job
#: quietly deleted -- `pypi`, say -- fails a test rather than a release.
JOBS = ("check", "build", "pypi", "github")

#: The three things `check` establishes before anything is built, each of which
#: is a way to publish something nobody looked at. Matched on the step *name*,
#: which is the line a reader of the workflow sees.
GUARDS = (
    "the tag matches the version",
    "the tagged commit is on main",
)

#: CLAUDE.md §7's preflight, in order. The same tuple `tests/test_ci.py` holds,
#: and for the same reason: a tag is not evidence that CI ever saw the commit,
#: so the release re-runs it rather than trusting that it happened.
PREFLIGHT = (
    "ruff check .",
    "ruff format --check .",
    "mypy tupferl tests tools",
    "python -m tools.run_tests",
)


def workflow() -> str:
    return RELEASE.read_text(encoding="utf-8")


def settings(text: str) -> str:
    """`text` with its comment lines removed.

    **Every negative assertion here reads this and not the raw file.** This
    workflow explains itself in comments, so "the file does not contain
    `skip-existing`" was false the moment a comment said *why* it does not --
    the first version of that test failed on its own explanation. A negative
    assertion that cannot tell a setting from prose about the setting is one
    that goes red when somebody documents the thing it is guarding, and the fix
    reached for under that pressure is deleting either the comment or the test.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def jobs() -> dict[str, str]:
    """Top-level job names mapped to their block of the file.

    Only what follows `jobs:`, and only to the next top-level key -- `on:`,
    `env:` and `concurrency:` have two-space children too, and a parser that
    collected those would report `push` as a job.
    """
    text = workflow()
    start = re.search(r"^jobs:$", text, re.MULTILINE)
    assert start, "no `jobs:` key in the workflow"
    body = text[start.end() :]
    end = re.search(r"^\S", body, re.MULTILINE)
    body = body[: end.start()] if end else body

    found: dict[str, str] = {}
    for match in re.finditer(r"^  ([a-z][\w-]*):$", body, re.MULTILINE):
        after = body[match.end() :]
        nxt = re.search(r"^  [a-z][\w-]*:$", after, re.MULTILINE)
        found[match.group(1)] = after[: nxt.start()] if nxt else after
    return found


class TestTheParserFindsTheJobs(unittest.TestCase):
    """The precondition for everything below. Without it a parser that matched
    nothing would satisfy every "the workflow contains X" test vacuously, in the
    flattering direction."""

    def test_the_file_exists_and_is_not_empty(self) -> None:
        self.assertTrue(RELEASE.is_file(), f"no {RELEASE}")
        self.assertGreater(len(workflow()), 500)

    def test_every_expected_job_is_found(self) -> None:
        self.assertEqual(sorted(JOBS), sorted(jobs()))


class TestNothingIsPublishedUnchecked(unittest.TestCase):
    """The guards, and that the jobs which publish depend on them.

    Each is a distinct way to put something on PyPI that nobody agreed to, and
    none of them announces itself afterwards: a wheel built from the wrong tree
    is a filename, and a tag cut from a branch records no branch.
    """

    def test_the_check_job_holds_every_guard(self) -> None:
        block = jobs()["check"]
        for guard in GUARDS:
            self.assertIn(guard, block, f"the {guard!r} guard is gone")

    def test_each_guard_runs_only_for_a_tag(self) -> None:
        """A guard that ran on `workflow_dispatch` too would fail every dry run
        -- there is no tag to compare -- and the fix somebody reaches for under
        that pressure is deleting the guard. Asserted so the `if` is understood
        as part of it rather than as noise."""
        block = jobs()["check"]
        for guard in GUARDS:
            after = block[block.index(guard) :]
            self.assertIn(
                "startsWith(github.ref, 'refs/tags/')",
                after[: after.index("run:")],
                f"the {guard!r} guard does not say when it applies",
            )

    def test_the_preflight_runs_on_the_tagged_tree_in_order(self) -> None:
        """A tag proves somebody typed a command, not that anything passed on
        this tree. The commit may predate a merge that broke it -- and a red
        main is exactly when someone reaches for a release of the last good
        one."""
        block = jobs()["check"]
        at = -1
        for step in PREFLIGHT:
            found = block.find(step, at + 1)
            self.assertNotEqual(-1, found, f"the release never runs `{step}`")
            self.assertGreater(found, at, f"`{step}` runs out of order")
            at = found

    def test_publishing_waits_on_the_checks(self) -> None:
        """`needs`, so a failed guard *skips* the upload rather than racing it.

        No `if: always()` anywhere here, which is the opposite of what the CI
        gate wants and for the same underlying reason: there, a skipped job
        counted as satisfied and had to be forced to run; here, skipping is
        precisely the behaviour that keeps a bad build off PyPI.
        """
        found = jobs()
        self.assertIn("needs: check", found["build"])
        self.assertIn("needs: [check, build]", found["pypi"])
        self.assertIn("needs: [check, build, pypi]", found["github"])
        self.assertNotIn(
            "always()", settings(workflow()), "a release job would run after a failed guard"
        )

    def test_only_a_tag_publishes(self) -> None:
        """`workflow_dispatch` is a dry run, and the thing that makes it safe to
        reach for is that the two publishing jobs test the ref rather than
        anyone remembering not to press it."""
        for name in ("pypi", "github"):
            self.assertIn(
                "if: startsWith(github.ref, 'refs/tags/')",
                jobs()[name],
                f"{name} would publish from a branch",
            )


class TestTheUploadCannotSucceedQuietly(unittest.TestCase):
    """CLAUDE.md §8: never trust a green run you cannot explain. A release has
    two ways to report success having done nothing, and both are one line."""

    def test_a_duplicate_upload_is_an_error_rather_than_a_no_op(self) -> None:
        """`skip-existing` turns re-running a finished release into a green tick
        over an upload that did not happen -- indistinguishable from a first run
        that worked, which is the direction every mistake in this class errs."""
        self.assertIn("gh-action-pypi-publish", jobs()["pypi"], "nothing publishes at all")
        self.assertNotIn("skip-existing", settings(jobs()["pypi"]))

    def test_the_build_is_checked_before_it_is_uploaded(self) -> None:
        """`twine check --strict` reads the long description the README becomes,
        which is the usual thing to be malformed -- and PyPI rejects it *after*
        the version number is spent."""
        self.assertIn("twine check --strict", jobs()["build"])

    def test_the_built_wheel_is_installed_and_asked_its_version(self) -> None:
        """The same claim ci.yml's `install` job makes, asked of the artifact
        that is about to be published rather than of the source tree. It is the
        last point at which a wheel missing the package or the entry point is
        still recallable."""
        block = jobs()["build"]
        self.assertIn("tupferl --version", block)
        self.assertIn("python -m venv", block)

    def test_the_artifact_names_are_asserted(self) -> None:
        """`python -m build` cannot fail for a wrong version: it names the file
        after whatever `hatchling` read. Without this the only place the tag and
        the wheel could disagree is a filename nobody reads until it is live."""
        self.assertIn("dist/tupferl-$version.tar.gz", jobs()["build"])


class TestTheWorkflowAsksForLittle(unittest.TestCase):
    """Permissions, which are the other thing a release workflow gets wrong once
    and lives with."""

    def test_the_default_is_nothing(self) -> None:
        self.assertRegex(workflow(), r"(?m)^permissions:\s*\{\}\s*$")

    def test_only_pypi_may_mint_a_token_and_only_github_may_write(self) -> None:
        """Trusted Publishing needs `id-token: write` and nothing else; the
        release needs `contents: write` and nothing else. Neither job should
        have the other's."""
        found = {name: settings(block) for name, block in jobs().items()}
        self.assertIn("id-token: write", found["pypi"])
        self.assertNotIn("contents: write", found["pypi"])
        self.assertIn("contents: write", found["github"])
        self.assertNotIn("id-token: write", found["github"])

    def test_the_checking_jobs_only_read(self) -> None:
        for name in ("check", "build"):
            block = settings(jobs()[name])
            self.assertIn("contents: read", block)
            granted = block.split("permissions:")[1].split("steps:")[0]
            self.assertNotIn("write", granted, f"{name} asks for a write it does not need")


class TestTheHistoryIsDeepEnoughToJudge(unittest.TestCase):
    """`git branch --contains` needs the branches, and a release checkout is
    shallow by default -- with depth 1 the commit is on no branch at all and the
    guard would fail on every correct release. The fix somebody reaches for then
    is deleting the guard, so the reason is asserted rather than remembered."""

    def test_the_check_job_fetches_the_whole_history(self) -> None:
        self.assertIn("fetch-depth: 0", jobs()["check"])


class TestEveryJobIsBounded(unittest.TestCase):
    """The same reason ci.yml gives: a hang is otherwise GitHub's six-hour
    default, and a running job's log is a 404 -- so a wedged release is the one
    failure with nothing to read."""

    def test_no_job_is_unbounded(self) -> None:
        for name, block in jobs().items():
            self.assertIn("timeout-minutes:", block, f"{name} has no timeout")


if __name__ == "__main__":
    unittest.main()
