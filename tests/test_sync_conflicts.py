"""Plan §7.4 item 2: every choice at the prompt, and the three flags, end to end.

Two computers, one bare remote, a real `python -m tupferl sync`, and -- where a
key is pressed -- a real pty on its stdin. Nothing here asserts on an internal:
what a choice means is a fact about the bytes in `$HOME`, in the repository and
on the *other* machine after it syncs, and that is what every test looks at.

**The two sides are named after the machines, not after the choice.** `machine-a`
writes `FROM-A` and `machine-b` writes `FROM-B`, and the prompt always runs on
`machine-b` -- so "local" is `FROM-B` and "the repository" is `FROM-A`, in every
test, with no sentence needed to say which is which. A fixture that called them
`MINE` and `THEIRS` reads the wrong way round on the machine doing the asking.

**Each choice is followed through to the other computer.** Plan §7.2's property 5
is "no silent loss", and a test that stopped at `machine-b`'s `$HOME` could not
tell a choice that was written from one that was written and then lost on the
next sync. So the shape is: choose, then sync `machine-a`, then read `machine-a`.

Named `test_sync_conflicts` rather than `test_conflicts_cli`, which is what it
is about: `tools/mutants.targets_for` picks a suite by the `test_<module>_`
prefix, and the branches this file covers -- `sync.settled`, the four new `RULES`
rows, the backup window -- are `sync.py`'s. Under the other name a mutant in
either module reached these tests only through the whole-suite confirmation
pass, which is a slower way to the same answer.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

import pytest

from tests import support

#: What each machine writes into the same line. Distinct, and neither a prefix
#: of the other, so no assertion below can pass against the wrong side.
FROM_A = "FROM-A\ntwo\nthree\nfour\nfive\n"
FROM_B = "FROM-B\ntwo\nthree\nfour\nfive\n"

#: What `[b]` keeps: both lines, in `$HOME`-first order, and no markers.
BOTH_KEPT = "FROM-B\nFROM-A\ntwo\nthree\nfour\nfive\n"

#: What the `[e]` editor below writes.
BY_HAND = "SETTLED-BY-HAND\n"


@dataclass(frozen=True)
class Conflicted(support.TwoMachines):
    """Both computers change the first line of `.bashrc`; `machine-a` pushes.

    So `machine-b`'s next sync has three versions that disagree: its own `$HOME`,
    the repository's copy that just arrived, and the snapshot from its `init`.
    """

    def settle(self, *args: str, keys: str | None = None) -> str:
        """Sync `machine-b` with the given flags or keypresses; return its stdout.

        Insists on exit 0, which is the assertion that *something was decided*:
        a run that left the conflict for a human returns 1, so a choice that
        silently failed to apply cannot reach the assertions in the caller.
        """
        done = self.second.run("sync", *args, keys=keys)
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout

    def everywhere(self, want: str) -> None:
        """Assert `want` is `machine-b`'s file, its stored copy, and -- after the
        other machine syncs -- `machine-a`'s file too.

        The third is the one that matters and the one a weaker test would leave
        out: plan §7.2's property 5 is that a choice made at the prompt survives
        the next sync on the other computer.
        """
        assert self.second.read(".bashrc") == want
        assert self.second.stored(".bashrc").read_text(encoding="utf-8") == want
        assert self.first.call("sync") == 0
        assert self.first.read(".bashrc") == want

    def editor_writing(self, text: str) -> None:
        """Point `machine-b`'s `$EDITOR` at a script that writes `text`.

        A real program, run through the real `shlex.split` and `subprocess.run`.
        `$EDITOR` is set in this machine's environment only -- the sandbox does
        not carry one in, so a developer's own editor cannot be launched by the
        suite.
        """
        where = support.fake_editor(self.tmp / "fake-editor", f'printf "{text}" > "$1"')
        self.second.env["EDITOR"] = str(where)


@pytest.fixture
def conflicted(two_machines: support.TwoMachines) -> Conflicted:
    """A `Conflicted`, with the conflict already arranged."""
    box = Conflicted(**vars(two_machines))
    assert box.second.call("init", str(box.remote)) == 0
    box.diverge(".bashrc", FROM_A.encode(), FROM_B.encode())
    return box


@pytest.mark.usefixtures("conflicted")
class TestTheFlags:
    """Plan §3.4's `--ours` / `--theirs` / `--no-input`, which answer for a
    script with nobody at the keyboard."""

    def test_ours_keeps_this_computer(self, conflicted: Conflicted) -> None:
        text = conflicted.settle("--ours")
        assert "kept local .bashrc" in text
        conflicted.everywhere(FROM_B)

    def test_theirs_keeps_the_repository(self, conflicted: Conflicted) -> None:
        text = conflicted.settle("--theirs")
        assert "kept remote .bashrc" in text
        conflicted.everywhere(FROM_A)

    def test_no_input_leaves_both_copies_alone(self, conflicted: Conflicted) -> None:
        done = conflicted.second.run("sync", "--no-input")
        assert done.returncode == 1
        assert "conflict in .bashrc" in done.stdout
        assert conflicted.second.read(".bashrc") == FROM_B
        assert conflicted.second.stored(".bashrc").read_text(encoding="utf-8") == FROM_A

    def test_a_terminal_that_is_not_there_is_no_input(self, conflicted: Conflicted) -> None:
        """No flag at all, and no stdin. A prompt here would block a cron job
        for ever; reading EOF and calling it a decision would be worse."""
        done = conflicted.second.run("sync")
        assert done.returncode == 1
        assert "conflict in .bashrc" in done.stdout

    def test_ours_and_theirs_cannot_both_be_given(self, conflicted: Conflicted) -> None:
        """ "Keep mine" and "keep theirs" cannot both be the answer, and a run
        that honoured the last one would resolve real conflicts by argument
        order."""
        done = conflicted.second.run("sync", "--ours", "--theirs")
        assert done.returncode == 2
        assert "not allowed with" in done.stderr


@pytest.fixture
def two_conflicts(two_machines: support.TwoMachines) -> support.TwoMachines:
    """Two files that both conflict in the same run.

    Built on the plain two-machine fixture rather than on `conflicted`, because
    the intermediate syncs here have to succeed and a third conflicting file
    would stop them.
    """
    box = two_machines
    for name in (".vimrc", ".inputrc"):
        box.first.write(name, "shared\n")
        assert box.first.call("add", str(box.first.home / name)) == 0
    assert box.first.call("sync") == 0
    assert box.second.call("init", str(box.remote)) == 0
    for name in (".vimrc", ".inputrc"):
        box.diverge(name, b"FROM-A\n", b"FROM-B\n")
    return box


@pytest.mark.usefixtures("two_conflicts")
class TestTwoConflictsAtOnce:
    """Two files that both conflict in the same run.

    One file cannot show that the answer is given *per file*: a settler called
    once and then remembered would pass every test above.
    """

    def test_a_flag_answers_each_of_them(self, two_conflicts: support.TwoMachines) -> None:
        done = two_conflicts.second.run("sync", "--ours")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "kept local .vimrc" in done.stdout
        assert "kept local .inputrc" in done.stdout
        assert "0 in conflict" in done.stdout

    def test_the_prompt_asks_about_each_of_them(self, two_conflicts: support.TwoMachines) -> None:
        """Two keys for two files, and they are different keys -- so a run that
        asked once and applied the answer twice gives the wrong file the wrong
        side, which is what the two assertions below tell apart.

        `.inputrc` sorts before `.vimrc`, and `manifest.managed` is sorted, so
        the first key answers `.inputrc`.
        """
        done = two_conflicts.second.run("sync", keys="lr")
        assert done.returncode == 0, done.stdout + done.stderr
        assert two_conflicts.second.read(".inputrc") == "FROM-B\n"
        assert two_conflicts.second.read(".vimrc") == "FROM-A\n"


@pytest.mark.usefixtures("conflicted")
class TestTheKeys:
    """The five answers, each typed at a real terminal."""

    def test_the_prompt_shows_both_sides_before_asking(self, conflicted: Conflicted) -> None:
        text = conflicted.second.run("sync", keys="s").stdout
        assert "FROM-B" in text
        assert "FROM-A" in text
        assert "this computer" in text
        assert "the repository" in text
        assert "[l] keep local" in text
        assert "[e] edit merged file" in text

    def test_l_keeps_this_computer(self, conflicted: Conflicted) -> None:
        assert "kept local .bashrc" in conflicted.settle(keys="l")
        conflicted.everywhere(FROM_B)

    def test_r_keeps_the_repository(self, conflicted: Conflicted) -> None:
        assert "kept remote .bashrc" in conflicted.settle(keys="r")
        conflicted.everywhere(FROM_A)

    def test_b_keeps_both(self, conflicted: Conflicted) -> None:
        assert "kept both .bashrc" in conflicted.settle(keys="b")
        conflicted.everywhere(BOTH_KEPT)

    def test_e_keeps_what_the_editor_saved(self, conflicted: Conflicted) -> None:
        conflicted.editor_writing(BY_HAND)
        assert "edited .bashrc" in conflicted.settle(keys="e")
        conflicted.everywhere(BY_HAND)

    def test_s_leaves_both_copies_alone_and_says_a_human_is_needed(
        self, conflicted: Conflicted
    ) -> None:
        done = conflicted.second.run("sync", keys="s")
        assert done.returncode == 1
        assert "conflict in .bashrc" in done.stdout
        assert conflicted.second.read(".bashrc") == FROM_B
        assert conflicted.second.stored(".bashrc").read_text(encoding="utf-8") == FROM_A

    def test_d_shows_the_whole_diff_and_asks_again(self, conflicted: Conflicted) -> None:
        done = conflicted.second.run("sync", keys="dl")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "-FROM-B" in done.stdout
        assert "+FROM-A" in done.stdout
        conflicted.everywhere(FROM_B)


@pytest.mark.usefixtures("conflicted")
class TestWhatSettlingLeavesBehind:
    """The parts a test that stopped at the file contents would miss."""

    def test_the_snapshot_moves_to_what_was_chosen(self, conflicted: Conflicted) -> None:
        """Otherwise the next run compares against a state neither computer
        holds, and re-raises a conflict the user already settled."""
        conflicted.settle("--ours")
        assert conflicted.second.snapshot(".bashrc").read_text(encoding="utf-8") == FROM_B

    def test_a_second_sync_changes_nothing(self, conflicted: Conflicted) -> None:
        """Plan §7.2's property 3, at the state a settled conflict leaves. A
        choice that did not reach all three copies shows up here as a run that
        keeps finding something to do."""
        conflicted.settle("--ours")
        text = conflicted.settle()
        assert ".bashrc" not in text.split("\n\n")[0]
        assert "0 changed" in text

    def test_the_repository_is_left_clean(self, conflicted: Conflicted) -> None:
        """A copy written and not committed is one the next run commits with a
        message that names nothing."""
        conflicted.settle("--theirs")
        assert conflicted.second.git("status", "--porcelain") == ""

    def test_the_commit_names_the_file(self, conflicted: Conflicted) -> None:
        conflicted.settle("--ours")
        assert ".bashrc" in conflicted.second.git("log", "-1", "--format=%s")

    def test_keeping_the_repository_backs_up_what_it_replaced(self, conflicted: Conflicted) -> None:
        """Plan §5: `$HOME`'s copy is the user's, and `[r]` overwrites it. This
        backup is the only surviving copy of what they had."""
        conflicted.settle("--theirs")
        saved = list(conflicted.second.backups.rglob(".bashrc"))
        assert len(saved) == 1, f"expected one backup, found {saved}"
        assert saved[0].read_text(encoding="utf-8") == FROM_B

    def test_keeping_this_computer_backs_up_nothing(self, conflicted: Conflicted) -> None:
        """`[l]` writes only the repository, so nothing in `$HOME` is replaced.
        A backup taken anyway would push a real one out of plan §5's window of
        five -- and it is `RULES`' `to_home` column that decides both."""
        conflicted.settle("--ours")
        assert not conflicted.second.backups.exists(), (
            "a backup was taken of a file nothing replaced"
        )


