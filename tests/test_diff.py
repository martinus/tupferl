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
import unittest
from pathlib import Path, PurePosixPath
from typing import Any
from unittest import mock

from tests import support
from tupferl import inspection, merge, sync
from tupferl.copies import Blob

#: The base both machines start from. Distinct lines, so a one-line edit is one
#: unambiguous hunk.
START = "one\ntwo\nthree\nfour\nfive\n"

#: What `$HOME` gets, and what the repository gets. Different lengths as well as
#: different text: a diff that swapped the two sides would still show one `-`
#: and one `+` line, and only the *content* of each says which way round it is.
MINE = "one\nedited on this computer\nthree\nfour\nfive\n"
THEIRS = "one\nfrom the repo\nthree\nfour\nfive\n"

#: The control file. Managed, and left identical on both sides in most fixtures,
#: so a `diff` that printed a heading per managed file fails.
CONTROL = "set number\nset expandtab\n"


class Machine(support.TwoMachinesCase):
    """`machine-b`, synced, with `.bashrc` and `.vimrc` both managed."""

    def setUp(self) -> None:
        super().setUp()
        self.first.write(".bashrc", START)
        self.first.write(".vimrc", CONTROL)
        self.assertEqual(0, self.first.call("add", str(self.first.home / ".vimrc")))
        self.assertEqual(0, self.first.call("sync"))
        self.assertEqual(0, self.second.call("init", str(self.remote)))
        self.assertEqual(0, self.second.call("sync"))

    def diff(self, *args: str) -> str:
        """`tupferl diff`, insisting it exited 0 -- including when files differ.

        `git diff` answers the same way, and there is no `--exit-code` here
        because plan §4 does not ask for one: the status of a command whose job
        is to show something should say whether it could, not what it found.
        """
        status, said = self.second.say("status", "--diff", *args)
        self.assertEqual(0, status, said)
        return said

    def apart(self) -> None:
        """Make `.bashrc` differ on the two sides, without syncing either."""
        self.second.write(".bashrc", MINE)
        (self.second.repo / ".bashrc").write_text(THEIRS)


class TestWhatDiffShows(Machine):
    def test_a_synced_machine_shows_one_sentence(self) -> None:
        said = self.diff()
        self.assertEqual("nothing differs between $HOME and the repository.", said.strip())

    def test_a_text_difference_is_a_unified_diff(self) -> None:
        self.apart()
        said = self.diff()
        self.assertIn("-edited on this computer", said)
        self.assertIn("+from the repo", said)
        self.assertIn("@@", said)
        # Both bits are the same, so nothing about the executable bit belongs
        # here. Without this, `rendered`'s equality test can be false always and
        # every other assertion in this class still holds.
        self.assertNotIn("executable", said)
        # And the whole-repository fallback sentence is for when nothing was
        # shown -- printing it beside a diff is the same branch inverted.
        self.assertNotIn("nothing differs", said)

    def test_an_identical_file_is_not_mentioned_at_all(self) -> None:
        """`.vimrc` is managed and unchanged, so it must be silent -- otherwise
        `diff` on a machine with forty dotfiles is forty headings and one
        difference buried in them."""
        self.apart()
        said = self.diff()
        self.assertIn(".bashrc", said)
        self.assertNotIn(".vimrc", said)

    def test_a_file_only_the_repository_has_says_so(self) -> None:
        """Not an empty diff. `$HOME` holding nothing is the state a fresh
        machine is in, and "no lines differ" would be the wrong report."""
        (self.second.home / ".bashrc").unlink()
        said = self.diff()
        self.assertIn("only in the repository", said)
        self.assertIn(".bashrc", said)

    def test_a_binary_difference_names_both_sizes_rather_than_showing_bytes(self) -> None:
        """git's own rule for "there are no lines here" -- a NUL in the first
        8000 bytes, asked through `merge.is_text`. Printing nothing would read
        as "these are the same", which is the one wrong answer."""
        (self.second.home / ".bashrc").write_bytes(b"bin\x00ary here\n")
        (self.second.repo / ".bashrc").write_bytes(b"bin\x00ary there, longer\n")
        said = self.diff()
        self.assertIn("are not text", said)
        # Two different numbers, so a report that printed one side's length
        # twice -- or the same length for both -- fails here.
        self.assertIn("13 bytes here", said)
        self.assertIn("22 in the repository", said)
        self.assertNotIn("\x00", said)

    def test_a_path_that_is_not_a_regular_file_is_skipped_with_its_reason(self) -> None:
        """A fifo rather than a socket -- `sun_path` is 104 bytes on macOS and
        a sandbox path plus the repository layout exceeds it."""
        (self.second.home / ".bashrc").unlink()
        os.mkfifo(self.second.home / ".bashrc")
        self.addCleanup((self.second.home / ".bashrc").unlink)
        said = self.diff()
        self.assertIn("skipped", said)
        self.assertIn("is not a regular file", said)

    def test_only_the_executable_bit_differing_is_still_a_difference(self) -> None:
        """`chmod +x` with no edit is a real change that travels (plan §5), and
        a diff of the *lines* renders it as nothing at all -- an empty answer to
        "why does status say this changed?"."""
        (self.second.home / ".bashrc").chmod(0o755)
        said = self.diff()
        self.assertIn("executable here, not in the repository", said)
        self.assertNotIn("@@", said)

    def test_the_bit_is_reported_the_other_way_round_too(self) -> None:
        """The mirror, because one direction alone passes against a sentence
        that names the same side whichever way the bit went."""
        (self.second.repo / ".bashrc").chmod(0o755)
        self.assertIn("executable in the repository, not here", self.diff())

    def test_a_bit_and_a_text_change_are_both_shown(self) -> None:
        """One or the other would be a report that hid half of what changed."""
        self.apart()
        (self.second.home / ".bashrc").chmod(0o755)
        said = self.diff()
        self.assertIn("executable here, not in the repository", said)
        self.assertIn("-edited on this computer", said)


