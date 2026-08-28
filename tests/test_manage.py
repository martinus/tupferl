"""`init`, `add`, `remove`, `list`, driven as a user drives them.

Real `git`, a real bare repository standing in for the remote, and the CLI in a
subprocess wherever the exit status or the printed output is the thing under
test. Plan §7.1 forbids mocking git, and these commands are almost entirely
*about* git: a mock would assert that this code calls the functions it calls,
which is true by construction and interesting to nobody.

The two-machine fixture is the one worth knowing about. `TestTwoMachines` gives
each host its own `$HOME` and its own clone of one bare remote, which is the
only shape in which "this host's overlay" means anything — a single-machine test
cannot tell an overlay that works from one that silently applies everywhere.
"""

from __future__ import annotations

import os
import stat
import unittest
from pathlib import Path, PurePosixPath
from unittest import mock

from tests import support
from tupferl import __main__ as cli
from tupferl import copies, gitrepo, manage, paths
from tupferl.config import Config, load
from tupferl.errors import TupferlError

#: The one-machine fixture, now in `tests/support.py` because `test_sync.py`
#: builds the same one. Bound here so the six `class Test...(Machine)` below read
#: as they always did.
Machine = support.Machine


class TestInit(Machine):
    def test_an_empty_remote_is_cloned_and_given_a_first_commit(self) -> None:
        """The first-run path: the user has just created an empty repository on
        their host. A clone with no commits sits on an unborn branch, the one
        state where `HEAD` does not resolve, so `init` normalises it."""
        done = self.init()
        self.assertIn("cloned", done.stdout)
        self.assertTrue(gitrepo.is_repository(self.repo))
        self.assertTrue(gitrepo.has_commits(self.repo))
        self.assertTrue(paths.config_file(self.repo).is_file())

    def test_the_settings_it_writes_parse_to_the_defaults(self) -> None:
        """Comments only. A template that shipped real values would make every
        repository disagree with the documented defaults the day one changed."""
        self.init()
        self.assertEqual(Config(), load(paths.config_file(self.repo)))

    def test_a_remote_with_content_is_cloned_and_then_synced(self) -> None:
        """The second-machine path, and the README's one-line promise.

        No settings file is invented -- the repository already has a shape -- but
        plan §4 says `init` "then runs a first sync", and milestone 3 made that
        true. So the file arrives in `$HOME`, and the one commit `init` adds is
        this host's merge base: without it the machine could not merge anything
        later, because it would have no common ancestor to merge against.
        """
        first = support.make_repo(self.tmp / "seed", self.env, remote=self.remote)
        (first / ".bashrc").write_text("export EDITOR=nvim\n", encoding="utf-8")
        support.git(["add", "-A"], first, self.env)
        support.git(["commit", "-m", "seeded"], first, self.env)
        support.git(["push"], first, self.env)

        self.init()
        stored = (self.repo / ".bashrc").read_text(encoding="utf-8")
        self.assertEqual("export EDITOR=nvim\n", stored)
        self.assertEqual("export EDITOR=nvim\n", (self.home / ".bashrc").read_text())
        self.assertEqual([f"sync from {self.host}: .bashrc", "seeded", "initial"], self.log())
        self.assertFalse(paths.config_file(self.repo).is_file())

    def test_a_url_that_cannot_be_cloned_is_reported(self) -> None:
        """And nothing is created. The alternative — falling back to a local
        repository pointed at the URL — hides a typo until the first sync, by
        which time the user has added files and believes they are backed up."""
        done = self.run_cli("init", str(self.tmp / "absent.git"))
        self.assertEqual(2, done.returncode)
        self.assertIn("could not clone", done.stderr)
        self.assertFalse(gitrepo.is_repository(self.repo))

    def test_an_empty_directory_in_the_way_is_cloned_into(self) -> None:
        """`~/.local/share/tupferl/` exists on any machine that has run
        `doctor`, and an empty directory is not something to refuse. It is also
        what tells `any(...)` from `all(...)`: `all([])` is True, so that mutant
        turns every ordinary first run into "already exists and is not empty"."""
        self.repo.mkdir(parents=True)
        self.init()
        self.assertTrue(gitrepo.has_commits(self.repo))

    def test_the_parent_directory_already_existing_is_fine(self) -> None:
        """The state `doctor` leaves behind when it checks the backup path: the
        XDG data directory is there and the repository is not."""
        self.repo.parent.mkdir(parents=True)
        self.init()
        self.assertTrue(gitrepo.is_repository(self.repo))

    def test_the_clone_failure_quotes_the_line_that_explains(self) -> None:
        """Neither the first line nor the last, and this fixture is why.

        `git clone` writes progress to stderr, so its *first* line is "Cloning
        into '...'" — which says nothing went wrong, and is what `init` reported
        for about an hour until this test was written. Its *last* is "and the
        repository exists.", half a sentence of generic advice, which is what
        `doctor` reported for a milestone. `gitrepo.reason` takes the line git
        marked `fatal:` instead.

        The URL is ssh at a refused local port rather than the missing directory
        the test above uses: a local path that is not a repository produces one
        line, and one line cannot tell any of these rules apart. No packet
        leaves the machine — 127.0.0.1:1 refuses instantly.
        """
        unreachable = "ssh://127.0.0.1:1/x"
        done = self.run_cli("init", unreachable)
        self.assertEqual(2, done.returncode)

        said = gitrepo.git(["clone", "--", unreachable, str(self.tmp / "x")], cwd=self.tmp)
        lines = [line.strip() for line in said.err.splitlines() if line.strip()]
        self.assertGreater(len(lines), 2, "the fixture produced too few lines to tell these apart")
        self.assertTrue(lines[0].startswith("Cloning into"), lines[0])

        self.assertIn(gitrepo.reason(said), done.stderr)
        self.assertTrue(gitrepo.reason(said).startswith("fatal:"), gitrepo.reason(said))
        self.assertNotIn("Cloning into", done.stderr)
        self.assertNotIn(lines[-1], done.stderr)

    def test_running_it_twice_is_refused(self) -> None:
        self.init()
        done = self.run_cli("init", str(self.remote))
        self.assertEqual(2, done.returncode)
        self.assertIn("already a tupferl repository", done.stderr)

    def test_a_first_commit_that_fails_is_reported(self) -> None:
        """`init` on an empty remote makes the repository's first commit, and a
        machine whose hooks refuse it cannot. Without the guard `init` reports
        success and leaves a clone with no branch — the one state where `HEAD`
        does not resolve, which is what the commit exists to avoid."""
        support.break_commits(self.home)
        done = self.run_cli("init", str(self.remote))
        self.assertEqual(2, done.returncode)
        self.assertIn("could not commit the settings file", done.stderr)

    def test_a_file_where_the_repository_belongs_is_refused(self) -> None:
        """One stray `touch` produces this, and `iterdir` raises
        `NotADirectoryError` on it -- a traceback where a sentence belongs."""
        self.repo.parent.mkdir(parents=True, exist_ok=True)
        self.repo.write_text("not a directory", encoding="utf-8")
        done = self.run_cli("init", str(self.remote))
        self.assertEqual(2, done.returncode)
        self.assertIn("not a directory", done.stderr)
        self.assertNotIn("Traceback", done.stderr)

    def test_a_non_empty_directory_in_the_way_is_refused(self) -> None:
        """Not cloned over. Whatever is there was put there by someone, and the
        message says to move it rather than doing it for them."""
        self.repo.mkdir(parents=True)
        (self.repo / "stray").write_text("mine", encoding="utf-8")
        done = self.run_cli("init", str(self.remote))
        self.assertEqual(2, done.returncode)
        self.assertIn("not empty", done.stderr)
        self.assertEqual("mine", (self.repo / "stray").read_text(encoding="utf-8"))


