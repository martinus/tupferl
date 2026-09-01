"""`tupferl diff`, driven end to end -- plan §4's `diff [path]`.

Two fixture rules run through the whole file, both from CLAUDE.md §2.

**Two managed files, and both differ, wherever the test is about *which* file
is shown.** With one managed file, `tupferl diff .bashrc` and `tupferl diff`
produce the same output, so "the path argument limited it" is unobservable --
the same shape as `tests/test_overlays.py`'s "an overlay fixture needs both
copies of the file".

**The two sides are never symmetric.** `MINE` and `THEIRS` differ in length as
well as in content, and the executable bit is set on exactly one side wherever
it matters. A diff rendered backwards is otherwise a diff that still looks
right, which is the whole hazard: the direction is a judgement this program had
to make, and `TestWhichSideIsWhich` is where it is written down.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from unittest import mock

import pytest

from tests import support
from tupferl import colours, inspection, merge, sync
from tupferl.copies import Blob

#: What `.bashrc` holds on both machines when a fixture here starts, which is
#: `support.STARTS_AS` because that is what `template()` synced. Aliased rather
#: than written out again: a second copy is free to drift from the tree these
#: tests are handed, and the drift would show up as a diff nobody asked for
#: rather than as a failure naming the constant.
START = support.STARTS_AS

#: What `$HOME` gets, and what the repository gets. Different lengths as well as
#: different text: a diff that swapped the two sides would still show one `-`
#: and one `+` line, and only the *content* of each says which way round it is.
MINE = "one\nedited on this computer\nthree\nfour\nfive\n"
THEIRS = "one\nfrom the repo\nthree\nfour\nfive\n"

#: The control file. Managed, and left identical on both sides in most fixtures,
#: so a `diff` that printed a heading per managed file fails.
CONTROL = "set number\nset expandtab\n"


class Synced(support.TwoMachines):
    """`machine-b`, synced, with `.bashrc` and `.vimrc` both managed."""

    def diff(self, *args: str) -> str:
        """`tupferl diff`, insisting it exited 0 -- including when files differ.

        `git diff` answers the same way, and there is no `--exit-code` here
        because plan §4 does not ask for one: the status of a command whose job
        is to show something should say whether it could, not what it found.
        """
        status, said = self.second.say("status", "--diff", *args)
        assert status == 0, said
        return said

    def apart(self) -> None:
        """Make `.bashrc` differ on the two sides, without syncing either."""
        self.second.write(".bashrc", MINE)
        (self.second.repo / ".bashrc").write_text(THEIRS)


@pytest.fixture
def synced(two_machines: support.TwoMachines) -> Synced:
    box = Synced(**vars(two_machines))
    box.first.write(".bashrc", START)
    box.first.write(".vimrc", CONTROL)
    assert box.first.call("add", str(box.first.home / ".vimrc")) == 0
    assert box.first.call("sync") == 0
    assert box.second.call("init", str(box.remote)) == 0
    assert box.second.call("sync") == 0
    return box


@pytest.mark.usefixtures("synced")
class TestWhatDiffShows:
    def test_a_synced_machine_shows_one_sentence(self, synced: Synced) -> None:
        said = synced.diff()
        assert said.strip() == "nothing differs between $HOME and the repository."

    def test_a_text_difference_is_a_unified_diff(self, synced: Synced) -> None:
        synced.apart()
        said = synced.diff()
        assert "-edited on this computer" in said
        assert "+from the repo" in said
        assert "@@" in said
        # Both bits are the same, so nothing about the executable bit belongs
        # here. Without this, `rendered`'s equality test can be false always and
        # every other assertion in this class still holds.
        assert "executable" not in said
        # And the whole-repository fallback sentence is for when nothing was
        # shown -- printing it beside a diff is the same branch inverted.
        assert "nothing differs" not in said

    def test_an_identical_file_is_not_mentioned_at_all(self, synced: Synced) -> None:
        """`.vimrc` is managed and unchanged, so it must be silent -- otherwise
        `diff` on a machine with forty dotfiles is forty headings and one
        difference buried in them."""
        synced.apart()
        said = synced.diff()
        assert ".bashrc" in said
        assert ".vimrc" not in said

    def test_a_file_only_the_repository_has_says_so(self, synced: Synced) -> None:
        """Not an empty diff. `$HOME` holding nothing is the state a fresh
        machine is in, and "no lines differ" would be the wrong report."""
        (synced.second.home / ".bashrc").unlink()
        said = synced.diff()
        assert "only in the repository" in said
        assert ".bashrc" in said

    def test_a_binary_difference_names_both_sizes_rather_than_showing_bytes(
        self, synced: Synced
    ) -> None:
        """git's own rule for "there are no lines here" -- a NUL in the first
        8000 bytes, asked through `merge.is_text`. Printing nothing would read
        as "these are the same", which is the one wrong answer."""
        (synced.second.home / ".bashrc").write_bytes(b"bin\x00ary here\n")
        (synced.second.repo / ".bashrc").write_bytes(b"bin\x00ary there, longer\n")
        said = synced.diff()
        assert "are not text" in said
        # Two different numbers, so a report that printed one side's length
        # twice -- or the same length for both -- fails here.
        assert "13 bytes here" in said
        assert "22 in the repository" in said
        assert "\x00" not in said

    def test_a_path_that_is_not_a_regular_file_is_skipped_with_its_reason(
        self, synced: Synced
    ) -> None:
        """A fifo rather than a socket -- `sun_path` is 104 bytes on macOS and
        a sandbox path plus the repository layout exceeds it."""
        (synced.second.home / ".bashrc").unlink()
        os.mkfifo(synced.second.home / ".bashrc")
        said = synced.diff()
        assert "skipped" in said
        assert "is not a regular file" in said

    def test_only_the_executable_bit_differing_is_still_a_difference(self, synced: Synced) -> None:
        """`chmod +x` with no edit is a real change that travels (plan §5), and
        a diff of the *lines* renders it as nothing at all -- an empty answer to
        "why does status say this changed?"."""
        (synced.second.home / ".bashrc").chmod(0o755)
        said = synced.diff()
        assert "executable here, not in the repository" in said
        assert "@@" not in said

    def test_the_bit_is_reported_the_other_way_round_too(self, synced: Synced) -> None:
        """The mirror, because one direction alone passes against a sentence
        that names the same side whichever way the bit went."""
        (synced.second.repo / ".bashrc").chmod(0o755)
        assert "executable in the repository, not here" in synced.diff()

    def test_a_bit_and_a_text_change_are_both_shown(self, synced: Synced) -> None:
        """One or the other would be a report that hid half of what changed."""
        synced.apart()
        (synced.second.home / ".bashrc").chmod(0o755)
        said = synced.diff()
        assert "executable here, not in the repository" in said
        assert "-edited on this computer" in said


def reading(
    found: Blob | None,
    stored: Blob | None,
    action: str = sync.TO_REPO,
    why: str = "",
) -> sync.Reading:
    """A `Reading` for `.bashrc`, with paths that are never touched.

    `shows` reads only the name, the outcome and the two blobs -- it opens
    nothing -- so the three paths can be anything. `/nowhere` rather than a
    temporary directory says that out loud: if a future `shows` starts reading
    from disk, this fixture fails rather than quietly working.
    """
    where = PurePosixPath(".bashrc")
    nowhere = Path("/nowhere")
    return sync.Reading(
        name=where,
        where=nowhere / ".bashrc",
        target=nowhere / ".bashrc",
        snapshot=nowhere / ".bashrc",
        found=found,
        stored=stored,
        outcome=sync.Outcome(where, action, None, why=why),
    )


class TestOneFileWithNoRepository:
    """`inspection.shows` on a `Reading` built by hand.

    Everything else in this file drives two real machines, which is right for a
    command whose product is what a user sees. This class is the other half:
    `shows` decides *which* of five reports a file gets, and five reports need
    five fixtures -- each of which costs a sync and a merge end to end. Here
    they cost nothing, in the same spirit as `sync.resolve` being pure so plan
    §7.4's table is a test with no repository in it.

    The end-to-end tests above are what prove these strings actually reach a
    terminal; this is what makes each branch cheap enough to have its own case.
    """

    def test_identical_copies_produce_nothing(self) -> None:
        """`None`, not an empty string: `difference` counts what it printed, and
        an empty heading would still be a heading."""
        same = Blob(b"one\n", False)
        assert inspection.shows(reading(same, same)) is None

    def test_a_refused_reading_reports_its_reason(self) -> None:
        said = inspection.shows(reading(None, None, action=sync.REFUSED, why="it is a fifo"))
        assert said is not None
        assert "it is a fifo" in said

    def test_a_file_missing_from_home_is_not_an_empty_diff(self) -> None:
        said = inspection.shows(reading(None, Blob(b"one\n", False)))
        assert said is not None
        assert "only in the repository" in said

    @pytest.mark.parametrize("side", ("home", "repository"))
    def test_a_binary_side_stops_the_lines_being_shown(self, side: str) -> None:
        """Either side is enough. Both-binary is the obvious fixture and would
        pass against a check that only looked at one of them."""
        text = Blob(b"one\n", False)
        binary = Blob(b"one\x00\n", False)
        found, stored = (binary, text) if side == "home" else (text, binary)
        said = inspection.shows(reading(found, stored))
        assert said is not None
        assert "are not text" in said

    def test_the_executable_bit_alone_still_says_something(self) -> None:
        """Same bytes, different bit. A diff of the lines is empty here, so a
        report built from the diff alone would say nothing at all."""
        said = inspection.shows(reading(Blob(b"x\n", True), Blob(b"x\n", False)))
        assert said is not None
        assert said == ".bashrc: executable here, not in the repository."


@pytest.mark.usefixtures("synced")
class TestWhichSideIsWhich:
    """The direction, pinned -- because it is a judgement rather than a given.

    `git diff` shows the working tree as `+`; this shows `$HOME` as `-`, because
    `merge.unified` renders it and the conflict prompt's `[d]` uses the same
    function on the same two files. A user who runs `tupferl diff .bashrc` and
    then settles a conflict about `.bashrc` minutes later sees one orientation,
    and both labels say in words which side they are.

    Pinned here so that flipping it is a visible decision rather than a silent
    one, and so that a reader who thinks it is backwards has one place to argue.
    """

    def test_home_is_the_minus_side_and_says_so(self, synced: Synced) -> None:
        synced.apart()
        said = synced.diff()
        assert "--- .bashrc (this computer)" in said
        assert "+++ .bashrc (the repository)" in said

    def test_the_labels_and_the_signs_agree(self, synced: Synced) -> None:
        """The assertion the one above cannot make: that the bytes on the `-`
        lines really are `$HOME`'s. Labels that were right while the sides were
        swapped would pass that test and fail this one."""
        synced.apart()
        rows = synced.diff().splitlines()
        minus = [row[1:] for row in rows if row.startswith("-") and not row.startswith("---")]
        plus = [row[1:] for row in rows if row.startswith("+") and not row.startswith("+++")]
        assert minus == ["edited on this computer"]
        assert plus == ["from the repo"]

    def test_the_prompt_shows_the_same_file_the_same_way(self, synced: Synced) -> None:
        """`conflicts.unified` and this command are one function now. Asserted
        rather than assumed: they were two renderers until milestone 6, and the
        argument for merging them is exactly that a user reads both."""
        from tupferl import conflicts

        sides = conflicts.Sides(
            name=PurePosixPath(".bashrc"),
            base=Blob(START.encode(), False),
            home=Blob(MINE.encode(), False),
            stored=Blob(THEIRS.encode(), False),
            marked=None,
            conflicts=1,
        )
        synced.apart()
        prompt = conflicts.unified(sides)
        assert prompt in synced.diff()
        assert "--- .bashrc (this computer)" in prompt
        assert merge.unified(".bashrc", MINE.encode(), THEIRS.encode()) == prompt


HERE = Blob(b"mine\n", executable=False)
THERE = Blob(b"theirs\n", executable=False)


def shown(action: str) -> str:
    return inspection.rendered(PurePosixPath(".bashrc"), HERE, THERE, action)


def sides(action: str) -> tuple[str, str]:
    """The two header lines of `shown(action)`, as `(minus, plus)`."""
    lines = [row for row in shown(action).split("\n") if row[:3] in ("---", "+++")]
    assert len(lines) == 2, f"no diff header for {action}:\n{shown(action)}"
    return lines[0], lines[1]


class TestWhichSideTheDiffPutsOnTheMinus:
    """`status --diff` answers "what will the next sync do", and a unified diff
    answers it with `-` for what goes and `+` for what arrives.

    So the side being *replaced* has to be on `-`, and which side that is
    depends on the file: sync's direction is per file, and the orientation was
    fixed at `$HOME` on `-`. That is right for a file being pulled and exactly
    backwards for one being pushed, where the `-` lines were the ones being
    kept -- the diff read as "here is what discarding your edit would do".

    **Both directions, always.** A test of either alone passes for an
    implementation that is backwards in the other, which is precisely how this
    survived: the two tests that did exist asserted the pushed case and had the
    bug written into them.
    """

    def test_a_push_puts_the_repository_on_the_minus_side(self) -> None:
        """The bug. Only `$HOME` changed, so sync writes `$HOME`'s bytes into
        the repository: the repository's copy is what disappears."""
        minus, plus = sides(sync.TO_REPO)
        assert "the repository" in minus
        assert "this computer" in plus
        assert "-theirs" in shown(sync.TO_REPO)
        assert "+mine" in shown(sync.TO_REPO)

    def test_a_pull_puts_this_computer_on_the_minus_side(self) -> None:
        """The half that was already right, and the reason the fix could not be
        "swap the arguments": that would correct the case above and break this
        one."""
        minus, plus = sides(sync.TO_HOME)
        assert "this computer" in minus
        assert "the repository" in plus
        assert "-mine" in shown(sync.TO_HOME)
        assert "+theirs" in shown(sync.TO_HOME)

    def test_a_restore_reads_as_a_pull(self) -> None:
        """`RESTORED` writes `$HOME` and not the repository, so it is a pull by
        the only definition that matters here. Asserted rather than assumed,
        because it is the action a reader is least likely to think about."""
        minus, _ = sides(sync.RESTORED)
        assert "this computer" in minus

    def test_a_two_sided_change_says_so_instead_of_implying_a_direction(self) -> None:
        """A conflict writes neither side and a clean merge writes both, so
        there is no side being replaced. Said in words rather than shown as an
        arrow that would be a guess -- the diff is still the difference, and a
        reader told that will not read the `-` lines as doomed."""
        for action in (sync.CONFLICT, sync.MERGED):
            assert "both sides changed" in shown(action)
        # **And they agree with each other.** A clean merge writes both sides,
        # so `to_repo` is true for it: orienting on that alone reversed a merge
        # and not a conflict, giving the one case with no direction two
        # displays depending on which two-sided outcome it happened to be. The
        # note above is printed either way, so only this sees it.
        assert sides(sync.MERGED) == sides(sync.CONFLICT)
        assert "this computer" in sides(sync.MERGED)[0]

    def test_a_one_sided_change_does_not_say_it(self) -> None:
        """The precondition. Without it the assertion above is satisfied by a
        note printed on every diff, which would make it noise rather than the
        thing that distinguishes the two-sided case."""
        for action in (sync.TO_REPO, sync.TO_HOME):
            assert "both sides changed" not in shown(action)

    def test_the_table_this_class_reads_has_not_shrunk(self) -> None:
        """The precondition for the parametrized test below, which a shrunken
        `RULES` would silently collect *no* cases for -- CLAUDE.md §2's
        zero-iteration trap, at collection time."""
        assert len(sync.RULES) >= 8, "the table shrank; this test reads it"

    @pytest.mark.parametrize("action", sorted(sync.RULES))
    def test_every_action_sync_knows_about_is_oriented(self, action: str) -> None:
        """Read out of `sync.RULES` rather than listed again here, so an action
        added there cannot be missed. It is the same table `rendered` derives
        the orientation from, which is the point: a sixth action gets an
        orientation by existing, and this asserts the orientation it gets is
        the one its own row implies."""
        rule = sync.RULES[action]
        minus, plus = sides(action)
        if rule.to_repo and not rule.to_home:
            assert "the repository" in minus, f"{action} is a push"
        elif rule.to_home and not rule.to_repo:
            assert "this computer" in minus, f"{action} is a pull"
        else:
            assert "both sides changed" in shown(action)
        assert plus[4:] != minus[4:], "both headers name the same side"


