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

from tests import support
from tupferl import __version__, paths
from tupferl.__main__ import PLANNED, build_parser, main

#: Plan §4's table, which is the contract this file guards. Written out rather
#: than taken from `PLANNED` plus `doctor`: a list derived from the code under
#: test cannot notice the code losing a command.
COMMANDS = ("init", "add", "remove", "sync", "status", "diff", "list", "doctor")

#: The arguments each unbuilt verb needs before argparse will let it through, so
#: the tests below reaches the command rather than a usage error. Only the verbs
#: with required positionals appear.
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


#: The verbs that do something, written out from what has actually been built.
#: Milestone 2 moved four of them out of `MILESTONES` and into here, milestone 3
#: moved `sync`; the two sets together must be the whole command set, which is
#: what `test_every_command_is_either_built_or_planned` asserts.
BUILT = {"doctor", "init", "add", "remove", "list", "sync"}

#: Which milestone of `docs/plan.md` builds each verb that is *not*. Written out
#: from the plan rather than read from `PLANNED`: the first version of the test
#: below took the number from the code it was checking, so every mutation of
#: those numbers survived -- a test containing a copy of its subject cannot fail
#: (CLAUDE.md §2). The mutation sweep is what found it.
MILESTONES = {
    "status": 6,
    "diff": 6,
}


class TestTheUnbuiltCommands(unittest.TestCase):
    def test_each_says_which_milestone_builds_it(self) -> None:
        for command, milestone in MILESTONES.items():
            with self.subTest(command=command), support.quiet() as said:
                status = main([command, *ARGUMENTS.get(command, [])])
            self.assertEqual(2, status)
            self.assertIn(f"milestone {milestone}", said.getvalue())

    def test_the_code_agrees_with_the_plan(self) -> None:
        """Stated separately from the test above, and against the same written-out
        table: this is the assertion that the *numbers* are right, where that one
        asserts they reach the user."""
        self.assertEqual(MILESTONES, PLANNED)

    def test_the_message_names_the_command_and_the_plan(self) -> None:
        """Checked through the real process, because the message goes to stderr
        and an in-process call cannot show that it did."""
        env = support.sandbox_env(paths.home())
        done = support.run_cli(["status"], env)
        self.assertEqual(2, done.returncode)
        self.assertIn("tupferl status", done.stderr)
        self.assertIn("milestone 6", done.stderr)
        self.assertEqual("", done.stdout)

    def test_every_command_is_either_built_or_planned(self) -> None:
        """A verb registered but in neither set raises `KeyError` at runtime
        rather than saying anything useful. The two halves must partition the
        command set exactly -- no gaps, and no verb claiming to be both."""
        self.assertEqual(set(COMMANDS), BUILT | set(PLANNED))
        self.assertEqual(set(), BUILT & set(PLANNED))
        self.assertEqual(set(PLANNED), set(MILESTONES))


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

    def test_diff_takes_an_optional_path(self) -> None:
        self.assertIsNone(self.parse(["diff"]).path)
        self.assertEqual(".bashrc", self.parse(["diff", ".bashrc"]).path)


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