class TestAdd(Machine):
    def setUp(self) -> None:
        super().setUp()
        self.init()

    def test_a_file_is_copied_and_committed(self) -> None:
        self.write(self.home / ".bashrc", "export EDITOR=nvim\n")
        done = self.run_cli("add", str(self.home / ".bashrc"))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual("export EDITOR=nvim\n", self.stored(".bashrc").read_text(encoding="utf-8"))
        self.assertIn("add from test-host: .bashrc", self.log())

    def test_the_repository_is_left_clean(self) -> None:
        """Everything commits immediately, so `doctor`'s "uncommitted changes"
        stays a real signal that a run was interrupted."""
        self.write(self.home / ".bashrc", "x")
        self.run_cli("add", str(self.home / ".bashrc"))
        self.assertEqual("", support.git(["status", "--porcelain"], self.repo, self.env))

    def test_a_directory_adds_every_file_under_it(self) -> None:
        """Two files in the *same* subdirectory, and a third one deeper. The
        pair sharing a parent is what tells `mkdir(exist_ok=True)` from dropping
        it: with distinct parents throughout, the second `mkdir` never sees a
        directory that is already there."""
        self.write(self.home / ".config" / "nvim" / "init.lua", "vim.opt.number = true\n")
        self.write(self.home / ".config" / "nvim" / "other.lua", "return {}\n")
        self.write(self.home / ".config" / "nvim" / "lua" / "opts.lua", "return {}\n")
        done = self.run_cli("add", str(self.home / ".config"))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertTrue(self.stored(".config/nvim/init.lua").is_file())
        self.assertTrue(self.stored(".config/nvim/other.lua").is_file())
        self.assertTrue(self.stored(".config/nvim/lua/opts.lua").is_file())

    def test_the_executable_bit_survives_and_nothing_else_does(self) -> None:
        """Plan §5. 0o700 in, 0o755 stored: the bit is kept, the rest is not,
        because git records exactly one bit and any other would be lost on the
        first clone."""
        script = self.write(self.home / ".local" / "bin" / "hello", "#!/bin/sh\necho hi\n")
        script.chmod(0o700)
        self.run_cli("add", str(script))
        self.assertEqual(0o755, stat.S_IMODE(self.stored(".local/bin/hello").stat().st_mode))

    def test_a_plain_file_is_stored_unexecutable(self) -> None:
        self.write(self.home / ".bashrc", "x")
        self.run_cli("add", str(self.home / ".bashrc"))
        self.assertEqual(0o644, stat.S_IMODE(self.stored(".bashrc").stat().st_mode))

    def test_a_named_path_that_is_refused_stops_the_whole_run(self) -> None:
        """They asked for that file by name. A run that skipped it and committed
        the others would leave them believing it was stored."""
        self.write(self.home / ".bashrc", "x")
        (self.home / ".linked").symlink_to(self.tmp / "elsewhere")
        done = self.run_cli("add", str(self.home / ".bashrc"), str(self.home / ".linked"))
        self.assertEqual(2, done.returncode)
        self.assertIn("symlink", done.stderr)
        self.assertFalse(self.stored(".bashrc").exists(), "it committed some of them anyway")

    def test_a_file_found_by_walking_is_skipped_and_reported(self) -> None:
        """The other half: adding `~/.config` with one socket in it must manage
        the rest, and say what it did not."""
        self.write(self.home / ".config" / "good.conf", "x")
        (self.home / ".config" / "linked").symlink_to(self.tmp / "elsewhere")
        done = self.run_cli("add", str(self.home / ".config"))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("skipped", done.stdout)
        self.assertTrue(self.stored(".config/good.conf").is_file())

    def test_re_adding_an_unchanged_file_changes_nothing(self) -> None:
        """No commit, and it says so. `add` is how someone re-stores a file they
        have edited; this is what it does when they had not."""
        self.write(self.home / ".bashrc", "x")
        self.run_cli("add", str(self.home / ".bashrc"))
        before = self.log()
        done = self.run_cli("add", str(self.home / ".bashrc"))
        self.assertEqual(0, done.returncode)
        self.assertIn("no change", done.stdout)
        self.assertEqual(before, self.log())

    def test_re_adding_an_edited_file_says_updated(self) -> None:
        """The precondition for the test above: "no change" has to be observable
        against a run that does change something."""
        self.write(self.home / ".bashrc", "x")
        self.run_cli("add", str(self.home / ".bashrc"))
        self.write(self.home / ".bashrc", "y")
        done = self.run_cli("add", str(self.home / ".bashrc"))
        self.assertIn("updated .bashrc", done.stdout)
        self.assertEqual("y", self.stored(".bashrc").read_text(encoding="utf-8"))

    def test_a_mode_change_alone_is_a_change(self) -> None:
        """`chmod +x` with no edit is a real change git will record, so a
        comparison on contents alone would silently drop it."""
        script = self.write(self.home / "s.sh", "#!/bin/sh\n")
        self.run_cli("add", str(script))
        script.chmod(0o755)
        done = self.run_cli("add", str(script))
        self.assertIn("updated", done.stdout)
        self.assertEqual(0o755, stat.S_IMODE(self.stored("s.sh").stat().st_mode))

    def test_home_itself_cannot_be_added(self) -> None:
        """The most extreme form of "contains the repository": adding `$HOME`
        would walk into `~/.local/share/tupferl/repo` and manage tupferl's own
        copies of everything, recursively."""
        done = self.run_cli("add", str(self.home))
        self.assertEqual(2, done.returncode)
        self.assertIn("own repository", done.stderr)

    def test_a_large_file_is_compared_without_reading_it_whole(self) -> None:
        """`max_file_size` is a setting and someone will raise it, so the
        unchanged-file comparison must not load both copies into memory. This
        asserts the *answer* rather than the mechanism -- a megabyte is not
        enough to prove the memory claim, but it does prove `filecmp` was given
        the two paths correctly, which is what a swap to it can get wrong."""
        big = self.write(self.home / ".big", "x" * 900_000)
        self.assertEqual(0, self.run_cli("add", str(big)).returncode)
        again = self.run_cli("add", str(big))
        self.assertIn("no change", again.stdout)
        self.write(self.home / ".big", "y" * 900_000)
        self.assertIn("updated", self.run_cli("add", str(big)).stdout)

    def test_a_file_whose_name_begins_with_a_dash(self) -> None:
        """`git add -- -x` is why `stage` passes `--`. Without it git reports
        "unknown switch" for a dotfile somebody really has, and the guard is
        otherwise a line nobody can tell works."""
        self.write(self.home / "-dashfile", "x")
        done = self.run_cli("add", str(self.home / "-dashfile"))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("add from test-host: -dashfile", self.log())
        gone = self.run_cli("remove", str(self.home / "-dashfile"))
        self.assertEqual(0, gone.returncode, gone.stdout + gone.stderr)

    def test_overlapping_paths_are_stored_once(self) -> None:
        """A directory and a file inside it, named in the same run. The commit
        message and the printed list both come from this set, so a duplicate
        would be visible twice in `git log`."""
        self.write(self.home / ".config" / "nvim" / "init.lua", "x")
        done = self.run_cli(
            "add", str(self.home / ".config"), str(self.home / ".config" / "nvim" / "init.lua")
        )
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(1, done.stdout.count("init.lua"), done.stdout)
        self.assertEqual(1, self.log()[0].count("init.lua"), self.log()[0])

    def test_the_order_it_stores_in_does_not_depend_on_the_argument_order(self) -> None:
        """Two files named in reverse order. The set `add` iterates is a dict
        built in argument order, so this is the fixture that tells a sort from
        no sort — and the order reaches the user twice, in what is printed and
        in the commit message."""
        self.write(self.home / ".zshrc", "z")
        self.write(self.home / ".aaa", "a")
        done = self.run_cli("add", str(self.home / ".zshrc"), str(self.home / ".aaa"))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertLess(done.stdout.index(".aaa"), done.stdout.index(".zshrc"), done.stdout)
        self.assertEqual("add from test-host: .aaa, .zshrc", self.log()[0])

    def test_a_directory_where_everything_is_refused_says_so(self) -> None:
        """Not "no change", which is what an empty admitted set would otherwise
        fall through to — and which reads as "already stored" rather than
        "stored nothing"."""
        (self.home / ".config" / "one").symlink_to(self.tmp / "elsewhere")
        (self.home / ".config" / "two").symlink_to(self.tmp / "elsewhere")
        done = self.run_cli("add", str(self.home / ".config"))
        self.assertEqual(2, done.returncode)
        self.assertIn("nothing to add", done.stderr)

    def test_a_failing_commit_is_reported_rather_than_ignored(self) -> None:
        """A brand-new machine with no git identity: `git commit` refuses, and
        without the guard `add` would report success having stored nothing.

        The fixture is a `pre-commit` hook that refuses -- see
        `support.break_commits` for why not the missing-identity state this test
        was originally written against.
        """
        support.break_commits(self.home)
        self.write(self.home / ".bashrc", "x")
        done = self.run_cli("add", str(self.home / ".bashrc"))
        self.assertEqual(2, done.returncode)
        self.assertIn("could not commit", done.stderr)

    def test_a_failing_stage_is_reported_rather_than_ignored(self) -> None:
        """A corrupted index: `.git/index` replaced by a directory, so `git add`
        cannot map it. Without the guard `add` walks on and commits nothing
        while reporting success.

        A directory rather than a `chmod`, because the suite runs as root in
        some containers and root ignores the mode bits.
        """
        index = self.repo / ".git" / "index"
        index.unlink()
        index.mkdir()
        self.write(self.home / ".bashrc", "x")
        done = self.run_cli("add", str(self.home / ".bashrc"))
        self.assertEqual(2, done.returncode)
        self.assertIn("could not stage", done.stderr)

    def test_a_commit_failure_is_reduced_to_the_line_that_explains(self) -> None:
        """Not the whole stderr blob.

        `gitrepo.reason` exists for this, and `add` and `remove` interpolated
        raw `.err` while `init` forty lines earlier did not -- three copies of
        one block, drifted. They are one helper now, and this is the assertion
        that would have noticed: the hook writes two lines, and only the first
        may reach the user.
        """
        support.break_commits(self.home)
        self.write(self.home / ".bashrc", "x")
        done = self.run_cli("add", str(self.home / ".bashrc"))
        self.assertEqual(2, done.returncode)
        self.assertIn(support.HOOK_REFUSED, done.stderr)
        self.assertNotIn(support.HOOK_TRAILER, done.stderr)

    def test_it_needs_a_repository(self) -> None:
        """Run in a home where `init` never was."""
        with support.tempdir() as box:
            home = box / "home"
            home.mkdir()
            support.seed_home(home)
            env = support.sandbox_env(home)
            (home / ".bashrc").write_text("x", encoding="utf-8")
            done = support.run_cli(["add", str(home / ".bashrc")], env)
        self.assertEqual(2, done.returncode)
        # On "no repository at", not on "tupferl init": *both* of `open_repo`'s
        # messages end in that command, so asserting it alone passes with the
        # existence check removed entirely. The mutation sweep found this, in
        # the same shape it found in `doctor.repository` a milestone ago.
        self.assertIn("no repository at", done.stderr)


