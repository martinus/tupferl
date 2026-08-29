"""Whether a long job is still working, still there, or gone -- said out loud.

Ported from `martinus/woswoar` (Apache-2.0), where the evidence quoted below
was collected; `woswoar#123` numbers index that repository's issues.

The other tools in here do something. This one only watches, and it exists
because two ways of *not* watching both failed in the same session.

**Liveness came from `pgrep -f <name>`, and that answers about the asker.** A
mutation sweep was checked with ``pgrep -f tools.mutate`` from a shell whose own
command line contained ``tools.mutate`` -- so the check matched itself and
reported a process alive that had been dead for ten minutes. Twice, to somebody
who had asked twice whether it was stuck. Any liveness test that matches on a
command line has this hole; the fix is not a better pattern but a different
question, so this takes a **pid recorded at launch** and asks the kernel.

**Silence was the only other signal.** A job that dies quietly looks exactly
like a job still working, and the reader cannot tell them apart by waiting
longer -- waiting longer is what both of them look like. So a death is an
*event* here, with its own line and its own exit status, and the line says the
job is gone rather than slow. `Monitor`'s own guidance is the same rule from the
other end: a watcher that greps only for the success marker stays silent through
a crash, and silence reads identically to progress.

What that buys is a stream a person can leave running: one line when the count
of finished work changes, one line when it ends, nothing in between. Emitting
per poll instead would be a line every twenty seconds for forty minutes, which
is the same as no signal at all by a different route.

**A job can also stop without ending, and that was the gap the first two
versions left.** Death and completion are events here; *stalling* was not, so a
sweep that was alive and wedged printed one line and then nothing -- which is
exactly what a sweep that is alive and slow prints. The reader is back to
guessing, from the other side of the same silence. Measured on this repository:
a nine-minute mutation run emitted a row every thirty seconds or so, and the run
that hung went quiet for ten minutes and more. So a stall long enough to be
abnormal is now its own line, and it says the job is still alive -- which is the
half that distinguishes it from `DIED`, and the half a reader cannot get by
waiting.

Reported on a doubling interval rather than every poll or once. Every poll is
the noise this tool exists to avoid; once is a line at minute five that a reader
arriving at minute forty cannot date. Doubling gives four lines in forty minutes,
each of which is real news -- a stall twice as long as the last report is worth
saying, one ten per cent longer is not.

**It watches rather than wraps.** The obvious shape is to run the job itself and
report as it goes, and that is wrong here: these jobs are already started
detached, precisely so that a foreground call timing out does not take them
with it. A watcher that insisted on being their parent would reintroduce the
coupling they were detached to avoid.

**It forks nothing and reads no `/proc`.** `os.kill(pid, 0)` is the whole of the
liveness check, so this runs on macOS as well, and a watcher cannot itself
become a source of load on a machine already busy with the thing it is
watching. `tests/test_watch.py` asserts the no-fork property rather than
trusting it, because a future "just shell out to `ps`" would restore exactly the
class of bug the first paragraph is about.
"""

from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path
from typing import TextIO

from tools import paint

#: What each of the four answers looks like on a terminal, by the word it starts
#: with.
#:
#: Chosen here rather than where the message is built, because `step` is a *pure
#: function* and thirty tests in `tests/test_watch.py` assert on the exact string
#: it returns. Painting inside it would make every one of those assertions depend
#: on whether the suite was run in a terminal -- green under `tools/run_tests.py`,
#: which pipes its batches, and red under `python -m unittest discover` in a
#: shell. Carrying the colour out of `step` instead means widening its result
#: through all thirty call sites, which is a larger change than this one and
#: about something else.
#:
#: So the print site reads the first word back. An unrecognised one is simply not
#: painted: a wording change then costs a colour, where a wrong colour would cost
#: a reader looking at an hour-long job and seeing green where it said DIED.
# survivor: arith -- `TypeError` every time, and not generated on purpose where the tool can tell:
#   these are `paint.GOOD + paint.HEAD`, two *attributes*, so `mutants.py`'s refusal -- which fires
#   on a string *literal* -- cannot see them. Proving them string-valued means resolving a name
#   across a module boundary, which is a type checker rather than a guard. CLAUDE.md records the
#   trade: refusing too strictly stops mutating real arithmetic and nothing would report it.
SHOUT = {
    "FINISHED": paint.GOOD + paint.HEAD,
    "DIED": paint.BAD + paint.HEAD,
    "STALLED": paint.ODD + paint.HEAD,
    # Dim, and the only one of the four that repeats. This prints every interval
    # for an hour; what a reader is waiting for is one of the other three, and
    # they have to catch an eye that has stopped looking.
    "working": paint.QUIET,
}


