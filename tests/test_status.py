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
import sys
from dataclasses import dataclass
from unittest import mock

import pytest

from tests import support
from tupferl import colours, inspection, paths, sync

#: Every phrase `status` can put beside a file name, keyed by the state that
#: produces it. Written out here rather than imported from
#: `tupferl.inspection.SAYS`: a table taken from the code under test cannot
#: notice the code changing (CLAUDE.md §2), and `test_cli.py`'s `MILESTONES`
#: carries the same note for the same reason -- it was written the other way
#: first and every mutation of it survived.
PHRASES = {
    "here": "store the change you made here",
    "there": "update it from the repository",
    "gone": "restore it; it is missing from $HOME",
    "merges": "merge both changes; they do not overlap",
}

#: The direction marker each state puts in the first column, written out for
#: `PHRASES`' reason. It carries a claim the phrase does not: `here` and `there`
#: are opposite directions, so a marker table that had them the same way round
#: would be a status telling the user their edit was about to be overwritten --
#: `test_the_two_files_can_say_different_things_at_once` is where that is caught,
#: because one run showing both is the only fixture a single wrong constant
#: cannot satisfy.
MARKS = {"here": "->", "there": "<-", "gone": "<-", "merges": "<>"}

#: What `.bashrc` holds on both machines when a fixture here starts, which is
#: `support.STARTS_AS` because that is what `template()` synced. Aliased rather
#: than written out again: a second copy is free to drift from the tree these
#: tests are handed, and the drift would show up as a diff nobody asked for
#: rather than as a failure naming the constant.
START = support.STARTS_AS

#: The control file, and the reason it is here: it stays untouched in every
#: fixture, so a phrase landing on *it* is a failure.
CONTROL = "set number\nset expandtab\n"


@dataclass(frozen=True)
class Shapes(support.TwoMachines):
    """One machine with a shared file edited and an overlay file untouched."""

    def said(self, *argv: str) -> str:
        status, out = self.first.say(*argv)
        assert status == 0, out
        return out


@pytest.fixture
def shapes(two_machines: support.TwoMachines) -> Shapes:
    box = Shapes(**vars(two_machines))
    # Not `.gitconfig`, which is where the sandbox keeps the git identity:
    # overwriting it makes every commit fail with "unable to auto-detect
    # email address", which CLAUDE.md records as its own gotcha.
    box.first.write(".bashrc", "one\ntwo\n")
    box.first.write(".inputrc", "set editing-mode vi\n")
    for name in (".bashrc", ".inputrc"):
        assert box.first.call("add", str(box.first.home / name)) == 0
    assert box.first.call("add", "--host", str(box.first.home / ".inputrc")) == 0
    box.first.write(".bashrc", "ONE\ntwo\n")
    return box


