# CLAUDE.md — tupferl

Working agreements for AI agents and humans in this repository. They apply to
every task unless the maintainer says otherwise in the moment.

> Sections 0–8 come from `martinus/ai`'s `templates/CLAUDE.md.template`,
> distilled from four projects that arrived at the same rules separately. They
> are generic and were kept as written. Everything under "Project specifics" is
> tupferl's own and was written against the code that is actually here — when a
> claim there stops being true, fixing it is part of the change that broke it.

---

## 0. This file rots, and a stale claim is worse than none

Everything below is here because something went wrong once. That also makes this
file a liability: it is read as authority, so a sentence that stopped being true
actively misleads.

Two real examples, both from files shaped like this one:

- A test suite carried a docstring explaining why a whole class of tests could
  only run on one filesystem. It was wrong, and the repository had been
  disproving it continuously — another test file did the same thing on the other
  filesystem and passed every run. Nobody checked for years, because the prose
  read like a finding.
- A rule said "commit a checkpoint before running the mutation tool, it rewrites
  your tree." The tool had stopped doing that months earlier. The stale warning
  was worse than no warning: believing it is exactly what would justify reaching
  for `git checkout --` to tidy up, which is the operation the same rule exists
  to prevent.

So: **when you change what the code guarantees, change the claim in the same
commit.** When you find a claim here that is wrong, fixing it is part of the
task, not a separate chore — and say in the PR that you did, because a
correction here is usually more valuable than the code change that prompted it.

Prefer claims a reader can check. "Measured 2.4× on a 174k-file tree" survives
contact with reality; "this is faster" does not.

---

## 1. Landing a change

The loop, in this order:

1. **Branch before the first commit** — not after. Undoing a commit that landed
   on the main branch means branching at `HEAD` and then `git reset --hard`,
   which is the operation rule 6 exists to prevent.
