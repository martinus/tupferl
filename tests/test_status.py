"""`tupferl status`, driven end to end -- plan §4's "never modifies anything".

Two things here are worth more than the rest, and both are about what a status
test can accidentally not check.

**Every fixture holds two managed files in *different* states**, and every
assertion names which file it expects a phrase beside. A machine where all the
managed files say the same thing cannot tell `status` from a command that prints
one phrase per file regardless -- CLAUDE.md §2's "two symmetric inputs", in the
spelling this command takes. `.bashrc` is the file under test throughout and
`.vimrc` is the control; `PHRASES` below is what makes "the wrong one" fail
rather than merely "no phrase at all".

**"It writes nothing" needs a run that would otherwise write.** A fingerprint
taken before and after `status` on a machine with nothing pending is equally
satisfied by a `status` that does nothing and by a machine where there was
nothing to do -- CLAUDE.md §2's negative assertion with no precondition. So
`TestStatusWritesNothing` arranges a pending change, checks the fingerprint is
unmoved, and then runs `sync` and checks it *moved* -- which is what establishes
that the fixture could have shown a write.
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from tests import support
from tupferl import inspection, paths, sync

#: Every phrase `status` can put beside a file name, keyed by the state that
#: produces it. Written out here rather than imported from
#: `tupferl.inspection.SAYS`: a table taken from the code under test cannot
#: notice the code changing (CLAUDE.md §2), and `test_cli.py`'s `MILESTONES`
#: carries the same note for the same reason -- it was written the other way
#: first and every mutation of it survived.
PHRASES = {
    "here": "changed here; the next sync stores it",
    "there": "changed in the repository; the next sync updates it",
    "gone": "missing from $HOME; the next sync restores it",
    "merges": "changed on both, and the two merge cleanly",
}

#: What the second machine's `.bashrc` starts as. Every line distinct, so a
#: one-line edit below is a one-hunk difference rather than an ambiguous one.
START = "one\ntwo\nthree\nfour\nfive\n"

#: The control file, and the reason it is here: it stays untouched in every
#: fixture, so a phrase landing on *it* is a failure.
CONTROL = "set number\nset expandtab\n"


class TestTheThreeShapesOfOneVerb(support.TwoMachines):
    """`status`, `--all` and `--diff` over one walk.

    They were three verbs. Folding them is only worth anything if each shape
    still answers its own question, so these are the assertions that would catch
    the fold being done badly: the plain status still hides what has nothing to
    report, `--all` still shows it *and* marks the overlay, `--diff` still shows
    lines rather than a summary, and a path still narrows.
    """

    def setUp(self) -> None:
        super().setUp()
        # Not `.gitconfig`, which is where the sandbox keeps the git identity:
        # overwriting it makes every commit fail with "unable to auto-detect
        # email address", which CLAUDE.md records as its own gotcha.
        self.first.write(".bashrc", "one\ntwo\n")
        self.first.write(".inputrc", "set editing-mode vi\n")
        for name in (".bashrc", ".inputrc"):
            self.assertEqual(0, self.first.call("add", str(self.first.home / name)))
        self.assertEqual(0, self.first.call("add", "--host", str(self.first.home / ".inputrc")))
        self.first.write(".bashrc", "ONE\ntwo\n")

    def said(self, *argv: str) -> str:
        status, out = self.first.say(*argv)
        self.assertEqual(0, status, out)
        return out

    def test_plain_status_names_only_what_has_something_to_report(self) -> None:
        """The unchanged file is most files on most machines, and a status that
        printed forty lines saying nothing happened would bury the one that
        mattered."""
        out = self.said("status")
        self.assertIn(".bashrc", out)
        self.assertNotIn(".inputrc", out)

    def test_all_names_everything_and_marks_the_overlay(self) -> None:
        """What `list` used to print, with each file's state beside it -- which
        is the half `list` could not say."""
        out = self.said("status", "--all")
        overlay = next(line for line in out.splitlines() if ".inputrc" in line)
        shared = next(line for line in out.splitlines() if ".bashrc" in line)
        self.assertTrue(overlay.startswith("host"), overlay)
        # The negative half, and without it a marker painted on *every* row
        # passes: "is this file overridden here" is only an answer if it can
        # come back no.
        self.assertFalse(shared.startswith("host"), shared)
        self.assertIn("unchanged", overlay)
        self.assertIn("1 from this host's overlay", out)

    def test_diff_shows_lines_rather_than_a_summary(self) -> None:
        out = self.said("status", "--diff")
        self.assertIn("--- .bashrc", out)
        self.assertIn("-ONE", out)
        self.assertNotIn("to change", out, "the summary line belongs to the other shape")

    def test_a_path_narrows_both_shapes(self) -> None:
        """One rule for the positional -- it limits what is shown -- rather than
        a rule that depends on which flag is also present."""
        self.assertNotIn(".inputrc", self.said("status", ".bashrc"))
        self.assertIn("1 file managed", self.said("status", ".bashrc"))
        self.assertIn("--- .bashrc", self.said("status", "--diff", ".bashrc"))


class Machine(support.TwoMachines):
    """`machine-b` synced and holding two managed files, ready to diverge.

    Both machines end up agreeing about `.bashrc` and `.vimrc`, so any line
    `status` prints afterwards is caused by what the test did and not by the
    fixture.
    """

    def setUp(self) -> None:
        super().setUp()
        self.first.write(".bashrc", START)
        self.first.write(".vimrc", CONTROL)
        self.assertEqual(0, self.first.call("add", str(self.first.home / ".vimrc")))
        self.assertEqual(0, self.first.call("sync"))
        self.assertEqual(0, self.second.call("init", str(self.remote)))
        # The second sync is not ceremony: `init` runs one, but `.bashrc` was
        # only just pushed, so this is what leaves both machines' snapshots
        # equal to both copies. Without it the tests below start from a machine
        # that already has something to report.
        self.assertEqual(0, self.second.call("sync"))
        status, said = self.second.say("status")
        self.assertEqual(0, status)
        self.assertNotIn(".bashrc", said, said)
        self.assertNotIn(".vimrc", said, said)

    def status(self) -> str:
        """`tupferl status` on the second machine, insisting it exited 0."""
        status, said = self.second.say("status")
        self.assertEqual(0, status, said)
        return said

    def line(self, said: str, name: str) -> str:
        """The one line of `said` that is about `name`.

        Raises rather than returning a default when there is none: a helper
        that answered `""` would make every `assertIn` below fail with "not in
        ''", which points at this function instead of at the missing line.
        """
        found = [row for row in said.splitlines() if row.startswith(name)]
        self.assertEqual(1, len(found), f"expected one line for {name} in:\n{said}")
        return found[0]

    def assertSaysAbout(self, name: str, phrase: str) -> None:
        """`name`'s line says `phrase`, and no other file's line does.

        The second half is the point. `.vimrc` is unchanged in every fixture
        here, so a `status` that printed one phrase for every managed file would
        satisfy the first half alone.
        """
        said = self.status()
        self.assertIn(phrase, self.line(said, name), said)
        others = [row for row in said.splitlines() if row and not row.startswith(name)]
        for row in others:
            self.assertNotIn(phrase, row, said)


class TestThePhrasesAreAllAccountedFor(unittest.TestCase):
    """`PHRASES` is written out, so something must say it is still the whole set.

    Without this, a phrase added to `inspection.SAYS` -- for an action `resolve`
    learns to produce -- arrives with no test, and every test below still
    passes: they each assert about one file in one state, and none of them can
    notice a sixth state existing. `tests/test_cli.py` pairs its written-out
    `MILESTONES` with the same assertion for the same reason.

    One-way on purpose. `SAYS` must be covered by `PHRASES`; `PHRASES` also
    holds the conflict and skipped wordings, which are built rather than looked
    up and so are not in `SAYS` at all.
    """

    def test_every_phrase_the_table_holds_is_tested_below(self) -> None:
        self.assertLessEqual(set(inspection.SAYS.values()), set(PHRASES.values()))

    def test_every_key_is_an_action_sync_really_has(self) -> None:
        """A key that is not one of `sync`'s action strings can never match, so
        the sentence beside it is unreachable and the state it was meant for
        raises `KeyError` instead. A typo here is invisible from the table."""
        for action in inspection.SAYS:
            with self.subTest(action=action):
                self.assertIn(action, sync.RULES)

    def test_the_settled_answers_are_deliberately_absent(self) -> None:
        """`status` never settles a conflict, so the four actions that only
        `sync.settled` produces cannot reach this table.

        Asserted rather than left implicit, because a row for one of them is
        worse than a missing row: it reads as a state that has been thought
        about, and no run can ever print it.
        """
        for action in (sync.KEPT_LOCAL, sync.KEPT_REMOTE, sync.KEPT_BOTH, sync.EDITED):
            with self.subTest(action=action):
                self.assertIn(action, sync.RULES)
                self.assertNotIn(action, inspection.SAYS)


class TestWhatEachChangeLooksLike(Machine):
    """Plan §7.4's rows, as sentences. One file changed, one file not."""

    def test_a_synced_machine_names_no_file(self) -> None:
        """The fixture's own claim, asserted where a reader will look for it:
        with nothing pending, the only lines are the remote and the summary."""
        said = self.status()
        self.assertNotIn(".bashrc", said)
        self.assertNotIn(".vimrc", said)
        self.assertIn("2 files managed, 0 to change, 0 in conflict", said)

    def test_an_edit_in_home_changed_here(self) -> None:
        self.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        self.assertSaysAbout(".bashrc", PHRASES["here"])

    def test_a_change_in_the_repository_changed_there(self) -> None:
        """Written into the repository's working tree, which is where a `sync`
        that pulled somebody else's commit would have put it."""
        (self.second.repo / ".bashrc").write_text("one\ntwo\nTHREE\nfour\nfive\n")
        self.assertSaysAbout(".bashrc", PHRASES["there"])

    def test_a_file_deleted_from_home_is_missing(self) -> None:
        (self.second.home / ".bashrc").unlink()
        self.assertSaysAbout(".bashrc", PHRASES["gone"])

    def test_disjoint_changes_on_both_sides_merge(self) -> None:
        """Both sides edited, different lines. `status` runs the real merge to
        find that out, which is the whole reason it can say so before a sync."""
        self.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        (self.second.repo / ".bashrc").write_text("one\ntwo\nthree\nfour\nFIVE\n")
        self.assertSaysAbout(".bashrc", PHRASES["merges"])

    def test_overlapping_changes_report_the_hunk_count(self) -> None:
        """The same two sides, editing the same line. The count comes from git's
        exit status (`merge.three_way`), so it is the number the prompt would
        show rather than a guess made here."""
        self.second.write(".bashrc", "one\nMINE\nthree\nfour\nfive\n")
        (self.second.repo / ".bashrc").write_text("one\nTHEIRS\nthree\nfour\nfive\n")
        self.assertSaysAbout(".bashrc", "changed on both, and they do not merge: 1 conflict")
        self.assertIn("2 files managed, 0 to change, 1 in conflict", self.status())

    def test_two_conflicts_are_two(self) -> None:
        """The plural, and that the number is not hard-coded at one.

        A longer file than `START`, because `git merge-file` needs three lines
        of agreement between two disagreements to call them two hunks -- and
        `START` has exactly three, which is the boundary. Written first and
        synced, so the snapshot is the base both edits are measured against.
        """
        lines = [f"line {number}\n" for number in range(12)]
        self.second.write(".bashrc", "".join(lines))
        self.assertEqual(0, self.second.call("sync"))
        mine = list(lines)
        mine[0], mine[11] = "MINE at the top\n", "MINE at the end\n"
        self.second.write(".bashrc", "".join(mine))
        theirs = list(lines)
        theirs[0], theirs[11] = "THEIRS at the top\n", "THEIRS at the end\n"
        (self.second.repo / ".bashrc").write_text("".join(theirs))
        self.assertSaysAbout(".bashrc", "2 conflicts to settle")

    def test_a_path_that_is_not_a_regular_file_is_skipped_with_a_reason(self) -> None:
        """A fifo, not a socket: `sun_path` is 104 bytes on macOS and a sandbox
        path plus the repository layout exceeds it, so `bind` raises and the
        test errors instead of testing."""
        (self.second.home / ".bashrc").unlink()
        os.mkfifo(self.second.home / ".bashrc")
        self.addCleanup((self.second.home / ".bashrc").unlink)
        self.assertSaysAbout(".bashrc", "skipped:")
        self.assertIn("is not a regular file", self.line(self.status(), ".bashrc"))

    def test_the_two_files_can_say_different_things_at_once(self) -> None:
        """The assertion the rest of this class rests on: two states, two lines,
        each with its own phrase. Without it, every test above would also pass
        against a `status` that printed the same phrase for every file."""
        self.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        (self.second.repo / ".vimrc").write_text("set number\nset ruler\n")
        said = self.status()
        self.assertIn(PHRASES["here"], self.line(said, ".bashrc"), said)
        self.assertIn(PHRASES["there"], self.line(said, ".vimrc"), said)
        self.assertIn("2 files managed, 2 to change, 0 in conflict", said)


