"""Host overlays end to end -- plan §3.3, and milestone 5 of `docs/plan.md`.

Plan §7.4 item 3 asks for two things by name, "replacement wins" and "add/remove
with `--host`", and both need two machines to mean anything: an overlay that
silently applied everywhere passes every single-machine test there is.

**`overlaid`, which two thirds of this module takes, holds a shared copy
*and* an overlay for the same name and asserts they differ before it asserts
anything else.** (`TestAnOverlayWithNoSharedCopy` is deliberately the other arm,
and says so in its name; `TestTwoHostsOverrideTheSameFile` holds both and checks
the shared one inside a test rather than in its fixture.) That is not ceremony.
Until this module, *no test that drove a real sync had both* -- the end-to-end
overlay test (`test_sync_cli.TestTwoMachines.
test_a_host_overlay_is_what_syncs_on_that_host`) adds `--host` to a file that
was never shared, so the repository holds one version of the name and "the
overlay won" is unobservable there.

Measured, with `tools/mutate.py`, by inverting `manifest.managed`'s merge so the
shared file overwrites the overlay instead of the other way round:

| run against | verdict |
|---|---|
| every test that drives a real sync (below) | **SURVIVED** |
| `test_manage.TestTwoMachines` | caught |
| this module | caught |

-- where "every test that drives a real sync" is `test_sync`, `test_sync_cli`,
`test_sync_conflicts`, `test_sync_commits` and `test_sync_properties`.

So the rule was asserted -- by `test_manifest` on `manifest.managed` with no
repository at all, and by `test_manage` through what `tupferl list` prints --
and its *consequence for the file in `$HOME`* was not. Those are the two
different questions plan §7.4.3 and §3.3 respectively ask.

`tests/test_manage.py` covers the listing side against a repository built by
hand -- `(host)` in `add`'s output, the counts, one host not seeing another's
overlay. The one test here that also looks at `list`
(`test_neither_overlay_is_offered_to_the_other_machine_as_managed`) is the
stronger of the pair, because the foreign overlay arrives through a real `sync`
rather than being planted; the hand-planted version is left alone as out of
scope. Everything else here is what happens on disk during a `sync`, and what
`remove --host` does.
"""

from __future__ import annotations

import shutil

import pytest

from tests import support
from tupferl import paths

#: The three versions of `.bashrc` this module works with. Distinct on every
#: line that matters, so an assertion that the wrong one arrived cannot pass by
#: resembling the right one.
#:
#: `SHARED` is what the template synced, so it is aliased rather than written
#: out again -- a second copy is free to drift from the tree these tests are
#: handed, and the drift would arrive as a diff nobody asked for rather than as
#: a failure naming the constant. `test_diff` and `test_status` say the same.
SHARED = support.STARTS_AS
OVERLAY = "one\nTWO on machine-b only\nthree\nfour\nfive\n"
RESHARED = "one\ntwo\nthree\nfour\nFIVE edited on machine-a\n"


@pytest.fixture
def overlaid(two_machines: support.TwoMachines) -> support.TwoMachines:
    """`.bashrc` shared by both machines, and overridden on `machine-b`.

    `support.two_machines` leaves it managed and synced from `machine-a` holding
    `SHARED`; this brings the second machine up and has it adopt an overlay.
    The state every test taking it starts from is the one the weak fixture
    cannot reach: **both** copies exist and they differ.
    """
    box = two_machines
    assert box.second.call("init", str(box.remote)) == 0
    box.second.write(".bashrc", OVERLAY)
    assert box.second.call("add", "--host", str(box.second.home / ".bashrc")) == 0
    assert box.second.call("sync") == 0
    # The precondition, asserted rather than assumed. Without the shared copy
    # every test in this file is about a repository with one version of the
    # file in it, and "the overlay won" is then true of any implementation.
    assert box.second.stored(".bashrc").read_text(encoding="utf-8") == SHARED
    assert box.second.stored(".bashrc", host=True).read_text(encoding="utf-8") == OVERLAY
    return box


