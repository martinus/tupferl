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

import stat
import subprocess
import unittest
from pathlib import Path, PurePosixPath

from tests import support
from tupferl import gitrepo, manage, manifest, paths
from tupferl.config import Config, load
from tupferl.errors import TupferlError


class Machine(support.SandboxCase):
    """A sandboxed home with a bare remote beside it, and the CLI pointed there."""

    def setUp(self) -> None:
        super().setUp()
        self.remote = support.make_remote(self.tmp / "remote.git", self.env)
        self.repo = paths.repo_dir()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return support.run_cli(list(args), self.env)

    def init(self) -> subprocess.CompletedProcess[str]:
        done = self.run_cli("init", str(self.remote))
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        return done

    def log(self) -> list[str]:
        return support.git(["log", "--format=%s"], self.repo, self.env).splitlines()

    def stored(self, name: str, host: bool = False) -> Path:
        """Where a managed file lives, from `manifest.roots` rather than from a
        ternary retyped here -- a test that spells the rule out itself cannot
        notice the rule changing."""
        tree, overlay = manifest.roots(self.repo, support.HOST)
        return (overlay if host else tree) / name


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

    def test_a_remote_with_content_is_cloned_untouched(self) -> None:
        """The second-machine path. Nothing is written and nothing is committed:
        the repository already has a shape, and `init` inventing a settings file
        here would be a commit the user did not ask for."""
        first = support.make_repo(self.tmp / "seed", self.env, remote=self.remote)
        (first / ".bashrc").write_text("export EDITOR=nvim\n", encoding="utf-8")
        support.git(["add", "-A"], first, self.env)
        support.git(["commit", "-m", "seeded"], first, self.env)
        support.git(["push"], first, self.env)

        self.init()
        stored = (self.repo / ".bashrc").read_text(encoding="utf-8")
        self.assertEqual("export EDITOR=nvim\n", stored)
        self.assertEqual(["seeded", "initial"], self.log())

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
        done = self.run_cli("list")
        self.assertEqual(0, done.returncode)
        self.assertIn("nothing is managed", done.stdout)

    def test_managed_files_are_listed(self) -> None:
        self.write(self.home / ".bashrc", "x")
        self.write(self.home / ".config" / "nvim" / "init.lua", "x")
        self.run_cli("add", str(self.home / ".bashrc"), str(self.home / ".config"))
        done = self.run_cli("list")
        self.assertIn(".bashrc", done.stdout)
        self.assertIn(".config/nvim/init.lua", done.stdout)
        self.assertIn("2 managed", done.stdout)

    def test_the_settings_file_is_not_listed_as_managed(self) -> None:
        """`init` committed `.tupferl/config.toml`. It is tupferl's, not a
        dotfile, and listing it would also make it removable by name."""
        done = self.run_cli("list")
        self.assertNotIn("config.toml", done.stdout)

    def test_an_overlay_file_is_marked(self) -> None:
        self.write(self.home / ".gitconfig", "[user]\n")
        self.run_cli("add", "--host", str(self.home / ".gitconfig"))
        done = self.run_cli("list")
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

        done = support.run_cli(["list"], self.envs["laptop"])
        self.assertIn("nothing is managed", done.stdout)

    def test_an_overlay_replaces_the_shared_file_for_that_host(self) -> None:
        """Plan §3.3, from the listing's point of view: one name, marked."""
        laptop = self.repo_of("laptop")
        (laptop / ".gitconfig").write_text(support.gitconfig("shared"), encoding="utf-8")
        mine = paths.host_overlay(laptop, "laptop") / ".gitconfig"
        mine.parent.mkdir(parents=True, exist_ok=True)
        mine.write_text(support.gitconfig("laptop"), encoding="utf-8")

        done = support.run_cli(["list"], self.envs["laptop"])
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
        self.assertEqual(0, self.quietly(lambda: manage.remove(str(self.home / ".bashrc"))))

    def test_list_when_empty(self) -> None:
        self.quietly(lambda: manage.init(str(self.remote)))
        self.assertEqual(0, self.quietly(manage.listing))

    def test_list_with_files(self) -> None:
        """The other branch of `listing`, and the one that counts the overlay."""
        self.quietly(lambda: manage.init(str(self.remote)))
        self.quietly(lambda: manage.add([str(self.home / ".bashrc")], False))
        self.assertEqual(0, self.quietly(manage.listing))


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
            done = support.run_cli(["init", str(self.remote)], env)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn("cloned", done.stdout)
        self.assertIn("the remote was empty", done.stdout)
        self.assertIn("tupferl add", done.stdout)

    def test_list_counts_what_it_showed(self) -> None:
        self.run_cli("add", str(self.home / ".bashrc"))
        done = self.run_cli("list")
        self.assertIn("1 managed, 0 from this host's overlay", done.stdout)

    def test_list_counts_the_overlay_separately(self) -> None:
        """0 and 1 rather than 1 and 1: with equal counts a swapped pair still
        reads correctly."""
        self.write(self.home / ".gitconfig-extra", "x")
        self.run_cli("add", str(self.home / ".bashrc"))
        self.run_cli("add", "--host", str(self.home / ".gitconfig-extra"))
        done = self.run_cli("list")
        self.assertIn("2 managed, 1 from this host's overlay", done.stdout)


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
        self.assertEqual(0o755, manage.mode_for(script))

    def test_a_plain_file_is_not(self) -> None:
        self.assertEqual(0o644, manage.mode_for(self.write(self.home / "plain", "x")))

    def test_executable_by_anyone_counts(self) -> None:
        """0o711 arrives from tarballs and is a script. Storing it
        non-executable puts it back unrunnable on the other machine."""
        script = self.write(self.home / "odd.sh", "#!/bin/sh\n")
        script.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXOTH)
        self.assertEqual(0o755, manage.mode_for(script))


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


if __name__ == "__main__":
    unittest.main()
