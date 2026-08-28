"""`tupferl status` and `tupferl diff`: the two commands that only look.

Plan §4 gives them one line each -- "show what changed locally and remotely.
Never modifies anything" and "show diffs between `$HOME` and the repository" --
and everything below follows from taking the first half of the first one
literally.

**Both borrow `sync.examine`, and that is the whole design.** `status` is a
promise about what the next `sync` will do, so it is worth very little if it is
computed by a second copy of the loop that does it. `examine` is that loop with
the writing taken out; this module reads its `Reading`s and turns each into a
sentence. A row added to plan §7.4's table therefore reaches `status` by
existing, rather than by somebody remembering to teach it.

**`status` fetches; it does not merge.** "What changed remotely" cannot be
answered without asking the remote, and fetching moves only the remote-tracking
refs -- no file in `$HOME`, no file in the working tree, no commit. What it
*cannot* do is tell the user what a merge of those new commits would produce,
because performing one is exactly the modification this command promises not to
make. So when the remote is ahead, the per-file lines are labelled as what they
are: the picture against this machine's checkout, before anything is pulled in.

**A fetch that fails is a worse status, not an error.** A laptop on a train
still has a local half to report, and `status` is the command someone runs when
something is already wrong. It says it could not reach the remote and carries
on; only `sync`, which is about to write, treats an unreachable remote as fatal.

**`diff` shows `$HOME` against the repository**, which is plan §4's wording and
not against the snapshot. The snapshot is a merge base -- machinery -- and a
user asking "what is different?" is asking about the two copies they can point
at. `merge.unified` renders it, the same function the conflict prompt's `[d]`
uses, so the two cannot disagree about which side is `---`.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from tupferl import gitrepo, manage, manifest, merge, paths, sync
from tupferl.copies import Blob
from tupferl.errors import TupferlError

#: What `status` says about each action `resolve` can reach, in the tense of
#: something that has not happened yet. A table for `sync.RULES`' reason: the
#: alternative is a chain of comparisons that has to be found and extended when
#: an action is added, where a missing row here is a `KeyError` on the first run.
#:
#: `CONFLICT` and `REFUSED` are deliberately absent. Both carry a *reason* --
#: a hunk count, a sentence -- so their lines are built rather than looked up,
#: and a row here would be a constant that could drift from the built one.
#: `UNCHANGED` is absent too: it is never printed.
SAYS: dict[str, str] = {
    sync.TO_REPO: "changed here; the next sync stores it",
    sync.TO_HOME: "changed in the repository; the next sync updates it",
    sync.RESTORED: "missing from $HOME; the next sync restores it",
    sync.MERGED: "changed on both, and the two merge cleanly",
}


def status(everything: bool = False, diffs: bool = False, wanted: str | None = None) -> int:
    """Print what changed on each side, and how far the remote has moved.

    **One verb for the three questions that only look**, because they are one
    walk. `--diff` shows the lines rather than a summary of them, and `--all`
    stops hiding the files with nothing to report -- neither is a different
    question from "what would the next sync do", which is why they were folded
    in: `status`, `diff` and `list` all read `sync.examine`, and three verbs
    over one walk is three things to learn instead of one.

    Always 0. `status` reports; it does not judge -- a conflict is a fact about
    the machine rather than a failure of this command, and the exit status that
    means "something needs a person" belongs to `sync --no-input`, which is the
    command a script would use for that. A status that exited 1 on a conflict
    would also exit 1 on a shell prompt that runs it every time.
    """
    if diffs:
        return difference(wanted)
    repo, config = manage.open_repo()
    host = paths.hostname(config.hostname)
    home = paths.home()

    lines = []
    marker = gitrepo.unfinished(repo)
    if marker is not None:
        # Reported rather than raised, unlike `sync`, which is about to write.
        # This is the command someone runs *because* something is wrong, and
        # refusing to say anything is the least useful moment to refuse.
        lines.append(
            f"{repo} has an unfinished git operation ({marker} is present), so the "
            f"repository's copies below may be a merge in progress rather than either "
            f"side; finish or abort it with git."
        )

    readings = list(sync.examine(repo, home, host))
    if not readings:
        # Still after the marker line above, not instead of it: a machine with
        # an unfinished merge and nothing managed yet is a real state -- the
        # sync that would have added the first file is what left the marker --
        # and it is the one thing worth saying about it.
        lines.append(manage.NOTHING_MANAGED)
        print("\n".join(lines))
        return 0
    readings = narrowed(readings, wanted, home)
    marked = overlays(repo, host) if everything else None
    per_file = sides(readings, marked)
    lines.extend(per_file)
    if per_file:
        # A blank line only when there is something above it to separate from.
        # Unconditionally, a machine with nothing to report opens its status
        # with an empty line, which reads as output having gone missing.
        lines.append("")
    lines.extend(remotely(repo))
    lines.append(summary(readings, marked))
    print("\n".join(lines))
    return 0


def narrowed(readings: list[sync.Reading], wanted: str | None, home: Path) -> list[sync.Reading]:
    """`readings`, limited to one file when the caller named one.

    One definition for both shapes of `status`. Written twice first -- once in
    the summary path and once in `difference` -- and the mutation table said so
    before a reader did: a row anchored on the filter matched in two places,
    which is the tool reporting duplication as an ambiguity.

    The name goes through `manifest.relative`, so `.bashrc`, `~/.bashrc` and an
    absolute path all arrive as the same thing -- see its docstring for why a
    tool that prints `.bashrc` has to take `.bashrc` back.
    """
    if wanted is None:
        return readings
    named = manifest.relative(wanted, home)
    found = [reading for reading in readings if reading.name == named]
    if not found:
        raise TupferlError(f"{named} is not managed; `tupferl status --all` lists what is.")
    return found


def overlays(repo: Path, host: str) -> set[PurePosixPath]:
    """The names this host overrides, asked of the code that decides it.

    `Reading.where` points at the overlay when there is one, so the answer is
    derivable from a path comparison -- and derived that way it is a second
    place that knows the repository's layout. `manifest.managed` already
    resolves shared against overlay and says which won.
    """
    return {item.name for item in manifest.managed(repo, host) if item.host}


def sides(readings: list[sync.Reading], marked: set[PurePosixPath] | None) -> list[str]:
    """One line per managed file, aligned. `marked` is `--all`.

    `None` means "only what has something to report", which is `status` on its
    own: silent about the unchanged ones, which are most of them on most
    machines -- `sync.report` is silent for the same reason, and a status that
    printed forty lines saying nothing happened would bury the one that
    mattered.

    A set of overridden names means "show everything, and mark those" -- the
    inventory `list` used to print, with each file's state beside it, which is
    the half `list` could not say.

    The column is measured over the names it will actually print, not over every
    managed name: a padding computed from a file with nothing to say leaves a
    gap the reader has to cross for no reason. A constant width would be worse
    still -- it is wrong for `.bashrc` and wrong for
    `.config/nvim/lua/plugins/telescope.lua`.
    """
    shown = (
        list(readings)
        if marked is not None
        else [reading for reading in readings if reading.outcome.action != sync.UNCHANGED]
    )
    if not shown:
        return []
    width = max(len(str(reading.name)) for reading in shown)
    if marked is None:
        return [f"{reading.name!s:<{width}}  {tells(reading.outcome)}" for reading in shown]
    return [
        f"{'host' if reading.name in marked else '    '}  "
        f"{reading.name!s:<{width}}  {tells(reading.outcome)}"
        for reading in shown
    ]


def tells(outcome: sync.Outcome) -> str:
    """What one file's outcome says, as the last column of a status line."""
    if outcome.action == sync.UNCHANGED:
        # Only reachable under `--all`; the plain status filters these out
        # before it gets here. A blank would leave the column ragged and read
        # as output that went missing.
        return "unchanged"
    if outcome.action == sync.REFUSED:
        return f"skipped: {outcome.why}"
    if outcome.sides is not None:
        # `sides is not None` rather than `action == CONFLICT`, which is
        # `sync.report`'s reason too: the two say the same thing, and this one
        # also narrows the type for the count below.
        settle = manage.count(outcome.sides.conflicts, "conflict")
        return f"changed on both, and they do not merge: {settle} to settle"
    return SAYS[outcome.action]