@pytest.mark.usefixtures("overlaid")
class TestReplacementWins:
    """Plan §3.3: "a file in `hosts/<hostname>/` replaces the shared file on that
    host". Three directions, because the rule has to hold in each."""

    def test_the_shared_copy_does_not_replace_the_overlay_in_this_hosts_home(
        self, overlaid: support.TwoMachines
    ) -> None:
        """Named for what it can see. `$HOME` already holds `OVERLAY` -- the
        fixture wrote it there before `add --host` -- so a `sync` that never
        wrote `$HOME` at all would leave this green; measured, gating off
        `sync.apply`'s write is caught by the reinstall test and by
        `test_the_next_sync_restores_the_shared_version_without_asking`, not
        here. What it does catch is the shared copy arriving instead, which is
        the direction this class is about."""
        assert overlaid.second.read(".bashrc") == OVERLAY

    def test_an_edit_on_this_host_goes_into_the_overlay_and_not_the_shared_copy(
        self, overlaid: support.TwoMachines
    ) -> None:
        """The direction that would be invisible with only one copy in the
        repository: `sync` must write up into the overlay, leaving the shared
        file exactly as the other machine pushed it."""
        mine = OVERLAY.replace("only", "only, edited")
        overlaid.second.write(".bashrc", mine)
        assert overlaid.second.call("sync") == 0
        assert overlaid.second.stored(".bashrc", host=True).read_text(encoding="utf-8") == mine
        assert overlaid.second.stored(".bashrc").read_text(encoding="utf-8") == SHARED

    def test_a_shared_edit_from_elsewhere_does_not_reach_a_host_that_overrides_it(
        self, overlaid: support.TwoMachines
    ) -> None:
        """`machine-a` edits the shared file and pushes; `machine-b` keeps its own.

        The second assertion is what stops this passing vacuously: it checks
        that `machine-b` really *received* the new shared version, so "the
        overlay survived" is about a choice rather than about a fetch that never
        happened.
        """
        overlaid.first.write(".bashrc", RESHARED)
        assert overlaid.first.call("sync") == 0
        assert overlaid.second.call("sync") == 0

        assert overlaid.second.read(".bashrc") == OVERLAY
        assert overlaid.second.stored(".bashrc").read_text(encoding="utf-8") == RESHARED

    def test_a_reinstalled_machine_gets_its_overlay_back(
        self, overlaid: support.TwoMachines
    ) -> None:
        """The overlay is committed and pushed like everything else, which is
        what makes it survive the machine it belongs to. Asserted because the
        README now promises it, and because it is the one direction where an
        overlay behaving like a purely local setting would look fine until the
        day it mattered.

        `$HOME` is deleted whole, and the repository lives under it
        (`XDG_DATA_HOME` is `$HOME/.local/share`), so this really is a machine
        with nothing but its hostname and the remote URL.
        """
        shutil.rmtree(overlaid.second.home)
        # Rebuilt through `Computer` rather than by repeating its first three
        # statements here: a machine brought up any other way stops resembling
        # the ones the rest of the suite uses the moment that constructor grows
        # a fourth step, and nothing in this test's text would show it. A local
        # name rather than an assignment back into the fixture, which is frozen.
        rebuilt = support.Computer(overlaid.tmp, overlaid.second.name)

        assert rebuilt.call("init", str(overlaid.remote)) == 0
        assert rebuilt.read(".bashrc") == OVERLAY

    def test_the_other_machine_is_unaffected_by_the_overlay(
        self, overlaid: support.TwoMachines
    ) -> None:
        """The overlay is committed and pushed, so `machine-a` has the bytes in
        its repository. It must still be using the shared version."""
        assert overlaid.first.call("sync") == 0
        assert overlaid.first.read(".bashrc") == SHARED
        # Asked of `paths`, not spelled out. This is the only assertion in the
        # file about the *other* machine's copy of an overlay, so it is the only
        # guard that overlays are pushed at all -- and a literal `.tupferl/hosts`
        # would go red pointing at the wrong thing if the layout ever moved,
        # with retyping the literal as the obvious repair.
        theirs = paths.host_overlay(overlaid.first.repo, overlaid.second.name) / ".bashrc"
        assert theirs.is_file(), "machine-b's overlay never reached machine-a"


