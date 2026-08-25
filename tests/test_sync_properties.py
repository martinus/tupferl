"""Plan §7.2 properties 3 and 4, written before this milestone's example tests.

Property 3 -- *sync is idempotent* -- is asserted after every sync the machine
performs, not once at the end: a run that changed nothing must leave `$HOME`, the
repository and the snapshots byte-identical and write no commit. Checked as a
fingerprint of every file in both trees, so a snapshot written with different
bytes, a backup directory created for nothing, or an empty commit all fail it.

Property 4 -- *two machines converge* -- is asserted in `teardown`, after syncing
both until nothing more moves. It is stated as **exact equality against a model**
rather than as "the two files match", which a sync that deleted the file on both
machines would also satisfy. The model is possible because of how the fixture is
built, and that is the part to read:

**Each machine owns its own regions of the file, and no two regions are
adjacent.** The file is a list of three-line regions; machine A only ever
rewrites the middle line of regions 0 and 2, machine B only of regions 1 and 3.
Two consequences, both deliberate:

- *No edit ever overlaps another*, so milestone 3 -- which resolves what it can
  and asks nobody -- is enough to converge. Overlapping edits are a true
  conflict, they are milestone 4's subject, and the example tests cover what
  this milestone does with one (reports it, writes nothing).
- *The final content is determined*: each region holds whatever its owner wrote
  last, whatever order the syncs happened in. So the invariant is an equality
  against a value this file computes without reference to how sync works, which
  is what CLAUDE.md §2 asks of an expectation.

Every line also carries its region's index, so no two regions are textually
identical. Without that, git's diff can attribute a change to the wrong region
and two edits meant for different places land on top of each other -- which
would show up as an intermittent conflict and read as a bug in the merge.

The commands run in-process rather than as subprocesses, which is the one place
this project does that. Plan §7.1 prefers a real subprocess "where speed
allows", and here it does not: a sync is 30ms in-process and 70ms out of it, and
this machine runs a dozen per example. git itself is still the real binary, and
the example tests drive the CLI the way a user does.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, rule

from tests import profiles, support

#: How many three-line regions the managed file has. Four, so each machine owns
#: two: one region each could not tell "the owner's latest edit" from "whatever
#: the last machine to sync wrote".
REGIONS = 4

#: The managed file. A real dotfile name, because the repository stores it under
#: exactly this name and a fixture called `f.txt` would not notice a rule about
#: leading dots.
MANAGED = ".bashrc"

#: The generated line and the three-line region shape come from
#: `tests/support.py`, shared with `tests/test_merge_properties.py`. The reason
#: for every excluded character, and for the index in every line, is stated
#: there once.
TEXT = support.line(max_size=8)


def fingerprint(*trees: Path) -> list[tuple[str, str]]:
    """Every file under `trees`, by relative name and content hash.

    Names as well as hashes, so a file that disappeared fails as loudly as one
    whose bytes changed. `.git` is skipped: it holds the commit graph, which a
    second sync legitimately touches -- reflogs and `FETCH_HEAD` move without
    anything the user owns changing -- and property 3 is about what the user
    owns.
    """
    found: list[tuple[str, str]] = []
    for tree in trees:
        for where in sorted(tree.rglob("*")):
            if not where.is_file() or ".git" in where.relative_to(tree).parts:
                continue
            digest = hashlib.sha256(where.read_bytes()).hexdigest()
            found.append((f"{tree.name}/{where.relative_to(tree)}", digest))
    return found


def state(machine: support.Computer) -> list[tuple[str, str]]:
    """What a sync must not change when it has nothing to do."""
    return fingerprint(machine.home, machine.repo)


def commits_in(machine: support.Computer) -> int:
    return len(machine.git("log", "--format=%H").splitlines())


class SyncMachine(RuleBasedStateMachine):
    """Two computers, one remote, and a model of what the file should hold."""

    def __init__(self) -> None:
        super().__init__()
        self.box = tempfile.TemporaryDirectory(prefix="tupferl-stateful-")
        root = Path(self.box.name)
        self.machines = [support.Computer(root, name) for name in ("machine-a", "machine-b")]
        first, second = self.machines
        remote = support.make_remote(root / "remote.git", first.env)

        #: The model: what each region's middle line should say. The initial file
        #: is written before anything is managed, so both machines start from it.
        self.middles = [f"start-{index}" for index in range(REGIONS)]
        first.write(MANAGED, support.regions(self.middles))

        assert first.call("init", str(remote)) == 0
        assert first.call("add", str(first.home / MANAGED)) == 0
        assert first.call("sync") == 0
        # `init` runs a first sync (plan §4), which is what puts the file on the
        # second machine -- so this is also the two-machine setup a user performs.
        assert second.call("init", str(remote)) == 0
        assert second.read(MANAGED) == support.regions(self.middles)

    @rule(who=st.integers(0, 1), slot=st.integers(0, 1), text=TEXT)
    def edit(self, who: int, slot: int, text: str) -> None:
        """Rewrite the middle line of one region this machine owns.

        `slot * 2 + who` rather than a free choice of region: ownership is what
        makes every merge decidable and the final content a function of the
        edits rather than of their order.
        """
        region = slot * 2 + who
        self.middles[region] = f"{self.machines[who].name}-{text}"
        self.machines[who].write(MANAGED, support.regions(self.middles_seen_by(who)))

    def middles_seen_by(self, who: int) -> list[str]:
        """What this machine's copy should say after an edit to one of its own
        regions: the model for its own regions, and whatever is on disk for the
        other machine's -- which it may not have synced yet.

        `split("\n")` and not `splitlines()`. Python splits on far more than a
        newline -- `\x0b \x0c \x1c \x1d \x1e \x85 \u2028 \u2029` as well -- so a
        generated line containing one of those became two lines here, every index
        below shifted, and the model disagreed with a file that was correct.
        This machine found it on its second run, with `text='m\x882\x1d'`. git
        splits on `\n` alone, which is why the file itself was never wrong.
        """
        on_disk = self.machines[who].read(MANAGED).split("\n")
        middles = []
        for index in range(REGIONS):
            mine = index % 2 == who
            middles.append(
                self.middles[index] if mine else on_disk[index * 3 + 1].split(": ", 1)[1]
            )
        return middles

    @rule(who=st.integers(0, 1))
    def sync(self, who: int) -> None:
        """Sync, then sync again and insist the second one did nothing.

        Property 3, asserted at every step rather than once: idempotence that
        only holds from a quiescent state is not the property, and the states
        this machine reaches are the ones a user reaches.
        """
        machine = self.machines[who]
        assert machine.call("sync") == 0
        before, commits = state(machine), commits_in(machine)
        assert machine.call("sync") == 0
        assert state(machine) == before, "a second sync changed something"
        assert commits_in(machine) == commits, "a second sync wrote a commit"

    def teardown(self) -> None:
        """Property 4: sync until nothing moves, then both files equal the model.

        Four syncs, alternating: one to push each machine's own edits, and one
        more each to take in what the other pushed. `assert` rather than
        `unittest`'s methods because a `RuleBasedStateMachine` is not a
        `TestCase`; Hypothesis reports the failing sequence either way.
        """
        try:
            for who in (0, 1, 0, 1):
                assert self.machines[who].call("sync") == 0
            want = support.regions(self.middles)
            for machine in self.machines:
                assert machine.read(MANAGED) == want, f"{machine.name} did not converge"
        finally:
            support.discard(self.box)


SyncMachine.TestCase.settings = settings(
    max_examples=profiles.STATEFUL,
    stateful_step_count=profiles.STEPS,
    deadline=None,
)

#: Named so `unittest` discovery finds it. The class Hypothesis generates is the
#: test; this is the whole of the plumbing.
#:
#: The three dunders are not decoration. Hypothesis builds `TestCase` inside its
#: own module, so the test's id is `hypothesis.stateful.SyncMachine.TestCase...`
#: -- and `tools/run_tests.py` shards by re-loading ids, which then fails to
#: import. Its accounting check caught it ("1 discovered tests never ran"), which
#: is the failure mode that check exists for: a serial run passed.
TestSyncIsIdempotentAndConverges = SyncMachine.TestCase
TestSyncIsIdempotentAndConverges.__module__ = __name__
TestSyncIsIdempotentAndConverges.__name__ = "TestSyncIsIdempotentAndConverges"
TestSyncIsIdempotentAndConverges.__qualname__ = "TestSyncIsIdempotentAndConverges"


#: The lines after the first, which no rule below ever touches. They exist so
#: that a file settled with `[b]` still has something in common with a file
#: settled with `[l]`, and so "the chosen line is present" is a claim about a
#: *line* rather than about the whole file.
TAIL = "unchanged-one\nunchanged-two\nunchanged-three\n"

#: The three answers this machine gives. `[s]` is excluded because skipping
#: chooses nothing, and `[e]` because it needs an editor -- both are example
#: tests in `tests/test_conflict_cli.py`. What is left is exactly the set of
#: answers plan §7.2's property 5 is about: a choice that writes.
CHOICES = ("l", "r", "b")


class ChoiceMachine(RuleBasedStateMachine):
    """Plan §7.2 property 5: nothing chosen at the prompt is silently lost.

    Where `SyncMachine` above gives each computer its own regions so that every
    edit merges, this one aims both computers at the **same line**, so that two
    edits are always a real conflict and the prompt is always what settles it.

    The property is stated as *presence of a line*, not as equality with a model,
    and that is deliberate: `[b]` keeps both sides, so the settled file is not
    either computer's and there is no third version to compare it to. What can be
    said exactly is which lines must have survived:

    - `[l]` keeps the line belonging to the computer that answered;
    - `[r]` keeps the other computer's;
    - `[b]` keeps both.

    Machine order is fixed -- `machine-a` syncs first, so `machine-b` is always
    the one that meets the conflict -- because otherwise "the computer that
    answered" is not a thing the test knows, and the three rows above become one
    row saying "one of them".

    Every written line carries a counter as well as the generated text. Without
    it Hypothesis can generate the same text twice, the second edit writes bytes
    that are already there, and the run that was meant to produce a conflict
    quietly produces nothing to settle -- a fixture too weak to tell the two
    answers apart, and invisible in the test's own text.
    """

    def __init__(self) -> None:
        super().__init__()
        self.box = tempfile.TemporaryDirectory(prefix="tupferl-choices-")
        root = Path(self.box.name)
        self.machines = [support.Computer(root, name) for name in ("machine-a", "machine-b")]
        first, second = self.machines
        remote = support.make_remote(root / "remote.git", first.env)

        self.step = 0
        #: The line each computer last wrote and has not yet had settled. Cleared
        #: after every settle, because a line the user overwrote afterwards is not
        #: one the tool promised to keep.
        self.wrote: dict[int, str] = {}

        first.write(MANAGED, f"start\n{TAIL}")
        assert first.call("init", str(remote)) == 0
        assert first.call("add", str(first.home / MANAGED)) == 0
        assert first.call("sync") == 0
        assert second.call("init", str(remote)) == 0

    @rule(who=st.integers(0, 1), text=TEXT)
    def edit(self, who: int, text: str) -> None:
        """Rewrite the first line -- the one line both computers write."""
        self.step += 1
        line = f"{self.machines[who].name}-{self.step}-{text}"
        self.machines[who].write(MANAGED, f"{line}\n{TAIL}")
        self.wrote[who] = line

    @rule(choice=st.sampled_from(CHOICES))
    def settle(self, choice: str) -> None:
        """Sync both computers, answering every conflict with `choice`.

        Three syncs: `machine-a` pushes what it has, `machine-b` meets the
        conflict and answers and pushes, and `machine-a` takes it back. A fourth
        on `machine-b` was there and was a no-op in every branch -- measured, 10
        examples of 8 steps, derandomised: 10.11s with it and 7.55s without.

        One key is typed at each. There is one managed file, so at most one
        question, and a key nothing asks for is simply never read.
        """
        for who in (0, 1, 0):
            assert self.machines[who].call("sync", keys=choice) == 0

        texts = [machine.read(MANAGED) for machine in self.machines]
        assert texts[0] == texts[1], "the two computers did not converge"
        assert "<<<<<<<" not in texts[0], "a conflict marker reached a managed file"

        for line in self.kept(choice):
            for machine, text in zip(self.machines, texts, strict=True):
                assert line in text, f"{line!r} was chosen but is not on {machine.name}"
        self.wrote.clear()

    def kept(self, choice: str) -> list[str]:
        """Which of the written lines the answer promised to keep.

        With only one computer having edited there was no conflict and no
        question, so its line stands whatever `choice` says -- which is why this
        is not simply a lookup on `choice`.
        """
        if len(self.wrote) < 2:
            return list(self.wrote.values())
        if choice == "b":
            return [self.wrote[0], self.wrote[1]]
        # `machine-b` answers, so `[l]` is its own line and `[r]` is the other's.
        return [self.wrote[1]] if choice == "l" else [self.wrote[0]]

    def teardown(self) -> None:
        support.discard(self.box)


ChoiceMachine.TestCase.settings = settings(
    max_examples=profiles.STATEFUL,
    stateful_step_count=profiles.STEPS,
    deadline=None,
)

#: Renamed for discovery, for the reason spelled out above `SyncMachine`'s own.
TestAChoiceIsNeverSilentlyLost = ChoiceMachine.TestCase
TestAChoiceIsNeverSilentlyLost.__module__ = __name__
TestAChoiceIsNeverSilentlyLost.__name__ = "TestAChoiceIsNeverSilentlyLost"
TestAChoiceIsNeverSilentlyLost.__qualname__ = "TestAChoiceIsNeverSilentlyLost"