@pytest.fixture
def binary(two_machines: support.TwoMachines) -> support.TwoMachines:
    """A file with a NUL in it that both computers changed."""
    box = two_machines
    (box.first.home / ".icon").write_bytes(b"\x00base\n")
    assert box.first.call("add", str(box.first.home / ".icon")) == 0
    assert box.first.call("sync") == 0
    assert box.second.call("init", str(box.remote)) == 0
    box.diverge(".icon", b"\x00from-a\n", b"\x00from-b\n")
    return box


@pytest.mark.usefixtures("binary")
class TestABinaryConflict:
    """A file with a NUL in it that both computers changed. There are no lines
    to take from each side, so `[b]`, `[e]` and `[d]` have nothing to offer --
    but `[l]` and `[r]` still do, and that is the whole of what is on offer."""

    def test_it_is_one_choice_for_the_whole_file(self, binary: support.TwoMachines) -> None:
        done = binary.second.run("sync", keys="s")
        assert "not a text file" in done.stdout
        assert "whole file" in done.stdout
        assert "[b] keep both" not in done.stdout

    def test_a_side_can_still_be_taken(self, binary: support.TwoMachines) -> None:
        done = binary.second.run("sync", keys="r")
        assert done.returncode == 0, done.stdout + done.stderr
        assert (binary.second.home / ".icon").read_bytes() == b"\x00from-a\n"


