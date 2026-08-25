"""Host overlays end to end -- plan §3.3, and milestone 5 of `docs/plan.md`.

Plan §7.4 item 3 asks for two things by name, "replacement wins" and "add/remove
with `--host`", and both need two machines to mean anything: an overlay that
silently applied everywhere passes every single-machine test there is.

**Every fixture here holds a shared copy *and* an overlay for the same name, and
asserts they differ before it asserts anything else.** That is not ceremony.
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

from tests import support
from tupferl import paths

#: The three versions of `.bashrc` this module works with. Distinct on every
#: line that matters, so an assertion that the wrong one arrived cannot pass by
#: resembling the right one.
SHARED = "one\ntwo\nthree\nfour\nfive\n"
OVERLAY = "one\nTWO on machine-b only\nthree\nfour\nfive\n"
RESHARED = "one\ntwo\nthree\nfour\nFIVE edited on machine-a\n"


class OverlaidOnB(support.TwoMachines):
    """`.bashrc` shared by both machines, and overridden on `machine-b`.

    `support.TwoMachines` leaves it managed and synced from `machine-a` holding
    `SHARED`; this brings the second machine up and has it adopt an overlay.
    The state every test below starts from is the one the weak fixture cannot
    reach: **both** copies exist and they differ.
    """

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(0, self.second.call("init", str(self.remote)))
        self.second.write(".bashrc", OVERLAY)
        self.assertEqual(0, self.second.call("add", "--host", str(self.second.home / ".bashrc")))
        self.assertEqual(0, self.second.call("sync"))
        # The precondition, asserted rather than assumed. Without the shared copy
        # every test in this file is about a repository with one version of the
        # file in it, and "the overlay won" is then true of any implementation.
        self.assertEqual(SHARED, self.second.stored(".bashrc").read_text(encoding="utf-8"))
        self.assertEqual(
            OVERLAY, self.second.stored(".bashrc", host=True).read_text(encoding="utf-8")
        )


class TestReplacementWins(OverlaidOnB):
    """Plan §3.3: "a file in `hosts/<hostname>/` replaces the shared file on that
    host". Three directions, because the rule has to hold in each."""

    def test_the_overlay_is_what_reaches_this_hosts_home(self) -> None:
        self.assertEqual(OVERLAY, self.second.read(".bashrc"))

    def test_an_edit_on_this_host_goes_into_the_overlay_and_not_the_shared_copy(self) -> None:
        """The direction that would be invisible with only one copy in the
        repository: `sync` must write up into the overlay, leaving the shared
        file exactly as the other machine pushed it."""
        self.second.write(".bashrc", OVERLAY.replace("only", "only, edited"))
        self.assertEqual(0, self.second.call("sync"))
        self.assertIn(
            "edited", self.second.stored(".bashrc", host=True).read_text(encoding="utf-8")
        )
        self.assertEqual(SHARED, self.second.stored(".bashrc").read_text(encoding="utf-8"))

    def test_a_shared_edit_from_elsewhere_does_not_reach_a_host_that_overrides_it(self) -> None:
        """`machine-a` edits the shared file and pushes; `machine-b` keeps its own.

        The second assertion is what stops this passing vacuously: it checks
        that `machine-b` really *received* the new shared version, so "the
        overlay survived" is about a choice rather than about a fetch that never
        happened.
        """
        self.first.write(".bashrc", RESHARED)
        self.assertEqual(0, self.first.call("sync"))
        self.assertEqual(0, self.second.call("sync"))

        self.assertEqual(OVERLAY, self.second.read(".bashrc"))
        self.assertEqual(RESHARED, self.second.stored(".bashrc").read_text(encoding="utf-8"))

    def test_a_reinstalled_machine_gets_its_overlay_back(self) -> None:
        """The overlay is committed and pushed like everything else, which is
        what makes it survive the machine it belongs to. Asserted because the
        README now promises it, and because it is the one direction where an
        overlay behaving like a purely local setting would look fine until the
        day it mattered.

        `$HOME` is deleted whole, and the repository lives under it
        (`XDG_DATA_HOME` is `$HOME/.local/share`), so this really is a machine
        with nothing but its hostname and the remote URL.
        """
        shutil.rmtree(self.second.home)
        # Rebuilt through `Computer` rather than by repeating its first three
        # statements here: a machine brought up any other way stops resembling
        # the ones the rest of the suite uses the moment that constructor grows
        # a fourth step, and nothing in this test's text would show it.
        self.second = support.Computer(self.tmp, self.second.name)

        self.assertEqual(0, self.second.call("init", str(self.remote)))
        self.assertEqual(OVERLAY, self.second.read(".bashrc"))

    def test_the_other_machine_is_unaffected_by_the_overlay(self) -> None:
        """The overlay is committed and pushed, so `machine-a` has the bytes in
        its repository. It must still be using the shared version."""
        self.assertEqual(0, self.first.call("sync"))
        self.assertEqual(SHARED, self.first.read(".bashrc"))
        # Asked of `paths`, not spelled out. This is the only assertion in the
        # file about the *other* machine's copy of an overlay, so it is the only
        # guard that overlays are pushed at all -- and a literal `.tupferl/hosts`
        # would go red pointing at the wrong thing if the layout ever moved,
        # with retyping the literal as the obvious repair.
        theirs = paths.host_overlay(self.first.repo, self.second.name) / ".bashrc"
        self.assertTrue(theirs.is_file(), "machine-b's overlay never reached machine-a")


