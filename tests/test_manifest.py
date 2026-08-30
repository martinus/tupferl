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
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from unittest import mock

import pytest

from tests import support
from tupferl import manifest, paths
from tupferl.config import Config
from tupferl.errors import TupferlError


@dataclass(frozen=True)
class Managed:
    """A sandboxed `$HOME` with an empty repository, and the two questions.

    `check` and `refusal` are the same call read two ways -- what a path is
    admitted *as*, and why it was not -- so they live together and every test
    below asks one or the other.
    """

    box: support.Sandbox
    repo: Path
    config: Config

    @property
    def tmp(self) -> Path:
        return self.box.tmp

    @property
    def home(self) -> Path:
        return self.box.home

    @property
    def env(self) -> dict[str, str]:
        return self.box.env

    def write(self, where: Path, text: str) -> Path:
        return self.box.write(where, text)

    def check(self, path: Path, config: Config | None = None) -> PurePosixPath:
        return manifest.check(path, self.home, self.repo, config or self.config)

    def refusal(self, path: Path, config: Config | None = None) -> str:
        with pytest.raises(TupferlError) as caught:
            self.check(path, config)
        return str(caught.value)


@pytest.fixture
def box(sandbox: support.Sandbox) -> Managed:
    repo = paths.repo_dir()
    repo.mkdir(parents=True)
    return Managed(sandbox, repo, Config())


@pytest.mark.usefixtures("box")
class TestWhatIsAdmitted:
    def test_an_ordinary_dotfile(self, box: Managed) -> None:
        assert box.check(box.write(box.home / ".bashrc", "x")) == PurePosixPath(".bashrc")

    def test_one_nested_in_directories(self, box: Managed) -> None:
        """The stored name is the whole path relative to `$HOME`, which is plan
        §3.2's entire mapping rule."""
        where = box.write(box.home / ".config" / "nvim" / "init.lua", "x")
        assert box.check(where) == PurePosixPath(".config/nvim/init.lua")

    def test_a_directory(self, box: Managed) -> None:
        """Named directories are admitted so `collect` can walk them; the size
        and ignore rules apply to the files inside, not to the directory."""
        (box.home / ".config").mkdir(exist_ok=True)  # `seed_home` already made it
        assert box.check(box.home / ".config") == PurePosixPath(".config")


