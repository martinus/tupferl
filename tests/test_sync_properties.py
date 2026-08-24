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

#: What a region's middle line can say. Short and without newlines: a generated
#: newline would change how many lines a region has, and the whole construction
#: rests on that count.
TEXT = st.text(
    alphabet=st.characters(blacklist_characters="\n\r\x00", blacklist_categories=("Cs",)),
    max_size=8,
)


def content(middles: list[str]) -> str:
    """The whole file, from the middle line of each region.

    The index appears in every line so that no two regions are textually
    identical; see the module docstring for what goes wrong without it.
    """
    lines = []
    for index, middle in enumerate(middles):
        lines.extend([f"{index}: top", f"{index}: {middle}", f"{index}: bottom"])
    return "".join(line + "\n" for line in lines)


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
        (first.home / MANAGED).write_text(content(self.middles), encoding="utf-8")

        assert first.call("init", str(remote)) == 0
        assert first.call("add", str(first.home / MANAGED)) == 0
        assert first.call("sync") == 0
        # `init` runs a first sync (plan §4), which is what puts the file on the
        # second machine -- so this is also the two-machine setup a user performs.
        assert second.call("init", str(remote)) == 0
        assert (second.home / MANAGED).read_text(encoding="utf-8") == content(self.middles)

    @rule(who=st.integers(0, 1), slot=st.integers(0, 1), text=TEXT)
    def edit(self, who: int, slot: int, text: str) -> None:
        """Rewrite the middle line of one region this machine owns.

        `slot * 2 + who` rather than a free choice of region: ownership is what
        makes every merge decidable and the final content a function of the
        edits rather than of their order.
        """
        region = slot * 2 + who
        self.middles[region] = f"{self.machines[who].name}-{text}"
        (self.machines[who].home / MANAGED).write_text(
            content(self.middles_seen_by(who)), encoding="utf-8"
        )

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
        on_disk = (self.machines[who].home / MANAGED).read_text(encoding="utf-8").split("\n")
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
            want = content(self.middles)
            for machine in self.machines:
                assert (machine.home / MANAGED).read_text(encoding="utf-8") == want, (
                    f"{machine.name} did not converge"
                )
        finally:
            self.box.cleanup()


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