class TestStatusWritesNothing(Machine):
    """Plan §4's "Never modifies anything", with the precondition established.

    `.git` is excluded from the fingerprint on purpose: `status` fetches, which
    moves remote-tracking refs and rewrites the directory's mtime. That is the
    one thing it is allowed to touch, and the module docstring of
    `tupferl/inspection.py` says why.
    """

    def fingerprint(self) -> set[tuple[str, int, bytes]]:
        """Every file under the second machine's `$HOME` outside `.git`.

        Path, mode and **content**. Size and mode alone was the first version of
        this and it could not fail: the edit these tests make is one line to
        upper case, so `ONE\ntwo\n...` is the same 24 bytes as the file it
        replaced, and the fingerprint after a real `sync` came back equal to the
        one before it. That is CLAUDE.md §2's fixture too weak to tell the two
        answers apart, found by the half of the test that exists to catch it.

        The mtime is left out deliberately: it is the one field a *read* can
        move on some filesystems, which would make this flaky rather than
        strict. Content covers everything mtime would have.
        """
        seen = set()
        for path in self.second.home.rglob("*"):
            if ".git" in path.parts or not path.is_file():
                continue
            rel = str(path.relative_to(self.second.home))
            seen.add((rel, path.stat().st_mode, path.read_bytes()))
        return seen

    def test_a_status_with_work_pending_writes_nothing_and_a_sync_writes(self) -> None:
        """Both halves in one test, because separated they are two claims that
        could each hold while the pair is meaningless.

        The `sync` at the end is what proves the fixture had something to write:
        without it, "the fingerprint did not move" is equally true of a machine
        with nothing to do.
        """
        self.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        before = self.fingerprint()
        self.status()
        self.assertEqual(before, self.fingerprint())

        self.assertEqual(0, self.second.call("sync"))
        self.assertNotEqual(before, self.fingerprint())

    def test_no_backup_directory_appears(self) -> None:
        """The backup root is created lazily by the first `sync` that replaces
        a `$HOME` file, and five empty ones would push the last real backup out
        of plan §5's window. A `status` that created one would do that silently.
        """
        (self.second.repo / ".bashrc").write_text("one\ntwo\nTHREE\nfour\nfive\n")
        self.assertFalse(self.second.backups.exists())
        self.status()
        self.assertFalse(self.second.backups.exists())

    def test_the_snapshot_is_left_where_the_last_sync_put_it(self) -> None:
        """The merge base is what makes a three-way comparison three-way. A
        `status` that moved it would make the *next* sync read a change on one
        side as no change at all -- silent, and the wrong way round.
        """
        with support.sandboxed(self.second.home, self.second.name):
            snapshot = paths.snapshot_dir(self.second.repo, self.second.name) / ".bashrc"
        self.assertEqual(START, snapshot.read_text())
        self.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        self.status()
        self.assertEqual(START, snapshot.read_text())

    def test_nothing_is_committed(self) -> None:
        """git's own record, which no fingerprint of the working tree can see."""
        before = self.second.git("rev-parse", "HEAD")
        self.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        self.status()
        self.assertEqual(before, self.second.git("rev-parse", "HEAD"))