class TestOneFileWithNoRepository(unittest.TestCase):
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

    def reading(
        self,
        found: Blob | None,
        stored: Blob | None,
        action: str = sync.TO_REPO,
        why: str = "",
    ) -> sync.Reading:
        """A `Reading` for `.bashrc`, with paths that are never touched.

        `shows` reads only the name, the outcome and the two blobs -- it opens
        nothing -- so the three paths can be anything. `/nowhere` rather than a
        temporary directory says that out loud: if a future `shows` starts
        reading from disk, this fixture fails rather than quietly working.
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

    def test_identical_copies_produce_nothing(self) -> None:
        """`None`, not an empty string: `difference` counts what it printed, and
        an empty heading would still be a heading."""
        same = Blob(b"one\n", False)
        self.assertIsNone(inspection.shows(self.reading(same, same)))

    def test_a_refused_reading_reports_its_reason(self) -> None:
        said = inspection.shows(self.reading(None, None, action=sync.REFUSED, why="it is a fifo"))
        self.assertIsNotNone(said)
        assert said is not None
        self.assertIn("it is a fifo", said)

    def test_a_file_missing_from_home_is_not_an_empty_diff(self) -> None:
        said = inspection.shows(self.reading(None, Blob(b"one\n", False)))
        assert said is not None
        self.assertIn("only in the repository", said)

    def test_a_binary_side_stops_the_lines_being_shown(self) -> None:
        """Either side is enough. Both-binary is the obvious fixture and would
        pass against a check that only looked at one of them."""
        text = Blob(b"one\n", False)
        binary = Blob(b"one\x00\n", False)
        for found, stored in ((binary, text), (text, binary)):
            with self.subTest(binary="home" if found is binary else "repository"):
                said = inspection.shows(self.reading(found, stored))
                assert said is not None
                self.assertIn("are not text", said)

    def test_the_executable_bit_alone_still_says_something(self) -> None:
        """Same bytes, different bit. A diff of the lines is empty here, so a
        report built from the diff alone would say nothing at all."""
        said = inspection.shows(self.reading(Blob(b"x\n", True), Blob(b"x\n", False)))
        assert said is not None
        self.assertEqual(".bashrc: executable here, not in the repository.", said)


class TestWhichSideIsWhich(Machine):
    """The direction, pinned -- because it is a judgement rather than a given.

    `git diff` shows the working tree as `+`; this shows `$HOME` as `-`, because
    `merge.unified` renders it and the conflict prompt's `[d]` uses the same
    function on the same two files. A user who runs `tupferl diff .bashrc` and
    then settles a conflict about `.bashrc` minutes later sees one orientation,
    and both labels say in words which side they are.

    Pinned here so that flipping it is a visible decision rather than a silent
    one, and so that a reader who thinks it is backwards has one place to argue.
    """

    def test_home_is_the_minus_side_and_says_so(self) -> None:
        self.apart()
        said = self.diff()
        self.assertIn("--- .bashrc (this computer)", said)
        self.assertIn("+++ .bashrc (the repository)", said)

    def test_the_labels_and_the_signs_agree(self) -> None:
        """The assertion the one above cannot make: that the bytes on the `-`
        lines really are `$HOME`'s. Labels that were right while the sides were
        swapped would pass that test and fail this one."""
        self.apart()
        rows = self.diff().splitlines()
        minus = [row[1:] for row in rows if row.startswith("-") and not row.startswith("---")]
        plus = [row[1:] for row in rows if row.startswith("+") and not row.startswith("+++")]
        self.assertEqual(["edited on this computer"], minus)
        self.assertEqual(["from the repo"], plus)

    def test_the_prompt_shows_the_same_file_the_same_way(self) -> None:
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
        self.apart()
        prompt = conflicts.unified(sides)
        self.assertIn(prompt, self.diff())
        self.assertIn("--- .bashrc (this computer)", prompt)
        self.assertEqual(prompt, merge.unified(".bashrc", MINE.encode(), THEIRS.encode()))


class TestWhichSideTheDiffPutsOnTheMinus(unittest.TestCase):
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

    HERE = Blob(b"mine\n", executable=False)
    THERE = Blob(b"theirs\n", executable=False)

    def shown(self, action: str) -> str:
        return inspection.rendered(PurePosixPath(".bashrc"), self.HERE, self.THERE, action)

    def sides(self, action: str) -> tuple[str, str]:
        """The two header lines, as `(minus, plus)`."""
        lines = [row for row in self.shown(action).split("\n") if row[:3] in ("---", "+++")]
        self.assertEqual(2, len(lines), f"no diff header for {action}:\n{self.shown(action)}")
        return lines[0], lines[1]

    def test_a_push_puts_the_repository_on_the_minus_side(self) -> None:
        """The bug. Only `$HOME` changed, so sync writes `$HOME`'s bytes into
        the repository: the repository's copy is what disappears."""
        minus, plus = self.sides(sync.TO_REPO)
        self.assertIn("the repository", minus)
        self.assertIn("this computer", plus)
        self.assertIn("-theirs", self.shown(sync.TO_REPO))
        self.assertIn("+mine", self.shown(sync.TO_REPO))

    def test_a_pull_puts_this_computer_on_the_minus_side(self) -> None:
        """The half that was already right, and the reason the fix could not be
        "swap the arguments": that would correct the case above and break this
        one."""
        minus, plus = self.sides(sync.TO_HOME)
        self.assertIn("this computer", minus)
        self.assertIn("the repository", plus)
        self.assertIn("-mine", self.shown(sync.TO_HOME))
        self.assertIn("+theirs", self.shown(sync.TO_HOME))

    def test_a_restore_reads_as_a_pull(self) -> None:
        """`RESTORED` writes `$HOME` and not the repository, so it is a pull by
        the only definition that matters here. Asserted rather than assumed,
        because it is the action a reader is least likely to think about."""
        minus, _ = self.sides(sync.RESTORED)
        self.assertIn("this computer", minus)

    def test_a_two_sided_change_says_so_instead_of_implying_a_direction(self) -> None:
        """A conflict writes neither side and a clean merge writes both, so
        there is no side being replaced. Said in words rather than shown as an
        arrow that would be a guess -- the diff is still the difference, and a
        reader told that will not read the `-` lines as doomed."""
        for action in (sync.CONFLICT, sync.MERGED):
            with self.subTest(action=action):
                self.assertIn("both sides changed", self.shown(action))
        # **And they agree with each other.** A clean merge writes both sides,
        # so `to_repo` is true for it: orienting on that alone reversed a merge
        # and not a conflict, giving the one case with no direction two
        # displays depending on which two-sided outcome it happened to be. The
        # note above is printed either way, so only this sees it.
        self.assertEqual(self.sides(sync.CONFLICT), self.sides(sync.MERGED))
        self.assertIn("this computer", self.sides(sync.MERGED)[0])

    def test_a_one_sided_change_does_not_say_it(self) -> None:
        """The precondition. Without it the assertion above is satisfied by a
        note printed on every diff, which would make it noise rather than the
        thing that distinguishes the two-sided case."""
        for action in (sync.TO_REPO, sync.TO_HOME):
            with self.subTest(action=action):
                self.assertNotIn("both sides changed", self.shown(action))

    def test_every_action_sync_knows_about_is_oriented(self) -> None:
        """Read out of `sync.RULES` rather than listed again here, so an action
        added there cannot be missed. It is the same table `rendered` derives
        the orientation from, which is the point: a sixth action gets an
        orientation by existing, and this asserts the orientation it gets is
        the one its own row implies."""
        self.assertGreaterEqual(len(sync.RULES), 8, "the table shrank; this test reads it")
        for action, rule in sync.RULES.items():
            with self.subTest(action=action):
                minus, plus = self.sides(action)
                if rule.to_repo and not rule.to_home:
                    self.assertIn("the repository", minus, f"{action} is a push")
                elif rule.to_home and not rule.to_repo:
                    self.assertIn("this computer", minus, f"{action} is a pull")
                else:
                    self.assertIn("both sides changed", self.shown(action))
                self.assertNotEqual(minus[4:], plus[4:], "both headers name the same side")


