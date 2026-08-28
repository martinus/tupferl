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
import unittest
from pathlib import Path, PurePosixPath
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


class Machine(support.TwoMachines):
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