2. Implement, with a regression test that **fails without the fix** (§2).
3. Run the full local gate (§7's preflight).
4. **Review your own diff, in proportion to risk (§3), and apply what it finds.**
5. Open the PR. Report CI. Stop.

**Order matters between 4 and 5.** A review that lands on an open PR means the
maintainer has already read code that was about to change. And if you run any
generated analysis over your own diff — a mutation sweep, a coverage delta — it
goes *last*, after the review, because a review of tests always edits tests and
every number is then computed against a tree that no longer exists.

### Merging

By default: open the PR, report its CI status, and stop. Wait to be told.

**Approval is per-PR and does not carry over.** "Merge it" for one PR is not
standing authority for the next, even when the next is a direct follow-up to the
same task. Ask again.

Every change reaches the main branch through a green PR — one-line fixes, docs
edits and lint reformats included. Enforce this in branch protection rather than
agreeing to it, or it decays the first time someone is in a hurry.

Here that enforcement is [`.github/rulesets/main.json`](.github/rulesets/main.json),
with an empty `bypass_actors` so it binds administrators too. GitHub does not
read the file; it has to be applied once by hand, and
[its README](.github/rulesets/README.md) says how and how to verify it took.

### The CI gate job

Require **one** status check that `needs:` every other job, not thirty job names
that silently stop being required as legs get renamed or added. Two parts of it
are load-bearing and both are easy to get wrong:

- `if: always()`, because a job whose dependency failed is *skipped*, not failed;
- an **explicit test of every dependency's result**, because a skipped required
  check counts as **satisfied**.

Without both, the gate goes green exactly when the matrix did not. Add a lint
that fails the build if a job is missing from the gate's `needs:`, if an entry is
stale, or if `if: always()` disappears.

---

## 2. The test bar: a test that cannot fail is decoration

**The bar is not "a test exists". It is "the test fails when the fix is
reverted."** Verify that; do not assume it:

1. Revert the fix in the working tree.
2. Run the suite, confirm the new test fails.
3. Restore the fix, confirm it passes.

Automate this if the project is big enough to justify it (a mutation harness that
applies a named edit in a throwaway copy of the tree, rebuilds, and reports
whether anything went red). **Never hand-roll it with `sed` on the real tree.**

**Coverage is not the bar.** Coverage says a line ran. It does not say anything
would have noticed it misbehaving. One suite measured at 88% line coverage
caught **4 of 24** deliberately injected one-line bugs, and two of those four
were caught by the compiler rather than by a test.

### Suspect the fixture before you suspect the code

Across seven consecutive review passes on one project, **every single one found a
test that could not fail** — not a weak test, a test whose assertion held no
matter what the code did. That rate did not fall as the work got better, which is
the argument for making this check mandatory rather than a finishing touch.

The shapes repeat, so they are worth memorising:

- a fixture whose winner leads on *every* column, so a much weaker ranking passes
  the test written for the strong one;
- two symmetric inputs, which make "which side was written" unobservable;
- a helper that cannot return the failing value (wrapping something that never
  returns NULL, so every `!= NULL` assertion holds regardless);
- an assertion that passes against its own mutation — usually a sign the thing
  being asserted has no reader at all;
- a negative assertion whose precondition was never established ("this shares
  nothing" is equally satisfied by "there was nothing to look at");
- a marker asserted in a shell's stdout that also appears in the harness's echo
  of the command that was typed.

Of every new assertion, ask: **what would have to be true for this to fail?**

### Two more traps

**A test containing a copy of the code it checks cannot fail.** Assert against an
independently-derived expectation, or against the real thing's output.

**A test that can only fail on one platform is worth having, and worth labelling
as such.** If a fixture behaves identically on three of four CI legs, say so at
the test — otherwise a green run on the other three reads as proof it guards
something. Where possible add a second test for the half of the guarantee every
platform can show.

Prefer driving the real thing over asserting on a mock.

---

## 3. Reviewing your own work, in proportion to risk

| The change | Review |
|---|---|
| Forgets, prunes, rewrites or replaces stored data; changes a security claim; caches a fact about the world | Full pass, every angle. This is where it pays. |
| Changes behaviour on a path a user reaches | One focused pass aimed at the specific hazard, or a careful read of the diff against the constraint that makes the obvious fix wrong |
| Mechanical, with a measured before and after and no new state | Re-read the diff yourself |

Pick the row honestly: **a change that *looks* mechanical but alters what reaches
disk is the top row.**

Asking four reviewers "what is wrong with this" will always return something.
That rate is a function of how many you ask, not of how bad the code is, and it
is how a backlog fills with polish nobody will ever do. If a finding is wrong, or
its fix exceeds the task's scope, **say so explicitly in the PR** rather than
silently dropping it.

Treat "this is only a quality pass" as an assumption to check. Findings in that
category have included real defects, more than once including a regression
introduced by the change under review.

---

## 4. Issues: file only what someone would actually do

While working you will notice bugs, missing tests, and improvements that do not
belong in the change at hand. Three answers, and filing is not the default:

- **Fix it here**, if it is small and in code the diff already touches. Say so in
  the PR body.
- **File it**, if it is P2 or above — or if it is a security item, a guard that
  would catch a future regression, or a symptom that actively misleads a user.
- **Say it in the task summary and let it go** otherwise.

A backlog of things nobody will do is worse than no record, because it hides the
two items that matter.

Each issue you file should carry enough to act on cold: what is wrong and the
concrete consequence; `file:line`; a reproduction, with measured numbers where
the claim is about performance; a suggested fix; and **any constraint that makes
the obvious fix wrong.**

**Search before you file, by `file:line` rather than by title.** A listing can
come back empty when it is not. The cost is not the duplicate — it is a duplicate
worked *in ignorance of the original*, where the original had already named the
better fix, so the wrong implementation ships and a second PR is needed to take
it back out.

### Priority labels

| Label | Meaning |
|---|---|
| `P0` | Exploitable now, loses data, or breaks a documented guarantee. Before anything else. |
| `P1` | Real defect with a plausible trigger, or a fix that unblocks other work. |
| `P2` | Worth fixing; no user is hurt today. |
| `P3` | Cleanup, polish, nice to have. |

Priority is **implementation order**, not just severity. Weigh impact,
reachability (can a stranger trigger it?), dependency (does another issue get
easier once this lands?), and cost — a five-line fix removing a whole class of
bug outranks a rewrite removing one instance.

An issue that only *contains* other issues gets `tracking` and **no priority**: a
container has no implementation order, its children do.

### An issue's suggested fix is a hypothesis

It was written by someone who had the bug in front of them and not the code you
are about to change. Check its premise before you write it — and often the
premise can be checked cheaply from something already in the repository, which
beats a fresh measurement. When your fix diverges from what the issue prescribed,
**say so in the PR body with the reason**; that is usually the most useful
paragraph in it.

---

## 5. Measurement: numbers, or it did not happen

- **Any claim about performance needs numbers**, and "below measurement" is a
  real answer. When a benchmark cannot resolve a difference, write that — not a
  figure with two decimal places. Say which statistic and how many samples; a
  difference of medians and a median of paired differences are not the same
  number.
- **A/B two distinctly-named binaries, and confirm they differ** before trusting
  anything. A stale object file once made a build look identical to itself and a
  real ~24% win got dismissed. **Commit before comparing revisions** — checking
  out the other branch carries uncommitted changes across, so you measure the
  same tree twice and report "no difference".
- **Interleave runs** (A B A B A B) and compare pairs. Machines drift by >10%
  over minutes. Judge micro-optimisations by mechanism plus a focused benchmark,
  not by one sub-benchmark delta — any edit, even to never-executed code, can
  shift alignment by ±3%.
- **One benchmark harness, reused.** Do not add a second script per question.
- **A lone surprising result that contradicts prior profiling is probably a
  measurement bug**, not a discovery. Reconcile before acting.
- **Distrust tools that measure by interception.** `strace -c` inflates the
  most-called syscall; it once reported one at 66% (really ~7%) and hid the real
  hotspot entirely.
- **Diagnose from the artefact, not from the build files.** Ask the binary what
  flags it was actually compiled with. A build system can silently drop an
  optimisation flag — one project's entire CI matrix was verifying unoptimised
  code for months while what shipped was optimised, and only the compiler's own
  recorded flag string settled it. An object-size comparison gave the wrong
  answer twice.
- **Before blaming your branch for a CI failure, measure the same job on the main
  branch**, enough times to see a one-in-forty flake. Two green runs prove
  nothing. And **diagnose a red leg from its numbers before fixing it** — a fix
  built on the wrong mechanism is worse than no fix, because it adds a permanent
  guard against something that cannot happen and leaves the real cause in place.
- **An optimisation sequence converges.** When the term you are about to remove
  is a small share of what is left, stop and say so.

---

## 6. Never discard a change you cannot get back

`git checkout -- <file>`, `git restore`, `git stash` and `git reset --hard` have
each destroyed uncommitted work. There is no reflog for a tree that was never
committed; the only recovery is writing it again from memory.

- Commit a checkpoint before anything that rewrites files in bulk.
- To undo your own edit, **rewrite the text you changed**. Do not discard the
  file. If you must restore, copy the file aside first and restore from the copy.
- **Tell subagents explicitly when they may not write.** A review agent asked
  only to *read* a diff has reverted a tree on its own initiative. Verify the
  tree yourself before believing a report that mentions touching it.

---

## 7. Writing things down

- **Comments explain *why*, especially why an obvious alternative was rejected.**
  A wrong "why" is worse than none — if you are not sure why a line is there, say
  that instead of inventing a reason.
- **Keep a "measured dead ends" section** and put every reverted experiment in
  it, with its numbers. Without it the same idea is re-attempted every six
  months. Say what would justify re-opening it.
- **Tools exist because the same mistake was made twice.** When you write one,
  put the full argument for its shape in its module docstring, and add a row to a
  table here saying *when* to reach for it. Reach for the tool rather than
  writing the loop again.
- **Preflight is one line**, and it is exactly what CI runs:

  ```sh
  ruff check . && ruff format --check . && mypy tupferl tests tools \
    && python -m tools.run_tests
  ```

  The table of what to reach for and when:

  | when | tool |
  |---|---|
  | a fix needs a test that fails without it (§2) | [`tools/mutate.py`](tools/mutate.py) |
  | a mutation run left more survivors than you can read | [`tools/reached.py`](tools/reached.py) |
  | the suite is slow enough that you are tempted to run a subset | [`tools/run_tests.py`](tools/run_tests.py) |
  | a long job is running detached and silence is ambiguous | [`tools/watch.py`](tools/watch.py) |

  ```sh
  python -m tools.mutate --base main --json sweeps/r.json   # generated from the diff
  python -m tools.mutate <spec>.py          # a table you wrote
  python -m tools.reached sweeps/r.json sweeps/c.json   # survivors missing tests
  python -m tools.watch $PID --log sweeps/r.log --done sweeps/r.json.done --match 'caught|SURVIVED'
  ```

  All four were ported from `martinus/woswoar` (Apache-2.0), where they were
  written; their module docstrings carry the argument for each one's shape and
  say which of its evidence was measured there rather than here.

---

## 8. Never trust a green run you cannot explain

The single most repeated lesson across these projects: **a passing check and a
real check are different things, and the gap is usually one measurable fact
away.** Collected instances, all real:

- A test-runner invocation that reported nothing because the binary had not been
  rebuilt. It exited 127 and the output filter matched no lines — **identical to
  a clean run.**
- A suppression file intended to silence one library's noise matched most of the
  program's own allocations, so a deliberately introduced leak of 769,200 bytes
  was reported by the sanitizer *and the run still exited 0*.
- A verification tool that quietly applied its edits to the wrong file and
  reported "nothing noticed this" — flattering the tests, which is the direction
  every bug in that class has erred.
- A required CI check that was skipped rather than run, and counted as satisfied.

So: **distrust a pass you cannot explain before you distrust the code.** Make
tools fail loudly rather than substituting a default. And when a check reports
success, confirm it did the work — a line count, an assertion count, a named
test in the log — rather than reading the absence of complaints as proof.

---

## Project specifics

*(Everything above is generic. Everything below is this project.)*

tupferl stores dotfiles in a git repository and syncs them between computers.
The design, the scope boundary and the build order are in
[`docs/plan.md`](docs/plan.md), which is the input this repository was built
from — read it before adding a feature, and especially before adding one it
lists under "What we deliberately do NOT build".

### Build & test

```sh
pip install -e '.[dev]'                   # ruff, mypy, hypothesis
python -m tools.run_tests                 # the suite, sharded across cores
ruff check . && ruff format --check . && mypy tupferl tests tools \
  && python -m tools.run_tests            # the preflight, exactly what CI runs
```

`python -m unittest discover -s . -t . -p 'test_*.py'` runs the same tests
serially, and is the one to reach for when a parallel run's output is confusing.

### Layout

| | |
|---|---|
| `tupferl/` | the package. `__main__.py` is the CLI and the only entry point |
| `tupferl/manifest.py` | what may be managed and what is. Read its docstring before touching the admission rules — four of the six are there to stop the wrong file being pushed |
| `tupferl/gitrepo.py` | every call to git. Nothing else in the package spawns a subprocess |
| `tupferl/copies.py` | what a stored copy is: bytes, the one mode bit that travels, and the single rule for "the target is already this file". Below `manage` and `sync`, because both write the same snapshots |
| `tupferl/sync.py` | the three-version comparison and everything it decides. `resolve` is pure, so plan §7.4's table is a test with no repository in it |
| `tupferl/merge.py` | the 3-way merge, over `git merge-file`. Bytes in, bytes out, and the conflict count is git's exit status |
| `tests/` | stdlib `unittest`, not pytest — the mutation tooling classifies unittest result objects |
| `tools/` | the test infrastructure, ported from `martinus/woswoar` |
| `docs/plan.md` | the plan this is built from |

Two things are not where a newcomer would guess, both on purpose:

- **`tests/support.py` builds a sandbox environment from nothing, not from
  `os.environ`.** Every variable tupferl reads is listed once, in
  `tupferl.paths.ENV_KEYS`, and the sandbox clears exactly that list. Inheriting
  and overriding is the shape that fails silently, and it fails *towards the
  real installation* — a test that then writes into the developer's own dotfiles
  repository.
- **`tests/profiles.py` holds every Hypothesis profile.** Selected by
  `TUPFERL_HYPOTHESIS_PROFILE`; `tools/mutate.py` sets it to `mutation` for the
  suites it runs. Without that, every mutant pays the full example budget and a
  sweep takes hours for no extra signal. It also holds `STATEFUL`/`STEPS`, which
  are *not* `max_examples`: one example of the sync state machine is a dozen
  real `tupferl sync` runs (~0.4s) against one `three_way` call (~3ms), and a
  single budget cannot suit both.
- **`sync` writes the snapshot last, and that ordering is a guarantee.** A run
  killed part-way then leaves the merge base *older* than both copies, so the
  next run merges conservatively. Written first, the same interruption leaves a
  snapshot claiming `$HOME` was already updated, and the next run copies the
  stale `$HOME` file over the new one. `tests/test_sync.py`'s
  `TestTheSnapshotIsWrittenLast` is the only thing that can see it.

### Testing rules this project adds to §2

- **No mocks for git.** Drive the real binary. The "remote" is a local bare
  repository in a temporary directory, and no test touches the network.
- **Property tests come before example tests for the sync engine** (plan §7.2),
  and the acceptance bar there is zero *unexplained* mutation survivors: every
  survivor is either killed by a new test or named equivalent with its reason,
  in the PR.
- **Never read a raw survivor list as a bug count.** Cross it with coverage
  (`python -m tools.reached results.json coverage.json --list`): a survivor on a
  line no test executes is a missing test; a survivor on a line the suite does
  execute is a weak fixture or an equivalent mutant. The two halves mean
  opposite things.

### Gotchas

- **`AssertionError: Cannot find component 'X' for 'tupferl.old_module.X'` from
  inside mypy** — moving a name between modules leaves `.mypy_cache` wrong.
  `rm -rf .mypy_cache` and re-run; it is not your change.
- **`ruff format --check` fails on code you did not touch** — the formatter's
  output changes between versions. The floors in `pyproject.toml` are the
  versions the tree is actually formatted by, not the oldest that would work.
- **A Hypothesis profile means something different in CI than on your machine.**
  Hypothesis registers and loads a derandomised profile of its own when it sees
  `CI` in the environment, and a profile that leaves a field unstated inherits
  whatever is default when it is registered. That is why every profile in
  `tests/profiles.py` states every field it cares about. To reproduce a CI-only
  failure of this shape, run the preflight the way CI does:

  ```sh
  CI=true TUPFERL_HYPOTHESIS_PROFILE=ci python -m tools.run_tests
  ```

- **A test that makes `git commit` fail must not do it by removing the git
  identity.** git falls back to `user@hostname`, and whether that *works*
  depends on the machine: in a Linux container the hostname is `(none)` and git
  refuses, on a macOS runner it is a real name and the commit succeeds. Three
  tests written that way were green on every Linux leg and red on macOS. Use
  `support.break_commits`, which installs a `pre-commit` hook that exits 1.
- **A unix socket cannot be bound at an arbitrary path.** `sun_path` is 104
  bytes on macOS, and a sandbox path plus `.local/share/tupferl/repo/…` exceeds
  it, so `bind` raises `OSError` and the test errors instead of testing. Use
  `os.mkfifo` where a "not a regular file" fixture is needed; it is the same
  class with no length limit.
- **`tomllib` is 3.11+, and this project supports 3.10.** `tupferl/config.py`
  falls back to `tomli`; the 3.10 CI leg is what proves the fallback is
  reachable, so do not drop that leg to save a minute.
- **A mutation sweep is minutes to hours.** Launch it detached, record the pid,
  and watch it with `tools/watch.py` — never answer "is it stuck?" with `pgrep
  -f`, whose pattern matches the asking shell's own command line. Point
  `--done` at `<json>.done`, never at the `--json` report, which under `--batch`
  and `--all` is rewritten after every file and so exists long before the run
  ends.
- **git never starts `receive-pack` for an up-to-date push**, so no hook on the
  remote can observe one: it compares the ref advertisement first and prints
  "Everything up-to-date". A `pre-receive` hook counting pushes therefore reports
  zero whether or not the push was skipped, and the test built on it passed with
  the code under test disabled. To tell the two apart, point `remote.origin.
  pushurl` somewhere that does not exist -- `fetch` still uses the real URL, so a
  sync that decides to push fails and one that skips it does not.
- **`PATH=""` makes git unfindable; deleting `PATH` does not.** With the variable
  absent, `subprocess` falls back to `confstr("CS_PATH")` and finds
  `/usr/bin/git` anyway. The obvious spelling of a "git is not installed" fixture
  merges successfully and passes for the wrong reason.
- **`str.splitlines()` splits on far more than `\n`** — `\x0b \x0c \x1c \x1d
  \x1e \x85 \u2028 \u2029` as well. A generated line containing one of those
  becomes two lines, and anything indexing by line number is then off by one for
  the rest of the file. It bit the sync state machine's *model*, not the code:
  git splits on `\n` alone, so the file under test was right and the expectation
  was wrong. Use `split("\n")` wherever the line count is load-bearing.
- **`git merge-file` refuses a file with a NUL byte in its first 8000 bytes**,
  with "Cannot merge binary files" and exit 255 — indistinguishable by exit code
  from git not being installed. `merge.is_text` asks the same question first, so
  a binary file both machines changed is reported as one conflict rather than as
  a broken git. Found by the merge property test on its first run.
- **A git receiving hook cannot update refs**: "ref updates forbidden inside
  quarantine environment". `unset GIT_QUARANTINE_PATH` first. Needed by
  `support.move_on_first_push`, which is the only way to make a push fail
  *because the remote moved* — pushing beforehand does not work, since a sync
  fetches before it pushes and would simply merge it.
- **A ref pushed to a name directly under `refs/` is a "funny refname"** and is
  rejected. Park a prepared commit under `refs/heads/`.
- **The generated sweep goes last.** Implement, preflight, review and *apply*
  the review, and only then `python -m tools.mutate --base main`. The table is
  generated from the lines as they stand, so any edit after it invalidates every
  row.

### Decisions from the plan's open questions (§9)

Recorded here as well as in the README because a later change is likelier to
read this file:

| question | answer | why |
|---|---|---|
| `argparse` or `click` | `argparse` | the plan says prefer fewer dependencies, and the command set is eight verbs with a handful of flags |
| snapshot format | plain copies under `.tupferl/state/<hostname>/` | the plan sanctions it for v1; content-addressing buys deduplication nobody has measured a need for |
| merge implementation | `git merge-file` | git is a hard requirement already, and its 3-way merge is battle-tested where a hand-written one would be the most defect-dense file in the project |

### Measured dead ends — do not re-attempt without new evidence

Empty, honestly: nothing has been tried and reverted yet. The first experiment
that does not survive goes here with its numbers, so it is not re-attempted in
six months.