@pytest.mark.usefixtures("shapes")
class TestTheThreeShapesOfOneVerb:
    """`status`, `--all` and `--diff` over one walk.

    They were three verbs. Folding them is only worth anything if each shape
    still answers its own question, so these are the assertions that would catch
    the fold being done badly: the plain status still hides what has nothing to
    report, `--all` still shows it *and* marks the overlay, `--diff` still shows
    lines rather than a summary, and a path still narrows.
    """

    def test_plain_status_names_only_what_has_something_to_report(self, shapes: Shapes) -> None:
        """The unchanged file is most files on most machines, and a status that
        printed forty lines saying nothing happened would bury the one that
        mattered."""
        out = shapes.said("status")
        assert ".bashrc" in out
        assert ".inputrc" not in out

    def test_all_names_everything_and_marks_the_overlay(self, shapes: Shapes) -> None:
        """What `list` used to print, with each file's state beside it -- which
        is the half `list` could not say."""
        out = shapes.said("status", "--all")
        overlay = next(line for line in out.splitlines() if ".inputrc" in line)
        shared = next(line for line in out.splitlines() if ".bashrc" in line)
        assert "host" in overlay.split(), overlay
        # The negative half, and without it a marker painted on *every* row
        # passes: "is this file overridden here" is only an answer if it can
        # come back no.
        assert "host" not in shared.split(), shared
        assert "unchanged" in overlay
        assert "1 from this host's overlay" in out

    def test_diff_shows_lines_rather_than_a_summary(self, shapes: Shapes) -> None:
        out = shapes.said("status", "--diff")
        assert "--- .bashrc" in out
        # `+ONE`, not `-ONE`. Only `$HOME` changed here, so the next sync pushes
        # it and the *repository* is the side being replaced -- see
        # `inspection.rendered`. This assertion read `-ONE` until the
        # orientation was fixed, which is what a test written against the bug
        # looks like from the inside.
        assert "+ONE" in out
        assert "to change" not in out, "the summary line belongs to the other shape"

    def test_the_two_directions_are_oriented_oppositely_in_one_run(self, shapes: Shapes) -> None:
        """The plumbing, which the unit tests of `rendered` cannot reach.

        They call it with an action; `shows` is what has to pass the file's
        *own* action through, and a `shows` that handed over a constant would
        satisfy every one of them. Two files in one run, going opposite ways,
        is the fixture that cannot be satisfied that way -- one output, two
        orientations, so no single hard-coded answer is right for both.

        `.bashrc` is edited here and pushed. `.inputrc`'s repository copy is
        edited instead, which is what a fetched change looks like before it is
        applied: the repository is ahead and `$HOME` has not moved.
        """
        # `stored()`, not a path built here: it goes through `manifest.location`,
        # so a change to the overlay layout moves this test with it rather than
        # leaving it asserting about a path nothing writes any more.
        stored = shapes.first.stored(".inputrc", host=True)
        assert stored.is_file(), f"the overlay fixture moved: {stored}"
        stored.write_text("set editing-mode emacs\n", encoding="utf-8")
        first = shapes.first
        support.git(["commit", "-am", "the repository moved on"], first.repo, first.env)

        out = shapes.said("status", "--diff")
        # Asserted against the whole output rather than a slice of it: the two
        # names disambiguate on their own, and slicing from `out.index(name)`
        # cut off the `--- ` the assertion was about.
        #
        # `.bashrc` goes out, so the repository's copy is what disappears.
        assert "--- .bashrc (the repository)" in out
        assert "+ONE" in out
        # `.inputrc` comes in, so this computer's copy is what disappears.
        assert "--- .inputrc (this computer)" in out
        assert "+set editing-mode emacs" in out

    def test_a_path_narrows_both_shapes(self, shapes: Shapes) -> None:
        """One rule for the positional -- it limits what is shown -- rather than
        a rule that depends on which flag is also present."""
        assert ".inputrc" not in shapes.said("status", ".bashrc")
        assert "1 file managed" in shapes.said("status", ".bashrc")
        assert "--- .bashrc" in shapes.said("status", "--diff", ".bashrc")


@dataclass(frozen=True)
class Synced(support.TwoMachines):
    """`machine-b` synced and holding two managed files, ready to diverge.

    Both machines end up agreeing about `.bashrc` and `.vimrc`, so any line
    `status` prints afterwards is caused by what the test did and not by the
    fixture.
    """

    def status(self) -> str:
        """`tupferl status` on the second machine, insisting it exited 0."""
        status, said = self.second.say("status")
        assert status == 0, said
        return said

    def line(self, said: str, name: str) -> str:
        """The one line of `said` that is about `name`.

        Raises rather than returning a default when there is none: a helper
        that answered `""` would make every `in` assertion below fail with "not
        in ''", which points at this function instead of at the missing line.
        """
        found = [row for row in said.splitlines() if name in row.split()]
        assert len(found) == 1, f"expected one line for {name} in:\n{said}"
        return found[0]

    def says_about(self, name: str, phrase: str) -> None:
        """`name`'s line says `phrase`, and no other file's line does.

        The second half is the point. `.vimrc` is unchanged in every fixture
        here, so a `status` that printed one phrase for every managed file would
        satisfy the first half alone.
        """
        said = self.status()
        assert phrase in self.line(said, name), said
        others = [row for row in said.splitlines() if row and name not in row.split()]
        for row in others:
            assert phrase not in row, said

    def marks(self, name: str, mark: str) -> None:
        """`name`'s row opens with `mark`, and no other file's row does.

        The direction column, held to the same standard as the phrase: `.vimrc`
        is unchanged in every fixture here, so a marker printed on every row
        would satisfy the first half on its own.
        """
        said = self.status()
        assert self.line(said, name).split()[0] == mark, said
        for row in said.splitlines():
            if row.startswith("  ") and name not in row.split():
                assert row.split()[0] != mark, said


