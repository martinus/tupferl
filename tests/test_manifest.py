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
from unittest import mock

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


class TestWhatLooksLikeASecret(unittest.TestCase):
    """`manifest.secret`, the seventh admission rule (#35).

    Plan §2 puts encryption out of scope, so what tupferl stores it stores in
    plaintext and pushes to a remote. That decision is fine; what was not is that
    `tupferl add ~/.ssh/id_ed25519` succeeded **silently**, and the key was then
    in a git history nothing here can rewrite.

    **The absences are tested as hard as the entries.** A rule that refused the
    whole of `~/.ssh` would be wrong far more often than right, and a rule nobody
    can predict is one people route around by moving files -- which is worse than
    no rule.
    """

    def matched(self, name: str) -> str | None:
        return manifest.secret(PurePosixPath(name))

    def test_every_pattern_matches_something(self) -> None:
        """One name per entry, so an entry that stopped matching anything --
        a typo, a pattern the parent walk cannot reach -- is a failure rather
        than a quietly dead line."""
        named = {
            ".ssh/id_ed25519": ".ssh/id_*",
            ".aws/credentials": ".aws/credentials",
            ".netrc": ".netrc",
            ".pgpass": ".pgpass",
            ".gnupg/secring.gpg": ".gnupg/*",
            ".config/app/server.pem": "*.pem",
            ".config/app/private.key": "*.key",
        }
        self.assertEqual(set(manifest.SECRETS), set(named.values()), "an entry has no example")
        for name, pattern in named.items():
            with self.subTest(name=name):
                self.assertEqual(pattern, self.matched(name))

    def test_the_rest_of_ssh_is_not_refused(self) -> None:
        """`config` and `known_hosts` are ordinary dotfiles people want synced,
        and they live in the directory this rule is most about. Refusing them
        would make the rule wrong more often than right."""
        for name in (".ssh/config", ".ssh/known_hosts", ".ssh/authorized_keys"):
            with self.subTest(name=name):
                self.assertIsNone(self.matched(name))

    def test_a_public_key_is_public(self) -> None:
        """`.ssh/id_*` matches `id_ed25519.pub` too, so `NOT_SECRET` has to take
        it back. Without that entry the half of the pair that is *meant* to be
        shared is the half tupferl refuses."""
        self.assertIsNone(self.matched(".ssh/id_ed25519.pub"))
        self.assertEqual(".ssh/id_*", self.matched(".ssh/id_ed25519"))

    def test_a_nested_name_under_gnupg_is_refused(self) -> None:
        """`.gnupg/*` has to mean the subtree, not one directory entry -- the
        parent walk `ignored` already does, for the reason written there."""
        self.assertIsNotNone(self.matched(".gnupg/private-keys-v1.d/ABCD.key"))

    def test_an_ordinary_dotfile_is_not_refused(self) -> None:
        """The precondition. Without it every assertion above is satisfied by a
        `secret` that answers a pattern for everything."""
        for name in (".bashrc", ".config/nvim/init.lua", ".gitconfig", ".ssh"):
            with self.subTest(name=name):
                self.assertIsNone(self.matched(name))

    def test_any_exemption_is_enough(self) -> None:
        """`NOT_SECRET` is scanned as a disjunction, and with one entry in it
        nothing can see that.

        `any` and `all` agree on a one-element collection, so the sweep reported
        `any` becoming `all` as a survivor -- and it would stay invisible right
        up to the day somebody adds a second exemption, at which point `all`
        silently requires a name to match *every* one of them and the first
        exemption stops working. Patching in a second entry is the only fixture
        that can tell the two apart, and the claim it checks -- "one match is
        enough" -- is the real contract of the tuple.
        """
        with mock.patch.object(manifest, "NOT_SECRET", ("*.pub", "*.example")):
            self.assertIsNone(self.matched(".ssh/id_ed25519.pub"))
            self.assertIsNone(self.matched(".ssh/id_ed25519.example"))
            self.assertIsNotNone(self.matched(".ssh/id_ed25519"))

    def test_the_anchored_half_and_the_unanchored_half(self) -> None:
        """`fnmatch`'s `*` matches `/`, so `*.pem` and `*.key` fire at any depth
        while `.ssh/id_*` fires only at the top of the tree.

        Both directions are pinned because the asymmetry is a *choice*: matching
        `id_*` anywhere would refuse `~/pictures/id_photo.png`, and a rule that
        fires on holiday snaps is one people learn to pass `--anyway` to without
        reading. Someone tightening this later should see what it costs.
        """
        self.assertIsNotNone(self.matched("projects/thing/server.pem"))
        self.assertIsNotNone(self.matched("projects/thing/private.key"))
        self.assertIsNone(self.matched("projects/thing/.ssh/id_rsa"))
        self.assertIsNone(self.matched("pictures/id_photo.png"))

    def test_it_does_not_fold_case(self) -> None:
        """`fnmatchcase`, for `ignored`'s reason: folding on macOS would make two
        machines disagree about the same repository, and the repository is the
        thing they share."""
        self.assertIsNone(self.matched(".SSH/ID_ED25519"))