@dataclass(frozen=True)
class Paged(support.TwoMachines):
    """`machine-a` with an edited `.bashrc` and a stand-in pager to page it."""

    seen: Path
    fake: Path

    def configure(self, command: str, key: str = "core.pager") -> None:
        support.git(["config", key, command], self.first.repo, self.first.env)

    def diff(self, terminal: bool = True) -> str:
        """`difference` with a stream that claims to be a terminal, or not.

        `support.Screen` for the terminal case -- the pager only runs when there
        is one to page, and a `StringIO` says it is not.
        """
        out = support.Screen() if terminal else support.Spill()
        here = Path.cwd()
        os.chdir(self.first.home)
        try:
            with mock.patch.dict(os.environ, self.first.env, clear=True):
                assert inspection.difference(None, out) == 0
        finally:
            os.chdir(here)
        return out.getvalue()

    def pager_saw(self) -> str:
        assert self.seen.is_file(), "the pager never ran"
        return self.seen.read_text(encoding="utf-8")


@pytest.fixture
def paged(two_machines: support.TwoMachines) -> Paged:
    seen = two_machines.tmp / "seen.txt"
    fake = two_machines.tmp / "pager.py"
    box = Paged(**vars(two_machines), seen=seen, fake=fake)
    box.first.write(".bashrc", "one\ntwo\n")
    assert box.first.call("add", str(box.first.home / ".bashrc")) == 0
    box.first.write(".bashrc", "ONE\ntwo\n")
    fake.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(seen)!r}).write_text('PAGED\\n' + sys.stdin.read())\n",
        encoding="utf-8",
    )
    return box


