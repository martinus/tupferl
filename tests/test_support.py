"""The sandbox's own guarantee: nothing in here can reach the real installation.

This is the test the rest of the suite rests on. Every other test writes files,
runs git and would happily do both in the developer's `$HOME` if the sandbox were
wrong -- and it would do it *quietly*, because a dotfiles manager pointed at real
dotfiles does exactly what it is supposed to do.

So the fixture poisons every name in `tupferl.paths.ENV_KEYS` first. A sandbox
that inherits rather than replaces then resolves to `/poison/...`, which no
assertion about "under the sandbox home" can accept. Poisoning from `ENV_KEYS`
rather than from a list written here is what makes these assertions keep up with
the code: a variable added there is poisoned by this fixture the same day.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tests import support
from tools import mutate
from tupferl import conflicts, paths

#: A directory that does not exist and never will. Absolute, because
#: `TUPFERL_DIR` rejects a relative value -- the poison has to survive that check
#: to be able to leak at all.
POISON = "/poison"


class Boxed(unittest.TestCase):
    """A throwaway directory and a seeded home, without the environment patch.

    `support.SandboxCase` would patch `os.environ` in `setUp`, which is exactly
    what these tests are trying to observe. So this stops one step short.
    """

    def setUp(self) -> None:
        box = tempfile.TemporaryDirectory(prefix="tupferl-support-")
        self.addCleanup(box.cleanup)
        self.box = Path(box.name)
        self.home = self.box / "home"
        self.home.mkdir()
        support.seed_home(self.home)
        self.env = support.sandbox_env(self.home)


class TestTheSandboxReplacesTheEnvironment(Boxed):
    def setUp(self) -> None:
        super().setUp()
        poisoned = {name: f"{POISON}/{name}" for name in paths.ENV_KEYS}
        patched = mock.patch.dict(os.environ, poisoned)
        patched.start()
        self.addCleanup(patched.stop)

    def test_no_poisoned_value_survives(self) -> None:
        with support.sandboxed(self.home):
            leaked = [name for name, value in os.environ.items() if value.startswith(POISON)]
        self.assertEqual([], leaked)

    def test_every_path_resolves_inside_the_sandbox(self) -> None:
        """The property that matters, stated as paths rather than as variables.

        `ENV_KEYS` is the list of what gets cleared, but clearing is only the
        means. What must be true is that the functions those variables feed all
        answer inside the box -- so this asks them, rather than asking about the
        environment a second time.
        """
        with support.sandboxed(self.home):
            answers = [paths.home(), paths.repo_dir(), paths.state_dir(), paths.backup_dir()]
        for answer in answers:
            self.assertTrue(answer.is_relative_to(self.home), f"{answer} is outside {self.home}")

    def test_the_poison_really_would_be_visible(self) -> None:
        """The precondition, asserted rather than assumed.

        Without this, both tests above pass just as well against a fixture that
        never managed to set anything -- "no poisoned value survives" is
        trivially true when there was no poison. CLAUDE.md §2 calls that a
        negative assertion whose precondition was never established.
        """
        self.assertTrue(paths.repo_dir().is_relative_to(POISON))

    def test_the_hostname_is_the_sandbox_one(self) -> None:
        """Not the real machine's, which differs per developer and per CI leg."""
        with support.sandboxed(self.home, host="other-host"):
            self.assertEqual("other-host", paths.hostname())