class TestRemoveHost(OverlaidOnB):
    """`tupferl remove --host`: stop overriding here, keep managing everywhere."""

    def test_it_removes_the_overlay_and_keeps_the_shared_copy(self) -> None:
        done = self.second.run("remove", "--host", str(self.second.home / ".bashrc"))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertFalse(self.second.stored(".bashrc", host=True).exists())
        self.assertEqual(SHARED, self.second.stored(".bashrc").read_text(encoding="utf-8"))

    def test_the_next_sync_restores_the_shared_version_without_asking(self) -> None:
        """The whole point of the command, and the assertion that "0 in conflict"
        is doing the work: `$HOME` still holds the overlay's bytes when the sync
        starts, so a run with no merge base would merge two different files and
        report a conflict instead of a copy."""
        self.assertEqual(0, self.second.call("remove", "--host", str(self.second.home / ".bashrc")))
        done = self.second.run("sync")
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(SHARED, self.second.read(".bashrc"))
        self.assertIn("0 in conflict", done.stdout)

    def test_what_the_overlay_put_in_home_is_backed_up_before_it_is_replaced(self) -> None:
        """Plan §5's backup, on the one path where this command destroys
        something: the user's overridden file is overwritten by the shared one,
        and these are the bytes they would want back."""
        self.assertEqual(0, self.second.call("remove", "--host", str(self.second.home / ".bashrc")))
        self.assertEqual(0, self.second.call("sync"))
        saved = [found for found in self.second.backups.rglob(".bashrc") if found.is_file()]
        self.assertEqual(1, len(saved), f"expected one backup, found {saved}")
        self.assertEqual(OVERLAY, saved[0].read_text(encoding="utf-8"))

    def test_it_leaves_the_snapshot_alone(self) -> None:
        """A guard rather than a symptom: nothing deletes the snapshot today, and
        this is here so that nothing starts.

        `manage.remove`'s docstring is the argument -- the snapshot is what makes
        the sync above a copy rather than a merge -- and the test above would go
        red if it were deleted. This one names the mechanism, so the failure
        arrives as "the snapshot was removed" rather than as "a conflict
        appeared", which is two inferences further from the cause.
        """
        self.assertEqual(0, self.second.call("remove", "--host", str(self.second.home / ".bashrc")))
        self.assertEqual(OVERLAY, self.second.snapshot(".bashrc").read_text(encoding="utf-8"))

    def test_it_says_the_shared_version_is_coming(self) -> None:
        """The user has just been told a file was removed, and the file in `$HOME`
        is about to change under them. This sentence is the only warning."""
        done = self.second.run("remove", "--host", str(self.second.home / ".bashrc"))
        self.assertIn("the shared version will replace", done.stdout)

    def test_the_commit_message_distinguishes_it_from_a_full_remove(self) -> None:
        """`git log` in the repository is the record of what the user asked for
        (see `manage`'s docstring). "remove" and "remove this host's override"
        are different requests and must not read the same."""
        self.assertEqual(0, self.second.call("remove", "--host", str(self.second.home / ".bashrc")))
        self.assertEqual(
            "remove overlay from machine-b: .bashrc", self.second.git("log", "-1", "--pretty=%s")
        )

    def test_a_file_with_no_overlay_here_is_refused_by_name(self) -> None:
        """`machine-a` manages `.bashrc` and has no overlay for it. The message
        has to say *that*, not "not managed", which is false and would send the
        user looking for the wrong thing."""
        done = self.first.run("remove", "--host", str(self.first.home / ".bashrc"))
        self.assertEqual(2, done.returncode)
        self.assertIn("is not in machine-a's overlay", done.stderr)

    def test_without_the_flag_it_still_removes_both(self) -> None:
        """The default did not change. Plan §4: `remove` stops managing the file,
        and leaving the shared copy behind would mean it kept syncing."""
        self.assertEqual(0, self.second.call("remove", str(self.second.home / ".bashrc")))
        self.assertFalse(self.second.stored(".bashrc", host=True).exists())
        self.assertFalse(self.second.stored(".bashrc").exists())
        self.assertTrue((self.second.home / ".bashrc").is_file())