class TestShowingTheDiffThroughTheUsersPager(support.TwoMachinesCase):
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

    def setUp(self) -> None:
        super().setUp()
        self.first.write(".bashrc", "one\ntwo\n")
        self.assertEqual(0, self.first.call("add", str(self.first.home / ".bashrc")))
        self.first.write(".bashrc", "ONE\ntwo\n")
        self.seen = self.tmp / "seen.txt"
        self.fake = self.tmp / "pager.py"
        self.fake.write_text(
            "import sys, pathlib\n"
            f"pathlib.Path({str(self.seen)!r}).write_text('PAGED\\n' + sys.stdin.read())\n",
            encoding="utf-8",
        )

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
                self.assertEqual(0, inspection.difference(None, out))
        finally:
            os.chdir(here)
        return out.getvalue()

    def paged(self) -> str:
        self.assertTrue(self.seen.is_file(), "the pager never ran")
        return self.seen.read_text(encoding="utf-8")

    def test_the_diff_goes_to_the_pager_git_is_configured_with(self) -> None:
        self.configure(f"{sys.executable} {self.fake}")
        printed = self.diff()
        self.assertIn("--- .bashrc", self.paged())
        # `+ONE`: only `$HOME` changed, so the repository is the side replaced.
        # This test is about the *pager*; the orientation itself is asserted by
        # `TestWhichSideTheDiffPutsOnTheMinus`.
        self.assertIn("+ONE", self.paged())
        self.assertNotIn("--- .bashrc", printed, "it was printed as well as paged")

    def test_with_no_pager_configured_it_prints(self) -> None:
        """The other half, and the one every machine without a `core.pager`
        gets. Without it, a `show` that never printed at all would pass the test
        above."""
        self.assertIn("--- .bashrc", self.diff())
        self.assertFalse(self.seen.exists())

    def test_a_redirected_diff_is_never_paged(self) -> None:
        """What keeps `tupferl status --diff | delta` working, and every test in
        this file: a redirected diff is something a program is about to read,
        and handing it to a pager would be the tool deciding it knew better."""
        self.configure(f"{sys.executable} {self.fake}")
        self.assertIn("--- .bashrc", self.diff(terminal=False))
        self.assertFalse(self.seen.exists(), "a redirected diff was paged")

    def test_a_pager_that_is_not_installed_costs_the_user_nothing(self) -> None:
        """The diff is the point and the pager is only how. A machine that lost
        its pager -- a shared `.gitconfig` naming one this host has not
        installed, which is exactly what tupferl makes easy -- must still show
        the diff."""
        self.configure("no-such-pager-anywhere")
        printed = self.diff()
        self.assertIn("--- .bashrc", printed)
        self.assertIn("could not show the diff", printed)

    def test_a_spawn_that_raises_still_shows_the_diff(self) -> None:
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
        self.configure(f"{sys.executable} {self.fake}")
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
            printed = self.diff()
        self.assertIn("could not show the diff", printed)
        self.assertIn("no fork for you", printed, "the reason was swallowed")
        self.assertIn("--- .bashrc", printed, "the diff itself was lost with the pager")
        self.assertFalse(self.seen.exists(), "the pager somehow ran")

    def test_git_pager_wins_over_the_configured_one(self) -> None:
        """git's own order is `GIT_PAGER`, then `core.pager`, then `PAGER`, and
        the point of reading git's config is that its answer matches git's. A
        variable set for one command has to beat a file set for all of them, or
        the escape hatch every git user reaches for does not work here."""
        self.configure(f"{sys.executable} {self.tmp / 'never.py'}")
        elsewhere = self.tmp / "chosen.txt"
        picked = self.tmp / "picked.py"
        picked.write_text(
            f"import sys, pathlib\npathlib.Path({str(elsewhere)!r}).write_text(sys.stdin.read())\n",
            encoding="utf-8",
        )
        self.first.env["GIT_PAGER"] = f"{sys.executable} {picked}"
        self.diff()
        self.assertIn("--- .bashrc", elsewhere.read_text(encoding="utf-8"))
        self.assertFalse(self.seen.exists(), "core.pager ran despite GIT_PAGER")

    def test_pager_is_the_last_resort(self) -> None:
        """The other end of the same order: `$PAGER` is honoured, but only when
        git has been told nothing more specific."""
        self.first.env["PAGER"] = f"{sys.executable} {self.fake}"
        self.diff()
        self.assertIn("--- .bashrc", self.paged())

    def test_an_empty_git_pager_means_no_pager_rather_than_unset(self) -> None:
        """`GIT_PAGER=` is how a git user turns paging off for one command, and
        it has to beat `core.pager` the same way a non-empty one does.

        This is why `pager` tests membership with `in` rather than chaining
        `or`: an `or` reads the empty string as "not set" and falls through to
        the file, which is the opposite of what was asked.
        """
        self.configure(f"{sys.executable} {self.fake}")
        self.first.env["GIT_PAGER"] = ""
        self.assertIn("--- .bashrc", self.diff())
        self.assertFalse(self.seen.exists(), "core.pager ran despite an empty GIT_PAGER")

    def test_pager_diff_is_read_and_beats_core_pager(self) -> None:
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
        self.configure(f"{sys.executable} {self.tmp / 'never.py'}")
        self.configure(f"{sys.executable} {self.fake}", key="pager.diff")
        self.diff()
        self.assertIn("--- .bashrc", self.paged())

    def test_core_pager_still_works_when_pager_diff_is_unset(self) -> None:
        """The other half. Reading the new key must not cost the old one, and a
        test of `pager.diff` alone cannot show that."""
        self.configure(f"{sys.executable} {self.fake}")
        self.diff()
        self.assertIn("--- .bashrc", self.paged())

    def test_git_pager_still_beats_pager_diff(self) -> None:
        """The new rung goes *below* the environment variable, not above it.
        Reading `pager.diff` first in the function is not the same as reading it
        first in the order, and this is the assertion that tells them apart."""
        self.configure(f"{sys.executable} {self.tmp / 'never.py'}", key="pager.diff")
        chosen = self.tmp / "chosen.txt"
        picked = self.tmp / "picked.py"
        picked.write_text(
            f"import sys, pathlib\npathlib.Path({str(chosen)!r}).write_text(sys.stdin.read())\n",
            encoding="utf-8",
        )
        self.first.env["GIT_PAGER"] = f"{sys.executable} {picked}"
        self.diff()
        self.assertIn("--- .bashrc", chosen.read_text(encoding="utf-8"))

    def test_a_pager_that_is_a_shell_command_line_works(self) -> None:
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
        self.configure(
            f'if [ -n "$HOME" ]; then {sys.executable} {self.fake}; else cat; fi',
            key="pager.diff",
        )
        self.diff()
        self.assertIn("--- .bashrc", self.paged())

    def test_a_shell_pager_that_is_not_installed_still_shows_the_diff(self) -> None:
        """The guarantee had to move with the mechanism. Run directly, a missing
        pager raised `OSError`; through a shell it is exit 127 and
        `check=False` reads that as a run that happened -- so the user would
        get an empty screen, which is the one thing `show` promises never to
        do. The `if` wrapper is what makes this a *shell* 127 rather than the
        bare-name case the test above it covers.
        """
        self.configure("if true; then no-such-pager-anywhere; fi", key="pager.diff")
        printed = self.diff()
        self.assertIn("--- .bashrc", printed)
        self.assertIn("could not show the diff", printed)

    def test_a_pager_that_stops_early_does_not_print_the_diff_twice(self) -> None:
        """The line the 126/127 test is drawn at. `q` in `less`, or a `head`
        that has seen enough, exits non-zero having *shown* the diff -- so
        falling back on any non-zero status would print it again underneath.
        Only the two codes the shell reserves for "could not run it" count.
        """
        self.configure(f"{sys.executable} {self.fake} && exit 3", key="pager.diff")
        printed = self.diff()
        self.assertIn("--- .bashrc", self.paged())
        self.assertNotIn("--- .bashrc", printed, "the diff was printed as well as paged")
        self.assertNotIn("could not show the diff", printed)

    def test_a_false_pager_diff_pages_with_nothing_at_all(self) -> None:
        """`pager.<cmd>` may be a boolean, and then it is not a command.

        Measured against git 2.43: a false one means *do not page*, and neither
        `core.pager` nor `$PAGER` is consulted. Read as a command instead it
        would be **spawned** -- `false` exits 1, which is not one of the two
        codes `show` falls back on, so the user would get an empty screen and no
        diff at all, and nobody would connect that to a setting meaning "do not
        page". Both other sources are set here, because the claim is that they
        are skipped and not merely that this one is.
        """
        self.configure(f"{sys.executable} {self.fake}")
        self.first.env["PAGER"] = f"{sys.executable} {self.fake}"
        self.configure("false", key="pager.diff")
        self.assertIn("--- .bashrc", self.diff())
        self.assertFalse(self.seen.exists(), "something was paged despite pager.diff = false")

    def test_off_is_false_too_and_git_is_asked_which(self) -> None:
        """Six spellings are false and six are true; git is asked rather than
        the list being copied here, where it would go stale in silence. `off` is
        the one a hand-rolled `== "false"` would miss."""
        self.configure(f"{sys.executable} {self.fake}")
        self.configure("off", key="pager.diff")
        self.assertIn("--- .bashrc", self.diff())
        self.assertFalse(self.seen.exists(), "something was paged despite pager.diff = off")

    def test_a_true_pager_diff_says_page_but_not_how(self) -> None:
        """The other half of the boolean rule, and a different answer from
        `false`: it falls through to `core.pager`. Without this, returning "do
        not page" for *any* boolean passes the two tests above."""
        self.configure(f"{sys.executable} {self.fake}")
        self.configure("true", key="pager.diff")
        self.diff()
        self.assertIn("--- .bashrc", self.paged())

    def test_cat_is_gits_spelling_of_no_pager(self) -> None:
        """git treats it as "do not page", and forking a process to do what a
        print already does is worth avoiding."""
        self.configure("cat")
        self.assertIn("--- .bashrc", self.diff())
        self.assertFalse(self.seen.exists())


