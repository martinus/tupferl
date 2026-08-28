"""The command line, driven as a user drives it: `python -m tupferl`.

Plan §7.1 prefers driving the real thing where speed allows, and here it does --
these are milliseconds. It is also the only way to see what actually reaches
stdout, stderr and the exit status, which for `doctor` *is* the product.

The exit statuses are three, and telling them apart is the point of several tests
below: 0 is "ran, nothing wrong", 1 is `doctor`'s "a check failed" -- a result --
and 2 is "tupferl could not run". A script that cannot tell 1 from 2 treats a
broken install as a finding.
"""

from __future__ import annotations

import argparse
import unittest
from unittest import mock

from tests import support
from tupferl import __version__, paths
from tupferl.__main__ import NOT_WIRED, build_parser, main

#: Plan §4's table, which is the contract this file guards. Written out rather
#: than taken from `PLANNED` plus `doctor`: a list derived from the code under
#: test cannot notice the code losing a command.
COMMANDS = ("init", "add", "remove", "sync", "status", "doctor")

#: The arguments each verb needs before argparse will let it through, so the
#: tests below reach the command rather than a usage error. Only the verbs with
#: required positionals appear.
ARGUMENTS = {"init": ["git@example.invalid:dotfiles"], "add": ["~/.bashrc"], "remove": [".bashrc"]}


class TestTheCommandSet(unittest.TestCase):
    def test_every_command_in_the_plan_parses(self) -> None:
        """Through the public parser, one verb at a time."""
        for command in COMMANDS:
            with self.subTest(command=command):
                args = build_parser().parse_args([command, *ARGUMENTS.get(command, [])])
                self.assertEqual(command, args.command)

    def test_nothing_else_is_registered(self) -> None:
        """The other direction, which parsing cannot show: a verb nobody planned.

        Read off `_SubParsersAction`, which is private. That is acceptable here
        and only here, because the failure direction is loud: if argparse moves
        the name, `registered` comes back empty and this test fails, rather than
        passing while checking nothing.
        """
        registered = {
            name
            for action in build_parser()._actions
            if isinstance(action, argparse._SubParsersAction)
            for name in action.choices
        }
        self.assertEqual(set(COMMANDS), registered)

    def test_the_help_names_them_all(self) -> None:
        """What a stranger sees. `--help` is the discovery path for the whole
        tool, so an unregistered verb is invisible even if it works."""
        text = build_parser().format_help()
        for command in COMMANDS:
            self.assertIn(command, text)

    def test_no_command_is_a_usage_error(self) -> None:
        """`required=True` on the subparsers: without it a bare `tupferl`
        returns 0 having done nothing, which any script reads as success."""
        with support.quiet() as said, self.assertRaises(SystemExit) as caught:
            main([])
        self.assertEqual(2, caught.exception.code)
        self.assertIn("required", said.getvalue())

    def test_an_unknown_command_is_a_usage_error(self) -> None:
        with support.quiet() as said, self.assertRaises(SystemExit):
            main(["nonesuch"])
        self.assertIn("invalid choice", said.getvalue())


class TestEveryVerbIsWired(support.SandboxCase):
    """Plan §4's eight verbs all reach code, and a ninth would say so.

    This replaced `TestTheUnbuiltCommands` when milestone 6 built the last two.
    That class asserted which milestone each unbuilt verb named; with none left,
    what is worth guarding is the other direction -- that a verb registered in
    the parser and forgotten in `main` is *loud*. `main` returns the exit status
    rather than calling `sys.exit`, so without the guard such a verb returns
    `None`, and `sys.exit(None)` is a **successful** exit: the shape of a command
    that worked.

    Both directions are here on purpose. The negative test alone would pass if
    `NOT_WIRED` never appeared for any input at all, which is CLAUDE.md §2's
    "negative assertion whose precondition was never established"; the positive
    one establishes it by registering a verb `main` has no branch for.
    """

    def test_no_planned_verb_falls_through(self) -> None:
        """Every one of the eight, driven for real in an empty sandbox.

        Most of them fail -- there is no repository -- and that is fine: the
        assertion is about *which* failure. `sync` and `diff` reaching "no
        repository at ..." is them being wired; either reaching `NOT_WIRED`
        would not be.
        """
        for command in COMMANDS:
            with self.subTest(command=command), support.quiet() as said:
                main([command, *ARGUMENTS.get(command, [])])
            self.assertNotIn(NOT_WIRED, said.getvalue())

    def test_a_verb_with_no_branch_says_so(self) -> None:
        """The precondition for the test above: the guard can fire.

        Through a parser with a ninth verb on it, because that is the only way
        to reach the branch -- and it is exactly the mistake the branch is for:
        somebody adds a subparser and forgets `main`.
        """

        def ninth() -> argparse.ArgumentParser:
            parser = build_parser()
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    action.add_parser("nonesuch")
            return parser

        with mock.patch("tupferl.__main__.build_parser", ninth), support.quiet() as said:
            status = main(["nonesuch"])
        self.assertEqual(2, status)
        self.assertIn(NOT_WIRED, said.getvalue())
        self.assertIn("tupferl nonesuch", said.getvalue())