@pytest.fixture
def synced(two_machines: support.TwoMachines) -> Synced:
    box = Synced(**vars(two_machines))
    box.first.write(".bashrc", START)
    box.first.write(".vimrc", CONTROL)
    assert box.first.call("add", str(box.first.home / ".vimrc")) == 0
    assert box.first.call("sync") == 0
    assert box.second.call("init", str(box.remote)) == 0
    # The second sync is not ceremony: `init` runs one, but `.bashrc` was
    # only just pushed, so this is what leaves both machines' snapshots
    # equal to both copies. Without it the tests below start from a machine
    # that already has something to report.
    assert box.second.call("sync") == 0
    status, said = box.second.say("status")
    assert status == 0
    assert ".bashrc" not in said, said
    assert ".vimrc" not in said, said
    return box


class TestThePhrasesAreAllAccountedFor:
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
        assert {said for _, _, said in inspection.SAYS.values()} <= set(PHRASES.values())

    def test_every_marker_the_table_holds_is_tested_below(self) -> None:
        """The same accounting for the first column. Without it a marker added
        for a new action arrives with nothing asserting it, exactly as a phrase
        would -- and a marker is the part a reader scans."""
        assert {mark for mark, _, _ in inspection.SAYS.values()} <= set(MARKS.values())

    def test_the_table_is_not_empty(self) -> None:
        """The precondition the two parametrized tests below cannot state.

        A `SAYS` that had become empty would collect *no* cases for the first of
        them -- CLAUDE.md §2's zero-iteration trap, which under pytest happens
        at collection time and so removes the test rather than failing it.
        """
        assert inspection.SAYS

    @pytest.mark.parametrize("action", sorted(inspection.SAYS))
    def test_every_key_is_an_action_sync_really_has(self, action: str) -> None:
        """A key that is not one of `sync`'s action strings can never match, so
        the sentence beside it is unreachable and the state it was meant for
        raises `KeyError` instead. A typo here is invisible from the table."""
        assert action in sync.RULES

    @pytest.mark.parametrize(
        "action", (sync.KEPT_LOCAL, sync.KEPT_REMOTE, sync.KEPT_BOTH, sync.EDITED)
    )
    def test_the_settled_answers_are_deliberately_absent(self, action: str) -> None:
        """`status` never settles a conflict, so the four actions that only
        `sync.settled` produces cannot reach this table.

        Asserted rather than left implicit, because a row for one of them is
        worse than a missing row: it reads as a state that has been thought
        about, and no run can ever print it.
        """
        assert action in sync.RULES
        assert action not in inspection.SAYS