class TestTheSandboxKeepsTheGuardsOn(Boxed):
    """What a sandbox must *carry*, which is the opposite failure to leaking.

    Building the environment from nothing is right, and it has one cost: a
    variable that makes the run stricter is dropped along with the ones that
    would point it at the real installation. CI's
    `PYTHONWARNINGS=error::DeprecationWarning` is the case -- without it every
    test that drives the CLI as a subprocess silently stops enforcing it.
    """

    def test_a_deprecation_warning_still_fails_a_sandboxed_child(self) -> None:
        with mock.patch.dict(os.environ, {"PYTHONWARNINGS": "error::DeprecationWarning"}):
            env = support.sandbox_env(self.home)
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import warnings; warnings.warn('x', DeprecationWarning)",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, done.returncode, "the sandbox dropped PYTHONWARNINGS")
        self.assertIn("DeprecationWarning", done.stderr)

    def test_without_it_set_the_child_is_unaffected(self) -> None:
        """The precondition: the test above must be observing the variable
        rather than a python that errors on every warning regardless."""
        with mock.patch.dict(os.environ, {}, clear=True):
            env = support.sandbox_env(self.home)
        done = subprocess.run(
            [
                sys.executable,
                "-c",
                "import warnings; warnings.warn('x', DeprecationWarning)",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, done.returncode)


class TestTheFixturesAreReal(Boxed):
    """The helpers build git repositories, not directories that look like them."""

    def test_make_repo_is_a_repository_with_a_commit(self) -> None:
        repo = support.make_repo(self.box / "repo", self.env)
        self.assertEqual("main", support.git(["branch", "--show-current"], repo, self.env))
        self.assertEqual("initial", support.git(["log", "-1", "--format=%s"], repo, self.env))

    def test_a_pushed_repo_and_its_remote_agree(self) -> None:
        """A remote is only a remote if something can be read back out of it."""
        remote = support.make_remote(self.box / "remote.git", self.env)
        repo = support.make_repo(self.box / "repo", self.env, remote=remote)
        here = support.git(["rev-parse", "HEAD"], repo, self.env)
        there = support.git(["ls-remote", str(remote), "refs/heads/main"], repo, self.env)
        self.assertTrue(there.startswith(here), f"{there} does not start with {here}")

    def test_git_raises_rather_than_returning_a_failure(self) -> None:
        """The fixture helper must be loud: a half-built repository is the weak
        fixture every other test would then be written against."""
        with self.assertRaises(AssertionError):
            support.git(["rev-parse", "HEAD"], self.box, self.env)


class TestADrivenChildIsNotCollectedThroughAPipe(Boxed):
    """`run_cli` hands the child real files, never `subprocess.PIPE`.

    A pipe makes *this* process hold everything the child writes -- measured at
    724 MiB of parent RSS in three seconds against a child printing 4 KiB at a
    time. Under `tools/mutate.py` the parent's memory is charged to the lane's
    share, so a mutant that made `conflicts.ask` loop at EOF killed the whole
    session and was reported `BROKE`: no verdict at all, for a line that
    `test_end_of_input_skips` does guard. It was the only `BROKE` in milestone
    4's sweep.

    This asserts the mechanism rather than the megabytes, and that is the honest
    shape here: `run_cli` always runs `python -m tupferl`, and no tupferl command
    prints without end, so a fixture built to measure growth through this
    function measures a child that exits immediately. The first attempt did
    exactly that and passed in 0.256s. Handing Popen a file is the *only* way the
    parent avoids accumulating, so it is the whole property, and
    `tests/test_sync_conflicts.py` asserts the consequence a file buys.
    """

    def test_popen_is_given_files_for_both_streams(self) -> None:
        seen: dict[str, object] = {}
        real = subprocess.Popen

        def watch(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return real(*args, **kwargs)

        with mock.patch.object(subprocess, "Popen", watch):
            support.run_cli(["--version"], self.env, keys="s")

        for stream in ("stdout", "stderr"):
            with self.subTest(stream=stream):
                self.assertIsNot(seen[stream], subprocess.PIPE, "collected through a pipe")
                self.assertTrue(
                    hasattr(seen[stream], "fileno"), f"{stream} is not a file: {seen[stream]!r}"
                )

    def test_the_precondition_that_the_pty_path_was_taken(self) -> None:
        """Both assertions above are vacuous if `keys` was ignored and the
        pipe-free `subprocess.run` branch ran instead -- that branch uses
        `capture_output`, which never reaches Popen's kwargs as this test reads
        them."""
        seen: dict[str, object] = {}
        real = subprocess.Popen

        def watch(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs)
            return real(*args, **kwargs)

        with mock.patch.object(subprocess, "Popen", watch):
            support.run_cli(["--version"], self.env, keys="s")
        self.assertTrue(hasattr(seen.get("stdin"), "__index__"), "no pty was attached")


class TestBackgroundGitIsOff(Boxed):
    """#17: no detached git process may outlive the command that started it.

    **Asked of git, not read out of the file.** `seed_home` writes
    `$HOME/.gitconfig`, and whether git reads it depends on the sandbox clearing
    `GIT_CONFIG_GLOBAL` and `XDG_CONFIG_HOME` -- which is the half a test that
    grepped the file it just wrote could not see. `git config --get` under this
    machine's environment is the whole claim: the setting is in force where git
    will look for it.

    Why it matters is in `support.NO_HOUSEKEEPING`. In short: `gc --auto` and
    `maintenance run --auto` are detached by default, and a detached process
    writing into `.git/objects` is what makes a tree non-empty a moment after
    `shutil` scanned it as empty. It has turned CI red twice, both times naming
    the sync property, which had passed.
    """

    def setUp(self) -> None:
        super().setUp()
        self.repo = self.box / "repo"
        support.git(["init", "--quiet", str(self.repo)], cwd=self.box, env=self.env)

    def asked(self, key: str) -> subprocess.CompletedProcess[str]:
        """What git answers for `key` inside the sandbox repository."""
        return subprocess.run(
            ["git", "config", "--get", key],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_git_reads_back_every_setting_that_stops_housekeeping(self) -> None:
        for key, want in (
            ("gc.auto", "0"),
            ("gc.autoDetach", "false"),
            ("maintenance.auto", "false"),
        ):
            with self.subTest(key=key):
                got = self.asked(key)
                self.assertEqual(0, got.returncode, got.stderr)
                self.assertEqual(want, got.stdout.strip())

    def test_the_probe_can_come_back_empty(self) -> None:
        """The precondition. Three `assertEqual`s against a `git config` that
        answered *anything* would pass if `--get` always printed the value asked
        for; this shows it does not, so the three above are reading real
        settings rather than an echo."""
        got = self.asked("gc.nosuchsetting")
        self.assertEqual(1, got.returncode)
        self.assertEqual("", got.stdout.strip())

    def test_the_identity_still_works_beside_them(self) -> None:
        """`seed_home` writes one file, and #17 appended to it. A malformed
        section would take git's identity down with it, and every commit in the
        suite with it -- so this asserts the half that was already there."""
        got = self.asked("user.email")
        self.assertEqual(0, got.returncode, got.stderr)
        self.assertEqual(f"{support.HOST}@example.invalid", got.stdout.strip())


class TestATreeThatWillNotGo(unittest.TestCase):
    """#17's other half: when cleanup fails, say what survived.

    The failure this exists for is a race nobody has reproduced on demand -- see
    #17, which could not do it on git 2.43 and could not get at the runner's
    2.55. So the *trigger* here is simulated: the box's own `cleanup` is made to
    raise the errno CI actually printed. Everything else is real -- a real tree,
    real files, and the listing read off the real filesystem after the failure.

    That is the honest shape for this claim. What is under test is "if cleanup
    raises, the error names what is left", not "git races teardown"; a fixture
    that waited for a real race would be a test that usually does nothing.

    **The bound method, not `tempfile`'s internals.** `TemporaryDirectory`
    reaches `shutil.rmtree` by a different private route on 3.10, 3.12 and 3.14,
    and this suite runs on all three -- a patch of one of them passes on one leg
    and fails on the others, which is the version trap CLAUDE.md's gotchas
    already collect two instances of. `box.cleanup()` is what `discard` calls,
    and making *that* raise is the precondition stated exactly.
    """

    #: The failure as CI printed it, down to the errno.
    REFUSED = OSError(39, "Directory not empty", "objects")

    def stuck(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        """A real tree holding a real pack temporary, whose cleanup refuses.

        The original `cleanup` is kept and registered, so the tree still goes
        away when the test ends: a test about a leaked tree that leaked one
        would be its own bug.
        """
        box = tempfile.TemporaryDirectory(prefix="tupferl-stuck-")
        root = Path(box.name)
        (root / "repo" / ".git" / "objects").mkdir(parents=True)
        (root / "repo" / ".git" / "objects" / "tmp_pack_abcdef").write_text("x")
        really = box.cleanup
        self.addCleanup(really)

        def refuse() -> None:
            raise self.REFUSED

        box.cleanup = refuse  # type: ignore[method-assign]
        return box, root

    def raised(self, box: tempfile.TemporaryDirectory[str]) -> OSError:
        with self.assertRaises(OSError) as caught:
            support.discard(box)
        return caught.exception

    def test_the_message_names_the_file_that_survived(self) -> None:
        """`tmp_pack_abcdef` is a writer's signature: git wrote it, and no test
        did. That name in the error is the whole point of #17's second half."""
        box, _ = self.stuck()
        self.assertIn("tmp_pack_abcdef", str(self.raised(box)))

    def test_it_keeps_the_original_error_rather_than_replacing_it(self) -> None:
        """The errno and the wording git's own failure produced are what a
        reader searches for. A wrapper that dropped them would send them looking
        for a different bug."""
        boom = self.raised(self.stuck()[0])
        self.assertIn("Directory not empty", str(boom))
        self.assertIs(self.REFUSED, boom.__cause__)

    def test_it_says_the_writer_outlived_its_command(self) -> None:
        """The sentence that stops the next reader diagnosing the sync engine,
        which is what #17 says cost the most both times it happened."""
        boom = self.raised(self.stuck()[0])
        self.assertIn("outliving the command that started it", str(boom))

    def test_a_long_listing_is_cut_and_says_how_much_it_cut(self) -> None:
        """A whole surviving tree is hundreds of paths, and a message nobody
        reads to the end names nothing.

        The count is written out rather than computed here: the same subtraction
        spelled twice is a test holding a copy of its subject, which cannot fail
        (CLAUDE.md §2). Arriving at it: `stuck` leaves four paths of its own --
        `repo`, `.git`, `objects` and the pack temporary -- so 13 files make 17,
        of which `NAMED_WHEN_STUCK` are named and 5 are not.
        """
        box, root = self.stuck()
        for number in range(13):
            (root / f"file{number:03d}").write_text("x")
        boom = str(self.raised(box))
        self.assertIn("and 5 more", boom)
        # The cut is real: `file012` sorts last and must not have been named.
        self.assertNotIn("file012", boom)
        self.assertIn("file000", boom)

    def test_a_tree_that_goes_quietly_raises_nothing(self) -> None:
        """The ordinary path, which is every other call in the suite. Without
        it, `discard` could raise always and the four tests above would still
        pass -- CLAUDE.md §2's negative assertion with no precondition."""
        box = tempfile.TemporaryDirectory(prefix="tupferl-quiet-")
        root = Path(box.name)
        (root / "a").mkdir()
        (root / "a" / "b").write_text("x")
        support.discard(box)
        self.assertFalse(root.exists())


class TestTheTwoMachineTemplate(unittest.TestCase):
    """#19's fixture: copies of one tree that must not be able to see each other.

    The saving is real -- 4.3 ms against 120.4 ms per test, and a measured
    median of 19.5 s off the six affected modules run serially -- but it trades
    a fresh build for a shared origin, and the failure that trade can produce is
    the worst kind: two tests quietly sharing a remote, so one sees another's
    commits and the pair pass or fail depending on the order they ran in.
    That is what this class is for.
    """

    def copy(self) -> tuple[support.Computer, support.Computer, Path]:
        box = tempfile.TemporaryDirectory(prefix="tupferl-copies-")
        self.addCleanup(support.discard, box)
        return support.two_machines(Path(box.name))

    def test_two_copies_do_not_share_a_remote(self) -> None:
        """The contamination test, and it is driven rather than asserted from
        the config: one copy syncs a change, and the other must not see it.

        A URL comparison alone would pass against two paths that differ in text
        and resolve to the same directory.
        """
        first, _, here = self.copy()
        other_first, other_second, there = self.copy()
        self.assertNotEqual(here, there)

        first.write(".bashrc", "CHANGED ON THE FIRST COPY\n")
        self.assertEqual(0, first.call("sync"))

        self.assertEqual(0, other_second.call("init", str(there)))
        self.assertNotIn("CHANGED ON THE FIRST COPY", other_second.read(".bashrc"))
        self.assertEqual(support.STARTS_AS, other_first.read(".bashrc"))

    def test_the_copy_points_at_its_own_remote(self) -> None:
        """The mechanism behind the test above. Left unrewritten, every copy's
        `origin` is the template's remote."""
        first, _, remote = self.copy()
        url = support.git(["remote", "get-url", "origin"], cwd=first.repo, env=first.env)
        self.assertEqual(str(remote), url)
        self.assertFalse(Path(url).is_relative_to(support.template()))

    def test_no_stale_fetch_head_survives_the_copy(self) -> None:
        """It records the URL of the last fetch, which in a copy is the
        template's. Nothing reads it -- `sync` merges `<remote>/<branch>` -- so
        this is a lie removed rather than a bug fixed, and the test says which."""
        first, _, _ = self.copy()
        self.assertFalse((first.repo / ".git" / "FETCH_HEAD").exists())

    def test_nothing_in_a_copy_still_names_the_template(self) -> None:
        """The general form of the two tests above, so a *third* file that
        learns to hold an absolute path is caught rather than waited for.

        This is how the two were found in the first place: grep the built tree
        for its own root.
        """
        _, _, remote = self.copy()
        root = str(support.template())
        named = []
        for path in remote.parent.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:  # pragma: no cover - a fifo or a socket, neither present
                continue
            if root in text:
                named.append(str(path.relative_to(remote.parent)))
        self.assertEqual([], named)

    def test_the_template_is_built_once(self) -> None:
        """Per *process*, not per class -- 40 classes inherit the fixture, so
        per class would be 40 builds. Asked of the cache rather than timed,
        because a timing assertion here would be a flake."""
        support.template()
        before = support.template.cache_info()
        support.template()
        support.template()
        after = support.template.cache_info()
        self.assertEqual(before.misses, after.misses)
        self.assertEqual(before.hits + 2, after.hits)

    def test_a_copy_starts_where_a_built_one_did(self) -> None:
        """The equivalence the whole change rests on: what `setUp` used to build
        by running `init`, `add` and `sync` is what a copy now holds.

        Asserted through the tool rather than by comparing trees -- commit
        hashes and timestamps differ between a build and a copy and always will,
        and none of that is what a test using this fixture depends on.
        """
        first, second, remote = self.copy()
        self.assertEqual(support.STARTS_AS, first.read(".bashrc"))
        status, said = first.say("status")
        self.assertEqual(0, status, said)
        self.assertIn("1 file managed, 0 to change, 0 in conflict", said)
        # And the remote really holds it: the second machine can be brought up.
        self.assertEqual(0, second.call("init", str(remote)))
        self.assertEqual(support.STARTS_AS, second.read(".bashrc"))


if __name__ == "__main__":
    unittest.main()


class TestAPromptIsBoundedRatherThanBlocking(unittest.TestCase):
    """`typing` fails a prompt that asks more often than its keys answer.

    **`FALLBACK` is not a bound.** `conflicts.one_key` sets `VMIN` to 1, so a
    read on a pty whose master is still open waits for ever rather than
    reporting exhaustion -- correct for a real terminal, and it leaves the eight
    `s` keys as the only thing between a prompt asking once too often and a
    suite that hangs.

    That gap had a measured cost. A mutation making `ask` treat *every* key as
    unrecognised eats all nine and blocks; under `tools/mutate.py` it tripped
    the 30s per-test alarm and was filed `BROKE`, which is never `caught` --
    `tupferl/conflicts.py:635` came back that way in three of four ordered
    sweeps and `caught` in none, so the line it appears to guard was guarded by
    nothing. With the bound in place the same mutation is `caught`.

    `PROMPTED` is patched down here rather than waited out: the claim is that a
    bound exists and fires, not what it is set to.
    """

    def test_a_prompt_that_never_settles_fails_instead_of_hanging(self) -> None:
        with (
            mock.patch.object(support, "PROMPTED", 0.5),
            self.assertRaises(TimeoutError),
            support.typing("l"),
        ):
            while True:
                conflicts.one_key(sys.stdin)

    def test_the_bounds_beat_the_harness_alarm(self) -> None:
        """The number, not just the mechanism -- and the tests above patch it, so
        nothing else can see it.

        `tools/mutate.py` arms a per-test alarm and files whatever trips it as
        `BROKE`, which is never `caught`. A fixture bound *above* that alarm
        therefore never fires: the two race and the harness always wins, and the
        line under test ends up guarded by nothing while the summary shows the
        row in neither of the two numbers a reader looks at. That is exactly how
        `conflicts.py:635` went unguarded, at `PROMPTED = 60.0` against a 30s
        alarm.

        Asserted against `mutate.EACH_TEST` rather than against a literal, so
        raising the alarm cannot silently re-open the gap.
        """
        self.assertLess(support.PROMPTED, mutate.EACH_TEST, "a whole keyed run")
        self.assertLess(support.PATIENCE, mutate.EACH_TEST, "a single read")

    def test_a_prompt_that_settles_is_left_alone(self) -> None:
        """The other half, and without it "always raise" passes the test above.
        A bound that fires on a prompt which *did* get its answer would fail
        every keyed test in the suite -- which is exactly what an earlier
        attempt at this did, 11 failures and an error, while the mutation run
        above them reported `caught` on a red baseline and read like a clean
        sweep.
        """
        with mock.patch.object(support, "PROMPTED", 0.5), support.typing("l"):
            self.assertEqual("l", conflicts.one_key(sys.stdin))