class TestTurningWhatWasTypedIntoAName(unittest.TestCase):
    """`manifest.relative`, and the two readings of a relative argument (#27).

    `tupferl list` prints `.bashrc`; `tupferl diff .bashrc` used to answer
    "`/somewhere-else/.bashrc` is outside your home directory". The tool printed
    an identifier it would not take back, and blamed the user for naming a file
    they had not named.

    No repository and no filesystem: this is a translation from a string to a
    key, and it says nothing about whether the file exists. Each test names the
    directory it pretends to stand in, because "which reading wins" is the whole
    subject and a fixture that never moved could not show one.
    """

    HOME = Path("/home/ada")

    def standing(self, where: str, typed: str) -> PurePosixPath:
        with mock.patch.object(Path, "cwd", return_value=Path(where)):
            return manifest.relative(typed, self.HOME)

    def test_a_name_from_list_works_from_anywhere(self) -> None:
        """The bug. Every directory but `$HOME` used to fail."""
        for where in ("/tmp", "/", "/var/log"):
            with self.subTest(cwd=where):
                self.assertEqual(PurePosixPath(".bashrc"), self.standing(where, ".bashrc"))

    def test_the_working_directory_still_wins_where_it_can(self) -> None:
        """Standing in `~/.config`, `nvim/init.lua` is the file under your feet.
        That is how paths are typed at a shell, and the fallback must not take
        it."""
        self.assertEqual(
            PurePosixPath(".config/nvim/init.lua"),
            self.standing("/home/ada/.config", "nvim/init.lua"),
        )

    def test_a_relative_path_under_home_is_not_re_read(self) -> None:
        """The half that makes the test above about *precedence* rather than
        about one path happening to work: from `~/.config`, `.bashrc` means
        `.config/.bashrc` and not `.bashrc`.

        Ambiguous, and documented as taking the first reading -- resolving it
        would mean asking the manifest what is managed, which this function
        deliberately does not know.
        """
        self.assertEqual(
            PurePosixPath(".config/.bashrc"), self.standing("/home/ada/.config", ".bashrc")
        )

    def test_an_absolute_path_outside_home_is_still_refused(self) -> None:
        """`/etc/hostname` must not become `$HOME/etc/hostname`. The fallback is
        for arguments that were relative to begin with."""
        with self.assertRaises(TupferlError) as caught:
            self.standing("/tmp", "/etc/hostname")
        self.assertIn("/etc/hostname is outside", str(caught.exception))

    def test_a_tilde_path_that_climbs_out_is_still_refused(self) -> None:
        """`named` expands and collapses it first, so it arrives absolute -- and
        an absolute argument gets no second reading. This is the case the
        docstring calls out by name."""
        with self.assertRaises(TupferlError):
            self.standing("/tmp", "~/../etc/passwd")

    def test_a_relative_path_that_climbs_out_of_home_is_refused(self) -> None:
        """The fallback re-reads it under `$HOME` and it still escapes, so both
        readings fail and the error stands. Without the second `relative_to`
        this would return a name pointing outside the repository."""
        with self.assertRaises(TupferlError):
            self.standing("/tmp", "../../etc/passwd")

    def test_the_error_names_both_ways_in(self) -> None:
        """A message that only said "name a file under it" left the reader
        without the thing that actually works: the name `list` prints."""
        with self.assertRaises(TupferlError) as caught:
            self.standing("/tmp", "/etc/hostname")
        said = str(caught.exception)
        self.assertIn("name a file under it", said)
        self.assertIn("tupferl list", said)