@pytest.mark.usefixtures("synced")
class TestWhatEachChangeLooksLike:
    """Plan §7.4's rows, as sentences. One file changed, one file not."""

    def test_a_synced_machine_names_no_file(self, synced: Synced) -> None:
        """The fixture's own claim, asserted where a reader will look for it:
        with nothing pending, the only lines are the remote and the summary."""
        said = synced.status()
        assert ".bashrc" not in said
        assert ".vimrc" not in said
        assert "2 files managed, nothing to do" in said

    def test_an_edit_in_home_changed_here(self, synced: Synced) -> None:
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        synced.says_about(".bashrc", PHRASES["here"])
        synced.marks(".bashrc", MARKS["here"])

    def test_a_change_in_the_repository_changed_there(self, synced: Synced) -> None:
        """Written into the repository's working tree, which is where a `sync`
        that pulled somebody else's commit would have put it."""
        (synced.second.repo / ".bashrc").write_text("one\ntwo\nTHREE\nfour\nfive\n")
        synced.says_about(".bashrc", PHRASES["there"])
        synced.marks(".bashrc", MARKS["there"])

    def test_a_file_deleted_from_home_is_missing(self, synced: Synced) -> None:
        (synced.second.home / ".bashrc").unlink()
        synced.says_about(".bashrc", PHRASES["gone"])
        synced.marks(".bashrc", MARKS["gone"])

    def test_disjoint_changes_on_both_sides_merge(self, synced: Synced) -> None:
        """Both sides edited, different lines. `status` runs the real merge to
        find that out, which is the whole reason it can say so before a sync."""
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        (synced.second.repo / ".bashrc").write_text("one\ntwo\nthree\nfour\nFIVE\n")
        synced.says_about(".bashrc", PHRASES["merges"])
        synced.marks(".bashrc", MARKS["merges"])

    def test_overlapping_changes_report_the_hunk_count(self, synced: Synced) -> None:
        """The same two sides, editing the same line. The count comes from git's
        exit status (`merge.three_way`), so it is the number the prompt would
        show rather than a guess made here."""
        synced.second.write(".bashrc", "one\nMINE\nthree\nfour\nfive\n")
        (synced.second.repo / ".bashrc").write_text("one\nTHEIRS\nthree\nfour\nfive\n")
        synced.says_about(".bashrc", "both changed and the edits overlap: 1 conflict")
        assert "2 files managed, 0 to change, 1 in conflict" in synced.status()

    def test_two_conflicts_are_two(self, synced: Synced) -> None:
        """The plural, and that the number is not hard-coded at one.

        A longer file than `START`, because `git merge-file` needs three lines
        of agreement between two disagreements to call them two hunks -- and
        `START` has exactly three, which is the boundary. Written first and
        synced, so the snapshot is the base both edits are measured against.
        """
        lines = [f"line {number}\n" for number in range(12)]
        synced.second.write(".bashrc", "".join(lines))
        assert synced.second.call("sync") == 0
        mine = list(lines)
        mine[0], mine[11] = "MINE at the top\n", "MINE at the end\n"
        synced.second.write(".bashrc", "".join(mine))
        theirs = list(lines)
        theirs[0], theirs[11] = "THEIRS at the top\n", "THEIRS at the end\n"
        (synced.second.repo / ".bashrc").write_text("".join(theirs))
        synced.says_about(".bashrc", "2 conflicts to settle")

    def test_a_path_that_is_not_a_regular_file_is_skipped_with_a_reason(
        self,
        synced: Synced,
    ) -> None:
        """A fifo, not a socket: `sun_path` is 104 bytes on macOS and a sandbox
        path plus the repository layout exceeds it, so `bind` raises and the
        test errors instead of testing."""
        (synced.second.home / ".bashrc").unlink()
        os.mkfifo(synced.second.home / ".bashrc")
        synced.says_about(".bashrc", "skipped:")
        assert "is not a regular file" in synced.line(synced.status(), ".bashrc")

    def test_the_two_files_can_say_different_things_at_once(self, synced: Synced) -> None:
        """The assertion the rest of this class rests on: two states, two lines,
        each with its own phrase. Without it, every test above would also pass
        against a `status` that printed the same phrase for every file."""
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        (synced.second.repo / ".vimrc").write_text("set number\nset ruler\n")
        said = synced.status()
        assert PHRASES["here"] in synced.line(said, ".bashrc"), said
        assert PHRASES["there"] in synced.line(said, ".vimrc"), said
        assert "2 files managed, 2 to change, 0 in conflict" in said