def tint(line: str, out: TextIO | None = None) -> str:
    """One of `Watch.step`'s answers, dressed for the stream it is going to.

    Public because the guard for `SHOUT` is a test that drives the four real
    messages through it -- a table keyed on the first word is exactly the thing
    that goes stale silently when a message is reworded, and the four are built
    three functions away from here.

    `out` for the same reason `paint.coloured` takes it: the decision is about a
    stream, and a caller writing somewhere other than stdout -- including a test
    -- must be able to say which.
    """
    # survivor: off-by-one -- `line.split(" ", 1)[0]` -- equivalent: `maxsplit` decides how many
    #   pieces come back, never what the *first* one is, so `[0]` is the same word at 1, 2 or any
    #   other bound.
    return paint.paint(line, SHOUT.get(line.split(" ", 1)[0].rstrip(":"), ""), out)


#: Seconds between polls. Twenty rather than one: the events this reports are
#: minutes apart, and the point of the tool is to be cheap enough to leave
#: running beside a job that is already using the machine.
INTERVAL = 20.0

#: Seconds without new work before a job is called stalled. Five minutes is a
#: judgement, and it is this one: the longest gap between rows in a healthy
#: mutation sweep here was about a minute, and the wedged one was silent for ten
#: and counting. Anything in between is arbitrary, so the value is generous --
#: a threshold that cries wolf is worse than none, because the next real one is
#: read as noise too.
#:
#: No job's own rhythm is knowable from here, which is why this is a flag. Zero
#: turns it off, for a job whose work genuinely arrives in one lump at the end.
STALE = 300.0


def alive(pid: int) -> bool:
    """Whether ``pid`` still exists, asked of the kernel rather than of a name.

    Signal 0 is the documented "check, do not deliver" spelling. Three answers
    collapse into two on purpose:

    - `ProcessLookupError` is the only one that means gone;
    - `PermissionError` means the process is there and belongs to somebody else,
      which is *alive* for this purpose -- reporting a job dead because it is not
      ours would be the same false negative as the pattern match was a false
      positive;
    - anything else propagates, because a watcher that swallows the unexpected
      is back to being silence.

    Two honest limits. The first is why the jobs this watches are detached: a
    *child* of this process that has exited but not been reaped is a zombie, and
    signal 0 succeeds for a zombie. Nothing here reaps, so a watcher that had
    spawned its subject could report it alive for ever. Watching something
    started elsewhere has no such state -- see the module docstring on why it
    does not wrap.

    The second is unfixable from here: pids are recycled, so a long-dead job
    whose number has been handed to something else reads as alive. Nothing short
    of a start time from `/proc` distinguishes them, and that is Linux-only for
    a window measured in tens of thousands of spawns. Said rather than guarded.
    """
    # 0 and the negatives are refused rather than answered, because `os.kill`
    # reads them as *process groups* -- 0 meaning "every process in my own
    # group", which signal 0 succeeds for unconditionally. A watcher handed one
    # would report alive for ever: the same false positive as the pattern match,
    # arriving through the front door. Raising is right where returning True
    # would be a lie and returning False would be a different one.
    if pid <= 0:
        raise ValueError(f"{pid} names a process group, not a process")
    try:
        # survivor: off-by-one -- signal 0 asks whether a process exists; signal 1 is SIGHUP. A
        #   fixture that let this run would send SIGHUP to the pid under test -- which in this suite
        #   is usually the test process itself, so the mutant kills the run rather than being
        #   noticed by it. That is why the row is `BROKE` and not `SURVIVED`, and why no honest
        #   fixture makes it otherwise.
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def counted(log: Path, pattern: re.Pattern[str]) -> int:
    """How many lines of ``log`` match, or 0 while it does not exist yet.

    Absent and empty are the same answer deliberately: a job that has not opened
    its log is at zero rows, and distinguishing the two would put a line on the
    stream for something nobody can act on.
    """
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return sum(1 for line in text.splitlines() if pattern.search(line))