@pytest.mark.usefixtures("box")
class TestWhatIsRefused:
    def test_a_symlink(self, box: Managed) -> None:
        """Plan §5. The target is outside `$HOME` on purpose: this is the shape
        where following the link would copy something the user never named."""
        outside = box.tmp / "secret"
        outside.write_text("token\n", encoding="utf-8")
        link = box.home / ".aws-credentials"
        link.symlink_to(outside)
        found = box.refusal(link)
        # On "is a symlink", not on "symlink": *both* refusals contain the bare
        # word, so asserting it alone passes with the direct check removed --
        # `links_between` then catches the same file one rule later and says the
        # path "goes through" a symlink, which is confusing when the path *is*
        # one. The mutation sweep found this; it is the third time this exact
        # shape has appeared in these tests.
        assert "is a symlink" in found
        assert "goes through" not in found

    def test_a_path_through_a_symlinked_parent(self, box: Managed) -> None:
        """The same hazard one level up, and the one a naive check misses: the
        *file* is an ordinary file, and only its parent is a link out."""
        outside = box.tmp / "elsewhere"
        outside.mkdir()
        (outside / "creds").write_text("token\n", encoding="utf-8")
        (box.home / ".aws").symlink_to(outside)
        assert "goes through the symlink" in box.refusal(box.home / ".aws" / "creds")

    def test_a_path_that_climbs_out_of_home(self, box: Managed) -> None:
        """`..` is collapsed lexically before the check, so this is refused for
        being outside `$HOME` rather than accepted and stored under a name with
        `..` in it."""
        outside = box.tmp / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        climbed = manifest.named(box.home / ".." / outside.name)
        assert "outside" in box.refusal(climbed)

    def test_a_file_that_is_simply_elsewhere(self, box: Managed) -> None:
        assert "outside" in box.refusal(box.write(box.tmp / "plain.txt", "x"))

    def test_the_repository_itself(self, box: Managed) -> None:
        assert "own repository" in box.refusal(box.repo)

    def test_a_directory_that_contains_the_repository(self, box: Managed) -> None:
        """`~/.local` holds the default repository, so adding it would walk into
        tupferl's own copies and manage them."""
        assert "own repository" in box.refusal(box.home / ".local")

    def test_something_that_is_not_a_file(self, box: Managed) -> None:
        """A named pipe. The realistic instance is a socket -- `~/.gnupg/S.agent`
        is one, in exactly the directory somebody would think to add -- but a
        socket cannot be bound at an arbitrary path: `sun_path` is 104 bytes on
        macOS, and the runner's temporary directories are long enough that
        binding inside a sandboxed repository fails with `OSError` rather than
        testing anything. A fifo is the same class of "not a regular file" with
        no such limit, so it is what these fixtures use."""
        where = box.home / ".agent-pipe"
        os.mkfifo(where)
        assert "not a regular file" in box.refusal(where)

    def test_a_file_that_is_not_there(self, box: Managed) -> None:
        assert "does not exist" in box.refusal(box.home / ".absent")

    def test_one_over_the_size_limit(self, box: Managed) -> None:
        big = box.write(box.home / ".big", "x" * 100)
        assert "over the" in box.refusal(big, Config(max_file_size=99))

    def test_one_exactly_at_the_limit_is_kept(self, box: Managed) -> None:
        """The boundary from the other side: `>` not `>=`, so a file the size of
        the limit is admitted. Without this the off-by-one is invisible."""
        edge = box.write(box.home / ".edge", "x" * 100)
        assert box.check(edge, Config(max_file_size=100)) == PurePosixPath(".edge")

    def test_a_directory_is_not_measured_against_the_size_limit(self, box: Managed) -> None:
        """A directory's `st_size` is its own bookkeeping — 4096 on ext4, more
        as it fills — and has nothing to do with what it holds. Applying the
        limit to it would refuse `~/.config` on any machine with a small
        `max_file_size`, for a number the user cannot see or change."""
        (box.home / ".config").mkdir(exist_ok=True)
        assert box.check(box.home / ".config", Config(max_file_size=1)) == PurePosixPath(".config")

    def test_one_matching_an_ignore_pattern(self, box: Managed) -> None:
        noisy = box.write(box.home / ".x.log", "x")
        assert "ignore" in box.refusal(noisy, Config(ignore=["*.log"]))


@pytest.mark.usefixtures("box")
class TestTheOrderOfTheRules:
    def test_a_symlink_is_refused_as_a_symlink_even_when_ignored(self, box: Managed) -> None:
        """The safety rules run before the settings, so the message names the
        reason that matters. A file can satisfy two refusals at once, and which
        one the user is told decides what they do about it."""
        outside = box.tmp / "big.log"
        outside.write_text("x" * 100, encoding="utf-8")
        link = box.home / "linked.log"
        link.symlink_to(outside)
        found = box.refusal(link, Config(ignore=["*.log"], max_file_size=1))
        assert "symlink" in found
        assert "ignore" not in found


class TestIgnorePatterns:
    def test_a_pattern_hides_the_subtree_below_it(self) -> None:
        """`ignore = [".cache"]` must mean the whole directory. Otherwise it
        hides an entry nobody stores and the contents are pushed anyway."""
        assert manifest.ignored(PurePosixPath(".cache/chromium/big"), [".cache"])

    def test_a_pattern_matches_at_any_depth(self) -> None:
        assert manifest.ignored(PurePosixPath(".local/state/x.log"), ["*.log"])

    def test_an_unrelated_file_is_kept(self) -> None:
        """The other answer, so the two above are not satisfied by an `ignored`
        that says yes to everything."""
        assert not manifest.ignored(PurePosixPath(".bashrc"), [".cache", "*.log"])

    def test_no_patterns_ignore_nothing(self) -> None:
        assert not manifest.ignored(PurePosixPath(".cache/x"), [])

    def test_matching_does_not_fold_case(self) -> None:
        """`fnmatch` folds case on macOS, which would make the same repository
        ignore different files on two machines. This is the half of the
        guarantee only a case-sensitive filesystem can show, but the assertion
        holds everywhere because `fnmatchcase` never folds."""
        assert not manifest.ignored(PurePosixPath(".CACHE/x"), [".cache"])