@pytest.mark.usefixtures("synced")
class TestStatusWritesNothing:
    """Plan §4's "Never modifies anything", with the precondition established.

    `.git` is excluded from the fingerprint on purpose: `status` fetches, which
    moves remote-tracking refs and rewrites the directory's mtime. That is the
    one thing it is allowed to touch, and the module docstring of
    `tupferl/inspection.py` says why.
    """

    def test_a_status_with_work_pending_writes_nothing_and_a_sync_writes(
        self,
        synced: Synced,
    ) -> None:
        """Both halves in one test, because separated they are two claims that
        could each hold while the pair is meaningless.

        The `sync` at the end is what proves the fixture had something to write:
        without it, "the fingerprint did not move" is equally true of a machine
        with nothing to do.
        """
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        before = support.fingerprint(synced.second.home)
        synced.status()
        assert support.fingerprint(synced.second.home) == before

        assert synced.second.call("sync") == 0
        assert support.fingerprint(synced.second.home) != before

    def test_no_backup_directory_appears(self, synced: Synced) -> None:
        """The backup root is created lazily by the first `sync` that replaces
        a `$HOME` file, and five empty ones would push the last real backup out
        of plan §5's window. A `status` that created one would do that silently.
        """
        (synced.second.repo / ".bashrc").write_text("one\ntwo\nTHREE\nfour\nfive\n")
        assert not synced.second.backups.exists()
        synced.status()
        assert not synced.second.backups.exists()

    def test_the_snapshot_is_left_where_the_last_sync_put_it(self, synced: Synced) -> None:
        """The merge base is what makes a three-way comparison three-way. A
        `status` that moved it would make the *next* sync read a change on one
        side as no change at all -- silent, and the wrong way round.
        """
        with support.sandboxed(synced.second.home, synced.second.name):
            snapshot = paths.snapshot_dir(synced.second.repo, synced.second.name) / ".bashrc"
        assert snapshot.read_text() == START
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        synced.status()
        assert snapshot.read_text() == START

    def test_nothing_is_committed(self, synced: Synced) -> None:
        """git's own record, which no fingerprint of the working tree can see."""
        before = synced.second.git("rev-parse", "HEAD")
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        synced.status()
        assert synced.second.git("rev-parse", "HEAD") == before


