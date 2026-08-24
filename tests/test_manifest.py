"""The admission rules: what may be managed, and what must never be.

Four of the six rules exist because `tupferl add` copies into a repository that
gets pushed. So the fixtures here are the hostile ones — a symlink pointing at a
file outside `$HOME`, a path that climbs out with `..`, a directory that contains
tupferl's own repository — and each asserts on *which* rule refused, not merely
that something did. A test that only checks "this raised" passes just as well
against a `check` that refuses everything, and would not notice the rules being
applied in an order that blames the wrong one.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path, PurePosixPath

from tests import support
from tupferl import manifest, paths
from tupferl.config import Config
from tupferl.errors import TupferlError


class ManifestCase(support.SandboxCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = paths.repo_dir()
        self.repo.mkdir(parents=True)
        self.config = Config()

    def check(self, path: Path, config: Config | None = None) -> PurePosixPath:
        return manifest.check(path, self.home, self.repo, config or self.config)

    def refusal(self, path: Path, config: Config | None = None) -> str:
        with self.assertRaises(TupferlError) as caught:
            self.check(path, config)
        return str(caught.exception)


class TestWhatIsAdmitted(ManifestCase):
    def test_an_ordinary_dotfile(self) -> None:
        self.assertEqual(
            PurePosixPath(".bashrc"), self.check(self.write(self.home / ".bashrc", "x"))
        )

    def test_one_nested_in_directories(self) -> None:
        """The stored name is the whole path relative to `$HOME`, which is plan
        §3.2's entire mapping rule."""
        where = self.write(self.home / ".config" / "nvim" / "init.lua", "x")
        self.assertEqual(PurePosixPath(".config/nvim/init.lua"), self.check(where))

    def test_a_directory(self) -> None:
        """Named directories are admitted so `collect` can walk them; the size
        and ignore rules apply to the files inside, not to the directory."""
        (self.home / ".config").mkdir(exist_ok=True)  # `seed_home` already made it
        self.assertEqual(PurePosixPath(".config"), self.check(self.home / ".config"))