class TestRemove(Machine):
    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.write(self.home / ".bashrc", "keep me\n")
        self.run_cli("add", str(self.home / ".bashrc"))

    def test_the_copy_goes_and_the_original_stays(self) -> None:
        done = self.run_cli("remove", str(self.home / ".bashrc"))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertFalse(self.stored(".bashrc").exists())
        self.assertEqual("keep me\n", (self.home / ".bashrc").read_text(encoding="utf-8"))
        self.assertIn("remove from test-host: .bashrc", self.log())

    def test_a_file_already_deleted_from_home_can_still_be_removed(self) -> None:
        """Often the reason someone reaches for it: the file is gone locally and
        they want the repository to stop pushing it to the other machine.
        Requiring existence would refuse exactly then."""
        (self.home / ".bashrc").unlink()
        done = self.run_cli("remove", str(self.home / ".bashrc"))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertFalse(self.stored(".bashrc").exists())

    def test_directories_left_empty_are_pruned(self) -> None:
        """git does not track directories, so an empty one is invisible in the
        commit and present in every clone — `~/.config/nvim/` with nothing in
        it, on a machine that never used nvim."""
        self.write(self.home / ".config" / "nvim" / "init.lua", "x")
        self.run_cli("add", str(self.home / ".config"))
        self.run_cli("remove", str(self.home / ".config" / "nvim" / "init.lua"))
        self.assertFalse((self.repo / ".config").exists())

    def test_the_repository_root_is_never_pruned(self) -> None:
        """The loop stops at the repository. Removing the last managed file must
        not delete the repository out from under the user."""
        self.run_cli("remove", str(self.home / ".bashrc"))
        self.assertTrue(gitrepo.is_repository(self.repo))

    def test_pruning_stops_at_the_repository_even_from_deep_inside(self) -> None:
        """The loop deletes directories and walks upwards. This is the deepest
        tree the tests build, so it is the one that would climb furthest if the
        stop condition were wrong -- and `$HOME` is what sits above it."""
        self.write(self.home / ".config" / "a" / "b" / "c" / "deep.conf", "x")
        self.run_cli("add", str(self.home / ".config"))
        self.run_cli("remove", str(self.home / ".config" / "a" / "b" / "c" / "deep.conf"))
        self.assertFalse((self.repo / ".config").exists())
        self.assertTrue(self.repo.is_dir(), "it pruned the repository itself")
        self.assertTrue(self.home.is_dir(), "it climbed out of the repository")
        self.assertTrue(paths.config_file(self.repo).is_file(), "it pruned .tupferl/")

    def test_pruning_stops_at_a_directory_that_still_holds_something(self) -> None:
        """The ordinary case, and the one that tells `and` from `or` in a loop
        that calls `rmdir`: with `or`, the walk enters a non-empty directory and
        `rmdir` raises."""
        self.write(self.home / ".config" / "nvim" / "init.lua", "x")
        self.write(self.home / ".config" / "nvim" / "other.lua", "x")
        self.run_cli("add", str(self.home / ".config"))
        done = self.run_cli("remove", str(self.home / ".config" / "nvim" / "init.lua"))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertTrue((self.repo / ".config" / "nvim" / "other.lua").is_file())
        self.assertNotIn("Traceback", done.stderr)

    def test_a_failing_stage_during_removal_is_reported(self) -> None:
        """Same corrupted index, on the other command. The copy is already gone
        from the working tree by the time staging fails, so a run that ignored
        it would leave the repository committed-clean and the file still
        tracked."""
        index = self.repo / ".git" / "index"
        index.unlink()
        index.mkdir()
        done = self.run_cli("remove", str(self.home / ".bashrc"))
        self.assertEqual(2, done.returncode)
        self.assertIn("could not stage", done.stderr)

    def test_a_failing_commit_during_removal_is_reported(self) -> None:
        """A `pre-commit` hook that refuses, which is a real thing to have and,
        unlike a missing git identity, fails the same way on every platform."""
        support.break_commits(self.home)
        done = self.run_cli("remove", str(self.home / ".bashrc"))
        self.assertEqual(2, done.returncode)
        self.assertIn("could not commit", done.stderr)

    def test_a_commit_failure_during_removal_is_reduced_too(self) -> None:
        """The other half of the same helper. One assertion per call site,
        because the drift this replaced was per call site."""
        support.break_commits(self.home)
        done = self.run_cli("remove", str(self.home / ".bashrc"))
        self.assertEqual(2, done.returncode)
        self.assertIn(support.HOOK_REFUSED, done.stderr)
        self.assertNotIn(support.HOOK_TRAILER, done.stderr)

    def test_removing_something_unmanaged_is_an_error(self) -> None:
        done = self.run_cli("remove", str(self.home / ".never-added"))
        self.assertEqual(2, done.returncode)
        self.assertIn("not managed", done.stderr)

    def test_removing_something_outside_home_is_an_error(self) -> None:
        done = self.run_cli("remove", "/etc/hostname")
        self.assertEqual(2, done.returncode)
        self.assertIn("outside", done.stderr)