#: The same conflict as `FROM_A`/`FROM_B` with Windows line endings.
CRLF_A = "FROM-A\r\ntwo\r\nthree\r\n"
CRLF_B = "FROM-B\r\ntwo\r\nthree\r\n"


@pytest.fixture
def crlf(conflicted: Conflicted) -> Conflicted:
    """The same conflict in a file with CRLF line endings."""
    # Replaces the LF conflict `conflicted` set up: `machine-b` has not
    # synced yet, so overwriting both copies leaves exactly one conflict.
    conflicted.diverge(".bashrc", CRLF_A.encode(), CRLF_B.encode())
    return conflicted


@pytest.mark.usefixtures("crlf")
class TestAFileWithWindowsLineEndings:
    """The end-to-end half of `tests/test_conflicts.py`'s CRLF cases.

    git writes CRLF markers into a CRLF file, so the guard that stops an
    unfinished `[e]` reaching disk was inert for the whole class: `sync` exited 0
    and the markers landed in `$HOME`, the repository and the snapshot, on both
    machines. Asserted here against the files themselves rather than against the
    prompt's text, because that is where the damage was.
    """

    def test_an_unfinished_edit_never_reaches_either_computer(self, crlf: Conflicted) -> None:
        """An editor that saves nothing is the classic "I quit without
        resolving". It must be refused, and the run must not report success."""
        # An editor that writes nothing at all, so what comes back is exactly
        # the merged file it was handed -- markers included.
        crlf.second.env["EDITOR"] = str(support.fake_editor(crlf.tmp / "quitter", "exit 0"))

        done = crlf.second.run("sync", keys="e")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "still has tupferl's conflict markers" in done.stdout
        for where_now in (
            crlf.second.home / ".bashrc",
            crlf.second.stored(".bashrc"),
            crlf.second.snapshot(".bashrc"),
        ):
            assert b"<<<<<<<" not in where_now.read_bytes(), f"markers reached {where_now}"

    def test_a_choice_still_settles_it(self, crlf: Conflicted) -> None:
        """The other half: the guard must not refuse every CRLF file.

        Read as bytes. `Path.read_text` translates newlines, so it reports a
        CRLF file and an LF one as the same string -- which would make this pass
        against a sync that had silently rewritten the user's line endings.
        """
        assert "kept local .bashrc" in crlf.settle(keys="l")
        want = CRLF_B.encode()
        assert (crlf.second.home / ".bashrc").read_bytes() == want
        assert crlf.first.call("sync") == 0
        assert (crlf.first.home / ".bashrc").read_bytes() == want


