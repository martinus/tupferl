"""The CLI: eight verbs, five of which work so far.

`argparse` rather than `click`, which plan §9 leaves to this decision. The
command set is eight verbs with a handful of flags, `argparse` is in the standard
library, and plan §5 says to prefer fewer dependencies -- a dependency that buys
decorators for a parser this shape is not worth the install.

**Every command in the plan is registered, including the three that are not
built.** The alternative -- registering only what works -- makes `tupferl sync`
say "invalid choice: 'sync'", which reads as "this tool has no sync" rather than
"not in this version". A verb that names its milestone tells the user which
release to wait for, and it fixed the CLI's shape while it was cheap to argue
about -- `add --host` and `sync --ours/--theirs` were parsed and tested a
milestone before anything read them.
"""

from __future__ import annotations

import argparse
import sys

from tupferl import __version__, doctor, manage
from tupferl.errors import TupferlError

#: Which milestone of `docs/plan.md` builds each unimplemented verb. The message
#: is generated from this rather than written out per command, so a verb cannot
#: be added here and left without one.
PLANNED: dict[str, int] = {
    "sync": 3,
    "status": 6,
    "diff": 6,
}


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

    drop = verbs.add_parser("remove", help="stop managing a file, keeping it in $HOME")
    drop.add_argument("path", help="a managed file")

    sync = verbs.add_parser("sync", help="pull, merge both directions, resolve, commit, push")
    # Plan §3.4: the flag set that makes a conflict resolvable without a human.
    # Mutually exclusive because "keep mine" and "keep theirs" cannot both be the
    # answer, and a run that silently honoured the last one would resolve real
    # conflicts the wrong way round.
    resolution = sync.add_mutually_exclusive_group()
    resolution.add_argument(
        "--ours", action="store_true", help="resolve conflicts in favour of $HOME"
    )
    resolution.add_argument(
        "--theirs", action="store_true", help="resolve conflicts in favour of the repository"
    )
    sync.add_argument(
        "--no-input", action="store_true", help="never prompt; report conflicts and skip them"
    )

    verbs.add_parser("status", help="show what changed locally and remotely; modifies nothing")

    diff = verbs.add_parser("diff", help="show diffs between $HOME and the repository")
    diff.add_argument("path", nargs="?", help="limit to one managed file")

    verbs.add_parser("list", help="list managed files, marking host-overlay ones")
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
            return manage.init(args.url)
        if args.command == "add":
            return manage.add(args.paths, to_host=args.host)
        if args.command == "remove":
            return manage.remove(args.path)
        if args.command == "list":
            return manage.listing()
        milestone = PLANNED[args.command]
        raise TupferlError(
            f"`tupferl {args.command}` is not built yet; it is milestone {milestone} of "
            f"docs/plan.md."
        )
    except TupferlError as wrong:
        # To stderr, and 2 rather than 1: 1 is `doctor`'s "a check failed", which
        # is a *result*, and a script that cannot tell it from "tupferl could not
        # run" will treat a broken install as a finding.
        print(f"tupferl: {wrong}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
