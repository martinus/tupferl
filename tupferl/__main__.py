"""The CLI: plan §4's eight verbs, all of them built.

`argparse` rather than `click`, which plan §9 leaves to this decision. The
command set is eight verbs with a handful of flags, `argparse` is in the standard
library, and plan §5 says to prefer fewer dependencies -- a dependency that buys
decorators for a parser this shape is not worth the install.

**Every command in the plan was registered from milestone 1, including the ones
not yet built.** The alternative -- registering only what works -- makes
`tupferl sync` say "invalid choice: 'sync'", which reads as "this tool has no
sync" rather than "not in this version". A verb that named its milestone told
the user which release to wait for, and it fixed the CLI's shape while it was
cheap to argue about: `add --host` and `sync --ours/--theirs` were parsed and
tested a milestone before anything read them.

Milestone 6 built the last two, so that table is gone and what stands in its
place is a guard: a verb that parses and reaches no branch below raises instead
of falling off the end of `main` and returning `None` where an exit status was
promised. It is unreachable while `tests/test_cli.py` holds -- which drives all
eight and drives a ninth that does not exist, so the guard is checked in both
directions rather than assumed.

**`--host` means the same thing on `add` and on `remove`**: this machine's
overlay rather than the shared tree. Two flags with one name and one meaning,
which is why `remove --host` is a flag rather than the `unhost` verb it was
briefly going to be -- plan §4 caps the command set at eight verbs, and a ninth
would have been the same idea spelled twice.
"""

from __future__ import annotations

import argparse
import sys

from tupferl import __version__, doctor, inspection, manage, sync
from tupferl.errors import TupferlError

#: The middle of the sentence a verb gets when it parses and reaches no branch
#: in `main`. A constant because `tests/test_cli.py` asserts it *absent* for all
#: eight real verbs, and a phrase written out there could stop matching the one
#: written here -- at which point the test passes by looking for the wrong string.
NOT_WIRED = "parses but is not wired to anything"


def build_parser() -> argparse.ArgumentParser:
    """The whole command line, as plan §4's table defines it.

    `required=True` on the subparsers: without it, a bare `tupferl` returns 0
    having done nothing, which is the shape of a successful run and would be
    read as one by any script.
    """
    parser = argparse.ArgumentParser(
        prog="tupferl",
        description="Store dotfiles in a git repository and share them between computers.",
    )
    parser.add_argument("--version", action="version", version=f"tupferl {__version__}")
    verbs = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    init = verbs.add_parser("init", help="clone or create the repository, then sync")
    init.add_argument("url", help="git URL to clone, or to set as the remote")

    add = verbs.add_parser("add", help="start managing files")
    add.add_argument("paths", nargs="+", help="files or directories under $HOME")
    add.add_argument(
        "--host",
        action="store_true",
        help="store in this host's overlay rather than as the shared version",
    )
    add.add_argument(
        "--anyway",
        action="store_true",
        # Plan §4 caps the *verbs* at eight and says nothing about flags;
        # `--host` set the precedent. This one exists because the alternative to
        # a refusal a user can overrule is a refusal they work around by moving
        # the file, which is worse for them and teaches them to distrust the
        # rule.
        # Names the patterns rather than pointing at `manifest.SECRETS`, which a
        # user reading `tupferl add --help` cannot see. A help text that cites a
        # module is a help text written for the wrong reader.
        help=(
            "store a file whose name says it holds a credential "
            "(.ssh/id_*, .aws/credentials, .netrc, .pgpass, .gnupg/*, *.pem, *.key)"
        ),
    )

    drop = verbs.add_parser("remove", help="stop managing a file, keeping it in $HOME")
    drop.add_argument("path", help="a managed file")
    drop.add_argument(
        "--host",
        action="store_true",
        help="remove only this host's overlay, leaving the shared version managed",
    )

    syncing = verbs.add_parser("sync", help="pull, merge both directions, resolve, commit, push")
    # Plan §3.4: the flag set that makes a conflict resolvable without a human.
    # Mutually exclusive because "keep mine" and "keep theirs" cannot both be the
    # answer, and a run that silently honoured the last one would resolve real
    # conflicts the wrong way round.
    resolution = syncing.add_mutually_exclusive_group()
    resolution.add_argument(
        "--ours", action="store_true", help="resolve conflicts in favour of $HOME"
    )
    resolution.add_argument(
        "--theirs", action="store_true", help="resolve conflicts in favour of the repository"
    )
    syncing.add_argument(
        "--no-input", action="store_true", help="never prompt; report conflicts and skip them"
    )

    # One verb for the three questions that only look. They were `status`,
    # `diff` and `list`, and all three read the same `sync.examine` walk -- so
    # they were three things to learn about one answer. `--diff` shows the lines
    # instead of a summary of them; `--all` stops hiding what has nothing to
    # report, which is the inventory `list` printed.
    look = verbs.add_parser("status", help="what the next sync would do; modifies nothing")
    look.add_argument("path", nargs="?", help="limit to one managed file")
    look.add_argument("--all", action="store_true", help="every managed file, not just changed")
    look.add_argument("--diff", action="store_true", help="show the lines that differ")

    verbs.add_parser("doctor", help="check git, the remote, permissions and dangling state")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one command. Returns the exit status rather than calling `sys.exit`,
    so the tests can drive it in-process as well as through a subprocess."""
    args = build_parser().parse_args(argv)
    try:
        if args.command == "doctor":
            return doctor.main()
        if args.command == "init":
            # Plan §4: init clones "then runs a first sync", which is what makes
            # `tupferl init <url>` alone set up a second machine. Composed here
            # rather than inside `manage.init`, because `sync` already imports
            # `manage` for the repository and the commit -- the other direction
            # would be a cycle, and a command calling a command is what this
            # function is for.
            return manage.init(args.url) or sync.main()
        if args.command == "add":
            return manage.add(args.paths, to_host=args.host, anyway=args.anyway)
        if args.command == "remove":
            return manage.remove(args.path, from_host=args.host)
        if args.command == "sync":
            return sync.main(no_input=args.no_input, ours=args.ours, theirs=args.theirs)
        if args.command == "status":
            # `getattr` for neither: argparse always sets both, and a default
            # reached through `getattr` is one that hides a parser that stopped
            # defining the flag.
            return inspection.status(everything=args.all, diffs=args.diff, wanted=args.path)
        raise TupferlError(
            f"`tupferl {args.command}` {NOT_WIRED}, which is a bug in tupferl rather "
            f"than anything you did; please report it."
        )
    except TupferlError as wrong:
        # To stderr, and 2 rather than 1: 1 is `doctor`'s "a check failed", which
        # is a *result*, and a script that cannot tell it from "tupferl could not
        # run" will treat a broken install as a finding.
        print(f"tupferl: {wrong}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