@pytest.mark.usefixtures("paged")
class TestShowingTheDiffThroughTheUsersPager:
    """`core.pager`, honoured so that a machine already set up for `delta` needs
    nothing here.

    A user who wrote `core.pager = delta` configured how they read a diff, not
    how they read a *git* diff -- and asking git for it rather than parsing
    `~/.gitconfig` gets the include directives, the system file and the
    per-repository override for free.

    Every test uses a stand-in pager that is a Python script writing a marker,
    so what is asserted is that the diff reached it: `delta` itself is not
    installed on every machine that runs this suite, and a test that skipped
    where it was missing would turn the `macos` leg red under `--no-skips`.
    """

    def test_the_diff_goes_to_the_pager_git_is_configured_with(self, paged: Paged) -> None:
        paged.configure(f"{sys.executable} {paged.fake}")
        printed = paged.diff()
        assert "--- .bashrc" in paged.pager_saw()
        # `+ONE`: only `$HOME` changed, so the repository is the side replaced.
        # This test is about the *pager*; the orientation itself is asserted by
        # `TestWhichSideTheDiffPutsOnTheMinus`.
        assert "+ONE" in paged.pager_saw()
        assert "--- .bashrc" not in printed, "it was printed as well as paged"

    def test_with_no_pager_configured_it_prints(self, paged: Paged) -> None:
        """The other half, and the one every machine without a `core.pager`
        gets. Without it, a `show` that never printed at all would pass the test
        above."""
        assert "--- .bashrc" in paged.diff()
        assert not paged.seen.exists()

    def test_a_redirected_diff_is_never_paged(self, paged: Paged) -> None:
        """What keeps `tupferl status --diff | delta` working, and every test in
        this file: a redirected diff is something a program is about to read,
        and handing it to a pager would be the tool deciding it knew better."""
        paged.configure(f"{sys.executable} {paged.fake}")
        assert "--- .bashrc" in paged.diff(terminal=False)
        assert not paged.seen.exists(), "a redirected diff was paged"

    def test_a_pager_that_is_not_installed_costs_the_user_nothing(self, paged: Paged) -> None:
        """The diff is the point and the pager is only how. A machine that lost
        its pager -- a shared `.gitconfig` naming one this host has not
        installed, which is exactly what tupferl makes easy -- must still show
        the diff."""
        paged.configure("no-such-pager-anywhere")
        printed = paged.diff()
        assert "--- .bashrc" in printed
        assert "could not show the diff" in printed

    def test_a_spawn_that_raises_still_shows_the_diff(self, paged: Paged) -> None:
        """The `except (OSError, BrokenPipeError, ValueError)` arm, which the
        test above does *not* reach: a pager that is not installed comes back as
        a shell **return code** of 127, not as an exception, which is the whole
        point of the comment beside that check. This arm is for the spawn itself
        failing -- a fork that cannot allocate, a pipe that closes early.

        `subprocess.run` is patched rather than provoked, and that is a
        concession worth naming: there is no portable way to make the operating
        system refuse a fork on demand. What is asserted is the same either way
        -- the user is told, and **the diff is printed anyway**, which is the one
        thing this function promises never to skip.
        """
        paged.configure(f"{sys.executable} {paged.fake}")
        real = subprocess.run

        def refuse(*args: Any, **kwargs: Any) -> Any:
            # Only the pager spawn, which is the one `shell=True` call in the
            # module. Patching `subprocess.run` outright also breaks the `git`
            # that `difference` runs to build the diff, and the test then fails
            # in the fixture rather than in the arm under test.
            if kwargs.get("shell"):
                raise OSError("no fork for you")
            return real(*args, **kwargs)

        with mock.patch.object(subprocess, "run", refuse):
            printed = paged.diff()
        assert "could not show the diff" in printed
        assert "no fork for you" in printed, "the reason was swallowed"
        assert "--- .bashrc" in printed, "the diff itself was lost with the pager"
        assert not paged.seen.exists(), "the pager somehow ran"

    def test_git_pager_wins_over_the_configured_one(self, paged: Paged) -> None:
        """git's own order is `GIT_PAGER`, then `core.pager`, then `PAGER`, and
        the point of reading git's config is that its answer matches git's. A
        variable set for one command has to beat a file set for all of them, or
        the escape hatch every git user reaches for does not work here."""
        paged.configure(f"{sys.executable} {paged.tmp / 'never.py'}")
        elsewhere = paged.tmp / "chosen.txt"
        picked = paged.tmp / "picked.py"
        picked.write_text(
            f"import sys, pathlib\npathlib.Path({str(elsewhere)!r}).write_text(sys.stdin.read())\n",
            encoding="utf-8",
        )
        paged.first.env["GIT_PAGER"] = f"{sys.executable} {picked}"
        paged.diff()
        assert "--- .bashrc" in elsewhere.read_text(encoding="utf-8")
        assert not paged.seen.exists(), "core.pager ran despite GIT_PAGER"

    def test_pager_is_the_last_resort(self, paged: Paged) -> None:
        """The other end of the same order: `$PAGER` is honoured, but only when
        git has been told nothing more specific."""
        paged.first.env["PAGER"] = f"{sys.executable} {paged.fake}"
        paged.diff()
        assert "--- .bashrc" in paged.pager_saw()

    def test_an_empty_git_pager_means_no_pager_rather_than_unset(self, paged: Paged) -> None:
        """`GIT_PAGER=` is how a git user turns paging off for one command, and
        it has to beat `core.pager` the same way a non-empty one does.

        This is why `pager` tests membership with `in` rather than chaining
        `or`: an `or` reads the empty string as "not set" and falls through to
        the file, which is the opposite of what was asked.
        """
        paged.configure(f"{sys.executable} {paged.fake}")
        paged.first.env["GIT_PAGER"] = ""
        assert "--- .bashrc" in paged.diff()
        assert not paged.seen.exists(), "core.pager ran despite an empty GIT_PAGER"

    def test_pager_diff_is_read_and_beats_core_pager(self, paged: Paged) -> None:
        """**The bug.** `configured_pager` read only `core.pager`, so a machine
        configured the per-command way -- which is the common shape -- got a
        plain diff and read that as tupferl ignoring its config.

        git's order for a command's pager is `GIT_PAGER`, then `pager.<cmd>`,
        then `core.pager`, then `PAGER`. Measured against git 2.43 with both
        keys set: the `pager.diff` one runs.

        Both keys set, not just the new one. With `core.pager` unset this
        passes for an implementation that reads `pager.diff` *instead of*
        `core.pager` rather than before it, and the test below would then be
        the only thing left holding the old key up.
        """
        paged.configure(f"{sys.executable} {paged.tmp / 'never.py'}")
        paged.configure(f"{sys.executable} {paged.fake}", key="pager.diff")
        paged.diff()
        assert "--- .bashrc" in paged.pager_saw()

    def test_core_pager_still_works_when_pager_diff_is_unset(self, paged: Paged) -> None:
        """The other half. Reading the new key must not cost the old one, and a
        test of `pager.diff` alone cannot show that."""
        paged.configure(f"{sys.executable} {paged.fake}")
        paged.diff()
        assert "--- .bashrc" in paged.pager_saw()

    def test_git_pager_still_beats_pager_diff(self, paged: Paged) -> None:
        """The new rung goes *below* the environment variable, not above it.
        Reading `pager.diff` first in the function is not the same as reading it
        first in the order, and this is the assertion that tells them apart."""
        paged.configure(f"{sys.executable} {paged.tmp / 'never.py'}", key="pager.diff")
        chosen = paged.tmp / "chosen.txt"
        picked = paged.tmp / "picked.py"
        picked.write_text(
            f"import sys, pathlib\npathlib.Path({str(chosen)!r}).write_text(sys.stdin.read())\n",
            encoding="utf-8",
        )
        paged.first.env["GIT_PAGER"] = f"{sys.executable} {picked}"
        paged.diff()
        assert "--- .bashrc" in chosen.read_text(encoding="utf-8")

    def test_a_pager_that_is_a_shell_command_line_works(self, paged: Paged) -> None:
        """**The second half of the bug**, and the one that would have kept the
        diff plain even after the key was fixed.

        A pager is a command *line*, not an argv, and git runs it through a
        shell. The reported configuration was

            diff = "if [ -t 1 ]; then delta; else cat; fi"

        which `shlex.split` turns into `['if', '[', '-t', ...]`; exec'ing `if`
        raised `OSError`, the fallback printed the diff plain, and the user saw
        exactly what an unconfigured machine shows.

        Driven with the real shape -- a conditional, a redirection and a pipe --
        rather than with a bare name, because a bare name works either way and
        is what made the original spelling look right.
        """
        paged.configure(
            f'if [ -n "$HOME" ]; then {sys.executable} {paged.fake}; else cat; fi',
            key="pager.diff",
        )
        paged.diff()
        assert "--- .bashrc" in paged.pager_saw()

    def test_a_shell_pager_that_is_not_installed_still_shows_the_diff(self, paged: Paged) -> None:
        """The guarantee had to move with the mechanism. Run directly, a missing
        pager raised `OSError`; through a shell it is exit 127 and
        `check=False` reads that as a run that happened -- so the user would
        get an empty screen, which is the one thing `show` promises never to
        do. The `if` wrapper is what makes this a *shell* 127 rather than the
        bare-name case the test above it covers.
        """
        paged.configure("if true; then no-such-pager-anywhere; fi", key="pager.diff")
        printed = paged.diff()
        assert "--- .bashrc" in printed
        assert "could not show the diff" in printed

    def test_a_pager_that_stops_early_does_not_print_the_diff_twice(self, paged: Paged) -> None:
        """The line the 126/127 test is drawn at. `q` in `less`, or a `head`
        that has seen enough, exits non-zero having *shown* the diff -- so
        falling back on any non-zero status would print it again underneath.
        Only the two codes the shell reserves for "could not run it" count.
        """
        paged.configure(f"{sys.executable} {paged.fake} && exit 3", key="pager.diff")
        printed = paged.diff()
        assert "--- .bashrc" in paged.pager_saw()
        assert "--- .bashrc" not in printed, "the diff was printed as well as paged"
        assert "could not show the diff" not in printed

    def test_a_false_pager_diff_pages_with_nothing_at_all(self, paged: Paged) -> None:
        """`pager.<cmd>` may be a boolean, and then it is not a command.

        Measured against git 2.43: a false one means *do not page*, and neither
        `core.pager` nor `$PAGER` is consulted. Read as a command instead it
        would be **spawned** -- `false` exits 1, which is not one of the two
        codes `show` falls back on, so the user would get an empty screen and no
        diff at all, and nobody would connect that to a setting meaning "do not
        page". Both other sources are set here, because the claim is that they
        are skipped and not merely that this one is.
        """
        paged.configure(f"{sys.executable} {paged.fake}")
        paged.first.env["PAGER"] = f"{sys.executable} {paged.fake}"
        paged.configure("false", key="pager.diff")
        assert "--- .bashrc" in paged.diff()
        assert not paged.seen.exists(), "something was paged despite pager.diff = false"

    def test_off_is_false_too_and_git_is_asked_which(self, paged: Paged) -> None:
        """Six spellings are false and six are true; git is asked rather than
        the list being copied here, where it would go stale in silence. `off` is
        the one a hand-rolled `== "false"` would miss."""
        paged.configure(f"{sys.executable} {paged.fake}")
        paged.configure("off", key="pager.diff")
        assert "--- .bashrc" in paged.diff()
        assert not paged.seen.exists(), "something was paged despite pager.diff = off"

    def test_a_true_pager_diff_says_page_but_not_how(self, paged: Paged) -> None:
        """The other half of the boolean rule, and a different answer from
        `false`: it falls through to `core.pager`. Without this, returning "do
        not page" for *any* boolean passes the two tests above."""
        paged.configure(f"{sys.executable} {paged.fake}")
        paged.configure("true", key="pager.diff")
        paged.diff()
        assert "--- .bashrc" in paged.pager_saw()

    def test_cat_is_gits_spelling_of_no_pager(self, paged: Paged) -> None:
        """git treats it as "do not page", and forking a process to do what a
        print already does is worth avoiding."""
        paged.configure("cat")
        assert "--- .bashrc" in paged.diff()
        assert not paged.seen.exists()


