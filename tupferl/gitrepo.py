"""Every call to git, in one place, because the ways it hangs are one fact.

Plan §5: call the `git` binary through `subprocess`, never GitPython. That keeps
behaviour identical to the user's own git -- their `.gitconfig`, their
credential helper, their merge driver -- and it is why the tests can drive the
real thing instead of a mock.

The wrapper exists for what a bare `subprocess.run(["git", ...])` gets wrong on
the paths that touch a remote:

- **git waits for a human.** An https remote with no cached credential asks on
  the terminal; an ssh remote with a passphrase asks too. In `tupferl doctor`,
  and in any non-interactive use, that is not a prompt -- it is a hang with no
  output. `GIT_TERMINAL_PROMPT=0` and ssh's `BatchMode=yes` turn both into a
  failure that can be reported.
- **and it waits without a bound.** A remote that accepts the connection and
  never answers holds the process for ever, so every call carries a timeout and
  the caller gets `TimedOut` rather than silence.

`GIT_SSH_COMMAND` is only set when the user has not set it. Someone who has
named their own ssh command has said something more specific than this default,
and overriding it would break the one configuration that was chosen deliberately.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

#: Long enough for a slow network, short enough that a wedged remote is reported
#: within the time someone will wait before pressing ctrl-c. Local operations do
#: not need it; they get it anyway, because a timeout that only some calls carry
#: is one somebody will forget to pass.
TIMEOUT = 30.0


class Result(NamedTuple):
    """What git said. `ok` rather than an exception, because most callers here
    are asking a question ("is this a repository?") where the answer "no" is not
    exceptional."""

    out: str
    err: str
    #: True when the call was killed by `TIMEOUT` rather than exiting. A separate
    #: field rather than a magic return code: "the remote never answered" and
    #: "the remote said no" need different sentences, and a caller that forgets
    #: the difference should not be able to print the wrong one by accident.
    timed_out: bool = False
    #: git's exit status, or `None` when git never got as far as exiting -- the
    #: timeout and not-installed paths, where there is no status to report.
    #:
    #: Only `merge_file` reads it, and it is why the field exists: `git
    #: merge-file` exits with the *count of conflict hunks*, so a caller reading
    #: only `ok` learns that there was at least one conflict and nothing more.
    #: Counting `<<<<<<<` markers in the output instead would be a second way to
    #: produce the same observable, and wrong for a file that legitimately
    #: contains one -- a merge driver's own documentation, say.
    code: int | None = None

    @property
    def ok(self) -> bool:
        """Whether git exited 0. Almost every caller wants this and nothing else.

        Derived rather than stored. It was a field beside `code` for about an
        hour, and two fields that must agree is one that can be set wrong:
        `Result(True, "", "", code=1)` was constructible and meaningless. `None`
        -- git never ran -- is not success.
        """
        return self.code == 0


def env() -> dict[str, str]:
    """The environment git is run with: the caller's, plus the two no-prompt keys."""
    prepared = dict(os.environ)
    prepared["GIT_TERMINAL_PROMPT"] = "0"
    prepared.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    return prepared