@pytest.mark.usefixtures("box")
class TestCollecting:
    def test_a_directory_yields_its_files_sorted(self, box: Managed) -> None:
        for name in ("b.conf", "a.conf", "sub/c.conf"):
            box.write(box.home / ".config" / name, "x")
        found, refused = manifest.collect(box.home / ".config", box.home, box.repo, box.config)
        assert found == [PurePosixPath(f".config/{n}") for n in ("a.conf", "b.conf", "sub/c.conf")]
        assert refused == []

    def test_the_refusals_come_back_rather_than_stopping_the_walk(self, box: Managed) -> None:
        """Adding `~/.config` with one socket in it must manage the rest. A walk
        that raised on the first problem would be unusable on a real machine."""
        box.write(box.home / ".config" / "good.conf", "x")
        (box.home / ".config" / "linked").symlink_to(box.tmp / "elsewhere")
        found, refused = manifest.collect(box.home / ".config", box.home, box.repo, box.config)
        assert found == [PurePosixPath(".config/good.conf")]
        assert len(refused) == 1
        assert "symlink" in refused[0].why

    def test_a_symlinked_directory_is_not_descended_into(self, box: Managed) -> None:
        """It would be refused file by file anyway; the point is not to *read* a
        tree the user never named, which for a link to `/` is most of the disk."""
        outside = box.tmp / "huge"
        (outside / "deep").mkdir(parents=True)
        (outside / "deep" / "file").write_text("x", encoding="utf-8")
        (box.home / ".linked-dir").symlink_to(outside)
        found, refused = manifest.collect(box.home / ".linked-dir", box.home, box.repo, box.config)
        assert found == []
        assert [item.path for item in refused] == [box.home / ".linked-dir"]


@pytest.mark.usefixtures("box")
class TestWhatTheRepositoryHolds:
    def test_nothing_at_first(self, box: Managed) -> None:
        assert manifest.managed(box.repo, support.HOST) == []

    def test_shared_files_are_listed_unmarked(self, box: Managed) -> None:
        box.write(box.repo / ".bashrc", "x")
        box.write(box.repo / ".config" / "nvim" / "init.lua", "x")
        found = manifest.managed(box.repo, support.HOST)
        assert [item.name for item in found] == [
            PurePosixPath(".bashrc"),
            PurePosixPath(".config/nvim/init.lua"),
        ]
        assert [item.host for item in found] == [False, False]

    def test_tupferls_own_directory_is_not_managed(self, box: Managed) -> None:
        """`.tupferl/` holds settings, overlays and (from milestone 3) snapshots.
        Listing them as managed would also make them removable by name."""
        box.write(box.repo / paths.META / "config.toml", "")
        box.write(box.repo / ".bashrc", "x")
        found = manifest.managed(box.repo, support.HOST)
        assert [item.name for item in found] == [PurePosixPath(".bashrc")]

    def test_an_overlay_file_is_marked(self, box: Managed) -> None:
        box.write(paths.host_overlay(box.repo, support.HOST) / ".gitconfig", "x")
        found = manifest.managed(box.repo, support.HOST)
        assert [item.name for item in found] == [PurePosixPath(".gitconfig")]
        assert [item.host for item in found] == [True]

    def test_the_listing_is_sorted_rather_than_however_it_was_built(self, box: Managed) -> None:
        """The overlay is merged into the shared map *after* it, so insertion
        order is "shared first". A name from the overlay that sorts before a
        shared one is therefore the fixture that can tell `sorted` from `list`
        — with any other pair the two answers coincide and the sort is
        untested."""
        box.write(box.repo / ".zshrc", "x")
        box.write(paths.host_overlay(box.repo, support.HOST) / ".aaa", "x")
        found = manifest.managed(box.repo, support.HOST)
        assert [item.name for item in found] == [PurePosixPath(".aaa"), PurePosixPath(".zshrc")]

    def test_the_listing_comes_back_sorted_whatever_the_filesystem_says(self, box: Managed) -> None:
        """Five names created in an order no filesystem returns alphabetically.

        No skip and no dependence on readdir order: `managed` sorts
        unconditionally, so this is deterministic everywhere. An earlier version
        skipped when readdir happened to be sorted, which under CI's
        `--no-skips` would have turned a lucky filesystem into a red build.
        """
        for name in (".yyy", ".bbb", ".aaa", ".mmm", ".zshrc"):
            box.write(box.repo / name, "x")
        found = [str(item.name) for item in manifest.managed(box.repo, support.HOST)]
        assert found == [".aaa", ".bbb", ".mmm", ".yyy", ".zshrc"]

    def test_an_overlay_replaces_the_shared_file_rather_than_doubling_it(
        self, box: Managed
    ) -> None:
        """Plan §3.3: the overlay *replaces* the shared version on this host. So
        the name appears once, marked — the marked one being the file that would
        actually reach `$HOME` here."""
        box.write(box.repo / ".gitconfig", "shared")
        box.write(paths.host_overlay(box.repo, support.HOST) / ".gitconfig", "mine")
        found = manifest.managed(box.repo, support.HOST)
        assert [item.name for item in found] == [PurePosixPath(".gitconfig")]
        assert [item.host for item in found] == [True]

    def test_something_that_is_not_a_file_in_the_repository_is_not_listed(
        self, box: Managed
    ) -> None:
        """A socket cannot be a managed dotfile, and `add` refuses to store one
        — but the repository is a directory on disk that other things can put
        entries in, and `list` reads whatever is there."""
        box.write(box.repo / ".bashrc", "x")
        os.mkfifo(box.repo / "stray.pipe")
        found = manifest.managed(box.repo, support.HOST)
        assert [item.name for item in found] == [PurePosixPath(".bashrc")]

    def test_another_hosts_overlay_is_not_listed(self, box: Managed) -> None:
        """The overlay belongs to one machine. Two hosts is the fixture that
        makes "this host's" observable at all."""
        box.write(paths.host_overlay(box.repo, "other-host") / ".gitconfig", "theirs")
        assert manifest.managed(box.repo, support.HOST) == []