def summary(readings: list[sync.Reading], marked: set[PurePosixPath] | None = None) -> str:
    """The tail line, counted the way `sync.report`'s is.

    Through `sync.changed` rather than by listing which actions count as a
    change here, so "how many files would this sync touch?" has one definition
    -- the one whose `RULES` row also decides whether anything is written.

    Under `--all` it also counts the overlay, which is the one thing the old
    `list` said that nothing else does: how many of these files this machine
    has its own version of. Only there, because the plain status shows the
    files that are *changing* and an overlay count over that subset answers a
    question nobody asked.
    """
    unsettled = sum(1 for reading in readings if reading.outcome.sides is not None)
    moving = sum(1 for reading in readings if sync.changed(reading.outcome))
    line = f"{manage.count(len(readings))} managed, {moving} to change, {unsettled} in conflict"
    if marked is None:
        return line
    overridden = sum(1 for reading in readings if reading.name in marked)
    return f"{line}, {overridden} from this host's overlay"


def remotely(repo: Path) -> list[str]:
    """How far this machine and the remote have drifted apart.

    Plan §4's "and remotely", which is a fact about *commits* rather than about
    files: the per-file lines above compare `$HOME` with the checkout, and
    anything the remote holds that this machine has not pulled is invisible to
    them. Saying so is the point -- a status that showed one file changed while
    thirty commits waited on the remote would be true and misleading.
    """
    remote = gitrepo.first_remote(repo)
    if remote is None:
        return [
            "no remote is configured, so nothing is pulled or pushed; `git -C "
            f"{repo} remote add origin <git-url>` sets one."
        ]
    branch = gitrepo.branch(repo)
    if branch is None:
        return [
            f"{repo} has no branch checked out, so there is nothing to compare "
            f"the remote against; `git -C {repo} checkout main`."
        ]

    fetched = gitrepo.fetch(repo, remote)
    if not fetched.ok:
        return [
            f"could not reach {remote} ({gitrepo.reason(fetched)}), so everything "
            f"above is this computer against its own copy of the repository; run "
            f"`tupferl doctor` to check the remote."
        ]

    there = f"{remote}/{branch}"
    if not gitrepo.has_ref(repo, there):
        return [
            f"{there} does not exist yet, so nothing has been pushed; `tupferl sync` creates it."
        ]
    apart = gitrepo.distance(repo, "HEAD", there)
    if apart is None:
        # `distance` says `None` rather than `(0, 0)` precisely so this line can
        # exist: "up to date" is the one wrong answer when git could not tell.
        return [
            f"git would not compare HEAD with {there}, so how far apart they are is "
            f"unknown; run `tupferl doctor` to check the repository."
        ]
    ahead, behind = apart
    if not ahead and not behind:
        return [f"{there} is exactly what this computer has."]
    parts = []
    if behind:
        parts.append(f"{manage.count(behind, 'commit')} to pull")
    if ahead:
        parts.append(f"{manage.count(ahead, 'commit')} to push")
    said = [f"{there}: {', '.join(parts)}."]
    if behind:
        said.append(
            "The lines above compare $HOME with this computer's copy of the repository, "
            "so they do not yet include what is waiting to be pulled."
        )
    return said