@pytest.mark.usefixtures("synced")
class TestHowTheDiffIsLaidOut:
    """The two things that made a multi-file diff hard to read.

    Both are about `difference`'s composition rather than about any one diff, so
    two files must differ -- with one, the separator has nothing to separate and
    every assertion below is vacuous.
    """

    def apart_twice(self, synced: Synced) -> str:
        """Make both managed files differ, and return what `diff` prints."""
        synced.apart()
        synced.second.write(".vimrc", "set number\n")
        said = synced.diff()
        assert ".bashrc" in said and ".vimrc" in said, "only one file differs"
        return said

    def test_two_files_are_separated_by_a_blank_line(self, synced: Synced) -> None:
        """Joined by a single newline, one file's last context line sat directly
        above the next file's `---` header, so a two-file diff read as one diff
        with a stray header in the middle.

        The blank line is asserted *above a header*, not merely somewhere in the
        output: a diff of a file that ends in a newline has a trailing space as
        its own last line, which looks like a separator without being one.
        """
        rows = self.apart_twice(synced).split("\n")
        headers = [at for at, row in enumerate(rows) if row.startswith("--- ")]
        assert len(headers) == 2, rows
        assert rows[headers[1] - 1] == "", "no blank line between the two files"

    def test_the_first_file_is_not_run_together_with_the_second(self, synced: Synced) -> None:
        """The same claim from the other end, and the one that fails if the
        separator is added *inside* a file's diff rather than between files:
        each file contributes exactly one header pair."""
        rows = self.apart_twice(synced).split("\n")
        assert sum(row.startswith("--- ") for row in rows) == 2
        assert sum(row.startswith("+++ ") for row in rows) == 2