class TestList(Machine):
    def setUp(self) -> None:
        super().setUp()
        self.init()

    def test_an_empty_repository_says_so(self) -> None:
        done = self.run_cli("status", "--all")
        self.assertEqual(0, done.returncode)
        self.assertIn("nothing is managed", done.stdout)

    def test_managed_files_are_listed(self) -> None:
        self.write(self.home / ".bashrc", "x")
        self.write(self.home / ".config" / "nvim" / "init.lua", "x")
        self.run_cli("add", str(self.home / ".bashrc"), str(self.home / ".config"))
        done = self.run_cli("status", "--all")
        self.assertIn(".bashrc", done.stdout)
        self.assertIn(".config/nvim/init.lua", done.stdout)
        self.assertIn("2 files managed", done.stdout)

    def test_the_settings_file_is_not_listed_as_managed(self) -> None:
        """`init` committed `.tupferl/config.toml`. It is tupferl's, not a
        dotfile, and listing it would also make it removable by name."""
        done = self.run_cli("status", "--all")
        self.assertNotIn("config.toml", done.stdout)

    def test_an_overlay_file_is_marked(self) -> None:
        self.write(self.home / ".gitconfig", "[user]\n")
        self.run_cli("add", "--host", str(self.home / ".gitconfig"))
        done = self.run_cli("status", "--all")
        self.assertIn("host  .gitconfig", done.stdout)
        self.assertIn("1 from this host's overlay", done.stdout)


class TestTwoMachines(support.SandboxCase):
    """One remote, two homes, two hostnames.

    A host overlay that silently applied everywhere would pass every
    single-machine test in this file. This is the fixture that can tell the
    difference, so it is the one the overlay's guarantee is asserted against.
    """

    def setUp(self) -> None:
        super().setUp()
        self.remote = support.make_remote(self.tmp / "remote.git", self.env)
        self.homes = {}
        self.envs = {}
        for host in ("laptop", "desktop"):
            home = self.tmp / host
            home.mkdir()
            support.seed_home(home, host)
            self.homes[host] = home
            self.envs[host] = support.sandbox_env(home, host)
            done = support.run_cli(["init", str(self.remote)], self.envs[host])
            self.assertEqual(0, done.returncode, done.stdout + done.stderr)

    def repo_of(self, host: str) -> Path:
        """Derived from the sandbox's own `XDG_DATA_HOME` rather than spelled out.

        `paths.repo_dir()` cannot be used: it reads the ambient environment,
        which here belongs to neither machine. Taking the base from the env this
        fixture built leaves only `paths`' own suffix repeated.
        """
        return Path(self.envs[host]["XDG_DATA_HOME"]) / "tupferl" / "repo"

    def test_each_host_writes_into_its_own_overlay(self) -> None:
        for host in ("laptop", "desktop"):
            (self.homes[host] / ".gitconfig").write_text(support.gitconfig(host), "utf-8")
            done = support.run_cli(
                ["add", "--host", str(self.homes[host] / ".gitconfig")], self.envs[host]
            )
            self.assertEqual(0, done.returncode, done.stdout + done.stderr)

        for host in ("laptop", "desktop"):
            overlay = paths.host_overlay(self.repo_of(host), host) / ".gitconfig"
            self.assertTrue(overlay.is_file(), f"{host} did not write its own overlay")
            self.assertIn(host, overlay.read_text(encoding="utf-8"))

    def test_a_host_lists_only_its_own_overlay(self) -> None:
        """The assertion a single-machine test cannot make. `desktop`'s overlay
        is put into `laptop`'s repository by hand — as a sync would — and must
        not appear in `laptop`'s listing."""
        laptop = self.repo_of("laptop")
        theirs = paths.host_overlay(laptop, "desktop") / ".gitconfig"
        theirs.parent.mkdir(parents=True, exist_ok=True)
        theirs.write_text(support.gitconfig("desktop"), encoding="utf-8")

        done = support.run_cli(["status", "--all"], self.envs["laptop"])
        self.assertIn("nothing is managed", done.stdout)

    def test_an_overlay_replaces_the_shared_file_for_that_host(self) -> None:
        """Plan §3.3, from the listing's point of view: one name, marked."""
        laptop = self.repo_of("laptop")
        (laptop / ".gitconfig").write_text(support.gitconfig("shared"), encoding="utf-8")
        mine = paths.host_overlay(laptop, "laptop") / ".gitconfig"
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text(support.gitconfig("laptop"), encoding="utf-8")

        done = support.run_cli(["status", "--all"], self.envs["laptop"])
        self.assertEqual(1, done.stdout.count(".gitconfig"), done.stdout)
        self.assertIn("host  .gitconfig", done.stdout)


