"""What `sync` decides, asserted without a repository in sight.

Plan §7.4's decision table, the executable bit, the backup window, the report and
the commit message -- everything `sync` works out before it touches git. The
CLI-driven half is `tests/test_sync_cli.py`, and the split is not tidiness:

**It is what stops a mutation sweep paying 20 seconds to learn something that
takes 0.3.** `tools/mutate.py` runs the modules a mutated file maps to, in order,
with `failfast`. Unittest orders classes alphabetically, so while these lived
beside the CLI tests, `TestAGitLevelConflict` and `TestSyncOneMachine` ran
*before* `TestTheDecisionTable` -- and a mutant in `resolve` was caught only
after the whole integration suite had run. Measured on this suite: 0.30s for the
classes here against 20.48s for the module they were in.

`test_sync` sorts before `test_sync_cli`, which is what puts these first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest

from tests import support
from tupferl import conflicts, copies, gitrepo, manage, merge, sync

#: Three versions of one file whose edits do not overlap, so a merge of any two
#: of them is decidable. Named rather than spelled out per test: half the table
#: below is the same three inputs in different roles.
BASE = copies.Blob(b"alpha\nbeta\ngamma\ndelta\nepsilon\n", False)
HOME_EDIT = copies.Blob(b"ALPHA\nbeta\ngamma\ndelta\nepsilon\n", False)
REPO_EDIT = copies.Blob(b"alpha\nbeta\ngamma\ndelta\nEPSILON\n", False)
BOTH = copies.Blob(b"ALPHA\nbeta\ngamma\ndelta\nEPSILON\n", False)

NAME = PurePosixPath(".bashrc")


class TestTheDecisionTable:
    """Plan §7.4 item 1, as decisions. `resolve` reads no files, so each row is
    three values in and one out -- and a row that is wrong here is wrong for
    every path through the engine that reaches it."""

    def test_neither_side_changed(self) -> None:
        got = sync.resolve(NAME, BASE, BASE, BASE)
        assert got.action == sync.UNCHANGED

    def test_only_home_changed(self) -> None:
        got = sync.resolve(NAME, BASE, HOME_EDIT, BASE)
        assert got.action == sync.TO_REPO
        assert got.blob == HOME_EDIT

    def test_only_the_repository_changed(self) -> None:
        got = sync.resolve(NAME, BASE, BASE, REPO_EDIT)
        assert got.action == sync.TO_HOME
        assert got.blob == REPO_EDIT

    def test_both_changed_in_different_places(self) -> None:
        got = sync.resolve(NAME, BASE, HOME_EDIT, REPO_EDIT)
        assert got.action == sync.MERGED
        assert got.blob == BOTH

    def test_both_changed_the_same_line(self) -> None:
        mine = copies.Blob(b"mine\nbeta\ngamma\ndelta\nepsilon\n", False)
        theirs = copies.Blob(b"theirs\nbeta\ngamma\ndelta\nepsilon\n", False)
        got = sync.resolve(NAME, BASE, mine, theirs)
        assert got.action == sync.CONFLICT
        assert got.blob is None
        assert got.sides is not None
        assert got.sides.conflicts == 1
        # The three versions travel with the conflict, so whoever settles it has
        # what the prompt needs without reading anything back off disk.
        assert (got.sides.base, got.sides.home, got.sides.stored) == (BASE, mine, theirs)
        assert got.sides.marked is not None
        assert b"mine" in got.sides.marked
        assert b"theirs" in got.sides.marked

    def test_both_arrived_at_the_same_content(self) -> None:
        """Two machines edited the same file to the same bytes. There is nothing
        to write, but the snapshot has to move -- otherwise the next run merges
        against a base neither side holds any more."""
        got = sync.resolve(NAME, BASE, BOTH, BOTH)
        assert got.action == sync.UNCHANGED
        assert got.blob == BOTH

    def test_a_file_missing_from_home_is_restored(self) -> None:
        """`remove` is how someone stops managing a file (plan §4), so a missing
        one is an `rm` or a new machine. Reading it as "delete it everywhere"
        would make one mistake lose the file on every computer the user owns."""
        got = sync.resolve(NAME, BASE, None, REPO_EDIT)
        assert got.action == sync.RESTORED
        assert got.blob == REPO_EDIT

    def test_a_missing_file_is_restored_even_when_the_snapshot_is_gone(self) -> None:
        """The check order: missing beats everything, because the comparisons
        below it have nothing to compare."""
        got = sync.resolve(NAME, None, None, REPO_EDIT)
        assert got.action == sync.RESTORED

    def test_no_snapshot_and_two_different_files_is_a_conflict(self) -> None:
        """Both machines created the file independently. Nothing in the data says
        which is newer, so taking either is a guess that loses the other."""
        got = sync.resolve(NAME, None, HOME_EDIT, REPO_EDIT)
        assert got.action == sync.CONFLICT

    def test_no_snapshot_and_identical_files_is_not(self) -> None:
        got = sync.resolve(NAME, None, BASE, BASE)
        assert got.action == sync.UNCHANGED

    def test_a_binary_file_both_sides_changed_is_a_conflict(self) -> None:
        base = copies.Blob(b"\x00\x01base", False)
        got = sync.resolve(NAME, base, copies.Blob(b"\x00\x01mine", False), REPO_EDIT)
        assert got.action == sync.CONFLICT
        assert got.blob is None


class TestTheExecutableBit:
    """Plan §5 asks for the bit to be preserved. Sync has to *decide* it, which
    is a merge of its own -- and one that cannot conflict, since a bit both sides
    changed they changed to the same value."""

    def test_a_chmod_with_no_edit_is_a_change(self) -> None:
        got = sync.resolve(NAME, BASE, copies.Blob(BASE.data, True), BASE)
        assert got.action == sync.TO_REPO
        assert got.blob is not None and got.blob.executable

    def test_it_travels_with_a_merge(self) -> None:
        """The content merges cleanly and the bit comes from the side that
        changed it, so a `chmod +x` on one machine survives an edit on the
        other."""
        got = sync.resolve(NAME, BASE, copies.Blob(HOME_EDIT.data, True), REPO_EDIT)
        assert got.action == sync.MERGED
        assert got.blob == copies.Blob(BOTH.data, True)

    @pytest.mark.parametrize(("ours", "theirs", "want"), ((False, True, True), (True, False, True)))
    def test_the_side_that_changed_it_wins(self, ours: bool, theirs: bool, want: bool) -> None:
        base, mine, stored = (copies.Blob(b"", bit) for bit in (False, ours, theirs))
        assert sync.executable_after(base, mine, stored) == want

    def test_taking_it_away_is_a_change_too(self) -> None:
        """The mirror of the test above, and not the same assertion: a rule that
        only ever answered `True` would pass that one."""
        assert not sync.executable_after(
            copies.Blob(b"", True), copies.Blob(b"", False), copies.Blob(b"", True)
        )

    def test_with_no_base_it_resolves_towards_executable(self) -> None:
        """Nothing says which side is right, and the two mistakes are not equal:
        a script restored without the bit fails when the user runs it."""
        assert sync.executable_after(None, copies.Blob(b"", False), copies.Blob(b"", True))
        assert not sync.executable_after(None, copies.Blob(b"", False), copies.Blob(b"", False))


@pytest.mark.usefixtures("sandbox")
class TestReadingAndWriting:
    def test_a_symlink_is_not_the_file(self, sandbox: support.Sandbox) -> None:
        """`manifest` refuses a symlink at `add` time; this is the same rule at
        sync time, for a path that has become one since. Following it would read
        -- and then overwrite -- something the user never named."""
        real = sandbox.write(sandbox.tmp / "real", "content\n")
        link = sandbox.tmp / "link"
        link.symlink_to(real)
        assert copies.read(link) is None

    def test_a_directory_is_not_the_file_either(self, sandbox: support.Sandbox) -> None:
        (sandbox.tmp / "adir").mkdir()
        assert copies.read(sandbox.tmp / "adir") is None

    def test_writing_the_same_bytes_and_bit_changes_nothing(self, sandbox: support.Sandbox) -> None:
        """What makes a second sync touch nothing at all. Asserted through the
        return value rather than through mtime, which some filesystems round to
        the second."""
        where = sandbox.tmp / "x"
        blob = copies.Blob(b"hello\n", False)
        assert copies.write(where, blob)
        assert not copies.write(where, blob)

    def test_the_executable_bit_round_trips(self, sandbox: support.Sandbox) -> None:
        where = sandbox.tmp / "x"
        copies.write(where, copies.Blob(b"#!/bin/sh\n", True))
        assert where.stat().st_mode & 0o777 == copies.EXECUTABLE
        assert copies.read(where) == copies.Blob(b"#!/bin/sh\n", True)

    def test_a_mode_change_alone_is_written(self, sandbox: support.Sandbox) -> None:
        where = sandbox.tmp / "x"
        copies.write(where, copies.Blob(b"same\n", False))
        assert copies.write(where, copies.Blob(b"same\n", True))


@pytest.fixture
def root(sandbox: support.Sandbox) -> Path:
    """An empty directory to keep backups in."""
    made = sandbox.tmp / "backup"
    made.mkdir()
    return made


@pytest.mark.usefixtures("sandbox")
class TestBackups:
    """Plan §5: a copy before anything in `$HOME` is overwritten, last 5 kept."""

    def test_nothing_is_created_until_something_needs_saving(self, root: Path) -> None:
        """A quiet sync must leave the disk as it found it. A directory per run
        would also push the last real backup out of the window of five."""
        sync.Backups(root)
        assert sorted(root.iterdir()) == []

    def test_a_saved_file_keeps_its_bytes_and_its_bit(self, root: Path) -> None:
        where = sync.Backups(root).take(PurePosixPath(".local/bin/x"), copies.Blob(b"hi\n", True))
        assert copies.read(where) == copies.Blob(b"hi\n", True)
        assert str(where.relative_to(where.parents[2])) == ".local/bin/x"

    def test_only_the_newest_five_runs_survive(self, root: Path) -> None:
        for index in range(8):
            (root / f"2026082{index}T000000.000000").mkdir()
        sync.Backups(root).take(NAME, copies.Blob(b"x", False))
        left = sorted(found.name for found in root.iterdir())
        assert len(left) == 5
        # The oldest three are gone and the newest of the old ones is not.
        assert "20260820T000000.000000" not in left
        assert "20260827T000000.000000" in left

    def test_one_directory_per_run_however_many_files_it_saves(self, root: Path) -> None:
        """`self.where` is set on the first save and reused. A fixture that backs
        up one file cannot tell that from a directory per *file*, which is what
        the mutation that always takes the branch produces -- and which would
        push the last real backup out of the window of five after five files."""
        backups = sync.Backups(root)
        backups.take(PurePosixPath(".bashrc"), copies.Blob(b"a", False))
        backups.take(PurePosixPath(".vimrc"), copies.Blob(b"b", False))
        assert len(list(root.iterdir())) == 1

    def test_a_directory_that_is_already_there_is_reused_rather_than_fatal(
        self, root: Path
    ) -> None:
        """`exist_ok=True`. `STAMP` carries microseconds, so two runs colliding
        is not something a test can race for honestly -- the clock is frozen
        instead, which is the same collision arrived at on purpose.

        It is worth guarding because of *when* it fires: `take` is called with
        the user's file already in hand and about to be overwritten, so a
        `FileExistsError` here is an exception raised out of a sync at exactly
        the moment the copy plan §5 promises exists.
        """
        frozen = datetime(2026, 8, 29, 12, 0, 0)
        with mock.patch.object(sync, "datetime") as clock:
            clock.now.return_value = frozen
            sync.Backups(root).take(NAME, copies.Blob(b"first", False))
            # A second run, same stamp: the directory is already there.
            sync.Backups(root).take(NAME, copies.Blob(b"second", False))
        (only,) = list(root.iterdir())
        assert copies.read(only / str(NAME)) == copies.Blob(b"second", False)

    @pytest.mark.parametrize("existing", range(5))
    def test_nothing_is_deleted_while_there_is_room(
        self, sandbox: support.Sandbox, existing: int
    ) -> None:
        """The other side of the window, and the one that loses data if it is
        wrong: with `BACKUPS_KEPT` or fewer runs kept there is nothing to forget.

        **Every count below the window, not one of them.** The first version of
        this test put four directories in and expected five out -- and five is
        the *single* count at which the correct `max(0, n - 5)` and a mutated
        `max(-1, n - 5)` agree, because both come to zero. At two, three and four
        the mutant deletes everything but the newest: it loses the user's saved
        copies precisely when there are fewest of them. The mutation sweep found
        it; the test as written could not.

        One parametrized case per count rather than a `subTest` loop, which is
        also what gives each count its own name in a failure.
        """
        root = sandbox.tmp / f"window-{existing}"
        root.mkdir()
        for index in range(existing):
            (root / f"2026082{index}T000000.000000").mkdir()
        sync.Backups(root).take(NAME, copies.Blob(b"x", False))
        left = len(list(root.iterdir()))
        assert left == existing + 1, f"a run with {existing + 1} backups deleted one"

    def test_a_file_a_user_left_here_is_not_deleted(
        self, sandbox: support.Sandbox, root: Path
    ) -> None:
        """This removes trees. Anything that is not one of its own directories
        belongs to somebody else."""
        keep = sandbox.write(root / "notes.txt", "mine\n")
        for index in range(8):
            (root / f"2026082{index}T000000.000000").mkdir()
        sync.Backups(root).take(NAME, copies.Blob(b"x", False))
        assert keep.is_file()


@pytest.mark.usefixtures("sandbox")
class TestStaleSnapshots:
    def test_snapshots_of_unmanaged_files_are_found(self, sandbox: support.Sandbox) -> None:
        snaps = sandbox.tmp / "state"
        sandbox.write(snaps / ".bashrc", "a")
        sandbox.write(snaps / ".config" / "foo.conf", "b")
        found = sync.stale(snaps, {PurePosixPath(".bashrc")})
        assert found == [PurePosixPath(".config/foo.conf")]

    def test_a_machine_that_has_never_synced_has_none(self, sandbox: support.Sandbox) -> None:
        assert sync.stale(sandbox.tmp / "never", set()) == []


class TestTheReport:
    def test_unchanged_files_are_not_listed_but_are_counted(self) -> None:
        """Most files on most runs. Forty lines saying nothing happened would
        bury the one line saying something did."""
        text = sync.report(
            [
                sync.Outcome(PurePosixPath(".bashrc"), sync.UNCHANGED, BASE),
                sync.Outcome(PurePosixPath(".vimrc"), sync.TO_REPO, HOME_EDIT),
            ]
        )
        assert ".bashrc" not in text
        assert "stored .vimrc" in text
        assert "2 files managed, 1 changed, 0 in conflict" in text

    def test_a_conflict_says_how_much_there_is_to_settle(self) -> None:
        name = PurePosixPath(".bashrc")
        sides = conflicts.Sides(name, BASE, HOME_EDIT, REPO_EDIT, b"<<<<<<<\n", 3)
        text = sync.report([sync.Outcome(name, sync.CONFLICT, None, sides)])
        assert "conflict in .bashrc (3 to settle)" in text
        assert "1 in conflict" in text

    def test_nothing_managed_says_so_rather_than_counting_an_empty_set(self) -> None:
        """`0 files managed, 0 changed, 0 in conflict` is arithmetic about an
        empty set, and it is the last line `tupferl init` prints on a fresh
        machine -- where "what now?" is the only question the user has.

        The same sentence `status` gives, from the same constant rather than a
        second copy of the wording: `init` used to answer this itself, one line
        early, and got it wrong on the machine that mattered."""
        text = sync.report([])
        assert text == manage.NOTHING_MANAGED
        assert "0 files managed" not in text


class TestTheCommitMessage:
    def test_it_names_what_changed(self) -> None:
        got = sync.message([PurePosixPath(".vimrc"), PurePosixPath(".bashrc")], "laptop")
        assert got == "sync from laptop: .bashrc, .vimrc"

    def test_with_nothing_decided_it_says_what_actually_happened(self) -> None:
        """Staged but untouched means an earlier run left a copy behind, or the
        user put a file in the repository by hand. Naming files would be a lie."""
        got = sync.message([], "laptop")
        assert "an earlier run" in got


@pytest.mark.usefixtures("sandbox")
class TestUnfinishedMarkers:
    @pytest.mark.parametrize("marker", ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD"))
    def test_every_marker_is_looked_for(self, sandbox: support.Sandbox, marker: str) -> None:
        """One per marker: a rebase and a cherry-pick leave the same half-done
        tree as a merge. The names are written out rather than read from the
        constant, because a loop over a shortened constant still passes.

        The clean answer is asserted first in each case, which is what the
        `try`/`finally` in the `subTest` loop this replaced was buying: a
        parametrized case gets its own repository, so the marker cannot be left
        behind for the next one.
        """
        repo = support.make_repo(sandbox.tmp / "repo", sandbox.env)
        assert gitrepo.unfinished(repo) is None
        (repo / ".git" / marker).write_text("deadbeef\n", encoding="utf-8")
        assert gitrepo.unfinished(repo) == marker

    def test_a_marker_outside_a_repository_is_not_reported_as_one(
        self, sandbox: support.Sandbox
    ) -> None:
        """The guard's whole job, and the fixture that can see it: when git
        cannot say where the git directory is, `inside.out` is empty and the
        lookup falls back to `repo` itself. A directory holding a file called
        `MERGE_HEAD` would then be reported as a half-finished merge -- and
        `sync` refuses to run on one, so a stray file would wedge the command.
        """
        empty = sandbox.tmp / "not-a-repo-either"
        empty.mkdir()
        (empty / "MERGE_HEAD").write_text("deadbeef\n", encoding="utf-8")
        assert gitrepo.unfinished(empty) is None

    def test_staging_nothing_is_refused_rather_than_staging_everything(
        self, sandbox: support.Sandbox
    ) -> None:
        """`git add --all --` with an empty pathspec stages the whole repository,
        untracked files included -- measured. A caller whose list came out empty
        means "nothing", and git's default reading is the most destructive one
        available, so it is answered rather than passed on."""
        repo = support.make_repo(sandbox.tmp / "guarded", sandbox.env)
        (repo / "stray.txt").write_text("not mine\n", encoding="utf-8")
        refused = gitrepo.stage(repo, [])
        assert not refused.ok
        assert "nothing to stage" in refused.err
        assert support.git(["status", "--porcelain"], repo, sandbox.env) == "?? stray.txt"

    def test_a_directory_git_cannot_read_reports_nothing_rather_than_guessing(
        self, sandbox: support.Sandbox
    ) -> None:
        """`None` because there is no answer, not because the answer is "no" --
        `sync` fails on its own next git call, and `doctor` reports the git
        failure separately."""
        empty = sandbox.tmp / "not-a-repo"
        empty.mkdir()
        assert gitrepo.unfinished(empty) is None


def sides(executable: bool = False) -> conflicts.Sides:
    """A settled-conflict fixture whose two sides differ, as `TestWhatAnAnswerMeansOnDisk` needs."""
    mine = copies.Blob(b"mine\nbeta\ngamma\n", executable)
    theirs = copies.Blob(b"theirs\nbeta\ngamma\n", False)
    base = copies.Blob(b"alpha\nbeta\ngamma\n", False)
    merged = merge.three_way(str(NAME), base.data, mine.data, theirs.data)
    return conflicts.Sides(NAME, base, mine, theirs, merged.data, merged.conflicts)


def settled(choice: str, data: bytes | None = None) -> sync.Outcome:
    return sync.settled(sides(), conflicts.Answer(choice, data))


class TestWhatAnAnswerMeansOnDisk:
    """`sync.settled`, which had no direct test at all until the review.

    Every route to a settled conflict passes through it -- the five keys and the
    three flags alike -- so a row that names the wrong side writes the wrong
    file on every one of them at once.
    """

    def test_keeping_local_writes_the_repository_only(self) -> None:
        """`$HOME` already holds these bytes, and `to_home` is also what takes
        the backup -- so a copy of a file nothing replaced would evict a real
        one from plan §5's window of five."""
        got = settled(conflicts.LOCAL)
        assert got.action == sync.KEPT_LOCAL
        assert got.blob == sides().home
        assert sync.RULES[got.action] == sync.Rule(to_repo=True, to_home=False, needs_user=False)

    def test_keeping_the_repository_writes_home_only(self) -> None:
        got = settled(conflicts.REMOTE)
        assert got.action == sync.KEPT_REMOTE
        assert got.blob == sides().stored
        assert sync.RULES[got.action] == sync.Rule(to_repo=False, to_home=True, needs_user=False)

    def test_the_two_sides_are_not_the_same_blob(self) -> None:
        """The precondition for the pair above. With a symmetric fixture both
        assertions hold against a table that has the two rows swapped, which
        CLAUDE.md §2 lists as its second-commonest shape."""
        assert sides().stored != sides().home

    @pytest.mark.parametrize(
        ("choice", "action"),
        ((conflicts.BOTH, sync.KEPT_BOTH), (conflicts.EDIT, sync.EDITED)),
    )
    def test_an_answer_that_carries_bytes_writes_them_to_both(
        self, choice: str, action: str
    ) -> None:
        got = settled(choice, b"by hand\n")
        assert got.action == action
        assert got.blob == copies.Blob(b"by hand\n", False)
        assert sync.RULES[action].to_repo and sync.RULES[action].to_home

    def test_skipping_leaves_the_conflict_standing(self) -> None:
        got = settled(conflicts.SKIP)
        assert got.action == sync.CONFLICT
        assert got.blob is None
        assert got.sides is not None

    def test_only_a_conflict_carries_its_sides(self) -> None:
        """The invariant `Outcome.sides` documents, asserted rather than
        described. `report` tests `sides is not None` where `main` tests
        `RULES[...].needs_user`, so an outcome that carried its sides under
        another action would print "1 in conflict" for a file that was settled.
        """
        for choice in (conflicts.LOCAL, conflicts.REMOTE):
            assert settled(choice).sides is None
        for choice in (conflicts.BOTH, conflicts.EDIT):
            assert settled(choice, b"x\n").sides is None

    def test_the_executable_bit_survives_an_answer_that_carries_bytes(self) -> None:
        """`chmod +x` on one machine and an edit on the other is one change to
        each of two things, and `[b]`/`[e]` must not drop the one they did not
        produce."""
        got = sync.settled(sides(executable=True), conflicts.Answer(conflicts.EDIT, b"by hand\n"))
        assert got.blob == copies.Blob(b"by hand\n", True)

    def test_taking_a_side_takes_that_side_s_bit(self) -> None:
        """`[l]` and `[r]` do not merge the bit -- they take a whole file, and
        its mode is part of it."""
        chosen = sync.settled(sides(executable=True), conflicts.Answer(conflicts.LOCAL))
        assert chosen.blob is not None and chosen.blob.executable
        other = sync.settled(sides(executable=True), conflicts.Answer(conflicts.REMOTE))
        assert other.blob is not None and not other.blob.executable

    def test_every_answer_the_prompt_can_give_has_a_row(self) -> None:
        """`MEANS` is the enumeration of the prompt's answers, so a key `ask`
        can return without a row here is a `KeyError` in the middle of a sync.
        `[d]` is deliberately absent: it is something the prompt does before
        asking again, not an answer."""
        assert set(sync.MEANS) == {
            conflicts.LOCAL,
            conflicts.REMOTE,
            conflicts.BOTH,
            conflicts.EDIT,
            conflicts.SKIP,
        }
        for means in sync.MEANS.values():
            assert means.action in sync.RULES