@pytest.mark.usefixtures("synced")
class TestTheDiffIsColouredForATerminalOnly:
    """Both halves, because either alone is satisfied by the wrong function.

    "A pipe gets no escapes" holds for a `diff` that never colours anything --
    which is what this was before -- and "a terminal gets escapes" says nothing
    about the redirected case every other test in this file depends on.
    """

    def coloured_run(self, synced: Synced) -> str:
        """`status --diff` in-process, writing to something that says it is a
        terminal, with the sandbox's `NO_COLOR` taken back out."""
        env = {key: value for key, value in synced.second.env.items() if key != "NO_COLOR"}
        seen = support.Screen()
        with mock.patch.dict(os.environ, env, clear=True):
            assert inspection.difference(None, out=seen) == 0
        return seen.getvalue()

    def test_a_terminal_gets_the_lines_in_colour(self, synced: Synced) -> None:
        synced.apart()
        said = self.coloured_run(synced)
        assert f"{colours.REMOVED}-edited on this computer{colours.OFF}" in said
        assert f"{colours.ADDED}+from the repo{colours.OFF}" in said

    def test_the_header_is_structure_rather_than_a_removed_line(self, synced: Synced) -> None:
        """`--- ` starts with `-`. Painting it red is the mistake `colours.diff`
        is ordered to avoid, and end to end is where it would actually be seen."""
        synced.apart()
        header = next(row for row in self.coloured_run(synced).split("\n") if "--- " in row)
        assert header.startswith(colours.BOLD)
        assert colours.REMOVED not in header

    def test_a_captured_stream_gets_exactly_what_it_always_did(self, synced: Synced) -> None:
        """Every other assertion in this file, and `tupferl status --diff |
        delta`, depend on this."""
        synced.apart()
        assert "\x1b" not in synced.diff()