class TestAnOverlayWithNoSharedCopy(support.TwoMachines):
    """`add --host` for a file that was never shared -- the other arm of every
    branch in `remove --host`, and the case where dropping the overlay stops the
    file being managed on this machine at all."""

    def setUp(self) -> None:
        super().setUp()
        self.first.write(".vimrc", "set number\n")
        # No sync here: `add` writes *and commits* the overlay and the snapshot,
        # so neither test below reads anything a sync would leave behind, and
        # neither asserts about the remote. Measured, interleaved, two runs
        # each: 0.272s per setUp with it, 0.217s without -- ~55ms x 2 tests,
        # paid again for every mutant in a sweep.
        self.assertEqual(0, self.first.call("add", "--host", str(self.first.home / ".vimrc")))
        self.assertFalse(self.first.stored(".vimrc").exists(), "the fixture stored it as shared")

    def test_it_says_nothing_else_manages_the_file(self) -> None:
        done = self.first.run("remove", "--host", str(self.first.home / ".vimrc"))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("nothing else manages it here", done.stdout)
        self.assertIn("was not touched", done.stdout)

    def test_the_file_stays_in_home_and_the_next_sync_prunes_the_snapshot(self) -> None:
        """`sync.stale` does the pruning, and it only sees the file because
        nothing manages it any more. A snapshot left behind is committed, and
        becomes the merge base if the name ever comes back."""
        self.assertEqual(0, self.first.call("remove", "--host", str(self.first.home / ".vimrc")))
        self.assertEqual(0, self.first.call("sync"))
        self.assertEqual("set number\n", self.first.read(".vimrc"))
        self.assertFalse(self.first.snapshot(".vimrc").exists())


class TestTwoHostsOverrideTheSameFile(support.TwoMachines):
    """Both machines override `.bashrc`, which is plan §3.3's motivating case --
    a different git email at work and at home, one shared file underneath."""

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(0, self.second.call("init", str(self.remote)))
        for machine, body in ((self.first, "A only\n"), (self.second, "B only\n")):
            machine.write(".bashrc", body)
            self.assertEqual(0, machine.call("add", "--host", str(machine.home / ".bashrc")))
            self.assertEqual(0, machine.call("sync"))

    def test_each_keeps_its_own_and_the_shared_copy_survives_both(self) -> None:
        # Twice each, so the last writer does not simply win: after this every
        # machine has seen the other's overlay and has had a chance to be wrong
        # about it.
        for machine in (self.first, self.second, self.first, self.second):
            self.assertEqual(0, machine.call("sync"))
        self.assertEqual("A only\n", self.first.read(".bashrc"))
        self.assertEqual("B only\n", self.second.read(".bashrc"))
        self.assertEqual(SHARED, self.first.stored(".bashrc").read_text(encoding="utf-8"))

    def test_a_plain_remove_here_leaves_the_other_machines_override_alone(self) -> None:
        """`remove` touches the shared tree and the overlay of the host it runs
        on, and nothing else. The README says so, which is why this is asserted:
        a `remove` that reached into `hosts/*/` would silently unmanage a file on
        a machine whose owner never asked for it, and the only place that would
        show up is the next sync on that machine.
        """
        self.assertEqual(0, self.second.call("remove", str(self.second.home / ".bashrc")))
        self.assertFalse(self.second.stored(".bashrc").exists(), "the shared copy survived")
        self.assertFalse(self.second.stored(".bashrc", host=True).exists())
        theirs = paths.host_overlay(self.second.repo, self.first.name) / ".bashrc"
        self.assertTrue(theirs.is_file(), "machine-a's override was taken too")

        # And `machine-a` still *manages* it after taking the removal in.
        # Reading `$HOME` here would prove nothing: if its overlay had been
        # deleted too the file would simply be unmanaged, and `$HOME` is left
        # untouched either way -- the same bytes for opposite reasons.
        self.assertEqual(0, self.second.call("sync"))
        self.assertEqual(0, self.first.call("sync"))
        done = self.first.run("list")
        self.assertIn("host  .bashrc", done.stdout)
        self.assertIn("1 managed, 1 from this host's overlay", done.stdout)

    def test_neither_overlay_is_offered_to_the_other_machine_as_managed(self) -> None:
        """`machine-a`'s repository holds `machine-b`'s overlay after the sync.
        `manifest.managed` must not return it, or `machine-a` would write
        `machine-b`'s file into its own `$HOME` under the same name."""
        self.assertEqual(0, self.first.call("sync"))
        done = self.first.run("list")
        self.assertEqual(1, done.stdout.count(".bashrc"), done.stdout)
        self.assertIn("1 managed, 1 from this host's overlay", done.stdout)