class TestWhatIsRefused(ManifestCase):
    def test_a_symlink(self) -> None:
        """Plan §5. The target is outside `$HOME` on purpose: this is the shape
        where following the link would copy something the user never named."""
        outside = self.tmp / "secret"
        outside.write_text("token\n", encoding="utf-8")
        link = self.home / ".aws-credentials"
        link.symlink_to(outside)
        found = self.refusal(link)
        # On "is a symlink", not on "symlink": *both* refusals contain the bare
        # word, so asserting it alone passes with the direct check removed --
        # `links_between` then catches the same file one rule later and says the
        # path "goes through" a symlink, which is confusing when the path *is*
        # one. The mutation sweep found this; it is the third time this exact
        # shape has appeared in these tests.
        self.assertIn("is a symlink", found)
        self.assertNotIn("goes through", found)

    def test_a_path_through_a_symlinked_parent(self) -> None:
        """The same hazard one level up, and the one a naive check misses: the
        *file* is an ordinary file, and only its parent is a link out."""
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        (outside / "creds").write_text("token\n", encoding="utf-8")
        (self.home / ".aws").symlink_to(outside)
        self.assertIn("goes through the symlink", self.refusal(self.home / ".aws" / "creds"))

    def test_a_path_that_climbs_out_of_home(self) -> None:
        """`..` is collapsed lexically before the check, so this is refused for
        being outside `$HOME` rather than accepted and stored under a name with
        `..` in it."""
        outside = self.tmp / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        climbed = manifest.named(self.home / ".." / outside.name)
        self.assertIn("outside", self.refusal(climbed))

    def test_a_file_that_is_simply_elsewhere(self) -> None:
        self.assertIn("outside", self.refusal(self.write(self.tmp / "plain.txt", "x")))

    def test_the_repository_itself(self) -> None:
        self.assertIn("own repository", self.refusal(self.repo))

    def test_a_directory_that_contains_the_repository(self) -> None:
        """`~/.local` holds the default repository, so adding it would walk into
        tupferl's own copies and manage them."""
        self.assertIn("own repository", self.refusal(self.home / ".local"))

    def test_something_that_is_not_a_file(self) -> None:
        """A named pipe. The realistic instance is a socket -- `~/.gnupg/S.agent`
        is one, in exactly the directory somebody would think to add -- but a
        socket cannot be bound at an arbitrary path: `sun_path` is 104 bytes on
        macOS, and the runner's temporary directories are long enough that
        binding inside a sandboxed repository fails with `OSError` rather than
        testing anything. A fifo is the same class of "not a regular file" with
        no such limit, so it is what these fixtures use."""
        where = self.home / ".agent-pipe"
        os.mkfifo(where)
        self.assertIn("not a regular file", self.refusal(where))

    def test_a_file_that_is_not_there(self) -> None:
        self.assertIn("does not exist", self.refusal(self.home / ".absent"))

    def test_one_over_the_size_limit(self) -> None:
        big = self.write(self.home / ".big", "x" * 100)
        self.assertIn("over the", self.refusal(big, Config(max_file_size=99)))

    def test_one_exactly_at_the_limit_is_kept(self) -> None:
        """The boundary from the other side: `>` not `>=`, so a file the size of
        the limit is admitted. Without this the off-by-one is invisible."""
        edge = self.write(self.home / ".edge", "x" * 100)
        self.assertEqual(PurePosixPath(".edge"), self.check(edge, Config(max_file_size=100)))

    def test_a_directory_is_not_measured_against_the_size_limit(self) -> None:
        """A directory's `st_size` is its own bookkeeping — 4096 on ext4, more
        as it fills — and has nothing to do with what it holds. Applying the
        limit to it would refuse `~/.config` on any machine with a small
        `max_file_size`, for a number the user cannot see or change."""
        (self.home / ".config").mkdir(exist_ok=True)
        self.assertEqual(
            PurePosixPath(".config"), self.check(self.home / ".config", Config(max_file_size=1))
        )

    def test_one_matching_an_ignore_pattern(self) -> None:
        noisy = self.write(self.home / ".x.log", "x")
        self.assertIn("ignore", self.refusal(noisy, Config(ignore=["*.log"])))


class TestTheOrderOfTheRules(ManifestCase):
    def test_a_symlink_is_refused_as_a_symlink_even_when_ignored(self) -> None:
        """The safety rules run before the settings, so the message names the
        reason that matters. A file can satisfy two refusals at once, and which
        one the user is told decides what they do about it."""
        outside = self.tmp / "big.log"
        outside.write_text("x" * 100, encoding="utf-8")
        link = self.home / "linked.log"
        link.symlink_to(outside)
        found = self.refusal(link, Config(ignore=["*.log"], max_file_size=1))
        self.assertIn("symlink", found)
        self.assertNotIn("ignore", found)


class TestIgnorePatterns(unittest.TestCase):
    def test_a_pattern_hides_the_subtree_below_it(self) -> None:
        """`ignore = [".cache"]` must mean the whole directory. Otherwise it
        hides an entry nobody stores and the contents are pushed anyway."""
        self.assertTrue(manifest.ignored(PurePosixPath(".cache/chromium/big"), [".cache"]))

    def test_a_pattern_matches_at_any_depth(self) -> None:
        self.assertTrue(manifest.ignored(PurePosixPath(".local/state/x.log"), ["*.log"]))

    def test_an_unrelated_file_is_kept(self) -> None:
        """The other answer, so the two above are not satisfied by an `ignored`
        that says yes to everything."""
        self.assertFalse(manifest.ignored(PurePosixPath(".bashrc"), [".cache", "*.log"]))

    def test_no_patterns_ignore_nothing(self) -> None:
        self.assertFalse(manifest.ignored(PurePosixPath(".cache/x"), []))

    def test_matching_does_not_fold_case(self) -> None:
        """`fnmatch` folds case on macOS, which would make the same repository
        ignore different files on two machines. This is the half of the
        guarantee only a case-sensitive filesystem can show, but the assertion
        holds everywhere because `fnmatchcase` never folds."""
        self.assertFalse(manifest.ignored(PurePosixPath(".CACHE/x"), [".cache"]))