@pytest.mark.usefixtures("overlaid")
class TestRemoveHost:
    """`tupferl remove --host`: stop overriding here, keep managing everywhere."""

    def test_it_removes_the_overlay_and_keeps_the_shared_copy(
        self, overlaid: support.TwoMachines
    ) -> None:
        done = overlaid.second.run("remove", "--host", str(overlaid.second.home / ".bashrc"))
        assert done.returncode == 0, done.stdout + done.stderr
        assert not overlaid.second.stored(".bashrc", host=True).exists()
        assert overlaid.second.stored(".bashrc").read_text(encoding="utf-8") == SHARED

    def test_the_next_sync_restores_the_shared_version_without_asking(
        self, overlaid: support.TwoMachines
    ) -> None:
        """The whole point of the command, and the assertion that "0 in conflict"
        is doing the work: `$HOME` still holds the overlay's bytes when the sync
        starts, so a run with no merge base would merge two different files and
        report a conflict instead of a copy."""
        assert overlaid.second.call("remove", "--host", str(overlaid.second.home / ".bashrc")) == 0
        done = overlaid.second.run("sync")
        assert done.returncode == 0, done.stdout + done.stderr
        assert overlaid.second.read(".bashrc") == SHARED
        assert "0 in conflict" in done.stdout

    def test_what_the_overlay_put_in_home_is_backed_up_before_it_is_replaced(
        self, overlaid: support.TwoMachines
    ) -> None:
        """Plan §5's backup, on the one path where this command destroys
        something: the user's overridden file is overwritten by the shared one,
        and these are the bytes they would want back."""
        assert overlaid.second.call("remove", "--host", str(overlaid.second.home / ".bashrc")) == 0
        assert overlaid.second.call("sync") == 0
        saved = [found for found in overlaid.second.backups.rglob(".bashrc") if found.is_file()]
        assert len(saved) == 1, f"expected one backup, found {saved}"
        assert saved[0].read_text(encoding="utf-8") == OVERLAY

    def test_it_leaves_the_snapshot_alone(self, overlaid: support.TwoMachines) -> None:
        """A guard rather than a symptom: nothing deletes the snapshot today, and
        this is here so that nothing starts.

        `manage.remove`'s docstring is the argument -- the snapshot is what makes
        the sync above a copy rather than a merge -- and the test above would go
        red if it were deleted. This one names the mechanism, so the failure
        arrives as "the snapshot was removed" rather than as "a conflict
        appeared", which is two inferences further from the cause.
        """
        assert overlaid.second.call("remove", "--host", str(overlaid.second.home / ".bashrc")) == 0
        assert overlaid.second.snapshot(".bashrc").read_text(encoding="utf-8") == OVERLAY

    def test_an_unsynced_edit_is_merged_rather_than_replaced(
        self, overlaid: support.TwoMachines
    ) -> None:
        """The case `said`'s docstring names as the approximation in "will
        replace": with an edit pending, the next sync merges instead.

        The edit is on the last line and the override was on the second, so the
        two do not overlap and git resolves it. Both survive: the shared body
        comes back *and* the edit is still there, which is the point -- "will
        replace" would have been a promise to discard it.
        """
        assert overlaid.second.call("remove", "--host", str(overlaid.second.home / ".bashrc")) == 0
        overlaid.second.write(".bashrc", OVERLAY.replace("five", "FIVE edited after the removal"))

        done = overlaid.second.run("sync")
        assert done.returncode == 0, done.stdout + done.stderr
        assert "0 in conflict" in done.stdout
        assert overlaid.second.read(".bashrc") == SHARED.replace(
            "five", "FIVE edited after the removal"
        )

    def test_it_says_the_shared_version_is_coming(self, overlaid: support.TwoMachines) -> None:
        """The user has just been told a file was removed, and the file in `$HOME`
        is about to change under them. This sentence is the only warning."""
        done = overlaid.second.run("remove", "--host", str(overlaid.second.home / ".bashrc"))
        assert "the shared version will replace" in done.stdout

    def test_the_commit_message_distinguishes_it_from_a_full_remove(
        self, overlaid: support.TwoMachines
    ) -> None:
        """`git log` in the repository is the record of what the user asked for
        (see `manage`'s docstring). "remove" and "remove this host's override"
        are different requests and must not read the same."""
        assert overlaid.second.call("remove", "--host", str(overlaid.second.home / ".bashrc")) == 0
        assert (
            overlaid.second.git("log", "-1", "--pretty=%s")
            == "remove overlay from machine-b: .bashrc"
        )

    def test_a_file_with_no_overlay_here_is_refused_by_name(
        self, overlaid: support.TwoMachines
    ) -> None:
        """`machine-a` manages `.bashrc` and has no overlay for it. The message
        has to say *that*, not "not managed", which is false and would send the
        user looking for the wrong thing."""
        done = overlaid.first.run("remove", "--host", str(overlaid.first.home / ".bashrc"))
        assert done.returncode == 2
        assert "is not in machine-a's overlay" in done.stderr

    def test_without_the_flag_it_still_removes_both(self, overlaid: support.TwoMachines) -> None:
        """The default did not change. Plan §4: `remove` stops managing the file,
        and leaving the shared copy behind would mean it kept syncing."""
        assert overlaid.second.call("remove", str(overlaid.second.home / ".bashrc")) == 0
        assert not overlaid.second.stored(".bashrc", host=True).exists()
        assert not overlaid.second.stored(".bashrc").exists()
        assert (overlaid.second.home / ".bashrc").is_file()