@pytest.mark.usefixtures("synced")
class TestNamingOneFile:
    def test_a_path_limits_the_output_to_that_file(self, synced: Synced) -> None:
        """Both files differ, so "it showed only the one asked for" is
        observable. With one differing file this test could not fail."""
        synced.apart()
        synced.second.write(".vimrc", CONTROL + "set ruler\n")
        whole = synced.diff()
        assert ".bashrc" in whole
        assert ".vimrc" in whole

        one = synced.diff(str(synced.second.home / ".bashrc"))
        assert ".bashrc" in one
        assert ".vimrc" not in one

    def test_a_named_file_that_matches_says_it_is_the_same(self, synced: Synced) -> None:
        """Not "nothing differs", which is the whole-repository sentence, and
        not the name plus "differs" -- which says the opposite of what it means.
        """
        synced.apart()
        said = synced.diff(str(synced.second.home / ".vimrc"))
        assert ".vimrc is the same in $HOME as in the repository." in said
        assert "nothing differs" not in said

    def test_a_tilde_path_is_expanded(self, synced: Synced) -> None:
        """`manifest.relative` expands and makes absolute, so a user need not
        type `$HOME` out.

        This used to be called *"a managed file named by a relative path is
        found"*, and `~/.bashrc` is not one -- `expanduser` makes it absolute
        before anything else looks at it. The test was right and its name was
        not, which is how it read as covering #27 while covering the one case
        that already worked. The real one is below.
        """
        synced.apart()
        assert "-edited on this computer" in synced.diff("~/.bashrc")

    def test_the_name_that_list_prints_is_accepted(self, synced: Synced) -> None:
        """#27: `tupferl list` prints `.bashrc`, and `diff` has to take it back.

        The working directory is the point, so it is set to somewhere that is
        **not** `$HOME` and has no `.bashrc` of its own -- from `$HOME` the old
        cwd-relative reading gave the right answer by accident, which is why the
        bug survived a suite that drives everything from a sandbox.
        """
        synced.apart()
        # The *name* column, which is neither the first nor the last: a row
        # under `--all` is `mark  [host]  name  state`, and the state is several
        # words for anything that is changing. Taking the first field that is
        # neither the direction marker nor the overlay marker is what "the name
        # this listing prints" means now.
        listed = [
            next(field for field in row.split() if field.startswith("."))
            for row in synced.second.say("status", "--all")[1].splitlines()
            if ".bashrc" in row
        ]
        assert listed == [".bashrc"], "the fixture no longer prints the name under test"

        with mock.patch.object(Path, "cwd", return_value=synced.tmp):
            said = synced.diff(".bashrc")
        assert "-edited on this computer" in said

    def test_an_unmanaged_file_is_an_error_that_names_the_way_out(self, synced: Synced) -> None:
        synced.second.write(".zshrc", "setopt nomatch\n")
        status, said = synced.second.say("status", "--diff", str(synced.second.home / ".zshrc"))
        assert status == 2, said
        assert "is not managed" in said
        assert "tupferl status --all" in said

    def test_a_path_outside_home_is_refused_before_anything_is_read(self, synced: Synced) -> None:
        status, said = synced.second.say("status", "--diff", "/etc/hostname")
        assert status == 2, said
        assert "is outside" in said