@pytest.mark.usefixtures("conflicted")
class TestAPromptNobodyAnswers:
    """What the fixture does when the keys run out: fails, and says what it saw.

    `support.FALLBACK` normally makes this impossible -- that is its whole job --
    so it is removed here to build the case it prevents. The child then sits at
    the prompt until `PROMPTED`, and is killed.

    The assertion is on the *output*, and it is what collecting to a file buys.
    With a pipe, the parent holds the child's bytes and `communicate` on a killed
    child hands back nothing, so the failure reads as a bare timeout with no clue
    which file was being asked about. With a file, whatever the child managed to
    say is still on disk.
    """

    def test_it_is_killed_and_what_it_printed_survives(self, conflicted: Conflicted) -> None:
        with (
            mock.patch.object(support, "FALLBACK", ""),
            mock.patch.object(support, "PROMPTED", 5.0),
        ):
            done = conflicted.second.run("sync", keys="")

        assert done.returncode != 0, "the prompt answered a key nobody typed"
        assert ".bashrc: 1 conflict to settle" in done.stdout
        assert "[l] keep local" in done.stdout

    def test_the_precondition_that_the_fallback_is_what_normally_saves_it(
        self, conflicted: Conflicted
    ) -> None:
        """Without this, the test above is equally satisfied by a fixture whose
        prompt never appears at all -- and `support.FALLBACK` would be free to
        stop working with nothing to notice."""
        done = conflicted.second.run("sync", keys="")
        assert done.returncode == 1, done.stdout + done.stderr
        assert "conflict in .bashrc" in done.stdout