class TestTheShapeOfTheReport(Machine):
    """The layout, which the rest of this file's assertions look straight past.

    Every test above asks "does this phrase appear beside this name?", and a
    `status` that printed a warning it should not, opened with a blank line, or
    lined the second column up wrongly answers all of them correctly. The
    mutation sweep said so: five survivors on `status` and `sides`, all of them
    about shape rather than content.
    """

    def test_a_healthy_machine_says_nothing_about_an_unfinished_merge(self) -> None:
        """The negative half of `TestStatusReportsWhatSyncRefuses`. Without it,
        `if marker is not None` can be true always and every other test passes.
        """
        self.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        self.assertNotIn("unfinished git operation", self.status())

    def test_nothing_to_report_does_not_open_with_a_blank_line(self) -> None:
        """The separator is only earned by something above it. Printed
        unconditionally, a quiet machine's status starts with an empty line,
        which reads as output having gone missing."""
        said = self.status()
        self.assertTrue(said.splitlines()[0].strip(), repr(said))

    def test_a_blank_line_separates_the_files_from_the_remote(self) -> None:
        """And the other direction: with files listed, the separator is there.
        The pair is what pins the branch -- either alone is satisfied by a
        `status` that never separates, or by one that always does."""
        self.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        rows = self.status().splitlines()
        blank = [index for index, row in enumerate(rows) if not row.strip()]
        self.assertEqual(1, len(blank), rows)
        self.assertTrue(rows[blank[0] - 1].startswith(".bashrc"), rows)
        self.assertIn("origin/", rows[blank[0] + 1], rows)

    def test_the_second_column_lines_up_under_names_of_different_lengths(self) -> None:
        """`.bashrc` and `.vimrc` differ by one character, so a width taken from
        the *shortest* name -- `max` written as `min` -- leaves the longer line
        unpadded and the two phrases one column apart."""
        self.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        (self.second.repo / ".vimrc").write_text("set number\nset ruler\n")
        rows = [row for row in self.status().splitlines() if row.startswith(".")]
        self.assertEqual(2, len(rows), rows)
        starts = {row.index("changed") for row in rows}
        self.assertEqual(1, len(starts), rows)