def git(
    args: list[str],
    cwd: Path | None = None,
    timeout: float | None = None,
    fed: str | None = None,
) -> Result:
    """Run git with `args`, and answer rather than raise.

    `cwd=None` means the current directory, which is right for `git --version`
    and wrong for everything else -- so every other caller passes one. Not
    defaulted to the repository, because that would make a call against the wrong
    tree the quiet option.

    `timeout=None` means `TIMEOUT`, resolved *here* rather than as
    `timeout: float = TIMEOUT` in the signature. A default argument is evaluated
    once, when the module is imported, so the signature version cannot be changed
    afterwards -- and `doctor.remote` reads `gitrepo.TIMEOUT` when it composes
    its message, so the two would disagree: a test that shortened the constant
    waited the full thirty seconds and was told it had waited half of one.

    `fed` is git's standard input, for `stage`'s `--pathspec-from-file=-`. Named
    that rather than `input`, which is a builtin and reads as the *result* of
    something here. `None` and not `""`: the empty string is a real thing to send
    -- an empty pathspec list -- and a signature that could not tell it from "no
    stdin at all" would make `stage`'s most dangerous case unexpressible.

    **No spawn failure escapes as an exception** (#3). The three `except` arms
    are one rule with three sentences, and the last is a catch-all on purpose:
    the first two were written for two failures that had already reached a user
    as a traceback, and a third was waiting -- `OSError: [Errno 7] Argument list
    too long` for a `git add` of tens of thousands of paths. Answering the whole
    class rather than the instance is what stops a fourth.
    """
    waiting = TIMEOUT if timeout is None else timeout
    if cwd is not None and not cwd.is_dir():
        # Checked before spawning, because `subprocess` reports both of these as
        # errors about the *command*: a missing directory raises
        # `FileNotFoundError`, indistinguishable from git not being installed, so
        # `doctor` on a machine with no repository said "git is not installed";
        # and a plain file raises `NotADirectoryError`, which nothing caught at
        # all and which reached the user as a traceback. Both were found by
        # reviewing this milestone rather than by anything failing.
        return Result("", f"{cwd} is not a directory")
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=fed,
            capture_output=True,
            text=True,
            # `surrogateescape`, not the default strict handler, and it is
            # load-bearing in both directions. A dotfile's name need not be valid
            # UTF-8 -- that is legal on Linux and `TestAPathThatIsNotUtf8` has a
            # fixture for it -- so `os.fsdecode` hands such a name back with
            # surrogates in it. Encoding *that* for `fed` raises
            # `UnicodeEncodeError` out of `subprocess.run`, which is a
            # `ValueError` and so sails past the three `except` arms above:
            # exactly the class of escape this function exists to prevent, and
            # introduced by the `--pathspec-from-file` change itself. The review
            # for #3 caught it before the PR opened.
            #
            # Decoding is the same hazard mirrored: git echoes those names back
            # in its output, and a strict handler raises `UnicodeDecodeError`
            # there instead -- which is what CLAUDE.md's gotcha about reading
            # `ls-files` through this function records, out of a half-finished
            # merge. One handler answers both.
            errors="surrogateescape",
            timeout=waiting,
            env=env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        # The whole argument list, not `args[0]`, which is `-c` as often as it is
        # the subcommand -- "git -c did not answer" names a flag and sends the
        # reader looking for a command by that name.
        shown = " ".join(args)
        return Result("", f"`git {shown}` did not answer within {waiting:g}s", True)
    except FileNotFoundError:
        # Before the `OSError` arm, which it is a subclass of. Now unambiguous:
        # `cwd` was checked above, so the only file `subprocess` can fail to find
        # here is `git` itself.
        return Result("", "git is not installed, or not on PATH")
    except OSError as refused:
        # Everything else the kernel can refuse a spawn with. `strerror` rather
        # than `str(refused)`, which repeats the errno and then appends `: 'git'`
        # -- a filename that is not the thing at fault, and which reads as "git
        # is missing" when the argument list was the problem.
        #
        # The size is what makes `[Errno 7] Argument list too long` actionable:
        # `ARG_MAX` bounds the *bytes* of argv (plus the environment and the
        # pointer array), so a count of arguments would leave the reader to
        # multiply. Not the arguments themselves: this is a list that was too
        # long to hand to a kernel, and printing it at a terminal is the same
        # mistake one layer up.
        size = sum(len(arg) + 1 for arg in args)
        return Result(
            "",
            f"could not run `git {args[0] if args else ''}` ({refused.strerror}), "
            f"with {len(args)} arguments totalling {size} bytes",
        )
    return Result(done.stdout.strip(), done.stderr.strip(), code=done.returncode)


