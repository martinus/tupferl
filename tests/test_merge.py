"""Example tests for the merge primitive: the cases a generator cannot reach.

`tests/test_merge_properties.py` holds plan §7.2's properties 1 and 2 and is
where the general claims live. This file is the boundary cases, and every one of
them is here because it decides something the sync engine does:

- a conflict must be *counted*, because that is how sync tells "written" from
  "left alone";
- a binary file has no merge, and must not be reported as a failed one;
- a missing snapshot is an empty base, so a file added on two machines conflicts
  rather than silently taking one side.
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from tests import support
from tupferl import gitrepo, merge
from tupferl.errors import TupferlError

BASE = b"one\ntwo\nthree\n"


class TestConflictsAreCounted(unittest.TestCase):
    def test_a_clean_merge_reports_none(self) -> None:
        got = merge.three_way(".bashrc", BASE, b"ONE\ntwo\nthree\n", b"one\ntwo\nTHREE\n")
        self.assertEqual(0, got.conflicts)
        self.assertEqual(b"ONE\ntwo\nTHREE\n", got.data)

    def test_both_sides_changing_one_line_is_one_conflict(self) -> None:
        got = merge.three_way(".bashrc", BASE, b"ours\ntwo\nthree\n", b"theirs\ntwo\nthree\n")
        self.assertEqual(1, got.conflicts)

    def test_two_disagreements_are_two_conflicts(self) -> None:
        """The count is a count, not a flag. Milestone 4 shows it per file, and a
        merge that reported 1 for everything would be indistinguishable here from
        one that reported `bool`."""
        base = b"a\nb\nc\nd\ne\nf\ng\n"
        got = merge.three_way(".bashrc", base, b"A\nb\nc\nd\ne\nf\nG\n", b"1\nb\nc\nd\ne\nf\n7\n")
        self.assertEqual(2, got.conflicts)

    def test_the_markers_name_the_file_and_not_a_temporary_path(self) -> None:
        """The three sides are written to a throwaway directory, and git labels
        its output with the file names it was given. Without `-L` the user's
        `.bashrc` would end up carrying a `/tmp` path from this machine."""
        got = merge.three_way(".bashrc", BASE, b"ours\ntwo\nthree\n", b"theirs\ntwo\nthree\n")
        assert got.data is not None
        text = got.data.decode("utf-8")
        self.assertIn("<<<<<<< .bashrc (this computer)", text)
        self.assertIn(">>>>>>> .bashrc (the repository)", text)
        self.assertNotIn("/tmp", text)
        self.assertNotIn("tupferl-merge-", text)


class TestBinaryFilesHaveNoMerge(unittest.TestCase):
    """The case `tests/test_merge_properties.py` excludes from its generator.

    git refuses a file with a NUL byte near its start, and the right answer is
    "this is one conflict covering the whole file" rather than an error -- a
    user whose `.gnupg` keyring is managed should be told it needs deciding, not
    that their git installation is broken.
    """

    def test_a_nul_byte_makes_the_whole_file_one_conflict(self) -> None:
        """`1`, not `merge.WHOLE_FILE`. Written against the constant, this test
        passed with the constant mutated to 0 and to 2 -- a test containing a
        copy of the code it checks cannot fail (CLAUDE.md §2), and the mutation
        sweep is what found it. 0 in particular matters: `report` prints the
        count, and "0 to settle" reads as nothing to do."""
        got = merge.three_way(".keyring", b"\x00base\n", b"\x00ours\n", b"\x00theirs\n")
        self.assertEqual(1, got.conflicts)
        self.assertIsNone(got.data)

    def test_one_binary_side_is_enough(self) -> None:
        """Text on two sides and binary on the third is still not mergeable, and
        `all()` over the three sides is what says so."""
        for side in ("base", "ours", "theirs"):
            with self.subTest(side=side):
                sides = {"base": b"a\n", "ours": b"b\n", "theirs": b"c\n"}
                sides[side] = b"\x00\n"
                got = merge.three_way(".keyring", sides["base"], sides["ours"], sides["theirs"])
                self.assertEqual(1, got.conflicts)

    def test_a_nul_past_gits_probe_is_still_text(self) -> None:
        """git looks at the first 8000 bytes and no further, and so does
        `is_text`. A probe that read the whole file would refuse to merge a file
        git merges happily -- measured at 7999 (refused) and 8000 (merged)."""
        self.assertFalse(merge.is_text(b"x" * (merge.PROBE - 1) + b"\x00"))
        self.assertTrue(merge.is_text(b"x" * merge.PROBE + b"\x00"))


class TestAMissingSnapshotIsAnEmptyBase(unittest.TestCase):
    def test_two_machines_that_added_the_same_file_conflict(self) -> None:
        """No snapshot means no common ancestor, so nothing in the data says
        which side is newer. Taking one silently is the loss this refuses."""
        got = merge.three_way(".bashrc", None, b"from A\n", b"from B\n")
        self.assertEqual(1, got.conflicts)

    def test_identical_additions_over_no_base_still_merge(self) -> None:
        """Both machines wrote the same bytes, so there is nothing to decide --
        and a merge that conflicted here would make the common "I set this up
        the same way twice" case need a human."""
        got = merge.three_way(".bashrc", None, b"same\n", b"same\n")
        self.assertEqual(0, got.conflicts)
        self.assertEqual(b"same\n", got.data)