class TestTheExitStatusEachCommandReturns(Machine):
    """Every command returns the status rather than calling `sys.exit`, which is
    what lets these run in-process.

    Asserted here because the subprocess tests *cannot* see it: `sys.exit(None)`
    and `sys.exit(0)` both exit 0, so a command that stopped returning a status
    would pass every one of them. The mutation sweep found seven such lines at
    once, which is what a whole class of unobservable code looks like.
    """

    def setUp(self) -> None:
        super().setUp()
        self.write(self.home / ".bashrc", "x")

    def quietly(self, run: object) -> int:
        with support.quiet():
            return int(run())  # type: ignore[operator]

    def test_init(self) -> None:
        self.assertEqual(0, self.quietly(lambda: manage.init(str(self.remote))))

    def test_add(self) -> None:
        self.quietly(lambda: manage.init(str(self.remote)))
        self.assertEqual(0, self.quietly(lambda: manage.add([str(self.home / ".bashrc")], False)))

    def test_add_when_nothing_changed(self) -> None:
        """The early return, which is a different line from the one above."""
        self.quietly(lambda: manage.init(str(self.remote)))
        self.quietly(lambda: manage.add([str(self.home / ".bashrc")], False))
        self.assertEqual(0, self.quietly(lambda: manage.add([str(self.home / ".bashrc")], False)))

    def test_remove(self) -> None:
        self.quietly(lambda: manage.init(str(self.remote)))
        self.quietly(lambda: manage.add([str(self.home / ".bashrc")], False))
        self.assertEqual(0, self.quietly(lambda: manage.remove(str(self.home / ".bashrc"), False)))


class TestWhatEachCommandPrints(Machine):
    """The printed output is the product for `list`, and the record of what
    happened for the others. A command that stopped printing would pass every
    exit-status test in this file."""

    def setUp(self) -> None:
        super().setUp()
        self.init()
        self.write(self.home / ".bashrc", "x")

    def test_add_names_each_file_it_stored(self) -> None:
        done = self.run_cli("add", str(self.home / ".bashrc"))
        self.assertIn("added .bashrc", done.stdout)
        # The negative half. `record` reports whether git had anything staged,
        # and a version that always answered "no" still printed "added" above --
        # it printed *both*, which is the shape only this assertion sees.
        self.assertNotIn("no change", done.stdout)

    def test_add_names_only_what_it_actually_stored(self) -> None:
        """Two files, one of them already stored. A fixture where every named
        file is also a stored file cannot tell "what changed" from "what was
        named" -- they are the same list."""
        self.run_cli("add", str(self.home / ".bashrc"))
        self.write(self.home / ".vimrc", "set nocompatible\n")
        self.run_cli("add", str(self.home / ".bashrc"), str(self.home / ".vimrc"))
        self.assertEqual(f"add from {self.host}: .vimrc", self.log()[0])

    def test_recording_a_stale_merge_base_does_not_claim_to_have_added(self) -> None:
        """`add` commits when the copies are identical but a snapshot is not --
        which is what an earlier run that died between the copy and the commit
        leaves. Naming the files there would describe something that did not
        happen, so the message says what did.

        The stale merge base has to be **committed**, and that took two goes.
        Deleting the snapshot only removes it from the working tree, so `add`
        rewrites the same bytes and the tree matches HEAD again; editing it
        without committing has exactly the same effect for the same reason. Both
        versions reached the branch they were written for not at all, and passed
        against the message they were written to reject.
        """
        self.write(self.home / ".vimrc", "set nocompatible\n")
        self.run_cli("add", str(self.home / ".bashrc"), str(self.home / ".vimrc"))
        self.snapshot(".bashrc").write_text("an older merge base\n", encoding="utf-8")
        support.git(["commit", "-qam", "a merge base from an older run"], self.repo, self.env)

        done = self.run_cli("add", str(self.home / ".vimrc"), str(self.home / ".bashrc"))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertEqual(f"add from {self.host}: record the merge base for 2 files", self.log()[0])
        self.assertTrue(self.snapshot(".bashrc").is_file())

    def test_add_commits_what_it_stored_and_not_what_it_found(self) -> None:
        """`git add --all --` with an *empty* pathspec stages the whole
        repository -- measured, and the reason dropping `add`'s staging list
        changed nothing observable until this test existed. `sync` stages
        everything on purpose; `add` must not, or a file dropped in the
        repository by hand rides along in a commit that names something else.
        """
        self.run_cli("add", str(self.home / ".bashrc"))
        stray = self.repo / "not-mine.txt"
        stray.write_text("dropped here by hand\n", encoding="utf-8")

        self.write(self.home / ".vimrc", "set nocompatible\n")
        self.run_cli("add", str(self.home / ".vimrc"))
        staged = support.git(["status", "--porcelain"], self.repo, self.env)
        self.assertIn("not-mine.txt", staged, "the stray file was committed by `add`")

    def test_add_marks_the_overlay(self) -> None:
        """`(host)` is the only thing distinguishing the two destinations in the
        output, and the destinations are otherwise invisible to the user."""
        done = self.run_cli("add", "--host", str(self.home / ".bashrc"))
        self.assertIn("added .bashrc (host)", done.stdout)

    def test_remove_says_the_original_was_left_alone(self) -> None:
        """The sentence that stops someone thinking `remove` deleted their
        dotfile — which is the first thing the name suggests."""
        self.run_cli("add", str(self.home / ".bashrc"))
        done = self.run_cli("remove", str(self.home / ".bashrc"))
        self.assertIn("was not touched", done.stdout)

    def test_init_says_what_it_did_and_what_to_do_next(self) -> None:
        """Three lines, and each is the only place the user learns something:
        where the repository went, that a settings file was created for them,
        and what to type next. `TestInit` asserts the *effects*; these are the
        words, which nothing else covers."""
        with support.tempdir() as box:
            home = box / "home"
            home.mkdir()
            support.seed_home(home)
            env = support.sandbox_env(home)
            # Its own remote, and freshly empty. Since milestone 3 `init` ends in
            # a sync, so it *pushes* -- and the class's shared remote has already
            # been initialised into by `setUp`, which makes it no longer empty.
            done = support.run_cli(["init", str(support.make_remote(box / "r.git", env))], env)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("cloned", done.stdout)
        self.assertIn("the remote was empty", done.stdout)
        self.assertIn("tupferl add", done.stdout)

    def test_list_counts_what_it_showed(self) -> None:
        self.run_cli("add", str(self.home / ".bashrc"))
        done = self.run_cli("status", "--all")
        self.assertIn(
            "1 file managed, 0 to change, 0 in conflict, 0 from this host's overlay",
            done.stdout,
        )

    def test_list_counts_the_overlay_separately(self) -> None:
        """0 and 1 rather than 1 and 1: with equal counts a swapped pair still
        reads correctly."""
        self.write(self.home / ".gitconfig-extra", "x")
        self.run_cli("add", str(self.home / ".bashrc"))
        self.run_cli("add", "--host", str(self.home / ".gitconfig-extra"))
        done = self.run_cli("status", "--all")
        self.assertIn(
            "2 files managed, 0 to change, 0 in conflict, 1 from this host's overlay",
            done.stdout,
        )