MANAGED = ".bashrc"


@dataclass(frozen=True)
class Edited(support.TwoMachines):
    """`machine-a` with `.bashrc` managed, synced, and then edited here.

    One fixture for both classes below because their setups were identical: the
    review prompt is what happens next when somebody is there to answer, and the
    silence flags are what happens next when nobody is.
    """

    def synced(self, keys: str | None = None, *args: str) -> tuple[int, str]:
        """One sync, bounded well below the mutation harness's 30s alarm.

        **The bound is the point, not a formality.** A mutant that makes
        `review` reject every key loops for ever against a pty that never
        reaches end of input, and the alarm then files the row `BROKE` -- which
        is not a verdict, so the line it was on ends up guarded by nothing a
        sweep can see. Four rows came back that way before this. `PATIENCE` is
        5s against tests that take about one, which is CLAUDE.md's rule: above
        the longest honest wait here and comfortably under the alarm.

        On the fixture rather than in one class, so that both classes below
        reach the same bound -- CLAUDE.md's fourth "where to arm it" lesson is
        that a bound around one call reads as though it covered the class.
        """
        with support.deadline(support.PATIENCE, f"the sync never finished on {keys!r}"):
            return self.first.say("sync", *args, keys=keys)

    def stored_it(self, *args: str, keys: str | None = None) -> str:
        """A sync that must have stored the edit, with what it printed."""
        status, said = self.synced(keys, *args)
        assert status == 0, said
        assert self.first.stored(MANAGED).read_text() == "alpha EDITED\nbeta\n", said
        return said


