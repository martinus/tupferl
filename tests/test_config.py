"""Reading `.tupferl/config.toml`, and refusing the values that would mislead.

The class this file is really about is `TestRejectingAnUnknownKey`: `ignroe =
["*.pem"]` must not be a file that reads as if a private key is excluded while
tupferl copies it into a repository that is pushed to a remote. Every other test
here is a boundary around that decision.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path
from unittest import mock

import pytest

from tests import support
from tupferl import config
from tupferl.config import DEFAULT_MAX_FILE_SIZE, KNOWN, Config, load, parse
from tupferl.errors import TupferlError

WHERE = "config.toml"


class TestReadingTheSettings:
    def test_an_empty_file_is_the_defaults(self) -> None:
        assert parse("", WHERE) == Config()

    def test_every_known_key_arrives(self) -> None:
        """Both at once, each with a value the other does not share, so a parser
        that put one in the wrong field would be visible.

        There were four. `hostname` and `editor` went together: this file is
        committed and shared, so a per-machine answer set here reaches every
        machine that clones it.
        """
        found = parse(
            'ignore = ["*.log", ".cache/**"]\nmax_file_size = 2048\n',
            WHERE,
        )
        assert found == Config(ignore=["*.log", ".cache/**"], max_file_size=2048)

    def test_the_default_size_limit_is_one_megabyte(self) -> None:
        """Plan §5 names the number, so it is asserted rather than derived."""
        assert DEFAULT_MAX_FILE_SIZE == 1024 * 1024
        assert parse("", WHERE).max_file_size == DEFAULT_MAX_FILE_SIZE

    def test_a_partial_file_keeps_the_other_defaults(self) -> None:
        found = parse("max_file_size = 2048", WHERE)
        assert found.max_file_size == 2048
        assert found.ignore == []


class TestTheTableAndTheRecordAgree:
    """`parse` ends in `Config(**raw)`, so the two definitions of "a setting"
    have to be the same one.

    A key added to `KNOWN` and not to `Config` is accepted by every check and
    then raises `TypeError` -- an unexpected keyword argument, reported as a
    traceback rather than as a sentence. The reverse is a field nobody can set.
    """

    def test_every_known_key_is_a_field(self) -> None:
        """Non-emptiness is asserted first, and not for symmetry: the test below
        is parametrized over `KNOWN`, so an empty table would collect no cases
        at all -- zero tests, all of them passing. §2's zero-iteration trap,
        arriving at collection time."""
        assert KNOWN
        assert set(KNOWN) == {field.name for field in fields(Config)}

    @pytest.mark.parametrize(("name", "expected"), sorted(KNOWN.items()))
    def test_every_field_has_the_type_the_table_claims(self, name: str, expected: type) -> None:
        """Reading the annotation, not the default: a field whose default is
        `None` or an empty list would have a default-based check say its type is
        `NoneType` or `list` regardless of what the table claims."""
        assert expected.__name__ in Config.__annotations__[name]


class TestWhichTomlParserIsUsed:
    """`config.toml()` picks stdlib `tomllib` on 3.11+ and the `tomli` backport
    below it, and the choice is deferred so that `tupferl --version` does not pay
    for a parser it will not use.

    Both branches are asserted here by saying which version this *is*, because a
    mutation sweep runs on one interpreter and cannot otherwise see the branch it
    did not take -- all three mutations of this function survived a full sweep
    for exactly that reason. The 3.10 CI leg proves the fallback works against a
    real 3.10; this proves the *choice* is the right way round on any of them.
    """

    @pytest.mark.parametrize("version", [(3, 11, 0), (3, 14, 1)])
    def test_three_eleven_and_later_use_the_standard_library(
        self, version: tuple[int, ...]
    ) -> None:
        """Stubbed, for the same reason the 3.10 case is: patching the version
        number does not conjure the module. On a real 3.10 interpreter `import
        tomllib` raises `ModuleNotFoundError`, so the first version of this test
        passed everywhere except the one leg that runs 3.10 -- which is the leg
        that exists to check exactly this branch."""
        stdlib = types.ModuleType("tomllib")
        with (
            mock.patch.dict(sys.modules, {"tomllib": stdlib}),
            mock.patch.object(sys, "version_info", version),
        ):
            assert config.toml() is stdlib

    def test_three_ten_uses_the_backport_it_was_taken_from(self) -> None:
        """`(3, 11, 0)` above and `(3, 10, 7)` here are the two sides of the
        branch.

        **They are not the two sides of the `>=`**, and this docstring used to
        claim they were: "with `>` instead of `>=`, 3.11 itself would fall
        through to the backport". That is false, and the sweep said so by
        reporting the row SURVIVED. `sys.version_info` is a five-field tuple, so
        the value compared is `(3, 11, 0, 'final', 0)` -- and a longer tuple with
        an equal prefix sorts *above* the shorter one, so `> (3, 11)` is `True`
        exactly where `>=` is. The only value that separates them is the bare
        two-tuple `(3, 11)`, which no interpreter can report.

        So `>=` against `>` is an equivalent mutant here, recorded rather than
        chased. Writing a fixture that patched `version_info` to `(3, 11)` would
        kill the row by testing a state that cannot occur, which is asserting
        the mutation rather than the code.

        `tomli` is a *conditional* dependency -- it is only installed below 3.11
        -- so on the interpreter this suite usually runs on, importing it fails.
        A stub in `sys.modules` is what makes the branch reachable from any
        version: the claim under test is "this branch imports `tomli`", and
        standing something there and watching it come back is exactly that
        claim, with no opinion about what the real backport does. The 3.10 CI
        leg is what exercises the real one.
        """
        backport = types.ModuleType("tomli")
        with (
            mock.patch.dict(sys.modules, {"tomli": backport}),
            mock.patch.object(sys, "version_info", (3, 10, 7)),
        ):
            assert config.toml() is backport


class TestRejectingAnUnknownKey:
    def test_the_settings_are_listed_in_a_fixed_order(self) -> None:
        """`sorted(KNOWN)`, which today's `KNOWN` cannot show: it holds two keys
        declared in the order `sorted` would put them in, so `list` gives the
        same answer and the row survives.

        Patched with keys declared *out* of order, because the claim is about
        the message rather than about the constant -- a reader comparing what
        tupferl printed against a colleague's should not have to allow for two
        machines listing the same settings differently, and the next key added
        to `KNOWN` will not be added alphabetically.
        """
        with (
            mock.patch.object(config, "KNOWN", {"zebra": list, "apple": int}),
            pytest.raises(TupferlError) as caught,
        ):
            parse("nope = 1", WHERE)
        assert "apple, zebra" in str(caught.value)

    def test_a_typo_is_an_error_rather_than_silence(self) -> None:
        with pytest.raises(TupferlError) as caught:
            parse('ignroe = ["*.pem"]', WHERE)
        assert "ignroe" in str(caught.value)

    def test_the_message_lists_what_is_accepted(self) -> None:
        """Naming the key alone leaves the reader guessing at the spelling; the
        message is generated from `KNOWN`, so it cannot drift from the check."""
        with pytest.raises(TupferlError) as caught:
            parse("nonsense = 1", WHERE)
        for key in KNOWN:
            assert key in str(caught.value)

    def test_the_list_is_in_a_fixed_order(self) -> None:
        """Alphabetical, and written out here as one string.

        Asserting only that each key *appears* leaves the order free, and the
        order a `dict` gives is its insertion order -- so adding a setting would
        quietly reshuffle an error message people paste into issues. The
        mutation sweep is what noticed: reversing the sort changed nothing any
        test could see.
        """
        with pytest.raises(TupferlError) as caught:
            parse("nonsense = 1", WHERE)
        assert "ignore, max_file_size" in str(caught.value)

    def test_the_file_is_named(self) -> None:
        with pytest.raises(TupferlError) as caught:
            parse("nonsense = 1", "/somewhere/config.toml")
        assert "/somewhere/config.toml" in str(caught.value)


class TestRejectingAWrongValue:
    def test_a_string_where_a_number_belongs(self) -> None:
        with pytest.raises(TupferlError) as caught:
            parse('max_file_size = "big"', WHERE)
        assert "int" in str(caught.value)

    def test_a_number_where_a_string_belongs(self) -> None:
        with pytest.raises(TupferlError):
            parse("ignore = 7", WHERE)

    def test_a_boolean_is_not_a_number(self) -> None:
        """`True` is an `int` in Python, so an `isinstance` check alone accepts
        `max_file_size = true` and then uses it as 1 -- every file "too large",
        with nothing in the file that looks wrong."""
        with pytest.raises(TupferlError):
            parse("max_file_size = true", WHERE)

    def test_a_non_string_ignore_entry(self) -> None:
        with pytest.raises(TupferlError) as caught:
            parse("ignore = [1, 2]", WHERE)
        assert "ignore" in str(caught.value)

    def test_a_size_limit_of_zero_would_refuse_everything(self) -> None:
        with pytest.raises(TupferlError) as caught:
            parse("max_file_size = 0", WHERE)
        assert "positive" in str(caught.value)

    def test_a_negative_size_limit(self) -> None:
        with pytest.raises(TupferlError):
            parse("max_file_size = -1", WHERE)

    def test_broken_toml_says_so(self) -> None:
        with pytest.raises(TupferlError) as caught:
            parse("this is not toml", WHERE)
        assert "TOML" in str(caught.value)


@pytest.fixture
def box() -> Iterator[Path]:
    """A throwaway directory, through `support.tempdir`.

    Not pytest's `tmp_path`, and the reason is the mutation harness rather than
    taste: `tmp_path` keeps the last three numbered roots per user under
    `/tmp/pytest-of-<user>`, and a sweep runs thousands of probes as separate
    processes racing over that same numbering. `support.tempdir` removes its own
    tree in a `finally` and names what survived if the delete fails.
    """
    with support.tempdir(prefix="tupferl-config-") as made:
        yield made


class TestLoadingFromDisk:
    def test_a_missing_file_is_the_defaults(self, box: Path) -> None:
        """Not an error: `init` need not write one, and a user who wants no
        settings should not have to keep an empty file to say so."""
        assert load(box / "absent.toml") == Config()

    def test_a_file_that_cannot_be_read_is_an_error(self, box: Path) -> None:
        """A directory rather than a chmod: the suite runs as root in some
        containers, and root ignores the mode bits -- so a permissions fixture
        would pass there whatever the code did. `IsADirectoryError` is an
        `OSError` and not a `FileNotFoundError`, which is exactly the
        distinction under test.
        """
        unreadable = box / "config.toml"
        unreadable.mkdir()
        with pytest.raises(TupferlError) as caught:
            load(unreadable)
        assert str(unreadable) in str(caught.value)

    def test_a_real_file_is_parsed(self, box: Path) -> None:
        where = box / "config.toml"
        where.write_text("max_file_size = 4096\n", encoding="utf-8")
        assert load(where).max_file_size == 4096