class TestCommitMessages(unittest.TestCase):
    def test_the_plans_shape(self) -> None:
        """Plan §3.5: `<what> from <hostname>: <names>`."""
        names = [PurePosixPath(".bashrc"), PurePosixPath(".gitconfig")]
        found = manage.describe("add", names, "laptop")
        self.assertEqual("add from laptop: .bashrc, .gitconfig", found)

    def test_a_long_list_is_summarised(self) -> None:
        """`git log --oneline` after adding a directory of two hundred files
        should still be readable."""
        names = [PurePosixPath(f".f{n}") for n in range(9)]
        found = manage.describe("add", names, "laptop")
        self.assertIn("and 4 more", found)
        self.assertNotIn(".f8", found)

    def test_the_boundary_names_everything(self) -> None:
        names = [PurePosixPath(f".f{n}") for n in range(manage.NAMED_IN_MESSAGE)]
        self.assertNotIn("more", manage.describe("add", names, "laptop"))


class TestModes(support.SandboxCase):
    def test_an_executable_file_is_stored_executable(self) -> None:
        script = self.write(self.home / "script.sh", "#!/bin/sh\n")
        script.chmod(0o700)
        self.assertEqual(0o755, copies.mode_for(script))

    def test_a_plain_file_is_not(self) -> None:
        self.assertEqual(0o644, copies.mode_for(self.write(self.home / "plain", "x")))

    def test_storing_something_that_stopped_being_a_file_is_an_error(self) -> None:
        """`manifest.check` saw a regular file; by the time the copy is made it
        is a fifo. That is a race rather than a rule the caller broke -- and
        answering `None` would report it as "nothing to do", which is how a file
        the user asked to manage ends up silently unmanaged."""
        where = self.home / "vanished"
        os.mkfifo(where)
        with self.assertRaises(OSError) as caught:
            copies.store(where, self.tmp / "target")
        self.assertIn("vanished", str(caught.exception))

    def test_executable_by_anyone_counts(self) -> None:
        """0o711 arrives from tarballs and is a script. Storing it
        non-executable puts it back unrunnable on the other machine."""
        script = self.write(self.home / "odd.sh", "#!/bin/sh\n")
        script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXOTH)
        self.assertEqual(0o755, copies.mode_for(script))


class TestOpenRepo(support.SandboxCase):
    def test_a_directory_that_is_not_a_repository_is_told_apart(self) -> None:
        """From "nothing here yet", which needs a different answer: run `init`
        over an empty path, look at a non-empty one first."""
        paths.repo_dir().mkdir(parents=True)
        (paths.repo_dir() / "stray").write_text("x", encoding="utf-8")
        with self.assertRaises(TupferlError) as caught:
            manage.open_repo()
        self.assertIn("not a git repository", str(caught.exception))

    def test_the_settings_come_back_with_it(self) -> None:
        """So a command reads the config once, rather than each of them
        deciding for itself where it lives."""
        support.make_repo(paths.repo_dir(), self.env)
        settings = paths.config_file(paths.repo_dir())
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text('editor = "helix"\n', encoding="utf-8")
        _, config = manage.open_repo()
        self.assertEqual("helix", config.editor)


class TestCounting(unittest.TestCase):
    def test_the_plural(self) -> None:
        self.assertEqual("1 file", manage.count(1))
        self.assertEqual("2 files", manage.count(2))
        self.assertEqual("0 files", manage.count(0))

    def test_a_second_noun_gets_the_same_rule(self) -> None:
        """`status` counts commits. Zero as well as one and many, because zero
        is where "1 file" logic written as `many > 1` goes wrong and it is the
        count a machine that agrees with its remote would have."""
        self.assertEqual("1 commit", manage.count(1, "commit"))
        self.assertEqual("2 commits", manage.count(2, "commit"))
        self.assertEqual("0 commits", manage.count(0, "commit"))

    def test_the_default_noun_is_still_files(self) -> None:
        """The argument was added in milestone 6, and every existing caller
        passes none. A default that had changed would rewrite `sync`'s report
        and `list`'s tail line without either of them being touched."""
        self.assertEqual(manage.count(3), manage.count(3, "file"))