class TestNaming:
    def test_a_tilde_is_expanded(self) -> None:
        assert manifest.named("~/.bashrc") == Path(os.path.expanduser("~")) / ".bashrc"

    def test_dot_dot_is_collapsed(self) -> None:
        assert manifest.named("/a/b/../c") == Path("/a/c")

    def test_a_relative_path_is_taken_from_the_working_directory(self) -> None:
        assert manifest.named("x") == Path.cwd() / "x"

    def test_links_are_not_followed(self) -> None:
        """The whole reason this is not `Path.resolve`: resolving would answer
        about a file the user did not name, and `check` needs to see the name
        they did."""
        with support.tempdir() as box:
            (box / "real").mkdir()
            (box / "link").symlink_to(box / "real")
            assert manifest.named(box / "link" / "f") == box / "link" / "f"


class TestLinksBetween:
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
            assert manifest.links_between(through / ".config" / "x", through) is None

    def test_a_link_below_home_is(self) -> None:
        with support.tempdir() as box:
            home = box / "home"
            (home).mkdir()
            (box / "target").mkdir()
            (home / ".aws").symlink_to(box / "target")
            assert manifest.links_between(home / ".aws" / "creds", home) == home / ".aws"


#: One filename per `manifest.SECRETS` entry, and the entry it must match.
#: Module-level so the two tests below read the same table -- one parametrized
#: over it, one asserting it covers `SECRETS`.
EXAMPLES = {
    ".ssh/id_ed25519": ".ssh/id_*",
    ".aws/credentials": ".aws/credentials",
    ".netrc": ".netrc",
    ".pgpass": ".pgpass",
    ".gnupg/secring.gpg": ".gnupg/*",
    ".config/app/server.pem": "*.pem",
    ".config/app/private.key": "*.key",
}