class TestTheRemoteHalf(Machine):
    """Plan §4's "and remotely", which is a fact about commits rather than files.

    The distinction this class exists for: the per-file lines compare `$HOME`
    with *this machine's* checkout, so anything sitting on the remote unpulled
    is invisible to them. A status that showed one file changed while thirty
    commits waited would be true and misleading, and the caveat line is the
    difference.
    """

    def test_a_machine_that_agrees_with_the_remote_says_so(self) -> None:
        self.assertIn("is exactly what this computer has", self.status())

    def test_commits_waiting_on_the_remote_are_counted(self) -> None:
        """Pushed from the first machine, so the second's fetch finds them."""
        self.first.write(".bashrc", "one\ntwo\nthree\nfour\nPUSHED\n")
        self.assertEqual(0, self.first.call("sync"))
        said = self.status()
        self.assertIn("1 commit to pull", said)
        self.assertNotIn("to push", said)

    def test_commits_here_and_not_there_are_counted_the_other_way(self) -> None:
        """`add` commits without pushing, which is how a machine gets ahead.

        The caveat about unpulled commits must *not* appear: nothing is waiting
        to be pulled, and a sentence that is always printed is one a reader
        stops seeing. That is the only fixture in this file with `ahead` above
        zero and `behind` at zero, so it is the only one that can say so.
        """
        self.second.write(".zshrc", "setopt nomatch\n")
        self.assertEqual(0, self.second.call("add", str(self.second.home / ".zshrc")))
        said = self.status()
        self.assertIn("1 commit to push", said)
        self.assertNotIn("to pull", said)
        self.assertNotIn("waiting to be pulled", said)

    def test_the_two_counts_are_separate_numbers(self) -> None:
        """Both directions at once, and the numbers differ -- so a status that
        printed one number twice, or swapped them, fails here. With equal
        counts on both sides this test could not tell either mistake."""
        for line in ("first\n", "second\n"):
            self.first.write(".bashrc", f"one\ntwo\nthree\nfour\n{line}")
            self.assertEqual(0, self.first.call("sync"))
        self.second.write(".zshrc", "setopt nomatch\n")
        self.assertEqual(0, self.second.call("add", str(self.second.home / ".zshrc")))
        said = self.status()
        self.assertIn("2 commits to pull", said)
        self.assertIn("1 commit to push", said)

    def test_being_behind_says_the_file_lines_are_not_the_whole_story(self) -> None:
        """The caveat, and that it is *not* printed when there is nothing to
        pull -- otherwise it would be a sentence that is always there, which a
        reader stops seeing."""
        self.assertNotIn("waiting to be pulled", self.status())
        self.first.write(".bashrc", "one\ntwo\nthree\nfour\nPUSHED\n")
        self.assertEqual(0, self.first.call("sync"))
        self.assertIn("waiting to be pulled", self.status())

    def test_an_unreachable_remote_is_reported_and_the_rest_still_prints(self) -> None:
        """A laptop with no network still has a local half worth reading, and
        `status` is the command someone runs when something is already wrong.

        The URL is repointed rather than the network being blocked: a path that
        does not exist fails the fetch for the same reason and needs nothing of
        the machine the suite runs on.
        """
        self.second.git("remote", "set-url", "origin", str(self.tmp / "not-a-repository"))
        self.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        said = self.status()
        self.assertIn("could not reach origin", said)
        self.assertIn("tupferl doctor", said)
        self.assertIn(PHRASES["here"], self.line(said, ".bashrc"), said)

    def test_no_remote_at_all_says_how_to_add_one(self) -> None:
        self.second.git("remote", "remove", "origin")
        said = self.status()
        self.assertIn("no remote is configured", said)
        self.assertIn("remote add origin", said)

    def test_a_branch_that_was_never_pushed_says_so(self) -> None:
        """`origin/<branch>` does not exist until something is pushed to it, and
        a fetch cannot invent it. A new local branch is the ordinary way to be
        in that state, and it is also the state a machine is in between `init`
        against an empty remote and its first successful push."""
        self.second.git("checkout", "-b", "somewhere-else")
        said = self.status()
        self.assertIn("does not exist yet", said)
        self.assertIn("tupferl sync", said)

    def test_a_comparison_git_will_not_make_is_unknown_rather_than_equal(self) -> None:
        """`distance` answering `None` -- which real git will not do here, since
        both refs resolve. Forced by patching tupferl's own wrapper rather than
        by breaking git: the branch exists precisely because "up to date" is the
        one wrong answer when the comparison could not be made, and a status
        that silently printed it would be indistinguishable from a healthy one.
        """
        with mock.patch("tupferl.gitrepo.distance", return_value=None):
            said = self.status()
        self.assertIn("git would not compare HEAD with", said)
        self.assertNotIn("is exactly what this computer has", said)

    def test_a_detached_head_is_reported_rather_than_compared(self) -> None:
        """`gitrepo.branch` answers `None`, and there is then no `<remote>/
        <branch>` to measure against. Reported, because a status that silently
        skipped the remote line would look like a machine that is up to date."""
        self.second.git("checkout", "--detach", "HEAD")
        said = self.status()
        self.assertIn("no branch checked out", said)