@pytest.fixture
def only_an_overlay(two_machines: support.TwoMachines) -> support.TwoMachines:
    """`.vimrc` overridden on `machine-a` and never shared."""
    box = two_machines
    box.first.write(".vimrc", "set number\n")
    # No sync here: `add` writes *and commits* the overlay and the snapshot,
    # so neither test taking this reads anything a sync would leave behind, and
    # neither asserts about the remote. Measured, interleaved, two runs
    # each: 0.272s per build with it, 0.217s without -- ~55ms x 2 tests,
    # paid again for every mutant in a sweep.
    assert box.first.call("add", "--host", str(box.first.home / ".vimrc")) == 0
    assert not box.first.stored(".vimrc").exists(), "the fixture stored it as shared"
    return box


@pytest.mark.usefixtures("only_an_overlay")
class TestAnOverlayWithNoSharedCopy:
    """`add --host` for a file that was never shared -- the other arm of every
    branch in `remove --host`, and the case where dropping the overlay stops the
    file being managed on this machine at all."""

    def test_it_says_nothing_else_manages_the_file(
        self, only_an_overlay: support.TwoMachines
    ) -> None:
        done = only_an_overlay.first.run(
            "remove", "--host", str(only_an_overlay.first.home / ".vimrc")
        )
        assert done.returncode == 0, done.stdout + done.stderr
        assert "nothing else manages it here" in done.stdout
        assert "was not touched" in done.stdout

    def test_the_file_stays_in_home_and_the_next_sync_prunes_the_snapshot(
        self, only_an_overlay: support.TwoMachines
    ) -> None:
        """`sync.stale` does the pruning, and it only sees the file because
        nothing manages it any more. A snapshot left behind is committed, and
        becomes the merge base if the name ever comes back."""
        # The precondition for the last line. "No snapshot afterwards" is
        # equally satisfied by "there was never a snapshot", and `add --host`
        # writing one is a fact about a different function.
        assert only_an_overlay.first.snapshot(".vimrc").is_file()

        assert (
            only_an_overlay.first.call(
                "remove", "--host", str(only_an_overlay.first.home / ".vimrc")
            )
            == 0
        )
        assert only_an_overlay.first.call("sync") == 0
        assert only_an_overlay.first.read(".vimrc") == "set number\n"
        assert not only_an_overlay.first.snapshot(".vimrc").exists()