class TestWhatAddSays(unittest.TestCase):
    """`manage.stored`, which is a decision about text and needs no repository.

    The end-to-end half is in `TestAddingADirectoryOfMany` below. This is where
    the two shapes and the split between the words are pinned, because five
    fixtures for five cases would be five real `add` runs.
    """

    def names(self, many: int, prefix: str = "f") -> list[PurePosixPath]:
        return [PurePosixPath(f".config/{prefix}{n:03d}.conf") for n in range(many)]

    def test_a_short_list_names_every_file(self) -> None:
        """Unchanged behaviour, and most runs. `NAMED_ONE_BY_ONE` files is the
        boundary and it is included, not excluded."""
        lines = manage.stored({"added": self.names(manage.NAMED_ONE_BY_ONE)}, to_host=False)
        self.assertEqual(manage.NAMED_ONE_BY_ONE, len(lines))
        self.assertTrue(all(line.startswith("added .config/") for line in lines), lines)

    def test_one_more_than_that_is_summarised(self) -> None:
        """The other side of the boundary, so `<=` written as `<` fails here."""
        lines = manage.stored({"added": self.names(manage.NAMED_ONE_BY_ONE + 1)}, to_host=False)
        self.assertEqual(1, len(lines), lines)
        self.assertIn(f"added {manage.NAMED_ONE_BY_ONE + 1} files", lines[0])

    def test_the_summary_names_a_few_and_counts_the_rest(self) -> None:
        """Through `a_few`, the same rule the commit message uses -- two
        thresholds would be two answers to "is this too long to read?"."""
        lines = manage.stored({"added": self.names(100)}, to_host=False)
        self.assertIn(".config/f000.conf", lines[0])
        self.assertIn(f"and {100 - manage.NAMED_IN_MESSAGE} more", lines[0])
        self.assertNotIn(".config/f099.conf", lines[0])

    def test_added_and_updated_are_never_counted_together(self) -> None:
        """The constraint that makes this more than a `len()`.

        `copies.store` answers "added", "updated" or `None`, and a run that
        stored one new file and rewrote ninety-nine must not report a hundred of
        either. `manage.added` carries the same split for the commit message,
        and its docstring says why.
        """
        lines = manage.stored(
            {"added": self.names(1, "new"), "updated": self.names(99, "old")}, to_host=False
        )
        self.assertEqual(2, len(lines), lines)
        self.assertIn("added 1 file", lines[0])
        self.assertIn("updated 99 files", lines[1])

    def test_the_words_come_out_in_the_same_order_whatever_went_in(self) -> None:
        """`stored` sorts by the word, so two machines that did the same things
        print the same lines. Insertion order is whichever file happened to sort
        first, which is not a property of the run.

        The fixture inserts **updated before added**, because a dict built the
        other way round is already in sorted order and cannot tell a sort from
        no sort at all -- CLAUDE.md §2's two symmetric inputs. The mutation
        sweep found exactly that: `sorted` becoming `list`, twice, and a
        reversed ordering, all three surviving.
        """
        backwards = {"updated": self.names(1, "u"), "added": self.names(1, "a")}
        for shape, lines in (
            ("short", manage.stored(backwards, to_host=False)),
            (
                "long",
                manage.stored(
                    {"updated": self.names(20, "u"), "added": self.names(20, "a")}, to_host=False
                ),
            ),
        ):
            with self.subTest(shape=shape):
                self.assertEqual(2, len(lines), lines)
                self.assertTrue(lines[0].startswith("added"), lines)
                self.assertTrue(lines[1].startswith("updated"), lines)

    def test_nothing_stored_says_nothing(self) -> None:
        """Every file was already byte-for-byte identical, so `store` answered
        `None` for all of them and none reaches here. `add` then prints its own
        "no change" sentence, which this must not pre-empt."""
        self.assertEqual([], manage.stored({}, to_host=False))

    def test_the_host_marker_survives_both_shapes(self) -> None:
        """`add --host` marks its lines, and a summary that dropped the mark
        would say a shared file was stored when an overlay was."""
        short = manage.stored({"added": self.names(2)}, to_host=True)
        long = manage.stored({"added": self.names(50)}, to_host=True)
        self.assertTrue(all("(host)" in line for line in short), short)
        self.assertIn("(host)", long[0])


class TestSayingTheWorkIsNotSharedYet(support.SandboxCase):
    """`add` and `remove` commit locally and do not push, so until a sync runs
    the change exists on this machine and nowhere else.

    Issue #60 asked whether they should sync by themselves. They should not: a
    sync can stop at a conflict prompt and open `$EDITOR`, so `tupferl add
    .bashrc` would be able to pause and ask about an unrelated file. What was
    missing is that neither command *said* so -- both report success, which is
    the whole reason the gap is easy to miss.
    """

    def setUp(self) -> None:
        super().setUp()
        remote = support.make_remote(self.tmp / "remote.git", self.env)
        self.assertEqual(0, self.call("init", str(remote)))
        (self.home / ".bashrc").write_text("one\n", encoding="utf-8")

    def call(self, *argv: str) -> int:
        with support.quiet():
            return cli.main(list(argv))

    def spoken(self, *argv: str) -> str:
        with support.quiet() as said:
            self.assertEqual(0, cli.main(list(argv)), said.getvalue())
        return said.getvalue()

    def test_add_says_the_file_is_not_shared_yet(self) -> None:
        self.assertIn(manage.NOT_SHARED, self.spoken("add", str(self.home / ".bashrc")))

    def test_it_names_the_command_that_would_share_it(self) -> None:
        """**Not `assertIn(manage.NOT_SHARED, ...)`, which is the constant
        compared with itself.** Every other test here asserts the message
        *arrives*, and all of them go on passing if it is shortened to "not
        shared yet" -- measured: that mutation survived them.

        What the line is for is telling someone who has just been told their
        `add` succeeded what to do next, so the command name is the part worth
        pinning. Read from the output rather than from the constant.
        """
        said = self.spoken("add", str(self.home / ".bashrc"))
        advice = next(line for line in said.splitlines() if "not shared" in line)
        self.assertIn("tupferl sync", advice)

    def test_remove_says_it_too(self) -> None:
        """The same gap in the other direction: the file is gone from this
        machine's repository and still on every other one."""
        self.assertEqual(0, self.call("add", str(self.home / ".bashrc")))
        self.assertIn(manage.NOT_SHARED, self.spoken("remove", str(self.home / ".bashrc")))

    def test_an_add_that_changed_nothing_does_not_send_the_user_to_sync(self) -> None:
        """The arm where nothing was committed. There is no work waiting, so
        the advice would be to run a sync with nothing in it -- and a line that
        appears whether or not it means anything is one nobody reads.

        This is the half that makes the two tests above assertions rather than
        a line that is always printed.
        """
        self.assertEqual(0, self.call("add", str(self.home / ".bashrc")))
        again = self.spoken("add", str(self.home / ".bashrc"))
        self.assertIn("no change", again)
        self.assertNotIn(manage.NOT_SHARED, again)