class TestWhatLooksLikeASecret:
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

    def test_every_entry_has_an_example(self) -> None:
        """The precondition for the parametrized test below, and it does more
        than assert non-emptiness: `EXAMPLES` is a literal, so it collects its
        cases whatever `SECRETS` says. What this catches is the other side --
        a pattern added to `SECRETS` with no example here, which would leave
        the new rule guarded by nothing while every case still passed."""
        assert set(EXAMPLES.values()) == set(manifest.SECRETS), "an entry has no example"

    @pytest.mark.parametrize(("name", "pattern"), sorted(EXAMPLES.items()))
    def test_every_pattern_matches_something(self, name: str, pattern: str) -> None:
        """One name per entry, so an entry that stopped matching anything --
        a typo, a pattern the parent walk cannot reach -- is a failure rather
        than a quietly dead line."""
        assert self.matched(name) == pattern

    @pytest.mark.parametrize("name", [".ssh/config", ".ssh/known_hosts", ".ssh/authorized_keys"])
    def test_the_rest_of_ssh_is_not_refused(self, name: str) -> None:
        """`config` and `known_hosts` are ordinary dotfiles people want synced,
        and they live in the directory this rule is most about. Refusing them
        would make the rule wrong more often than right."""
        assert self.matched(name) is None

    def test_a_public_key_is_public(self) -> None:
        """`.ssh/id_*` matches `id_ed25519.pub` too, so `NOT_SECRET` has to take
        it back. Without that entry the half of the pair that is *meant* to be
        shared is the half tupferl refuses."""
        assert self.matched(".ssh/id_ed25519.pub") is None
        assert self.matched(".ssh/id_ed25519") == ".ssh/id_*"

    def test_a_nested_name_under_gnupg_is_refused(self) -> None:
        """`.gnupg/*` has to mean the subtree, not one directory entry -- the
        parent walk `ignored` already does, for the reason written there."""
        assert self.matched(".gnupg/private-keys-v1.d/ABCD.key") is not None

    @pytest.mark.parametrize("name", [".bashrc", ".config/nvim/init.lua", ".gitconfig", ".ssh"])
    def test_an_ordinary_dotfile_is_not_refused(self, name: str) -> None:
        """The precondition. Without it every assertion above is satisfied by a
        `secret` that answers a pattern for everything."""
        assert self.matched(name) is None

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
            assert self.matched(".ssh/id_ed25519.pub") is None
            assert self.matched(".ssh/id_ed25519.example") is None
            assert self.matched(".ssh/id_ed25519") is not None

    def test_the_anchored_half_and_the_unanchored_half(self) -> None:
        """`fnmatch`'s `*` matches `/`, so `*.pem` and `*.key` fire at any depth
        while `.ssh/id_*` fires only at the top of the tree.

        Both directions are pinned because the asymmetry is a *choice*: matching
        `id_*` anywhere would refuse `~/pictures/id_photo.png`, and a rule that
        fires on holiday snaps is one people learn to pass `--anyway` to without
        reading. Someone tightening this later should see what it costs.
        """
        assert self.matched("projects/thing/server.pem") is not None
        assert self.matched("projects/thing/private.key") is not None
        assert self.matched("projects/thing/.ssh/id_rsa") is None
        assert self.matched("pictures/id_photo.png") is None

    def test_it_does_not_fold_case(self) -> None:
        """`fnmatchcase`, for `ignored`'s reason: folding on macOS would make two
        machines disagree about the same repository, and the repository is the
        thing they share."""
        assert self.matched(".SSH/ID_ED25519") is None