@pytest.mark.usefixtures("synced")
class TestDiffWritesNothing:
    """The same claim `status` makes, and for the same reason.

    `diff` runs `sync.examine`, and `resolve` inside it merges any file both
    sides changed. That merge happens in a temporary directory of git's own --
    but "it does not reach `$HOME`" is a claim about a real merge running, and
    `apart()` is what makes one run.
    """

    def test_the_merge_it_runs_reaches_no_file(self, synced: Synced) -> None:
        synced.apart()
        before = support.fingerprint(synced.second.home)
        synced.diff()
        assert support.fingerprint(synced.second.home) == before
        # The precondition, without which "nothing moved" is equally true of a
        # machine with nothing to move -- CLAUDE.md §2.
        assert synced.second.call("sync", "--ours") == 0
        assert support.fingerprint(synced.second.home) != before


@pytest.mark.usefixtures("sandbox")
class TestAMachineWithNothingManaged:
    def test_diff_says_how_to_start(self, sandbox: support.Sandbox) -> None:
        from tupferl.__main__ import main

        remote = support.make_remote(sandbox.tmp / "remote.git", sandbox.env)
        with support.quiet():
            assert main(["init", str(remote)]) == 0
        with support.quiet() as said:
            assert main(["status", "--diff"]) == 0
        assert "nothing is managed yet" in said.getvalue()