@pytest.mark.usefixtures("synced")
class TestTheShapeOfTheReport:
    """The layout, which the rest of this file's assertions look straight past.

    Every test above asks "does this phrase appear beside this name?", and a
    `status` that printed a warning it should not, opened with a blank line, or
    lined the second column up wrongly answers all of them correctly. The
    mutation sweep said so: five survivors on `status` and `sides`, all of them
    about shape rather than content.
    """

    def test_a_healthy_machine_says_nothing_about_an_unfinished_merge(self, synced: Synced) -> None:
        """The negative half of `TestStatusReportsWhatSyncRefuses`. Without it,
        `if marker is not None` can be true always and every other test passes.
        """
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        assert "unfinished git operation" not in synced.status()

    def test_the_heading_is_said_once_and_not_on_every_row(self, synced: Synced) -> None:
        """Each row used to carry "the next sync" in it, so a machine with forty
        managed files printed the same six words forty times with the three that
        differ buried in them. The count is the assertion: "a heading appears"
        is equally true of a status that still repeats itself."""
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        (synced.second.repo / ".vimrc").write_text("set number\nset ruler\n")
        said = synced.status()
        assert said.count("the next sync") == 1, said
        assert said.splitlines()[0] == inspection.HEADING, said

    def test_the_inventory_gets_a_heading_that_describes_an_inventory(self, synced: Synced) -> None:
        """`--all` is mostly `unchanged` rows, over which "what the next sync
        would do" describes nothing that is under it.

        Both headings are asserted in one run, because a single constant used
        for both places satisfies either test alone."""
        assert synced.status().splitlines()[0] != inspection.EVERYTHING
        status, listed = synced.second.say("status", "--all")
        assert status == 0, listed
        assert listed.splitlines()[0] == inspection.EVERYTHING, listed

    def test_a_quiet_machine_is_told_so_rather_than_handed_two_zeroes(self, synced: Synced) -> None:
        """`0 to change, 0 in conflict` makes the reader do the arithmetic to
        reach "nothing to do", which is the one thing they wanted to know -- and
        it is the line a synced machine sees every single time.

        Both arms, because "always says nothing to do" passes the first alone
        and "never does" passes the second."""
        assert "nothing to do" in synced.status()
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        pending = synced.status()
        assert "nothing to do" not in pending, pending
        assert "1 to change" in pending, pending

    def test_a_conflict_alone_is_enough_to_lose_the_quiet_sentence(self, synced: Synced) -> None:
        """A conflict changes nothing yet, so `moving` is zero for it -- and a
        summary that asked only about `moving` would call a machine with an
        unsettled conflict "nothing to do"."""
        synced.second.write(".bashrc", "one\nMINE\nthree\nfour\nfive\n")
        (synced.second.repo / ".bashrc").write_text("one\nTHEIRS\nthree\nfour\nfive\n")
        said = synced.status()
        assert "nothing to do" not in said, said
        assert "0 to change, 1 in conflict" in said, said

    def test_nothing_to_report_does_not_open_with_a_blank_line(self, synced: Synced) -> None:
        """The separator is only earned by something above it. Printed
        unconditionally, a quiet machine's status starts with an empty line,
        which reads as output having gone missing."""
        said = synced.status()
        assert said.splitlines()[0].strip(), repr(said)

    def test_a_blank_line_separates_the_files_from_the_remote(self, synced: Synced) -> None:
        """And the other direction: with files listed, the separator is there.
        The pair is what pins the branch -- either alone is satisfied by a
        `status` that never separates, or by one that always does."""
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        rows = synced.status().splitlines()
        # Anchored on the remote line rather than counted, because the heading
        # puts a blank under itself too and a count would then be pinning the
        # heading rather than the separator this test is named for.
        at = next(index for index, row in enumerate(rows) if "origin/" in row)
        assert not rows[at - 1].strip(), rows
        assert ".bashrc" in rows[at - 2].split(), rows

    def test_the_second_column_lines_up_under_names_of_different_lengths(
        self,
        synced: Synced,
    ) -> None:
        """`.bashrc` and `.vimrc` differ by one character, so a width taken from
        the *shortest* name -- `max` written as `min` -- leaves the longer line
        unpadded and the two phrases one column apart."""
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        (synced.second.repo / ".vimrc").write_text("set number\nset ruler\n")
        rows = [row for row in synced.status().splitlines() if row.startswith("  ")]
        assert len(rows) == 2, rows
        # Where the phrase begins, found from the *last* run of two spaces
        # rather than from a word both rows share -- the two phrases differ,
        # since the two files are going opposite ways, which is the fixture's
        # whole point.
        starts = {row.rindex("  ") + 2 for row in rows}
        assert len(starts) == 1, rows


@pytest.mark.usefixtures("synced")
class TestTheMarkersAreColouredOnATerminal:
    """The half no captured run can show.

    Everything in this file reads `status` out of a `StringIO` under a sandbox
    that sets `NO_COLOR`, so both halves of `colours.coloured` are false and
    every painted branch is unreachable from every other test here -- the same
    blind spot `tests/test_conflicts.py` had, measured there.
    """

    def coloured_status(self, synced: Synced) -> str:
        """`status` in-process, writing to something that says it is a
        terminal, with the sandbox's `NO_COLOR` taken back out."""
        env = {key: value for key, value in synced.second.env.items() if key != "NO_COLOR"}
        seen = support.Screen()
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sys, "stdout", seen):
            assert inspection.status() == 0
        return seen.getvalue()

    def test_the_marker_names_whose_change_it_is(self, synced: Synced) -> None:
        """Cyan for this computer and yellow for the repository, which is what
        those two colours already mean in the conflict prompt.

        One run with both directions in it: a single hard-coded colour is right
        for neither test alone but would pass one of them, and the pair is the
        fixture that no constant satisfies.
        """
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        (synced.second.repo / ".vimrc").write_text("set number\nset ruler\n")
        said = self.coloured_status(synced)
        mine = next(row for row in said.splitlines() if ".bashrc" in row)
        theirs = next(row for row in said.splitlines() if ".vimrc" in row)
        assert f"{colours.MINE}->{colours.OFF}" in mine, mine
        assert f"{colours.THEIRS}<-{colours.OFF}" in theirs, theirs

    def test_the_column_is_padded_before_it_is_painted(self, synced: Synced) -> None:
        """`f"{painted:2}"` counts the escape bytes as columns, so painting
        first makes every coloured row short and every plain row right -- a
        table ragged only on a terminal, which is the one place nothing else in
        this suite looks.

        Asserted by stripping the escapes and comparing the *plain* run, which
        is the strongest available claim: colour adds colour and moves nothing.
        """
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        (synced.second.repo / ".vimrc").write_text("set number\nset ruler\n")
        painted = support.ESCAPES.sub("", self.coloured_status(synced))
        assert painted == synced.status(), repr(painted)