class TestNamingOneFile(Machine):
    def test_a_path_limits_the_output_to_that_file(self) -> None:
        """Both files differ, so "it showed only the one asked for" is
        observable. With one differing file this test could not fail."""
        self.apart()
        self.second.write(".vimrc", CONTROL + "set ruler\n")
        whole = self.diff()
        self.assertIn(".bashrc", whole)
        self.assertIn(".vimrc", whole)

        one = self.diff(str(self.second.home / ".bashrc"))
        self.assertIn(".bashrc", one)
        self.assertNotIn(".vimrc", one)

    def test_a_named_file_that_matches_says_it_is_the_same(self) -> None:
        """Not "nothing differs", which is the whole-repository sentence, and
        not the name plus "differs" -- which says the opposite of what it means.
        """
        self.apart()
        said = self.diff(str(self.second.home / ".vimrc"))
        self.assertIn(".vimrc is the same in $HOME as in the repository.", said)
        self.assertNotIn("nothing differs", said)

    def test_a_tilde_path_is_expanded(self) -> None:
        """`manifest.relative` expands and makes absolute, so a user need not
        type `$HOME` out.

        This used to be called *"a managed file named by a relative path is
        found"*, and `~/.bashrc` is not one -- `expanduser` makes it absolute
        before anything else looks at it. The test was right and its name was
        not, which is how it read as covering #27 while covering the one case
        that already worked. The real one is below.
        """
        self.apart()
        status, said = self.second.say("status", "--diff", "~/.bashrc")
        self.assertEqual(0, status, said)
        self.assertIn("-edited on this computer", said)

    def test_the_name_that_list_prints_is_accepted(self) -> None:
        """#27: `tupferl list` prints `.bashrc`, and `diff` has to take it back.

        The working directory is the point, so it is set to somewhere that is
        **not** `$HOME` and has no `.bashrc` of its own -- from `$HOME` the old
        cwd-relative reading gave the right answer by accident, which is why the
        bug survived a suite that drives everything from a sandbox.
        """
        self.apart()
        # The *name* column, which is no longer the last one: a row under
        # `--all` is `[host]  name  state`, and the state is several words for
        # anything that is changing. Taking the first field that is not the
        # overlay marker is what "the name this listing prints" means now.
        listed = [
            next(field for field in row.split() if field != "host")
            for row in self.second.say("status", "--all")[1].splitlines()
            if ".bashrc" in row
        ]
        self.assertEqual([".bashrc"], listed, "the fixture no longer prints the name under test")

        with mock.patch.object(Path, "cwd", return_value=self.tmp):
            status, said = self.second.say("status", "--diff", ".bashrc")
        self.assertEqual(0, status, said)
        self.assertIn("-edited on this computer", said)

    def test_an_unmanaged_file_is_an_error_that_names_the_way_out(self) -> None:
        self.second.write(".zshrc", "setopt nomatch\n")
        status, said = self.second.say("status", "--diff", str(self.second.home / ".zshrc"))
        self.assertEqual(2, status, said)
        self.assertIn("is not managed", said)
        self.assertIn("tupferl status --all", said)

    def test_a_path_outside_home_is_refused_before_anything_is_read(self) -> None:
        status, said = self.second.say("status", "--diff", "/etc/hostname")
        self.assertEqual(2, status, said)
        self.assertIn("is outside", said)


