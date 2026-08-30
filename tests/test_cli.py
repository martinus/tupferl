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
from unittest import mock

import pytest

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


def parse(argv: list[str]) -> argparse.Namespace:
    """`build_parser().parse_args`, with argparse's own output swallowed."""
    with support.quiet():
        return build_parser().parse_args(argv)


class TestTheCommandSet:
    @pytest.mark.parametrize("command", COMMANDS)
    def test_every_command_in_the_plan_parses(self, command: str) -> None:
        """Through the public parser, one verb at a time."""
        assert parse([command, *ARGUMENTS.get(command, [])]).command == command

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
        assert registered == set(COMMANDS)

    @pytest.mark.parametrize("command", COMMANDS)
    def test_the_help_names_them_all(self, command: str) -> None:
        """What a stranger sees. `--help` is the discovery path for the whole
        tool, so an unregistered verb is invisible even if it works."""
        assert command in build_parser().format_help()

    def test_no_command_is_a_usage_error(self) -> None:
        """`required=True` on the subparsers: without it a bare `tupferl`
        returns 0 having done nothing, which any script reads as success."""
        with support.quiet() as said, pytest.raises(SystemExit) as caught:
            main([])
        assert caught.value.code == 2
        assert "required" in said.getvalue()

    def test_an_unknown_command_is_a_usage_error(self) -> None:
        with support.quiet() as said, pytest.raises(SystemExit):
            main(["nonesuch"])
        assert "invalid choice" in said.getvalue()


@pytest.mark.usefixtures("sandbox")
class TestEveryVerbIsWired:
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

    @pytest.mark.parametrize("command", COMMANDS)
    def test_no_planned_verb_falls_through(self, command: str) -> None:
        """Every one of the eight, driven for real in an empty sandbox.

        Most of them fail -- there is no repository -- and that is fine: the
        assertion is about *which* failure. `sync` and `diff` reaching "no
        repository at ..." is them being wired; either reaching `NOT_WIRED`
        would not be.
        """
        with support.quiet() as said:
            main([command, *ARGUMENTS.get(command, [])])
        assert NOT_WIRED not in said.getvalue()

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
        assert status == 2
        assert NOT_WIRED in said.getvalue()
        assert "tupferl nonesuch" in said.getvalue()


class TestTheFlags:
    """Plan §4's flags, each parsed once.

    Registering a verb is not registering its flags, and nothing else in this
    file would notice one going missing: an unbuilt command exits 2 whether it
    was reached with the right arguments or refused for the wrong ones.
    """

    def test_add_takes_several_paths_and_the_host_flag(self) -> None:
        args = parse(["add", "--host", "~/.bashrc", "~/.gitconfig"])
        assert args.host
        assert args.paths == ["~/.bashrc", "~/.gitconfig"]

    def test_add_without_the_host_flag_is_the_shared_version(self) -> None:
        assert not parse(["add", "~/.bashrc"]).host

    def test_remove_takes_one_path_and_the_host_flag(self) -> None:
        """The same flag name as `add`, meaning the same thing: this machine's
        overlay rather than the shared tree."""
        args = parse(["remove", "--host", "~/.gitconfig"])
        assert args.host
        assert args.path == "~/.gitconfig"

    def test_remove_without_the_host_flag_parses_as_false(self) -> None:
        assert not parse(["remove", "~/.gitconfig"]).host

    def test_sync_takes_the_scripted_resolution_flags(self) -> None:
        assert parse(["sync", "--ours"]).ours
        assert parse(["sync", "--theirs"]).theirs
        assert parse(["sync", "--no-input"]).no_input

    def test_ours_and_theirs_cannot_both_be_given(self) -> None:
        """ "Keep mine" and "keep theirs" cannot both be the answer, and a run
        that silently honoured the last one would resolve real conflicts the
        wrong way round."""
        with support.quiet(), pytest.raises(SystemExit):
            build_parser().parse_args(["sync", "--ours", "--theirs"])

    def test_status_takes_an_optional_path(self) -> None:
        assert parse(["status"]).path is None
        assert parse(["status", ".bashrc"]).path == ".bashrc"
        assert parse(["status", "--diff", ".bashrc"]).path == ".bashrc"

    def test_the_two_folded_verbs_are_flags_and_default_off(self) -> None:
        """`diff` and `list` were their own commands until they were folded in:
        all three read the same `sync.examine` walk, so they were three things
        to learn about one answer. Asserted off by default, because a `status`
        that showed every file or every diff without being asked would be the
        fold done badly rather than done."""
        plain = parse(["status"])
        assert not plain.all
        assert not plain.diff
        assert parse(["status", "--all"]).all
        assert parse(["status", "--diff"]).diff

    @pytest.mark.parametrize("gone", ["diff", "list"])
    def test_the_old_verbs_are_gone_rather_than_hidden(self, gone: str) -> None:
        """No aliases. A verb that still works but is absent from `--help` is a
        third state -- neither supported nor removed -- and the whole point of
        the fold is that there are six commands to learn."""
        with support.quiet(), pytest.raises(SystemExit):
            build_parser().parse_args([gone])