@pytest.fixture
def both_override(two_machines: support.TwoMachines) -> support.TwoMachines:
    """Both machines with their own overlay for `.bashrc`, both pushed."""
    box = two_machines
    assert box.second.call("init", str(box.remote)) == 0
    for machine, body in ((box.first, "A only\n"), (box.second, "B only\n")):
        machine.write(".bashrc", body)
        assert machine.call("add", "--host", str(machine.home / ".bashrc")) == 0
        assert machine.call("sync") == 0
    return box


@pytest.mark.usefixtures("both_override")
class TestTwoHostsOverrideTheSameFile:
    """Both machines override `.bashrc`, which is plan §3.3's motivating case --
    a different git email at work and at home, one shared file underneath."""

    def test_each_keeps_its_own_and_the_shared_copy_survives_both(
        self, both_override: support.TwoMachines
    ) -> None:
        # One each, which is what it takes: `both_override` already left both
        # overlays on the remote, and this is `machine-a` taking `machine-b`'s in.
        # Measured with `rev-parse HEAD` around each call -- of four syncs only
        # the first moved anything, so the extra pair claimed a "chance to be
        # wrong" the fixture never gave them.
        for machine in (both_override.first, both_override.second):
            assert machine.call("sync") == 0
        assert both_override.first.read(".bashrc") == "A only\n"
        assert both_override.second.read(".bashrc") == "B only\n"
        assert both_override.first.stored(".bashrc").read_text(encoding="utf-8") == SHARED

    def test_a_plain_remove_here_leaves_the_other_machines_override_alone(
        self, both_override: support.TwoMachines
    ) -> None:
        """`remove` touches the shared tree and the overlay of the host it runs
        on, and nothing else. The README says so, which is why this is asserted:
        a `remove` that reached into `hosts/*/` would silently unmanage a file on
        a machine whose owner never asked for it, and the only place that would
        show up is the next sync on that machine.
        """
        assert both_override.second.call("remove", str(both_override.second.home / ".bashrc")) == 0
        assert not both_override.second.stored(".bashrc").exists(), "the shared copy survived"
        assert not both_override.second.stored(".bashrc", host=True).exists()
        theirs = paths.host_overlay(both_override.second.repo, both_override.first.name) / ".bashrc"
        assert theirs.is_file(), "machine-a's override was taken too"

        # And `machine-a` still *manages* it after taking the removal in.
        # Reading `$HOME` here would prove nothing: if its overlay had been
        # deleted too the file would simply be unmanaged, and `$HOME` is left
        # untouched either way -- the same bytes for opposite reasons.
        assert both_override.second.call("sync") == 0
        assert both_override.first.call("sync") == 0
        done = both_override.first.run("status", "--all")
        assert "host  .bashrc" in done.stdout
        assert (
            "1 file managed, 0 to change, 0 in conflict, 1 from this host's overlay" in done.stdout
        )

    def test_neither_overlay_is_offered_to_the_other_machine_as_managed(
        self, both_override: support.TwoMachines
    ) -> None:
        """`machine-a`'s repository holds `machine-b`'s overlay after the sync.
        `manifest.managed` must not return it, or `machine-a` would write
        `machine-b`'s file into its own `$HOME` under the same name."""
        assert both_override.first.call("sync") == 0
        done = both_override.first.run("status", "--all")
        assert done.stdout.count(".bashrc") == 1, done.stdout
        assert (
            "1 file managed, 0 to change, 0 in conflict, 1 from this host's overlay" in done.stdout
        )