def difference(wanted: str | None) -> int:
    """Print the diff between `$HOME` and the repository, for one file or all.

    Always 0, including when files differ -- `git diff` answers the same way,
    and the exit status of a command whose whole job is to show something should
    say whether it could, not what it found. There is no `--exit-code` here
    because plan §4 does not ask for one.

    Through `sync.examine`, whose `resolve` runs a real merge for any file both
    sides changed -- work this command then discards. That is accepted rather
    than optimised away: the merge is bounded by the files that differ on both
    sides and by plan §5's one-megabyte limit, and the alternative is a second
    walk of the managed files, which is the duplication `examine` exists to
    remove.
    """
    repo, config = manage.open_repo()
    host = paths.hostname(config.hostname)
    home = paths.home()

    readings = list(sync.examine(repo, home, host))
    if not readings and wanted is None:
        print(manage.NOTHING_MANAGED)
        return 0

    named = None if wanted is None else manifest.relative(wanted, home)
    readings = narrowed(readings, wanted, home)

    # A list rather than a counter. `shown` was an `int` that nothing read the
    # value of -- only its truthiness -- so `+= 1` could become `-= 1` or `+= 2`
    # with no observable difference, which is exactly what the mutation sweep
    # reported: three survivors on one line carrying more state than it needed.
    shown = [said for reading in readings if (said := shows(reading)) is not None]
    for said in shown:
        print(said)
    if not shown:
        # Two sentences, because one with the name substituted into it says the
        # opposite of what it means: "`.bashrc` differs between $HOME and the
        # repository" is exactly the report this branch exists to *deny*.
        print(
            "nothing differs between $HOME and the repository."
            if named is None
            else f"{named} is the same in $HOME as in the repository."
        )
    return 0


def shows(reading: sync.Reading) -> str | None:
    """One file's difference as text, or `None` when there is nothing to show.

    `None` for a file whose two copies are identical, which is the ordinary
    case: `diff` with no argument on a synced machine should print one sentence,
    not one heading per managed file.
    """
    # One arm, not two. `examine` yields `stored is None` **only** with a
    # `REFUSED` outcome -- so a separate `if stored is None` below it was code
    # no input could reach, and the sweep reported both of its mutants as
    # survivors. Here the same test does the narrowing and is reachable: any
    # `REFUSED` reading takes it, which the fifo fixtures drive.
    stored = reading.stored
    if stored is None or reading.outcome.action == sync.REFUSED:
        return f"{reading.name}: skipped, {reading.outcome.why}"
    if reading.found is None:
        return (
            f"{reading.name}: only in the repository, so there is nothing here to "
            f"compare it with; the next sync restores it."
        )
    if reading.found == stored:
        return None
    if not (merge.is_text(reading.found.data) and merge.is_text(stored.data)):
        # git's own rule for "there are no lines here", asked with git's own
        # probe -- see `merge.is_text`. Printing a binary diff is not an option
        # and printing nothing would read as "these are the same".
        return (
            f"{reading.name}: the two copies differ and are not text "
            f"({len(reading.found.data)} bytes here, {len(stored.data)} in the "
            f"repository), so there are no lines to show."
        )
    return rendered(reading.name, reading.found, stored)


def rendered(name: PurePosixPath, found: Blob, stored: Blob) -> str:
    """The unified diff, plus the executable bit when only that differs.

    The bit travels (plan §5) and `copies.Blob` compares it, so `chmod +x` with
    no edit is a real difference that a diff of the *lines* renders as nothing
    at all -- an empty answer to "why does status say this changed?".
    """
    text = merge.unified(str(name), found.data, stored.data)
    if found.executable == stored.executable:
        return text
    said = (
        f"{name}: executable here, not in the repository."
        if found.executable
        else f"{name}: executable in the repository, not here."
    )
    return f"{said}\n{text}" if text else said