class Watch:
    """One job being watched. Yields the lines a reader should see, in order.

    A class rather than a loop with prints in it, so the decisions -- what counts
    as an event, and in which order the two endings are checked -- can be tested
    without a clock or a subprocess. `main` is the part that sleeps.
    """

    def __init__(
        self,
        pid: int,
        log: Path,
        done: Path,
        pattern: re.Pattern[str],
        stale: float = STALE,
    ) -> None:
        self.pid = pid
        self.log = log
        self.done = done
        self.pattern = pattern
        self.stale = stale
        self.began = time.monotonic()
        #: -1 rather than 0, so a job that is already at zero rows still gets its
        #: first "working" line. Starting at 0 would make the opening silence
        #: indistinguishable from a job that never starts.
        # survivor: off-by-one -- equivalent: the comment above says the value is "-1 rather than
        #   0", and any negative number carries that meaning -- a first count of 0 is still an
        #   increase, so the opening "working" line still prints.
        self.last = -1
        #: When the count last moved. `began` rather than 0, because nothing has
        #: moved yet and "since we started watching" is what that means. In
        #: practice the first `step` overwrites it before anything reads it --
        #: `last` starts at -1, so the opening poll is always a change -- which
        #: is why a job stuck at zero rows stalls a `stale` after the watcher
        #: starts rather than after the job did. The watcher cannot know the
        #: latter; it was not there.
        self.moved = self.began
        #: The stall this last spoke about, which is what makes the next report a
        #: doubling rather than a repeat. Zero means nothing said yet -- and, as
        #: with `moved`, the opening poll overwrites it before `stalling` can
        #: read it, so this value is the declaration rather than the behaviour.
        # survivor: drop-assign -- equivalent, and the comment above says why: the opening poll
        #   assigns `self.told` again (`Watch.poll`) before `stalling` ever reads it, so this line
        #   declares the attribute rather than deciding anything.
        self.told = 0.0

    def minutes(self) -> int:
        return int((time.monotonic() - self.began) // 60)

    def step(self) -> tuple[str, int] | None:
        """The next line to print and an exit status, or None to keep watching.

        **`done` is checked before `alive`, and the order is the whole
        correctness of this function.** A job's last two acts are to write its
        report and to exit, so there is a window in which it is finished *and*
        gone. Asking about the process first reports a successful run as a death
        -- the exact false alarm this tool exists to prevent, arriving from the
        other direction.
        """
        rows = counted(self.log, self.pattern)
        if self.done.exists():
            return f"FINISHED after {self.minutes()}m: {rows} rows, report written", 0
        if not alive(self.pid):
            return (
                f"DIED after {self.minutes()}m: {rows} rows done, no report written "
                f"-- the job is gone, not slow",
                1,
            )
        if rows != self.last:
            self.last = rows
            self.moved = time.monotonic()
            self.told = 0.0
            return f"working: {rows} rows after {self.minutes()}m", -1
        return self.stalling(rows)

    def stalling(self, rows: int) -> tuple[str, int] | None:
        """The line for a job that is alive and getting nothing done.

        Status -1, like `working`: a stall is not a verdict. The job may still
        finish, and quite often does -- what the reader gains is the chance to
        go and look rather than to keep waiting on a guess.

        In seconds, where the rest of this speaks in minutes, and deliberately:
        this is the one figure a reader compares against `--stale`, which is a
        number of seconds they typed. Rendering it in a different unit from the
        flag that controls it is how a threshold gets read as not working.
        """
        if not self.stale:
            return None
        idle = time.monotonic() - self.moved
        # `told * 2` is the doubling. Below `stale` nothing is said at all, and
        # `told` is 0 until the first report, so that term cannot suppress it.
        # survivor: boundary -- equivalent in practice: both sides are `time.monotonic()`
        #   differences, so `<` and `<=` differ only when two floats are bit-identical. A fixture
        #   that produced that would be pinning the clock, not the rule.
        if idle < self.stale or idle < self.told * 2:
            return None
        self.told = idle
        return (
            f"STALLED: no new rows for {int(idle)}s at {rows} rows, "
            f"{self.minutes()}m in -- the process is alive and not working",
            -1,
        )


def a_pid(text: str) -> int:
    """``int``, minus the two values that name a group instead of a process.

    A second copy of `alive`'s guard, on purpose: this one exists for the
    *message*. argparse turns an `ArgumentTypeError` into a usage error naming
    the argument, where letting `alive` raise would be a traceback arriving one
    poll after the reader looked away.
    """
    pid = int(text)
    if pid <= 0:
        raise argparse.ArgumentTypeError(f"{pid} names a process group, not a process")
    return pid


#: How long `--pidfile` waits for the job to write one before giving up, unless
#: `--pidfile-wait` says otherwise. Ten seconds because the file is written
#: before the first row of work, so the only thing being waited on is an
#: interpreter starting -- and a watcher that hung here for ever would be the
#: silence this tool exists to break.
PIDFILE_WAIT = 10.0


def _await_pid(where: Path, interval: float, patience: float | None = None) -> int:
    """The pid in ``where``, once the job has written it.

    Waited for rather than required up front, because the watcher is usually
    started in the same breath as the job and may win the race. Polled at
    `--interval` or a tenth of a second, whichever is shorter: this is the one
    place a poll is cheap and the wait is measured in a startup rather than in
    minutes.

    A file that never appears, or holds something that is not a pid, is an
    error and not a wait: the job it names is not running, and a watcher that
    settled into watching nothing would be reporting the thing it was built to
    refuse.
    """
    step = min(interval, 0.1)
    # Resolved here rather than as `patience: float = PIDFILE_WAIT` in the
    # signature. A default argument is evaluated once, when the module is
    # imported, so a test that shortened the constant would still get ten
    # seconds -- which is `gitrepo.git`'s `timeout=None` and the paragraph it
    # carries, one module over, for the same reason.
    waiting = PIDFILE_WAIT if patience is None else patience
    deadline = time.monotonic() + waiting
    while True:
        try:
            return a_pid(where.read_text(encoding="utf-8").strip())
        except (OSError, ValueError, argparse.ArgumentTypeError) as exc:
            # survivor: boundary -- equivalent in practice: both sides are `time.monotonic()`
            #   differences, so `<` and `<=` differ only when two floats are bit-identical. A
            #   fixture that produced that would be pinning the clock, not the rule.
            if time.monotonic() >= deadline:
                raise SystemExit(f"no usable pid in {where} after {waiting:g}s: {exc}") from None
        # survivor: drop-call -- costs CPU, not correctness: without the sleep the loop spins
        #   instead of waiting, and still leaves at the same deadline. Worth keeping and not worth a
        #   test -- asserting *that the process idled* means timing the watcher, which is the
        #   flakiest kind of assertion this suite could hold.
        time.sleep(step)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tools.watch",
        description="Report a long job's progress, and say plainly when it dies.",
    )
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "pid", type=a_pid, nargs="?", help="the job's process id, recorded when it started"
    )
    where.add_argument(
        "--pidfile",
        type=Path,
        # The job writes its own, which is the only party that cannot get it
        # wrong. `... & echo $! > sweep.pid` looks equivalent and is not: it
        # produced no file at all in one shell, and the pid was recovered from
        # the process table fifteen times running -- by pattern, which is the
        # `pgrep -f` hole this tool exists to close, back in the step that feeds
        # it. `tools.mutate --json r.json` writes `r.json.pid`.
        help="read the pid from this file instead, waiting briefly for it to appear",
    )
    parser.add_argument("--log", type=Path, required=True, help="the file it appends progress to")
    parser.add_argument(
        "--done",
        type=Path,
        required=True,
        # A file left by an earlier run reads as an instant finish, because this
        # asks whether it is there and not who wrote it. Cheaper to say so than
        # to date-stamp it and be wrong about clock skew -- and `tools.mutate`
        # now clears its own marker before it starts, for that reason.
        #
        # It must be a file that means *finished*. A report written
        # incrementally does not: pointed at one, this announced a finish nine
        # minutes early. `tools.mutate --json r.json` writes `r.json.done` last.
        help="the file it writes when it finishes; not one it appends to as it goes",
    )
    parser.add_argument(
        "--match", default=".", help="count lines of --log matching this regex (default: all)"
    )
    parser.add_argument("--interval", type=float, default=INTERVAL, help="seconds between polls")
    parser.add_argument(
        "--pidfile-wait",
        type=float,
        default=PIDFILE_WAIT,
        # A real setting, not a test hook: a job whose interpreter has a large
        # import to do before it writes its pid is a reason to wait longer, and
        # a caller who knows the file is already there is a reason to wait less.
        #
        # That the two tests for the *deadline* can then take a fraction of a
        # second instead of ten is the reason it was added now rather than the
        # reason it exists -- they were the two slowest tests in the suite, 20s
        # of a 138s serial run, spent watching a clock.
        help="seconds to wait for --pidfile to appear (default: %(default)s)",
    )
    parser.add_argument(
        "--stale",
        type=float,
        default=STALE,
        help="say so when --log has not grown for this long; 0 to never (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    pid = (
        args.pid
        if args.pid is not None
        else _await_pid(args.pidfile, args.interval, args.pidfile_wait)
    )
    watch = Watch(pid, args.log, args.done, re.compile(args.match), args.stale)
    while True:
        event = watch.step()
        if event is not None:
            line, status = event
            # Flushed, because this is read as it happens rather than afterwards
            # -- an unflushed pipe is a watcher that says nothing for an hour and
            # then everything at once, which is the failure it was written for.
            print(tint(line), flush=True)
            if status >= 0:
                return status
        # survivor: drop-call -- costs CPU, not correctness -- the same argument as the sleep in
        #   `_await_pid`. Without it the poll spins instead of waiting and reports the identical
        #   lines at the identical points. Asserting that a process *idled* means timing the
        #   watcher, which is the flakiest assertion this suite could hold.
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