#: The three stages a conflicted index holds for one path: the merge base, the
#: version on this branch, and the version being merged in. git numbers them,
#: and the numbering is what `sync` has to get right -- see `version`.
BASE, OURS, THEIRS = 1, 2, 3


def version(repo: Path, number: int, name: str) -> bytes | None:
    """One stage of `name` from a conflicted index, as bytes. `None` if absent.

    **Not through `git` above**, and that is the whole reason this exists. That
    function is `text=True` and returns `stdout.strip()`, which would decode a
    dotfile on the user's behalf and eat its trailing newline and any leading
    blank line -- the same loss `merge_file`'s docstring records as its reason
    for rewriting a file in place rather than reading `-p` back. A file's
    *content* cannot travel through a function that strips it.

    `cat-file` rather than `show`: `show` is porcelain and applies the
    repository's filters, so a `.gitattributes` with a clean/smudge rule would
    hand back something other than what is stored. This is the plumbing command
    for "give me these bytes".

    `None` for a stage that is not there, which is a real answer rather than an
    error: a file added on both branches independently has no stage 1, and that
    is exactly the "no common ancestor" that `conflicts.Sides.base` models as
    `None` already.
    """
    try:
        done = subprocess.run(
            ["git", "cat-file", "blob", f":{number}:{name}"],
            cwd=repo,
            capture_output=True,
            timeout=TIMEOUT,
            env=env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Same three failures `git` guards, and the same answer: nothing to
        # return. The caller is already in the "could not settle this" branch.
        return None
    return done.stdout if done.returncode == 0 else None


def reason(result: Result) -> str:
    """The one line of git's stderr worth showing a user.

    Not the first line, and not the last. Both were tried and both are wrong on
    a real failure, in opposite directions -- measured across four of them:

        $ git clone ssh://unreachable/x
        Cloning into 'x'...                      <- progress, on stderr
        ssh: connect ...: Connection refused
        fatal: Could not read from remote repository.
        <blank>
        Please make sure you have the correct access rights
        and the repository exists.               <- the last line

    Taking the first gives "Cloning into 'x'...", which says nothing went wrong.
    Taking the last gives "and the repository exists.", half a sentence of
    generic advice. `doctor` shipped the second of those for a milestone, and
    `init` shipped the first for about an hour.

    So: the first line git marked `fatal:`, which is git's own word for the line
    that explains. The fallbacks are for the commands that fail without one --
    the first line that is neither blank nor progress, and then a placeholder,
    because a message about something going wrong must not itself raise
    `IndexError`.
    """
    lines = [line.strip() for line in result.err.splitlines() if line.strip()]
    fatal = [line for line in lines if line.startswith("fatal:")]
    if fatal:
        return fatal[0]
    speaking = [line for line in lines if not line.startswith("Cloning into")]
    return speaking[0] if speaking else "no reason given"


def is_repository(where: Path) -> bool:
    """Whether `where` is the top of a git working tree.

    `--show-toplevel` compared against `where`, not `--is-inside-work-tree`,
    which answers yes for any *sub*directory. tupferl's repository directory
    being inside someone else's repository is a real shape -- `~/.local/share`
    is under `$HOME`, and a `$HOME` that is itself a git repository is a
    configuration people have -- and treating that as "already initialised"
    would have tupferl commit dotfiles into whatever tree happened to enclose it.
    """
    found = git(["rev-parse", "--show-toplevel"], cwd=where)
    if not found.ok:
        return False
    # Resolved on both sides: `--show-toplevel` reports the real path, so a
    # repository reached through a symlink -- which `/tmp` is on macOS -- would
    # otherwise compare unequal to the path the caller holds.
    return Path(found.out).resolve() == where.resolve()


def has_commits(repo: Path) -> bool:
    """Whether anything has been committed yet.

    A freshly cloned *empty* remote is a real state -- it is what someone gets
    on the first run, having just created the repository on their host -- and it
    is the one state where `HEAD` does not resolve. Asking before assuming keeps
    every caller from having to interpret a `rev-parse HEAD` failure, which is
    also what "the repository is broken" looks like.
    """
    return git(["rev-parse", "--verify", "HEAD"], cwd=repo).ok


def clone(url: str, into: Path) -> Result:
    """Clone `url` into `into`, which must not exist yet.

    `--` before the URL: a URL beginning with a dash would otherwise be read as
    an option by git. tupferl passes whatever the user typed straight through, so
    this is the one place to stop it.
    """
    into.parent.mkdir(parents=True, exist_ok=True)
    return git(["clone", "--", url, str(into)], cwd=into.parent)


def stage(repo: Path, paths: list[Path]) -> Result:
    """Stage exactly these paths, including deletions.

    **The pathspecs go on stdin, not on the command line** (#3).
    `--pathspec-from-file=-` with `--pathspec-file-nul`, which removes the
    `ARG_MAX` limit entirely: a single `tupferl add` of tens of thousands of
    files otherwise reached the user as `OSError: [Errno 7] Argument list too
    long`, a traceback rather than a sentence. Measured for a realistic path
    shape, roughly 64 000 files to exceed Linux's 2 MiB and 30 000 for macOS's
    1 MiB -- `tupferl add ~/.local/share/` is the shape that gets there.

    **Not batching, which is the obvious fix and is worse than the limit.**
    Several `git add` calls are not one operation: a failure in the third leaves
    the first two staged, and `manage.record` then commits a *partial* set under
    a message naming all of them. That is a wrong commit rather than a failed
    command. One call is the property worth keeping, and this is how it is kept.

    `--pathspec-file-nul`, so the separator is NUL. Newline separation would
    break on a managed filename containing one, which is legal and which `add`
    accepts today.

    The `--` is gone with the argv pathspecs, and nothing is lost: it was there
    because a managed file can legitimately begin with a dash, and a pathspec
    read from a file is never parsed as an option in the first place. That is
    the same guarantee by construction rather than by a separator.

    Paths are given absolute and passed relative, which is what git records.
    `relative_to` raising is the point rather than a nuisance: every caller
    builds these from `repo`, so one that does not is a bug, and failing here
    names the path instead of handing git something it will interpret against
    its own working directory.

    Requires git 2.25 (January 2020), which `doctor` checks and the README
    states.
    """
    if not paths:
        # Measured, and it is why this guard exists: an empty pathspec makes
        # `git add --all` stage the *whole repository*, untracked files included
        # -- and measured again for this spelling, because "the list is empty" is
        # exactly the case a change of mechanism could have altered: empty stdin
        # does the same thing as an empty argv pathspec. So the most dangerous
        # possible reading is still what git does by default with a list a caller
        # built and got wrong. A caller that means "everything" says so by
        # passing `repo` itself, which `sync` does.
        # survivor: off-by-one -- TODO: why is this acceptable?
        return Result("", f"nothing to stage in {repo}", code=1)
    relative = [str(path.relative_to(repo)) for path in paths]
    return git(
        ["add", "--all", "--pathspec-from-file=-", "--pathspec-file-nul"],
        cwd=repo,
        fed="\0".join(relative) + "\0",
    )


def commit(repo: Path, message: str, empty: bool = False) -> Result:
    """Commit what is staged. Returns git's own answer, unexamined.

    Deliberately *not* checking "is anything staged" first: that is a race, and
    git already answers it. A caller that cares looks at `changed` before
    staging, which is a question about the working tree rather than about a
    moment in the middle of this function.

    `empty` is for the one caller that has nothing to commit and needs a commit
    anyway: a clone of an empty remote sits on an unborn branch, the one state
    where `HEAD` does not resolve and half of git answers oddly. `init` used to
    normalise that by writing a settings file into the repository; the settings
    left, and an empty commit says the same thing without inventing a file.
    """
    return git(["commit", *(["--allow-empty"] if empty else []), "-m", message], cwd=repo)


def first_remote(repo: Path) -> str | None:
    """The remote tupferl syncs with, or `None` if there is none.

    The first one git lists, which for a repository `tupferl init` created is
    `origin` and the only one. Someone who has added a second has said something
    tupferl has no way to interpret -- plan §4 has one `sync` and no `--remote`
    flag -- so the alternative to picking one is refusing to sync at all.

    `doctor` and `sync` both call this rather than reading `git remote`
    themselves, so the two cannot report on and push to different remotes.
    """
    named = git(["remote"], cwd=repo)
    return named.out.splitlines()[0] if named.ok and named.out else None


def branch(repo: Path) -> str | None:
    """The checked-out branch, or `None` on a detached HEAD.

    `symbolic-ref` rather than `rev-parse --abbrev-ref HEAD`, which answers the
    literal string `HEAD` when detached -- a name that then gets pushed as a
    branch. It also works on an *unborn* branch, which is the state a clone of an
    empty remote is in, and where `rev-parse` fails outright.
    """
    found = git(["symbolic-ref", "--short", "HEAD"], cwd=repo)
    return found.out if found.ok else None


def has_ref(repo: Path, ref: str) -> bool:
    """Whether `ref` resolves. Used for `<remote>/<branch>`, which does not exist
    until the first fetch that finds something there."""
    return git(["rev-parse", "--verify", "--quiet", ref], cwd=repo).ok


def is_ancestor(repo: Path, older: str, newer: str) -> bool:
    """Whether `older` is reachable from `newer` -- so, whether it holds nothing
    `newer` does not.

    This is how `sync` tells "the remote moved under me" from "the push was
    refused for some other reason", and it is a fact rather than a reading of
    git's English. The alternative -- matching `! [rejected]` or `non-fast-forward`
    in stderr -- is a decision made from a string that changes with git's version
    and with the user's locale.
    """
    return git(["merge-base", "--is-ancestor", older, newer], cwd=repo).ok


def distance(repo: Path, here: str, there: str) -> tuple[int, int] | None:
    """How many commits each of `here` and `there` holds that the other does not.

    `(ahead, behind)`, which is the pair `status` prints as "n to push, m to
    pull". `None` when git could not answer -- an unborn branch, or a ref that
    does not resolve -- because `(0, 0)` would say "the two agree", and a status
    line claiming a machine is up to date with a remote it could not read is the
    one wrong answer this function must not give.

    `--left-right --count` in one call rather than two `rev-list | wc -l`: the
    two numbers are then computed from one walk of one symmetric difference, so
    they cannot come from refs that moved between them.
    """
    counted = git(["rev-list", "--left-right", "--count", f"{here}...{there}"], cwd=repo)
    # survivor: branch -- tupferl/gitrepo.py:426 in distance() -- the `if` is never taken -- named
    #   in the code: `distance`'s own comment calls this an equivalent mutant and says why -- a git
    #   that would not compare produces output the parse below rejects anyway, reaching the same
    #   `None`.
    if not counted.ok:
        # An **equivalent mutant** lives here, and it is named rather than
        # tested: removing this `if` changes nothing observable, because a
        # failed `rev-list` prints nothing, so `"".split()` is `[]`, so the
        # format guard below returns `None` two lines later. Both roads reach
        # the same answer and no test can tell them apart.
        #
        # Kept because the two questions are different -- "git failed" and "git
        # answered something that is not two numbers" -- and a reader who found
        # only the second would reasonably conclude that a failed call falls
        # through to `int(fields[0])`.
        return None
    fields = counted.out.split()
    if len(fields) != 2 or not all(field.isdigit() for field in fields):
        # git's own format, so this is unreachable today. Guarded rather than
        # asserted because the alternative to a guard is a `ValueError` traceback
        # out of `status`, and plan §5 rules that out for anything a user meets.
        return None
    return (int(fields[0]), int(fields[1]))


def fetch(repo: Path, remote: str) -> Result:
    """Bring the remote's refs up to date without touching the working tree."""
    return git(["fetch", "--quiet", remote], cwd=repo)


def merge(repo: Path, ref: str) -> Result:
    """Merge `ref` into the current branch, without opening an editor.

    `--no-edit` because there is no terminal to open one on in a `sync`, and git
    would otherwise wait for a message on the merge commits it writes itself.
    """
    return git(["merge", "--no-edit", ref], cwd=repo)


def abort_merge(repo: Path) -> Result:
    """Put the tree back as it was before a merge that conflicted."""
    return git(["merge", "--abort"], cwd=repo)


def unmerged(repo: Path) -> list[str]:
    """The paths git left with conflict markers, relative to the repository.

    From `conflicted` rather than from `diff --name-only`, which quotes a path
    that is not plain ASCII and splits on newlines -- so a file named with either
    came back as a name that does not exist, and `sync` then told the user to go
    and resolve it. One question, one spelling, one git call.
    """
    # survivor: order -- tupferl/gitrepo.py:474 in unmerged() -- `sorted` becomes `list` --
    #   equivalent for the names git can produce: `conflicted` builds its dict from `ls-files -u
    #   -z`, which git emits in byte order, and UTF-8 byte order agrees with Python's code-point
    #   order. The *reversal* of this line is caught by
    #   `test_two_conflicted_files_come_back_in_a_settled_order`; only the redundancy of sorting an
    #   already-sorted list is not.
    return sorted(conflicted(repo))


def conflicted(repo: Path) -> dict[str, dict[int, int]]:
    """Every unmerged path, and the file mode git recorded for each of its stages.

    One `ls-files -u` rather than a call per path: a merge that went wrong across
    forty files would otherwise spawn forty processes to ask a question git
    already answered in one.

    The mode is what carries the executable bit through a merge git could not
    finish -- and what says whether the entry is a regular file at all, since
    git records a symlink as `0o120000` and a submodule as `0o160000`. See
    `copies.REGULAR`, and what happens without that check.

    **Bytes, not `git()`.** A path is not required to be UTF-8, and `git()` runs
    with `text=True`, so a latin-1 dotfile name raised `UnicodeDecodeError` out
    of `subprocess.run` -- past the two exceptions `git()` catches, out of
    `reconcile`, and out of a half-finished merge. `surrogateescape` keeps such a
    name round-trippable back to the filesystem. `version` bypasses `git()` for
    the same family of reason.

    `-z`, so the answer is unquoted and a path may hold a newline or a tab
    without being split or escaped. Lines look like `100644 <sha> 2\t.bashrc`. A
    path is absent when it is not conflicted, and a *stage* is absent when that
    side has no version of the file -- a delete against an edit, which `sync`
    cannot settle by picking lines and does not pretend to.
    """
    try:
        done = subprocess.run(
            ["git", "ls-files", "-u", "-z"],
            cwd=repo,
            capture_output=True,
            timeout=TIMEOUT,
            env=env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    # survivor: branch -- tupferl/gitrepo.py:513 in conflicted() -- the `if` is never taken --
    #   equivalent: an exit status git sets also means empty stdout, and the parse below turns that
    #   into `{}` by itself -- the loop sees one empty field, `not raw` skips it, and the same empty
    #   dict comes back. The guard is an early-out, not a decision. Its neighbour on 514 (`return
    #   {}` becoming `return None`) *is* caught, by `TestWhenGitWillNotAnswerAboutConflicts`.
    if done.returncode != 0:
        return {}
    stages: dict[str, dict[int, int]] = {}
    for row in done.stdout.split(b"\0"):
        head, _, raw = row.partition(b"\t")
        parts = head.split()
        # Only the empty tail after the final NUL is reachable here; git's own
        # output always has three fields and two numbers. Guarded anyway because
        # the alternative to a skipped row is a `ValueError` from inside a merge.
        # survivor: connector -- tupferl/gitrepo.py:522 in conflicted() -- `or` becomes `and` --
        #   equivalent given git's contract: the two operands disagree only for a row that is non-
        #   empty *and* malformed, and `ls-files -u -z` always emits three fields. The trailing
        #   empty field after the final NUL satisfies both spellings. The guard is there so a future
        #   git cannot turn a merge into a `ValueError`, which is a claim about git rather than
        #   about this code.
        if not raw or len(parts) != 3:
            continue
        stages.setdefault(raw.decode("utf-8", "surrogateescape"), {})[int(parts[2])] = int(
            parts[0], 8
        )
    return stages


def push(repo: Path, remote: str, ref: str) -> Result:
    """Push `ref` to `remote`, naming both rather than relying on tracking.

    A branch created locally -- which is what a clone of an *empty* remote
    leaves, since there was no branch to track -- has no upstream, and a bare
    `git push` there fails with advice rather than pushing.
    """
    return git(["push", remote, ref], cwd=repo)


#: Files git leaves in the git directory while an operation is half-done. A
#: killed `tupferl sync` leaves one behind, and the next sync would otherwise
#: build on top of a merge nobody finished.
UNFINISHED = ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD")


def configured_pager(repo: Path) -> str:
    """`pager.diff`, else `core.pager`, or `""` when git has neither.

    Here because this module is every call to git, and because *this* is the
    point of reading it: a user who set `core.pager = delta` configured how they
    read a diff, not how they read a git diff. Asking git rather than parsing
    `~/.gitconfig` also gets the include directives, the system file and the
    per-repository override for free -- all of which a hand-rolled reader would
    get wrong on the machine that used them.

    **`pager.diff` first, which this missed.** git's order for a command's pager
    is `GIT_PAGER`, then `pager.<cmd>`, then `core.pager`, then `PAGER` -- and
    reading only `core.pager` finds nothing on a machine that configured the
    per-command key, which is the common shape:

        [pager]
            diff = "if [ -t 1 ]; then delta; else cat; fi"

    That user had `delta` set up for every git command and `tupferl status
    --diff` printed plain. Measured against git 2.43 with both keys set: the
    `pager.diff` one runs.

    `diff` is the right command name to ask under. It is the question this
    output is -- a unified diff -- and the same reasoning that makes reading
    git's config right at all: they configured how they read a diff.

    **`pager.<cmd>` may be a boolean, and then it is not a command.** Measured
    against git 2.43, with `core.pager` and `$PAGER` both set:

    | `pager.diff` | git |
    |---|---|
    | a string | runs it |
    | `false`, `no`, `off`, `0` | **no pager at all** -- neither `core.pager` nor `$PAGER` is read |
    | `true`, `yes`, `on`, `1` | says *page*, not how: falls to `core.pager`, then `$PAGER` |

    Read as a command instead, `false` would be *spawned* -- it exits 1, which
    is not one of the two codes `show` falls back on, so the user would get an
    empty screen and no diff. Nobody would connect that to a setting that means
    "do not page".

    git is asked whether the value is a boolean rather than the answer being
    reimplemented here, because "which spellings are false" is six of them and
    a list that would go stale silently. A false one comes back as `cat`, which
    is git's own way of saying *do not page* and which `show` already knows.
    """
    found = git(["config", "--get", "pager.diff"], cwd=repo)
    if found.ok:
        yes_or_no = git(["config", "--get", "--bool", "pager.diff"], cwd=repo)
        if not yes_or_no.ok:
            return found.out
        if yes_or_no.out == "false":
            return "cat"
    core = git(["config", "--get", "core.pager"], cwd=repo)
    return core.out if core.ok else ""


def configured_editor(repo: Path) -> str:
    """`core.editor`, or `""` when git has none.

    Beside `configured_pager` and for the same reason: someone who wrote
    `core.editor = nvim` said how they edit text, not how they edit a commit
    message. Asking git rather than reading `~/.gitconfig` gets the includes,
    the system file and the per-repository override, which a hand-rolled reader
    gets wrong exactly on the machine that used them.
    """
    found = git(["config", "--get", "core.editor"], cwd=repo)
    return found.out if found.ok else ""


def unfinished(repo: Path) -> str | None:
    """The name of the marker file for an operation left half-done, if any.

    `None` also when git cannot say -- the caller has to ask git something else
    anyway, and a *guess* that nothing is in progress is the more dangerous of
    the two errors: `doctor` reports the git failure separately, and `sync`
    fails on its own first git call.
    """
    inside = git(["rev-parse", "--git-dir"], cwd=repo)
    if not inside.ok:
        return None
    git_dir = repo / inside.out
    return next((name for name in UNFINISHED if (git_dir / name).exists()), None)


#: The most conflict hunks `git merge-file` will report. Above this its exit
#: status saturates, so a larger number cannot be told from an error -- which is
#: a fact about the git binary, and so lives here beside the call rather than in
#: `merge.py` beside the caller.
MOST_CONFLICTS = 127


def merge_file(
    ours: Path,
    base: Path,
    theirs: Path,
    labels: tuple[str, str, str],
    union: bool = False,
) -> Result:
    """3-way merge `ours` against `theirs` over `base`, rewriting `ours` in place.

    `union=True` is git's `--union`: a hunk both sides changed keeps *both*
    versions, one after the other, with no markers. That is the prompt's "keep
    both", and it is git's own resolution rather than a marker-stripper written
    here -- which would have to re-derive, from the marked text, the hunk
    boundaries git had already worked out.

    In place, rather than `-p` to stdout, for a reason that is not style: `git`
    above strips the output it returns, so a merged file would lose its trailing
    newline and any leading blank line. Reading the file back gives exactly the
    bytes git wrote.

    `-L` three times because the default labels are the *file names*, and these
    are temporary files -- the conflict markers would carry a path under
    `/tmp` into the user's `$HOME` file.

    `Result.code` is the answer: `git merge-file` exits with the number of
    conflict hunks, and negative (255 here) on an error such as an unreadable
    input. `ok` is therefore true only for a clean merge.
    """
    marks = [argument for label in labels for argument in ("-L", label)]
    mode = ["--union"] if union else []
    # The conflict style is pinned rather than left to the user's git config,
    # because `conflicts.hunks` parses the markers back out to show them. The
    # git that "started honouring it" this guard was written against has
    # arrived: measured on **git 2.55**, `merge-file` takes `merge.conflictStyle`
    # from a config *file* and emits the three-section form, where 2.43 ignored
    # the setting entirely.
    #
    # **`-c` is not the pin, and was never tested to be.** Measured on 2.55 with
    # the config isolated, `-c merge.conflictStyle=zdiff3` produces the
    # *two*-section form -- so `merge-file` reads the file and ignores the
    # command-line override, and the `-c` spelling this used to carry was inert
    # in exactly the case it existed for. A user with `merge.conflictStyle =
    # zdiff3` in `~/.gitconfig` -- an ordinary setting -- got a base section that
    # `conflicts.hunks` read as part of *this computer's* version, so the prompt
    # showed them the wrong side and `[m]`/`[t]` could discard the edit they
    # meant to keep.
    #
    # `--no-diff3` rather than `--no-zdiff3`: both defeat either setting
    # (measured, all four combinations), and `--diff3` is far older than
    # `--zdiff3`, which arrived in git 2.35.
    style = ["--no-diff3"]
    return git(
        ["merge-file", *style, *mode, *marks, str(ours), str(base), str(theirs)],
        cwd=ours.parent,
    )


def staged(repo: Path) -> bool:
    """Whether the index holds anything a commit would record.

    `git diff --cached --quiet` exits 1 when it does, which is why this is a
    `bool` and not a `Result`: there is no third answer to distinguish, and a
    git that could not run at all fails on the `add` that necessarily precedes
    this.
    """
    return not git(["diff", "--cached", "--quiet"], cwd=repo).ok