class TestCollecting(ManifestCase):
    def test_a_directory_yields_its_files_sorted(self) -> None:
        for name in ("b.conf", "a.conf", "sub/c.conf"):
            self.write(self.home / ".config" / name, "x")
        found, refused = manifest.collect(self.home / ".config", self.home, self.repo, self.config)
        self.assertEqual(
            [PurePosixPath(f".config/{n}") for n in ("a.conf", "b.conf", "sub/c.conf")], found
        )
        self.assertEqual([], refused)

    def test_the_refusals_come_back_rather_than_stopping_the_walk(self) -> None:
        """Adding `~/.config` with one socket in it must manage the rest. A walk
        that raised on the first problem would be unusable on a real machine."""
        self.write(self.home / ".config" / "good.conf", "x")
        (self.home / ".config" / "linked").symlink_to(self.tmp / "elsewhere")
        found, refused = manifest.collect(self.home / ".config", self.home, self.repo, self.config)
        self.assertEqual([PurePosixPath(".config/good.conf")], found)
        self.assertEqual(1, len(refused))
        self.assertIn("symlink", refused[0].why)

    def test_a_symlinked_directory_is_not_descended_into(self) -> None:
        """It would be refused file by file anyway; the point is not to *read* a
        tree the user never named, which for a link to `/` is most of the disk."""
        outside = self.tmp / "huge"
        (outside / "deep").mkdir(parents=True)
        (outside / "deep" / "file").write_text("x", encoding="utf-8")
        (self.home / ".linked-dir").symlink_to(outside)
        found, refused = manifest.collect(
            self.home / ".linked-dir", self.home, self.repo, self.config
        )
        self.assertEqual([], found)
        self.assertEqual([self.home / ".linked-dir"], [item.path for item in refused])


class TestWhatTheRepositoryHolds(ManifestCase):
    def test_nothing_at_first(self) -> None:
        self.assertEqual([], manifest.managed(self.repo, support.HOST))

    def test_shared_files_are_listed_unmarked(self) -> None:
        self.write(self.repo / ".bashrc", "x")
        self.write(self.repo / ".config" / "nvim" / "init.lua", "x")
        found = manifest.managed(self.repo, support.HOST)
        self.assertEqual(
            [PurePosixPath(".bashrc"), PurePosixPath(".config/nvim/init.lua")],
            [item.name for item in found],
        )
        self.assertEqual([False, False], [item.host for item in found])

    def test_tupferls_own_directory_is_not_managed(self) -> None:
        """`.tupferl/` holds settings, overlays and (from milestone 3) snapshots.
        Listing them as managed would also make them removable by name."""
        self.write(self.repo / paths.META / "config.toml", "")
        self.write(self.repo / ".bashrc", "x")
        found = manifest.managed(self.repo, support.HOST)
        self.assertEqual([PurePosixPath(".bashrc")], [item.name for item in found])

    def test_an_overlay_file_is_marked(self) -> None:
        self.write(paths.host_overlay(self.repo, support.HOST) / ".gitconfig", "x")
        found = manifest.managed(self.repo, support.HOST)
        self.assertEqual([PurePosixPath(".gitconfig")], [item.name for item in found])
        self.assertEqual([True], [item.host for item in found])

    def test_the_listing_is_sorted_rather_than_however_it_was_built(self) -> None:
        """The overlay is merged into the shared map *after* it, so insertion
        order is "shared first". A name from the overlay that sorts before a
        shared one is therefore the fixture that can tell `sorted` from `list`
        — with any other pair the two answers coincide and the sort is
        untested."""
        self.write(self.repo / ".zshrc", "x")
        self.write(paths.host_overlay(self.repo, support.HOST) / ".aaa", "x")
        found = manifest.managed(self.repo, support.HOST)
        self.assertEqual(
            [PurePosixPath(".aaa"), PurePosixPath(".zshrc")], [item.name for item in found]
        )

    def test_the_listing_comes_back_sorted_whatever_the_filesystem_says(self) -> None:
        """Five names created in an order no filesystem returns alphabetically.

        No skip and no dependence on readdir order: `managed` sorts
        unconditionally, so this is deterministic everywhere. An earlier version
        skipped when readdir happened to be sorted, which under CI's
        `--no-skips` would have turned a lucky filesystem into a red build.
        """
        for name in (".yyy", ".bbb", ".aaa", ".mmm", ".zshrc"):
            self.write(self.repo / name, "x")
        found = [str(item.name) for item in manifest.managed(self.repo, support.HOST)]
        self.assertEqual([".aaa", ".bbb", ".mmm", ".yyy", ".zshrc"], found)

    def test_an_overlay_replaces_the_shared_file_rather_than_doubling_it(self) -> None:
        """Plan §3.3: the overlay *replaces* the shared version on this host. So
        the name appears once, marked — the marked one being the file that would
        actually reach `$HOME` here."""
        self.write(self.repo / ".gitconfig", "shared")
        self.write(paths.host_overlay(self.repo, support.HOST) / ".gitconfig", "mine")
        found = manifest.managed(self.repo, support.HOST)
        self.assertEqual([PurePosixPath(".gitconfig")], [item.name for item in found])
        self.assertEqual([True], [item.host for item in found])

    def test_something_that_is_not_a_file_in_the_repository_is_not_listed(self) -> None:
        """A socket cannot be a managed dotfile, and `add` refuses to store one
        — but the repository is a directory on disk that other things can put
        entries in, and `list` reads whatever is there."""
        self.write(self.repo / ".bashrc", "x")
        os.mkfifo(self.repo / "stray.pipe")
        found = manifest.managed(self.repo, support.HOST)
        self.assertEqual([PurePosixPath(".bashrc")], [item.name for item in found])

    def test_another_hosts_overlay_is_not_listed(self) -> None:
        """The overlay belongs to one machine. Two hosts is the fixture that
        makes "this host's" observable at all."""
        self.write(paths.host_overlay(self.repo, "other-host") / ".gitconfig", "theirs")
        self.assertEqual([], manifest.managed(self.repo, support.HOST))