@pytest.mark.usefixtures("synced")
class TestTheRemoteHalf:
    """Plan §4's "and remotely", which is a fact about commits rather than files.

    The distinction this class exists for: the per-file lines compare `$HOME`
    with *this machine's* checkout, so anything sitting on the remote unpulled
    is invisible to them. A status that showed one file changed while thirty
    commits waited would be true and misleading, and the caveat line is the
    difference.
    """

    def test_a_machine_that_agrees_with_the_remote_says_so(self, synced: Synced) -> None:
        assert "is exactly what this computer has" in synced.status()

    def test_commits_waiting_on_the_remote_are_counted(self, synced: Synced) -> None:
        """Pushed from the first machine, so the second's fetch finds them."""
        synced.first.write(".bashrc", "one\ntwo\nthree\nfour\nPUSHED\n")
        assert synced.first.call("sync") == 0
        said = synced.status()
        assert "1 commit to pull" in said
        assert "to push" not in said

    def test_commits_here_and_not_there_are_counted_the_other_way(self, synced: Synced) -> None:
        """`add` commits without pushing, which is how a machine gets ahead.

        The caveat about unpulled commits must *not* appear: nothing is waiting
        to be pulled, and a sentence that is always printed is one a reader
        stops seeing. That is the only fixture in this file with `ahead` above
        zero and `behind` at zero, so it is the only one that can say so.
        """
        synced.second.write(".zshrc", "setopt nomatch\n")
        assert synced.second.call("add", str(synced.second.home / ".zshrc")) == 0
        said = synced.status()
        assert "1 commit to push" in said
        assert "to pull" not in said
        assert "does not include it yet" not in said

    def test_the_two_counts_are_separate_numbers(self, synced: Synced) -> None:
        """Both directions at once, and the numbers differ -- so a status that
        printed one number twice, or swapped them, fails here. With equal
        counts on both sides this test could not tell either mistake."""
        for line in ("first\n", "second\n"):
            synced.first.write(".bashrc", f"one\ntwo\nthree\nfour\n{line}")
            assert synced.first.call("sync") == 0
        synced.second.write(".zshrc", "setopt nomatch\n")
        assert synced.second.call("add", str(synced.second.home / ".zshrc")) == 0
        said = synced.status()
        assert "2 commits to pull" in said
        assert "1 commit to push" in said

    def test_being_behind_says_the_file_lines_are_not_the_whole_story(self, synced: Synced) -> None:
        """The caveat, and that it is *not* printed when there is nothing to
        pull -- otherwise it would be a sentence that is always there, which a
        reader stops seeing."""
        assert "does not include it yet" not in synced.status()
        synced.first.write(".bashrc", "one\ntwo\nthree\nfour\nPUSHED\n")
        assert synced.first.call("sync") == 0
        assert "does not include it yet" in synced.status()

    def test_an_unreachable_remote_is_reported_and_the_rest_still_prints(
        self,
        synced: Synced,
    ) -> None:
        """A laptop with no network still has a local half worth reading, and
        `status` is the command someone runs when something is already wrong.

        The URL is repointed rather than the network being blocked: a path that
        does not exist fails the fetch for the same reason and needs nothing of
        the machine the suite runs on.
        """
        synced.second.git("remote", "set-url", "origin", str(synced.tmp / "not-a-repository"))
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        said = synced.status()
        assert "could not reach origin" in said
        assert "tupferl doctor" in said
        assert PHRASES["here"] in synced.line(said, ".bashrc"), said

    def test_no_remote_at_all_says_how_to_add_one(self, synced: Synced) -> None:
        synced.second.git("remote", "remove", "origin")
        said = synced.status()
        assert "no remote is configured" in said
        assert "remote add origin" in said

    def test_a_branch_that_was_never_pushed_says_so(self, synced: Synced) -> None:
        """`origin/<branch>` does not exist until something is pushed to it, and
        a fetch cannot invent it. A new local branch is the ordinary way to be
        in that state, and it is also the state a machine is in between `init`
        against an empty remote and its first successful push."""
        synced.second.git("checkout", "-b", "somewhere-else")
        said = synced.status()
        assert "does not exist yet" in said
        assert "tupferl sync" in said

    def test_a_comparison_git_will_not_make_is_unknown_rather_than_equal(
        self,
        synced: Synced,
    ) -> None:
        """`distance` answering `None` -- which real git will not do here, since
        both refs resolve. Forced by patching tupferl's own wrapper rather than
        by breaking git: the branch exists precisely because "up to date" is the
        one wrong answer when the comparison could not be made, and a status
        that silently printed it would be indistinguishable from a healthy one.
        """
        with mock.patch("tupferl.gitrepo.distance", return_value=None):
            said = synced.status()
        assert "git would not compare HEAD with" in said
        assert "is exactly what this computer has" not in said

    def test_a_detached_head_is_reported_rather_than_compared(self, synced: Synced) -> None:
        """`gitrepo.branch` answers `None`, and there is then no `<remote>/
        <branch>` to measure against. Reported, because a status that silently
        skipped the remote line would look like a machine that is up to date."""
        synced.second.git("checkout", "--detach", "HEAD")
        said = synced.status()
        assert "no branch checked out" in said