class TestAMergeThatCouldNotRun(unittest.TestCase):
    def test_git_failing_is_an_error_and_not_a_conflict(self) -> None:
        """A merge that never happened must not look like one that happened
        badly: sync *skips* a conflict and carries on, which for a git that will
        not run would mean skipping every file and reporting them as the user's
        problem.

        Driven by making `git` genuinely unfindable rather than by a mock of
        `merge_file` -- plan §7.1 forbids mocking git, and a mock would assert
        that this function reads a field where this asserts what happens when
        git is missing, which is the thing that goes wrong.

        `PATH=""` and not `del PATH`: with the variable *absent*, `subprocess`
        falls back to `confstr("CS_PATH")` and finds `/usr/bin/git` anyway, so
        the obvious spelling of this fixture would have merged successfully and
        the test would have failed for the wrong reason. Measured both ways.
        """
        with mock.patch.dict(os.environ, {"PATH": ""}), self.assertRaises(TupferlError) as caught:
            merge.three_way(".bashrc", BASE, b"ours\n", b"theirs\n")
        self.assertIn(".bashrc", str(caught.exception))
        self.assertIn("not installed", str(caught.exception))


class TestAGitThatDiedRatherThanAnswered(unittest.TestCase):
    def test_a_signal_killed_git_is_an_error_not_a_conflict_count(self) -> None:
        """`git merge-file` reports conflicts *as its exit status*, so the guard
        has to reject a status that is not a count. A process killed by a signal
        exits `-signal`, and SIGHUP is `-1` -- which reads as "one conflict" to
        any guard that accepts negative numbers. The user would then be told
        their file conflicts in `-1` places.

        This is the one test in the project that puts a stub on `PATH` instead of
        driving the real binary, and plan §7.1's rule is aimed at something else:
        asserting against a fake git rather than exercising a real one. The
        subject here is what *tupferl* does with a hostile exit status, and no
        argument to the real git produces one on demand.
        """
        with support.tempdir() as box:
            stub = box / "git"
            # Python rather than `#!/bin/sh\nkill -HUP $$`, and the reason is a
            # real one: a signal ignored on entry to a non-interactive shell
            # cannot be reset by it (POSIX), and `nohup` ignores SIGHUP and
            # passes that disposition to every descendant. Launch a mutation
            # sweep under `nohup` and the stub's kill is a no-op, `sh` exits 0,
            # and this test fails -- which voids a whole sweep and blames a file
            # nothing touched. A process may always change its *own* handler, so
            # restoring SIG_DFL first makes the fixture independent of how the
            # suite was started.
            stub.write_text(
                f"#!{sys.executable}\n"
                "import os, signal\n"
                "signal.signal(signal.SIGHUP, signal.SIG_DFL)\n"
                "os.kill(os.getpid(), signal.SIGHUP)\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            with mock.patch.dict(os.environ, {"PATH": f"{box}:{os.environ['PATH']}"}):
                self.assertEqual(-1, gitrepo.git(["merge-file"]).code)
                with self.assertRaises(TupferlError) as caught:
                    merge.three_way(".bashrc", BASE, b"ours\n", b"theirs\n")
        self.assertIn(".bashrc", str(caught.exception))


if __name__ == "__main__":
    unittest.main()


class TestKeepingBothVersions(unittest.TestCase):
    """`merge.keep_both`, the conflict prompt's `[b]`."""

    def test_it_keeps_both_sides_of_every_hunk(self) -> None:
        got = merge.keep_both(".bashrc", BASE, b"alpha\nMINE\ngamma\n", b"alpha\nTHEIRS\ngamma\n")
        self.assertEqual(b"alpha\nMINE\nTHEIRS\ngamma\n", got)

    def test_it_leaves_no_markers(self) -> None:
        """The property, stated apart from the bytes: a union merge that kept a
        marker would put `<<<<<<<` into the file on both computers."""
        got = merge.keep_both(".bashrc", BASE, b"alpha\nMINE\ngamma\n", b"alpha\nTHEIRS\ngamma\n")
        self.assertNotIn(b"<<<<<<<", got)
        self.assertNotIn(b"=======", got)

    def test_a_binary_side_is_refused_rather_than_returned_empty(self) -> None:
        """The guard the prompt can never reach -- `[b]` is not offered for a file
        with no lines -- but the function is callable and its contract is
        `bytes`. Returning `None` from here would be a `TypeError` two frames
        later, in `sync.settled`, about a file rather than about the merge.
        """
        with self.assertRaises(TupferlError) as raised:
            merge.keep_both(".icon", b"\x00base", b"\x00mine", b"\x00theirs")
        self.assertIn(".icon", str(raised.exception))