class TestNaming(unittest.TestCase):
    def test_a_tilde_is_expanded(self) -> None:
        self.assertEqual(Path(os.path.expanduser("~")) / ".bashrc", manifest.named("~/.bashrc"))

    def test_dot_dot_is_collapsed(self) -> None:
        self.assertEqual(Path("/a/c"), manifest.named("/a/b/../c"))

    def test_a_relative_path_is_taken_from_the_working_directory(self) -> None:
        self.assertEqual(Path.cwd() / "x", manifest.named("x"))

    def test_links_are_not_followed(self) -> None:
        """The whole reason this is not `Path.resolve`: resolving would answer
        about a file the user did not name, and `check` needs to see the name
        they did."""
        with support.tempdir() as box:
            (box / "real").mkdir()
            (box / "link").symlink_to(box / "real")
            self.assertEqual(box / "link" / "f", manifest.named(box / "link" / "f"))


class TestLinksBetween(unittest.TestCase):
    def test_a_link_above_home_is_not_the_files_fault(self) -> None:
        """`$HOME` itself is reached through a symlink on any macOS machine whose
        home is under `/tmp`, and in every test here. A check that walked all the
        way up would refuse everything, everywhere, for a reason that has nothing
        to do with the file.
        """
        with support.tempdir() as box:
            real = box / "real-home"
            (real / ".config").mkdir(parents=True)
            (real / ".config" / "x").write_text("x", encoding="utf-8")
            through = box / "home"
            through.symlink_to(real)
            self.assertIsNone(manifest.links_between(through / ".config" / "x", through))

    def test_a_link_below_home_is(self) -> None:
        with support.tempdir() as box:
            home = box / "home"
            (home).mkdir()
            (box / "target").mkdir()
            (home / ".aws").symlink_to(box / "target")
            self.assertEqual(home / ".aws", manifest.links_between(home / ".aws" / "creds", home))


if __name__ == "__main__":
    unittest.main()