class TestAddingADirectoryOfMany(support.SandboxCase):
    """#28 end to end: the README's own example is a directory of hundreds."""

    def setUp(self) -> None:
        super().setUp()
        remote = support.make_remote(self.tmp / "remote.git", self.env)
        with support.quiet():
            self.assertEqual(0, cli.main(["init", str(remote)]))

    def added(self, *paths: str) -> str:
        with support.quiet() as said:
            self.assertEqual(0, cli.main(["add", *paths]))
        return said.getvalue()

    def summary(self, *paths: str) -> list[str]:
        """The lines `add` writes *about the files*, without the trailing
        advisory.

        `NOT_SHARED` is one line on every successful `add`, and this class
        counts lines to check that a hundred files summarise to one. Filtered
        by identity rather than by position, so a future line added anywhere
        does not silently shift what these tests believe they are counting.
        """
        return [
            line
            for line in self.added(*paths).splitlines()
            if line.strip() and line != manage.NOT_SHARED
        ]

    def test_a_hundred_files_are_one_line(self) -> None:
        where = self.home / ".local" / "share" / "app"
        where.mkdir(parents=True)
        for number in range(100):
            (where / f"f{number:03d}.conf").write_text("x\n", encoding="utf-8")

        said = self.summary(str(where))
        self.assertEqual(1, len(said), said)
        self.assertIn("added 100 files", said[0])

    def test_a_re_add_reports_only_what_changed(self) -> None:
        """The case the summary must not get wrong: 100 files, two edited.

        The other 98 are byte-for-byte identical, so `store` answers `None` and
        they are silent -- which puts the run back under `NAMED_ONE_BY_ONE` and
        names the two. A summary counting all hundred would tell the user it had
        stored ninety-eight files it did not touch.
        """
        where = self.home / ".local" / "share" / "app"
        where.mkdir(parents=True)
        for number in range(100):
            (where / f"f{number:03d}.conf").write_text("x\n", encoding="utf-8")
        self.added(str(where))

        for number in (0, 1):
            (where / f"f{number:03d}.conf").write_text("changed\n", encoding="utf-8")
        said = self.summary(str(where))
        self.assertEqual(2, len(said), said)
        self.assertTrue(all(line.startswith("updated ") for line in said), said)

    def test_refusals_are_still_one_line_each(self) -> None:
        """The part a long listing used to push off the screen, and the reason
        this issue is about noise rather than tidiness."""
        where = self.home / ".local" / "share" / "app"
        where.mkdir(parents=True)
        for number in range(20):
            (where / f"f{number:03d}.conf").write_text("x\n", encoding="utf-8")
        (where / "link").symlink_to(self.home / ".bashrc")
        (where / "big.bin").write_bytes(b"x" * (2 << 20))

        said = self.added(str(where))
        skipped = [line for line in said.splitlines() if line.startswith("skipped ")]
        self.assertEqual(2, len(skipped), said)
        self.assertIn("added 20 files", said)


class TestAddingSomethingThatHoldsACredential(support.SandboxCase):
    """#35 end to end: refused by name, skipped in a walk, allowed by `--anyway`.

    The three shapes are different code paths -- `check` raises for a named file,
    `collect` turns the same refusal into a `Refused` for a walked one, and the
    flag has to reach both. A test of only the first would leave
    `tupferl add ~/.ssh` pushing the key it is most likely to meet.
    """

    def setUp(self) -> None:
        super().setUp()
        remote = support.make_remote(self.tmp / "remote.git", self.env)
        with support.quiet():
            self.assertEqual(0, cli.main(["init", str(remote)]))
        self.ssh = self.home / ".ssh"
        self.ssh.mkdir()
        for name, body in (
            ("id_ed25519", "PRIVATE KEY\n"),
            ("id_ed25519.pub", "ssh-ed25519 AAAA\n"),
            ("config", "Host *\n"),
            ("known_hosts", "example.com ssh-rsa AAAA\n"),
        ):
            (self.ssh / name).write_text(body, encoding="utf-8")

    def add(self, *args: str) -> tuple[int, str]:
        with support.quiet() as said:
            return cli.main(["add", *args]), said.getvalue()

    def stored(self) -> set[str]:
        repo = paths.repo_dir()
        return {
            str(path.relative_to(repo)) for path in (repo / ".ssh").rglob("*") if path.is_file()
        }

    def test_naming_the_key_is_refused(self) -> None:
        status, said = self.add(str(self.ssh / "id_ed25519"))
        self.assertEqual(2, status, said)
        self.assertIn(".ssh/id_*", said)
        self.assertIn("--anyway", said)
        self.assertFalse((paths.repo_dir() / ".ssh").exists(), "it was stored anyway")

    def test_the_message_says_what_the_danger_is(self) -> None:
        """Not "this looks like a secret", which tells a user nothing they can
        act on. The reason tupferl refuses is that it stores plaintext and
        pushes it, and that is the sentence."""
        said = self.add(str(self.ssh / "id_ed25519"))[1]
        self.assertIn("plaintext", said)
        self.assertIn("remote", said)

    def test_walking_the_directory_skips_it_and_keeps_the_rest(self) -> None:
        """`collect`'s half. Refusing the whole walk would be the wrong answer:
        `.ssh/config` and `known_hosts` are exactly what someone adding `~/.ssh`
        wants, and the public key is public."""
        status, said = self.add(str(self.ssh))
        self.assertEqual(0, status, said)
        self.assertIn("skipped", said)
        self.assertIn("id_ed25519", said)
        self.assertEqual({".ssh/config", ".ssh/known_hosts", ".ssh/id_ed25519.pub"}, self.stored())

    def test_anyway_stores_it(self) -> None:
        """The refusal has to be overrulable, or it is worked around by moving
        the file -- which is worse for the user and teaches them to distrust the
        rule."""
        status, said = self.add("--anyway", str(self.ssh / "id_ed25519"))
        self.assertEqual(0, status, said)
        self.assertIn(".ssh/id_ed25519", self.stored())

    def test_anyway_reaches_a_directory_walk_too(self) -> None:
        """The flag threads through `collect`, not only `check`. Wired to one of
        them, `tupferl add --anyway ~/.ssh` silently keeps skipping the file the
        user just said to store."""
        status, said = self.add("--anyway", str(self.ssh))
        self.assertEqual(0, status, said)
        self.assertNotIn("skipped", said)
        self.assertIn(".ssh/id_ed25519", self.stored())


class TestRemoveTakesTheNameListPrints(support.TwoMachines):
    """#27's other caller. `remove` goes through `manifest.relative` too.

    The unit cases are in `test_manifest.TestTurningWhatWasTypedIntoAName`;
    this is the end-to-end half, and it exists because the two commands used to
    share a bug and could as easily share a fix that reached only one of them.
    """

    def test_a_name_from_list_is_removed(self) -> None:
        """The working directory is set away from `$HOME` deliberately: from
        `$HOME` the old cwd-relative reading happened to be right, which is why
        a suite that drives everything from a sandbox never saw this."""
        listed = self.first.say("status", "--all")[1]
        self.assertIn(".bashrc", listed)

        with mock.patch.object(Path, "cwd", return_value=self.tmp):
            status, said = self.first.say("remove", ".bashrc")
        self.assertEqual(0, status, said)
        self.assertIn("removed .bashrc", said)
        self.assertFalse((self.first.repo / ".bashrc").exists())
        # Plan §4: `remove` keeps the file in `$HOME`.
        self.assertTrue((self.first.home / ".bashrc").is_file())


if __name__ == "__main__":
    unittest.main()