@pytest.mark.usefixtures("sandbox")
class TestTheRealProcess:
    def test_version_prints_the_package_version(self, sandbox: support.Sandbox) -> None:
        """One version, from one declaration -- `pyproject.toml` reads the same
        string, so this also catches the two disagreeing."""
        done = support.run_cli(["--version"], sandbox.env)
        assert done.returncode == 0
        assert done.stdout.strip() == f"tupferl {__version__}"

    def test_doctor_on_a_bare_machine_reports_and_exits_one(
        self,
        sandbox: support.Sandbox,
    ) -> None:
        done = support.run_cli(["doctor"], sandbox.env)
        assert done.returncode == 1
        assert "✘" in done.stdout
        assert "tupferl init" in done.stdout

    def test_doctor_on_a_healthy_machine_exits_zero(self, sandbox: support.Sandbox) -> None:
        """The other answer, so the test above is not satisfied by a `doctor`
        that always fails."""
        remote = support.make_remote(sandbox.tmp / "remote.git", sandbox.env)
        support.make_repo(paths.repo_dir(), sandbox.env, remote=remote)
        done = support.run_cli(["doctor"], sandbox.env)
        assert done.returncode == 0, done.stdout + done.stderr
        assert "✘" not in done.stdout

    def test_a_misconfigured_environment_is_reported_not_traced(
        self, sandbox: support.Sandbox
    ) -> None:
        """A relative `TUPFERL_DIR` raises inside `paths.repo_dir`, which
        `doctor` calls before it prints anything. It must arrive as the one
        sentence and exit 2 -- "tupferl could not run" -- rather than as a
        traceback, and rather than as `doctor`'s own exit 1, which would say a
        check failed when no check ever ran.
        """
        done = support.run_cli(["doctor"], {**sandbox.env, "TUPFERL_DIR": "relative/path"})
        assert done.returncode == 2
        assert "absolute" in done.stderr
        assert "Traceback" not in done.stderr
        assert done.stdout == ""

    def test_it_runs_from_anywhere(self, sandbox: support.Sandbox) -> None:
        """The repository is found from the environment, not from the current
        directory -- so a `cd` must not change what `doctor` looks at."""
        remote = support.make_remote(sandbox.tmp / "remote.git", sandbox.env)
        support.make_repo(paths.repo_dir(), sandbox.env, remote=remote)
        elsewhere = sandbox.tmp / "elsewhere"
        elsewhere.mkdir()
        done = support.run_cli(["doctor"], sandbox.env, cwd=elsewhere)
        assert done.returncode == 0, done.stdout + done.stderr
        assert str(paths.repo_dir()) in done.stdout