class TestWhatMayBeMerged(unittest.TestCase):
    """`manifest.mergeable`, which decides what `sync.reconcile` may settle (#15).

    Pure, and needs no repository: it answers about a *path*. That is why the
    admission this class exists for -- **this host's own overlay** -- is checked
    here rather than end to end. Two machines can only conflict over the same
    host's overlay if they share a hostname, and a shared hostname collides
    `state/<host>/` as well, so the snapshot is refused first and the overlay
    never reaches the prompt. `test_sync_commits.TestNotEverythingUnderMetaIsRefused`
    carries the end-to-end half that *is* reachable, `config.toml`, and says so.

    `HERE` and `ELSEWHERE` are different on purpose: a table where every host is
    the same host cannot tell "this machine's overlay" from "any overlay", which
    is the distinction the function exists to make.
    """

    HERE = "laptop"
    ELSEWHERE = "desktop"

    #: Any absolute path does: `mergeable` compares paths, it never opens one.
    #: A directory that does not exist says so out loud -- if a future version
    #: starts reading from disk, this fixture fails rather than quietly working.
    REPO = Path("/nowhere/repo")

    def mergeable(self, name: str) -> bool:
        return manifest.mergeable(PurePosixPath(name), self.REPO, self.HERE)

    def test_an_ordinary_dotfile_is_merged(self) -> None:
        for name in (".bashrc", ".config/nvim/init.lua", "tupferl-ish/notes"):
            with self.subTest(name=name):
                self.assertTrue(self.mergeable(name))

    def test_a_snapshot_is_never_merged(self) -> None:
        """Either host's. This machine's is the one that breaks the interruption
        guarantee; another's is not this machine's to touch at all."""
        for host in (self.HERE, self.ELSEWHERE):
            with self.subTest(host=host):
                self.assertFalse(self.mergeable(f"{paths.META}/state/{host}/.bashrc"))

    def test_this_hosts_overlay_is_merged(self) -> None:
        """An overlay file is a dotfile that happens to live under `.tupferl/`,
        and a conflict over it is exactly what the prompt is for. Refusing it
        would be the regression the issue's prescribed fix would have caused."""
        self.assertTrue(self.mergeable(f"{paths.META}/hosts/{self.HERE}/.vimrc"))
        self.assertTrue(self.mergeable(f"{paths.META}/hosts/{self.HERE}/.config/a/b.conf"))

    def test_another_hosts_overlay_is_not(self) -> None:
        """The half that makes the test above about *this* host rather than
        about overlays in general."""
        self.assertFalse(self.mergeable(f"{paths.META}/hosts/{self.ELSEWHERE}/.vimrc"))

    def test_the_settings_file_is_merged(self) -> None:
        """The reason the rule is not "skip everything under `.tupferl/`": two
        machines really can disagree about it."""
        self.assertTrue(self.mergeable(f"{paths.META}/config.toml"))

    def test_anything_else_under_meta_is_not(self) -> None:
        """A closed rule rather than a list of known-bad names, so a directory
        added under `.tupferl/` later is refused until somebody admits it."""
        for name in (f"{paths.META}/whatever", f"{paths.META}/state", f"{paths.META}/hosts/x"):
            with self.subTest(name=name):
                self.assertFalse(self.mergeable(name))

    def test_a_name_that_merely_starts_with_the_same_letters_is_merged(self) -> None:
        """`.tupferlish` is a dotfile somebody may really have, and a prefix
        match rather than a path-component one would refuse it."""
        self.assertTrue(self.mergeable(f"{paths.META}ish/notes"))
        self.assertTrue(self.mergeable(f"{paths.META}x"))


if __name__ == "__main__":
    unittest.main()