class TestDiffWritesNothing(Machine):
    """The same claim `status` makes, and for the same reason.

    `diff` runs `sync.examine`, and `resolve` inside it merges any file both
    sides changed. That merge happens in a temporary directory of git's own --
    but "it does not reach `$HOME`" is a claim about a real merge running, and
    `apart()` is what makes one run.
    """

    def contents(self) -> dict[str, bytes]:
        """Every file under `$HOME` outside `.git`, by name and by bytes.

        Bytes rather than size and mode: the edits here are one line to another
        of a similar length, and `tests/test_status.py`'s first fingerprint could
        not tell two 24-byte files apart. Same trap, same answer.
        """
        return {
            str(path.relative_to(self.second.home)): path.read_bytes()
            for path in self.second.home.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }

    def test_the_merge_it_runs_reaches_no_file(self) -> None:
        self.apart()
        before = self.contents()
        self.diff()
        self.assertEqual(before, self.contents())
        # The precondition, without which "nothing moved" is equally true of a
        # machine with nothing to move -- CLAUDE.md §2.
        self.assertEqual(0, self.second.call("sync", "--ours"))
        self.assertNotEqual(before, self.contents())


class TestAMachineWithNothingManaged(support.SandboxCase):
    def test_diff_says_how_to_start(self) -> None:
        from tupferl.__main__ import main

        remote = support.make_remote(self.tmp / "remote.git", self.env)
        with support.quiet():
            self.assertEqual(0, main(["init", str(remote)]))
        with support.quiet() as said:
            self.assertEqual(0, main(["status", "--diff"]))
        self.assertIn("nothing is managed yet", said.getvalue())