@pytest.mark.usefixtures("synced")
class TestStatusReportsWhatSyncRefuses:
    """The two states where `sync` raises and `status` does not.

    `sync` is about to write, so it stops. `status` is what someone runs to find
    out *why* sync stopped, and refusing to say anything is the least useful
    moment to refuse.
    """

    def test_an_unfinished_merge_is_a_line_not_an_error(self, synced: Synced) -> None:
        (synced.second.repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
        synced.second.write(".bashrc", "ONE\ntwo\nthree\nfour\nfive\n")
        status, said = synced.second.say("status")
        assert status == 0, said
        assert "unfinished git operation" in said
        assert "MERGE_HEAD" in said
        assert PHRASES["here"] in synced.line(said, ".bashrc"), said

    def test_sync_still_refuses_the_same_state(self, synced: Synced) -> None:
        """The other half, which is what makes the test above about `status`
        rather than about `MERGE_HEAD` being harmless."""
        (synced.second.repo / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
        status, said = synced.second.say("sync")
        assert status == 2, said
        assert "unfinished git operation" in said


@pytest.mark.usefixtures("sandbox")
class TestAMachineWithNothingManaged:
    """`init` and no `add`, which is every second machine's first minute."""

    def test_status_says_how_to_start(self, sandbox: support.Sandbox) -> None:
        from tupferl.__main__ import main

        remote = support.make_remote(sandbox.tmp / "remote.git", sandbox.env)
        with support.quiet():
            assert main(["init", str(remote)]) == 0
        with support.quiet() as said:
            assert main(["status"]) == 0
        assert "nothing is managed yet" in said.getvalue()
        assert "0 files managed" not in said.getvalue()
