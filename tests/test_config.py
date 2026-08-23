"""Reading `.tupferl/config.toml`, and refusing the values that would mislead.

The class this file is really about is `TestRejectingAnUnknownKey`: `ignroe =
["*.pem"]` must not be a file that reads as if a private key is excluded while
tupferl copies it into a repository that is pushed to a remote. Every other test
here is a boundary around that decision.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tupferl.config import DEFAULT_MAX_FILE_SIZE, KNOWN, Config, load, parse
from tupferl.errors import TupferlError

WHERE = "config.toml"


class TestReadingTheSettings(unittest.TestCase):
    def test_an_empty_file_is_the_defaults(self) -> None:
        self.assertEqual(Config(), parse("", WHERE))

    def test_every_known_key_arrives(self) -> None:
        """All four at once, each with a value nothing else in the file shares,
        so a parser that put one in the wrong field would be visible."""
        found = parse(
            'hostname = "work-laptop"\n'
            'editor = "nvim"\n'
            'ignore = ["*.log", ".cache/**"]\n'
            "max_file_size = 2048\n",
            WHERE,
        )
        self.assertEqual(
            Config(
                hostname="work-laptop",
                editor="nvim",
                ignore=["*.log", ".cache/**"],
                max_file_size=2048,
            ),
            found,
        )

    def test_the_default_size_limit_is_one_megabyte(self) -> None:
        """Plan §5 names the number, so it is asserted rather than derived."""
        self.assertEqual(1024 * 1024, DEFAULT_MAX_FILE_SIZE)
        self.assertEqual(DEFAULT_MAX_FILE_SIZE, parse("", WHERE).max_file_size)

    def test_a_partial_file_keeps_the_other_defaults(self) -> None:
        found = parse('editor = "vim"', WHERE)
        self.assertEqual("vim", found.editor)
        self.assertIsNone(found.hostname)
        self.assertEqual([], found.ignore)


class TestRejectingAnUnknownKey(unittest.TestCase):
    def test_a_typo_is_an_error_rather_than_silence(self) -> None:
        with self.assertRaises(TupferlError) as caught:
            parse('ignroe = ["*.pem"]', WHERE)
        self.assertIn("ignroe", str(caught.exception))

    def test_the_message_lists_what_is_accepted(self) -> None:
        """Naming the key alone leaves the reader guessing at the spelling; the
        message is generated from `KNOWN`, so it cannot drift from the check."""
        with self.assertRaises(TupferlError) as caught:
            parse("nonsense = 1", WHERE)
        for key in KNOWN:
            self.assertIn(key, str(caught.exception))

    def test_the_file_is_named(self) -> None:
        with self.assertRaises(TupferlError) as caught:
            parse("nonsense = 1", "/somewhere/config.toml")
        self.assertIn("/somewhere/config.toml", str(caught.exception))


class TestRejectingAWrongValue(unittest.TestCase):
    def test_a_string_where_a_number_belongs(self) -> None:
        with self.assertRaises(TupferlError) as caught:
            parse('max_file_size = "big"', WHERE)
        self.assertIn("int", str(caught.exception))

    def test_a_number_where_a_string_belongs(self) -> None:
        with self.assertRaises(TupferlError):
            parse("editor = 7", WHERE)

    def test_a_boolean_is_not_a_number(self) -> None:
        """`True` is an `int` in Python, so an `isinstance` check alone accepts
        `max_file_size = true` and then uses it as 1 -- every file "too large",
        with nothing in the file that looks wrong."""
        with self.assertRaises(TupferlError):
            parse("max_file_size = true", WHERE)

    def test_a_non_string_ignore_entry(self) -> None:
        with self.assertRaises(TupferlError) as caught:
            parse("ignore = [1, 2]", WHERE)
        self.assertIn("ignore", str(caught.exception))

    def test_a_size_limit_of_zero_would_refuse_everything(self) -> None:
        with self.assertRaises(TupferlError) as caught:
            parse("max_file_size = 0", WHERE)
        self.assertIn("positive", str(caught.exception))

    def test_a_negative_size_limit(self) -> None:
        with self.assertRaises(TupferlError):
            parse("max_file_size = -1", WHERE)

    def test_broken_toml_says_so(self) -> None:
        with self.assertRaises(TupferlError) as caught:
            parse("this is not toml", WHERE)
        self.assertIn("TOML", str(caught.exception))


class TestLoadingFromDisk(unittest.TestCase):
    def setUp(self) -> None:
        box = tempfile.TemporaryDirectory(prefix="tupferl-config-")
        self.addCleanup(box.cleanup)
        self.box = Path(box.name)

    def test_a_missing_file_is_the_defaults(self) -> None:
        """Not an error: `init` need not write one, and a user who wants no
        settings should not have to keep an empty file to say so."""
        self.assertEqual(Config(), load(self.box / "absent.toml"))

    def test_a_file_that_cannot_be_read_is_an_error(self) -> None:
        """A directory rather than a chmod: the suite runs as root in some
        containers, and root ignores the mode bits -- so a permissions fixture
        would pass there whatever the code did. `IsADirectoryError` is an
        `OSError` and not a `FileNotFoundError`, which is exactly the
        distinction under test.
        """
        unreadable = self.box / "config.toml"
        unreadable.mkdir()
        with self.assertRaises(TupferlError) as caught:
            load(unreadable)
        self.assertIn(str(unreadable), str(caught.exception))

    def test_a_real_file_is_parsed(self) -> None:
        where = self.box / "config.toml"
        where.write_text('editor = "helix"\n', encoding="utf-8")
        self.assertEqual("helix", load(where).editor)


if __name__ == "__main__":
    unittest.main()