class TestTurningWhatWasTypedIntoAName:
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

    @pytest.mark.parametrize("where", ["/tmp", "/", "/var/log"])
    def test_a_name_from_list_works_from_anywhere(self, where: str) -> None:
        """The bug. Every directory but `$HOME` used to fail."""
        assert self.standing(where, ".bashrc") == PurePosixPath(".bashrc")

    def test_the_working_directory_still_wins_where_it_can(self) -> None:
        """Standing in `~/.config`, `nvim/init.lua` is the file under your feet.
        That is how paths are typed at a shell, and the fallback must not take
        it."""
        assert self.standing("/home/ada/.config", "nvim/init.lua") == PurePosixPath(
            ".config/nvim/init.lua"
        )

    def test_a_relative_path_under_home_is_not_re_read(self) -> None:
        """The half that makes the test above about *precedence* rather than
        about one path happening to work: from `~/.config`, `.bashrc` means
        `.config/.bashrc` and not `.bashrc`.

        Ambiguous, and documented as taking the first reading -- resolving it
        would mean asking the manifest what is managed, which this function
        deliberately does not know.
        """
        assert self.standing("/home/ada/.config", ".bashrc") == PurePosixPath(".config/.bashrc")

    def test_an_absolute_path_outside_home_is_still_refused(self) -> None:
        """`/etc/hostname` must not become `$HOME/etc/hostname`. The fallback is
        for arguments that were relative to begin with."""
        with pytest.raises(TupferlError) as caught:
            self.standing("/tmp", "/etc/hostname")
        assert "/etc/hostname is outside" in str(caught.value)

    def test_a_tilde_path_that_climbs_out_is_still_refused(self) -> None:
        """`named` expands and collapses it first, so it arrives absolute -- and
        an absolute argument gets no second reading. This is the case the
        docstring calls out by name."""
        with pytest.raises(TupferlError):
            self.standing("/tmp", "~/../etc/passwd")

    def test_a_relative_path_that_climbs_out_of_home_is_refused(self) -> None:
        """The fallback re-reads it under `$HOME` and it still escapes, so both
        readings fail and the error stands. Without the second `relative_to`
        this would return a name pointing outside the repository."""
        with pytest.raises(TupferlError):
            self.standing("/tmp", "../../etc/passwd")

    def test_the_error_names_both_ways_in(self) -> None:
        """A message that only said "name a file under it" left the reader
        without the thing that actually works: the name `list` prints."""
        with pytest.raises(TupferlError) as caught:
            self.standing("/tmp", "/etc/hostname")
        said = str(caught.value)
        assert "name a file under it" in said
        assert "tupferl status --all" in said


class TestWhatMayBeMerged:
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

    @pytest.mark.parametrize("name", [".bashrc", ".config/nvim/init.lua", "tupferl-ish/notes"])
    def test_an_ordinary_dotfile_is_merged(self, name: str) -> None:
        assert self.mergeable(name)

    @pytest.mark.parametrize("host", [HERE, ELSEWHERE])
    def test_a_snapshot_is_never_merged(self, host: str) -> None:
        """Either host's. This machine's is the one that breaks the interruption
        guarantee; another's is not this machine's to touch at all."""
        assert not self.mergeable(f"{paths.META}/state/{host}/.bashrc")

    def test_this_hosts_overlay_is_merged(self) -> None:
        """An overlay file is a dotfile that happens to live under `.tupferl/`,
        and a conflict over it is exactly what the prompt is for. Refusing it
        would be the regression the issue's prescribed fix would have caused."""
        assert self.mergeable(f"{paths.META}/hosts/{self.HERE}/.vimrc")
        assert self.mergeable(f"{paths.META}/hosts/{self.HERE}/.config/a/b.conf")

    def test_another_hosts_overlay_is_not(self) -> None:
        """The half that makes the test above about *this* host rather than
        about overlays in general."""
        assert not self.mergeable(f"{paths.META}/hosts/{self.ELSEWHERE}/.vimrc")

    def test_nothing_under_meta_is_merged_but_this_hosts_overlay(self) -> None:
        """`.tupferl/config.toml` used to be the exception, because the settings
        lived in the repository and two machines really could disagree about
        them. The settings are a dotfile in `$HOME` now, so they arrive by the
        ordinary path and `META` holds only machinery again -- which makes this
        a closed rule rather than a rule with a name in it."""
        assert not self.mergeable(f"{paths.META}/config.toml")
        assert not self.mergeable(f"{paths.META}/whatever")
        assert not self.mergeable(f"{paths.META}/state")
        assert not self.mergeable(f"{paths.META}/hosts/x")

    def test_a_name_that_merely_starts_with_the_same_letters_is_merged(self) -> None:
        """`.tupferlish` is a dotfile somebody may really have, and a prefix
        match rather than a path-component one would refuse it."""
        assert self.mergeable(f"{paths.META}ish/notes")
        assert self.mergeable(f"{paths.META}x")