class TestTheFlags(unittest.TestCase):
    """Plan §4's flags, each parsed once.

    Registering a verb is not registering its flags, and nothing else in this
    file would notice one going missing: an unbuilt command exits 2 whether it
    was reached with the right arguments or refused for the wrong ones.
    """

    def parse(self, argv: list[str]) -> argparse.Namespace:
        with support.quiet():
            return build_parser().parse_args(argv)

    def test_add_takes_several_paths_and_the_host_flag(self) -> None:
        args = self.parse(["add", "--host", "~/.bashrc", "~/.gitconfig"])
        self.assertTrue(args.host)
        self.assertEqual(["~/.bashrc", "~/.gitconfig"], args.paths)

    def test_add_without_the_host_flag_is_the_shared_version(self) -> None:
        self.assertFalse(self.parse(["add", "~/.bashrc"]).host)

    def test_remove_takes_one_path_and_the_host_flag(self) -> None:
        """The same flag name as `add`, meaning the same thing: this machine's
        overlay rather than the shared tree."""
        args = self.parse(["remove", "--host", "~/.gitconfig"])
        self.assertTrue(args.host)
        self.assertEqual("~/.gitconfig", args.path)

    def test_remove_without_the_host_flag_parses_as_false(self) -> None:
        self.assertFalse(self.parse(["remove", "~/.gitconfig"]).host)

    def test_sync_takes_the_scripted_resolution_flags(self) -> None:
        self.assertTrue(self.parse(["sync", "--ours"]).ours)
        self.assertTrue(self.parse(["sync", "--theirs"]).theirs)
        self.assertTrue(self.parse(["sync", "--no-input"]).no_input)

    def test_ours_and_theirs_cannot_both_be_given(self) -> None:
        """ "Keep mine" and "keep theirs" cannot both be the answer, and a run
        that silently honoured the last one would resolve real conflicts the
        wrong way round."""
        with support.quiet(), self.assertRaises(SystemExit):
            build_parser().parse_args(["sync", "--ours", "--theirs"])

    def test_status_takes_an_optional_path(self) -> None:
        self.assertIsNone(self.parse(["status"]).path)
        self.assertEqual(".bashrc", self.parse(["status", ".bashrc"]).path)
        self.assertEqual(".bashrc", self.parse(["status", "--diff", ".bashrc"]).path)

    def test_the_two_folded_verbs_are_flags_and_default_off(self) -> None:
        """`diff` and `list` were their own commands until they were folded in:
        all three read the same `sync.examine` walk, so they were three things
        to learn about one answer. Asserted off by default, because a `status`
        that showed every file or every diff without being asked would be the
        fold done badly rather than done."""
        plain = self.parse(["status"])
        self.assertFalse(plain.all)
        self.assertFalse(plain.diff)
        self.assertTrue(self.parse(["status", "--all"]).all)
        self.assertTrue(self.parse(["status", "--diff"]).diff)

    def test_the_old_verbs_are_gone_rather_than_hidden(self) -> None:
        """No aliases. A verb that still works but is absent from `--help` is a
        third state -- neither supported nor removed -- and the whole point of
        the fold is that there are six commands to learn."""
        for gone in ("diff", "list"):
            with self.subTest(command=gone), support.quiet(), self.assertRaises(SystemExit):
                build_parser().parse_args([gone])


class TestTheRealProcess(support.SandboxCase):
    def test_version_prints_the_package_version(self) -> None:
        """One version, from one declaration -- `pyproject.toml` reads the same
        string, so this also catches the two disagreeing."""
        done = support.run_cli(["--version"], self.env)
        self.assertEqual(0, done.returncode)
        self.assertEqual(f"tupferl {__version__}", done.stdout.strip())

    def test_doctor_on_a_bare_machine_reports_and_exits_one(self) -> None:
        done = support.run_cli(["doctor"], self.env)
        self.assertEqual(1, done.returncode)
        self.assertIn("✘", done.stdout)
        self.assertIn("tupferl init", done.stdout)

    def test_doctor_on_a_healthy_machine_exits_zero(self) -> None:
        """The other answer, so the test above is not satisfied by a `doctor`
        that always fails."""
        remote = support.make_remote(self.tmp / "remote.git", self.env)
        support.make_repo(paths.repo_dir(), self.env, remote=remote)
        done = support.run_cli(["doctor"], self.env)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertNotIn("✘", done.stdout)

    def test_a_misconfigured_environment_is_reported_not_traced(self) -> None:
        """A relative `TUPFERL_DIR` raises inside `paths.repo_dir`, which
        `doctor` calls before it prints anything. It must arrive as the one
        sentence and exit 2 -- "tupferl could not run" -- rather than as a
        traceback, and rather than as `doctor`'s own exit 1, which would say a
        check failed when no check ever ran.
        """
        done = support.run_cli(["doctor"], {**self.env, "TUPFERL_DIR": "relative/path"})
        self.assertEqual(2, done.returncode)
        self.assertIn("absolute", done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        self.assertEqual("", done.stdout)

    def test_it_runs_from_anywhere(self) -> None:
        """The repository is found from the environment, not from the current
        directory -- so a `cd` must not change what `doctor` looks at."""
        remote = support.make_remote(self.tmp / "remote.git", self.env)
        support.make_repo(paths.repo_dir(), self.env, remote=remote)
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        done = support.run_cli(["doctor"], self.env, cwd=elsewhere)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)
        self.assertIn(str(paths.repo_dir()), done.stdout)


if __name__ == "__main__":
    unittest.main()