@pytest.fixture
def edited(two_machines: support.TwoMachines) -> Edited:
    box = Edited(**vars(two_machines))
    box.first.write(MANAGED, "alpha\nbeta\n")
    assert box.first.call("add", str(box.first.home / MANAGED)) == 0
    assert box.first.call("sync", "--auto") == 0
    box.first.write(MANAGED, "alpha EDITED\nbeta\n")
    return box


@pytest.mark.usefixtures("edited")
class TestBeingAskedBeforeAChangeIsStored:
    """The per-file review: `sync` shows what this computer changed and asks.

    Only what *this* computer changed. An incoming change is applied without
    asking -- see the guard in `sync.settle` for why, and `status --diff` for
    where to look at one before it arrives.

    Every test here drives the real prompt through a pty, because the thing
    under test is a keypress reaching a decision. `support.FALLBACK` is appended
    to whatever is typed, so a prompt that asks one more question than the test
    answers is answered `[s]` and fails on an assertion rather than hanging.
    """

    def test_l_stores_this_computers_version(self, edited: Edited) -> None:
        status, said = edited.synced("l")
        assert status == 0, said
        assert "stored" in said
        assert edited.first.stored(MANAGED).read_text() == "alpha EDITED\nbeta\n"

    def test_r_puts_the_repositorys_copy_back_and_the_edit_is_gone(self, edited: Edited) -> None:
        """The undo tupferl had no command for, and the one answer here that
        destroys something.

        **Both sides asserted.** `[r]` reported `reverted` while writing `$HOME`
        back over itself for as long as it took to press the key once: `resolve`
        had set the blob to what `[l]` would write, so replacing the action
        alone changed nothing. Asserting the report alone would still pass for
        that, which is why the file's bytes are the assertion and the word is
        only the second half.
        """
        status, said = edited.synced("r")
        assert status == 0, said
        assert "reverted" in said
        assert edited.first.read(MANAGED) == "alpha\nbeta\n"
        assert edited.first.stored(MANAGED).read_text() == "alpha\nbeta\n"

    def test_r_backs_up_the_edit_it_is_about_to_destroy(self, edited: Edited) -> None:
        """**What makes `[r]` safe to offer at all.** It is the one answer here
        that throws work away, and plan §5's backup is the net under it -- so
        the net is asserted rather than assumed to still be reachable from a
        code path that did not exist when it was written.

        `apply` takes one when `to_home` and the bytes differ, which `REVERTED`
        satisfies; nothing in `looked_at` had to arrange it. That is the point:
        the new action gets the guarantee by having the right `RULES` row, and
        this is what would notice if it were given the wrong one.
        """
        assert edited.synced("r")[0] == 0
        # `edited.first.backups`, not `paths.backup_dir()`: the machine computed
        # it inside its own sandbox environment, and calling it here would read
        # the developer's real `XDG_STATE_HOME`.
        kept = [
            saved.read_text() for saved in edited.first.backups.rglob(MANAGED) if saved.is_file()
        ]
        assert kept == ["alpha EDITED\nbeta\n"], "the discarded edit was not backed up"

    def test_s_leaves_both_copies_and_says_the_run_is_unfinished(self, edited: Edited) -> None:
        """A skipped file is neither changed nor in conflict, so the summary
        said "0 changed, 0 in conflict" over an exit status of 1 -- a line that
        reads as "nothing outstanding" under a status meaning the opposite."""
        status, said = edited.synced("s")
        assert status == 1, said
        assert "left alone" in said
        assert "1 left alone" in said, "the summary does not mention it"
        assert edited.first.read(MANAGED) == "alpha EDITED\nbeta\n"
        assert edited.first.stored(MANAGED).read_text() == "alpha\nbeta\n"

    def test_a_run_with_nothing_skipped_does_not_mention_it(self, edited: Edited) -> None:
        """The half of the summary line that is easy to leave untested: `if
        left:` becoming always-true appends ", 0 left alone" to every run and
        the "1 left alone" assertion above passes for it regardless."""
        _, said = edited.synced("l")
        assert "1 changed" in said
        assert "left alone" not in said, "a clean run advertised a count of nothing"

    def test_the_diff_is_shown_before_the_keys_and_is_oriented_outward(
        self, edited: Edited
    ) -> None:
        """The complaint that produced this prompt was that a diff's direction
        is not obvious enough to bet a dotfile on. The repository's copy is what
        is about to be replaced, so it is on `-`."""
        _, said = edited.synced("l")
        assert f"--- {MANAGED} (the repository)" in said
        assert f"+++ {MANAGED} (this computer)" in said
        assert "+alpha EDITED" in said
        assert said.index("--- ") < said.index("[l] store"), "the keys came first"

    def test_a_key_that_is_not_offered_asks_again(self, edited: Edited) -> None:
        """`[b]` and `[e]` are the conflict prompt's and mean nothing here.
        Typing one has to re-ask rather than be taken for something."""
        status, said = edited.synced("bl")
        assert status == 0, said
        assert "not one of the keys" in said
        assert "stored" in said

    def test_d_shows_the_whole_diff_and_asks_again(self, edited: Edited) -> None:
        edited.first.write(MANAGED, "alpha EDITED\n" + "".join(f"line {n}\n" for n in range(60)))
        status, said = edited.synced("dl")
        assert status == 0, said
        assert "more line(s)" in said, "the first display was not capped"
        assert "line 59" in said, "[d] did not show the rest"