class TestStatusReportsWhatSyncRefuses(Machine):
    """The two states where `sync` raises and `status` does not.

    `sync` is about to write, so it stops. `status` is what someone runs to find
    out *why* sync stopped, and refusing to say anything is the least useful
    moment to refuse.
    """

    def test_an_unfinished_merge_is_a_line_not_an_error(self) -> None:
        (self.second.repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
        self.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        status, said = self.second.say("status")
        self.assertEqual(0, status, said)
        self.assertIn("unfinished git operation", said)
        self.assertIn("MERGE_HEAD", said)
        self.assertIn(PHRASES["here"], self.line(said, ".bashrc"), said)

    def test_sync_still_refuses_the_same_state(self) -> None:
        """The other half, which is what makes the test above about `status`
        rather than about `MERGE_HEAD` being harmless."""
        (self.second.repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
        status, said = self.second.say("sync")
        self.assertEqual(2, status, said)
        self.assertIn("unfinished git operation", said)


class TestAMachineWithNothingManaged(support.SandboxCase):
    """`init` and no `add`, which is every second machine's first minute."""

    def test_status_says_how_to_start(self) -> None:
        from tupferl.__main__ import main

        remote = support.make_remote(self.tmp / "remote.git", self.env)
        with support.quiet():
            self.assertEqual(0, main(["init", str(remote)]))
        with support.quiet() as said:
            self.assertEqual(0, main(["status"]))
        self.assertIn("nothing is managed yet", said.getvalue())
        self.assertNotIn("0 files managed", said.getvalue())