@pytest.mark.usefixtures("edited")
class TestWhenTheReviewDoesNotHappen:
    """Every way a run says "do not ask me", and the one that says it by being
    a pipe.

    This is the half that keeps `init`, CI and a timer-driven sync working:
    each of them runs `sync` with nobody there, and a prompt would block for
    ever or read EOF and call that a decision.
    """

    def test_auto_stores_without_asking(self, edited: Edited) -> None:
        said = edited.stored_it("--auto")
        assert "[l] store" not in said, "it asked anyway"

    def test_no_input_does_not_ask_either(self, edited: Edited) -> None:
        """It already meant "never prompt"; that has to cover the new prompt as
        well, or the flag stops meaning what it says."""
        assert "[l] store" not in edited.stored_it("--no-input")

    def test_ours_and_theirs_answer_in_advance(self, edited: Edited) -> None:
        """A run that has answered every *conflict* in advance has said what it
        wants. Stopping it on a one-sided change would make those flags mean
        less than they say -- and `--theirs` on a scripted sync would then hang
        on the first file this computer edited."""
        for flag in ("--ours", "--theirs"):
            edited.first.write(MANAGED, "alpha EDITED\nbeta\n")
            assert "[l] store" not in edited.stored_it(flag)

    def test_a_stdin_that_is_not_a_terminal_does_not_ask(self, edited: Edited) -> None:
        """No flag at all: `keys=None` leaves stdin a pipe, which is what a cron
        job and a CI step have. Without this the default became a hang."""
        assert "[l] store" not in edited.stored_it()

    def test_an_incoming_change_is_never_asked_about(self, edited: Edited) -> None:
        """The deliberate omission. The repository's copy moves and `$HOME` does
        not, so a review would have something to show -- and the run must apply
        it silently anyway. A pty is given here so that a prompt *could* happen:
        without one this passes for the reason above rather than for its own.
        """
        assert edited.first.call("sync", "--auto") == 0
        edited.first.stored(MANAGED).write_text("alpha FROM ELSEWHERE\nbeta\n", encoding="utf-8")
        support.git(["commit", "-am", "elsewhere"], edited.first.repo, edited.first.env)

        status, said = edited.first.say("sync", keys="")
        assert status == 0, said
        assert "[l] store" not in said, "an incoming change was put to the user"
        assert edited.first.read(MANAGED) == "alpha FROM ELSEWHERE\nbeta\n"
