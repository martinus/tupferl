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

  **And then it started being true again.** `--accept` writes `# survivor:`
  comments into `tupferl/**` and `tools/**`, so the tool does now rewrite your
  tree — only under that flag, and only by inserting comment lines. Both halves
  of this example are the same lesson: the claim has to be re-checked against the
  code, in both directions, whenever either moves. §6 applies to `--accept`
  exactly as it once did to the sweep: commit first, and never tidy up after it
  with `git checkout --`.

So: **when you change what the code guarantees, change the claim in the same
commit.** When you find a claim here that is wrong, fixing it is part of the
task, not a separate chore — and say in the PR that you did, because a
correction here is usually more valuable than the code change that prompted it.

Prefer claims a reader can check. "Measured 2.4× on a 174k-file tree" survives
contact with reality; "this is faster" does not.

**Everything here describes the tree as it is, except where an entry opens
`Historical —`.** Those describe code that has been removed, kept because the
argument for removing it is worth more than the code was; the names in them will
not resolve. Nothing else should read as retrospective, and if it does that is a
claim to fix rather than a tense to admire.

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

### Cutting a release

Tag, and [`.github/workflows/release.yml`](.github/workflows/release.yml) does
the rest — build, PyPI, and the GitHub release with generated notes.

**The version lives in `tupferl/__init__.py` and nowhere else.** `pyproject.toml`
takes it from there (`dynamic = ["version"]`), ci.yml's `install` job asserts the
installed `--version` matches that line, and the release refuses a tag that
disagrees with it. So a release is two steps, not one:

1. Bump `__version__` in a PR like any other change, and merge it.
2. `git tag -a v1.2.3 -m 'tupferl 1.2.3' && git push origin v1.2.3`.

Doing it the other way round — tagging first — builds a wheel carrying the *old*
number and publishes it under a release named for the new one, and PyPI keeps
whichever number the wheel says. The guard exists because that mistake is only
visible to someone who reads the file list.

Three things run before anything is built, and none is a formality:

- the tag matches `tupferl/__init__.py`;
- the tagged commit is on `origin/main` — a tag can be pushed from anywhere, and
  that is the one path around "every change reaches main through a green PR";
- the full preflight, again, on the tagged tree. A tag proves somebody typed a
  command, not that CI ever saw this commit — and a red main is exactly when
  someone reaches for a release of the last good one.

`tests/test_release.py` asserts all three exist, for the same reason
`tests/test_ci.py` exists: a release that quietly stopped checking looks exactly
like one that checked and was satisfied.

**`workflow_dispatch` is a dry run.** It builds, runs `twine check --strict`,
installs the built wheel into a fresh virtualenv and asks its `--version` — and
publishes nothing, because both publishing jobs test `github.ref` for a tag
rather than relying on anyone remembering not to press it.

**Publishing is the one irreversible act in this repository.** A version number,
once taken on PyPI, can never be reused: a wrong 1.0.0 is 1.0.1 for ever. That is
why every check is before the upload, why `skip-existing` is not set (re-running
a finished release should be an error somebody reads, not a green tick over a
no-op), and why PyPI goes before the GitHub release — both orders leave a partial
state if the second half fails, and this one leaves the installable half done.

Authentication is [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/),
so there is no API token in the repository to leak or rotate. It is configured
once, by hand, on PyPI: owner `martinus`, repository `tupferl`, workflow
`release.yml`, environment `pypi`. Until that exists the `pypi` job fails at the
upload with an OIDC error, having already built and checked everything — which
is the right place for it to fail.

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

**A hand-written spec proves the row against the selection *you chose*, and the
sweep will use a different one.** This is the most expensive mistake recorded
here: four rows across two pull requests were reported fixed on the strength of
`python -m tools.mutate <spec>.py`, and came back `BROKE` on the next whole-tree
run — because the generated selection reaches the mutated line by a route the
chosen one did not, and something on that route hangs or exits before the
killing test is reached. A spec is the right tool for *iterating*; it is not the
evidence. Before reporting a row fixed, run it under the selection the generator
builds:

```sh
python -m tools.mutate --all --only <the file>      # the selection a sweep uses
```

The general form, which also governs where a bound goes: **the killer a sweep
reports is one route to the line, not all of them.**

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
  of the command that was typed;
- **an assertion inside a loop that can iterate zero times.** `for row in
  produced(...): assertNotEqual(...)` is satisfied by producing nothing, which
  is exactly what the mutation under test does. Assert the count first;
- **a textual comparison standing in for a semantic one.** `ast.unparse` adds
  parentheses, so a clone nobody modified comes back as `(a and b)` against
  `a and b` — different strings, identical program, six survivors hidden. Compare
  through `ast.unparse(ast.parse(...))` when the claim is about the code;
- **a bound whose exception is a subclass of the one under test.** `TimeoutError`
  *is* an `OSError`, so `support.deadline` inside `assertRaises(OSError)` is
  swallowed: the hang becomes the error the test was asserting, and one unguarded
  line becomes a test that cannot fail. Read the type back explicitly.

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

- Commit a checkpoint before anything that rewrites files in bulk. In this
  repository that is `python -m tools.mutate --all --accept`, which inserts a
  `# survivor:` comment above every unread row -- 159 lines across 14 files the
  first time it ran here.
- To undo your own edit, **rewrite the text you changed**. Do not discard the
  file. If you must restore, copy the file aside first and restore from the copy.
- **Tell subagents explicitly when they may not write.** A review agent asked
  only to *read* a diff has reverted a tree on its own initiative. Verify the
  tree yourself before believing a report that mentions touching it.

---

## 7. Writing things down

- **Everything worth remembering goes in the repository, and never into an
  assistant's private memory.** Agent tooling offers a local memory store; it is
  not used for this project. A note there lives on one machine, under one tool,
  for one person: nobody else can read it, no PR reviews it, and nothing ever
  catches it going stale — which §0 says is worse than not having written it at
  all. A note here arrives through a PR someone reads, and is corrected by the
  same review that notices it stopped being true.

  Where, specifically: a fact about the code goes in a docstring or a comment
  beside it; a fact about *how the work is done* goes in this file; a measured
  result goes in this file's "Measured" or "Measured dead ends" sections or in
  `docs/`; and anything somebody should act on later is an issue (§4), not a
  note anywhere.
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

  Two clauses this repository added by needing them, both live rules rather than
  history — the instances that produced them are in the `Historical` notes below
  the table and in the mutation-harness gotchas:

  - **A tool written for a finite job says in its docstring what finishes it.**
    Otherwise nothing ever notices the job is done, and the tool outlives its
    reason as a row in the table above that nobody dares delete.
    `tools/unassert.py` named Phase C and was deleted on time because of it.
  - **A compatibility switch has a half-life, and it wants a stated end.** A
    second implementation kept "so the two can be compared" is an instrument,
    and an instrument outlives its experiment silently. Say in the switch's own
    docstring what removes it. `TUPFERL_MUTATE_VERDICT` did, which turned its
    removal into a checklist item instead of an archaeology exercise.
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
  | a tool's output needs a colour, and its log must not get one | [`tools/paint.py`](tools/paint.py) |
  | a name under `tools/` says `tupferl` and should not | [`tools/settings.py`](tools/settings.py) |

  ```sh
  python -m tools.mutate --base main --json sweeps/r.json   # generated from the diff
  python -m tools.mutate <spec>.py          # a table you wrote
  python -m tools.reached sweeps/r.json sweeps/c.json   # survivors missing tests
  python -m tools.watch $PID --log sweeps/r.log --done sweeps/r.json.done --match 'caught|SURVIVED'
  ```

  The first four were ported from `martinus/woswoar` (Apache-2.0), where they
  were written; their module docstrings carry the argument for each one's shape
  and say which of its evidence was measured there rather than here.
  `tools/paint.py` and `tools/settings.py` were written here.

  **Historical — `tools/unassert.py` was a sixth row, and its deletion is the
  one thing in §7 that argues against §7.** It rewrote `self.assertX(...)` into
  bare `assert` for a converting cluster, and it was committed rather than left
  in `/tmp` because a note "on one machine, under one tool, for one person" is
  worse than none: the realistic alternative was never that the next cluster
  reused a scratch file, it was that the next cluster wrote it again with the
  same four mistakes. That held — it was reused five times — and then the work
  it existed for finished and it was deleted with `tools/verdict_unittest.py` in
  Phase C. So the rule survives its instance with one clause added: **a tool
  written for a finite job says in its docstring what finishes it**, or nothing
  ever notices that the job is done.

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
- **Asking for a run *by revision* is necessary and not sufficient: zero runs
  registered reads as zero runs pending.** The entry below says to ask
  `…/actions/runs?head_sha=$(git rev-parse HEAD)` rather than for the pull
  request's check list, and that is right — but a poll loop written against it
  broke the same way. `until [ "$(… | jq '[.workflow_runs[].status] |
  map(select(. != "completed")) | length')" = "0" ]` is satisfied by an **empty
  list**, so it fell straight through and reported success having read nothing;
  the reporting line after it then printed no jobs, and the whole thing exited
  0. Wait for `.workflow_runs | length` to be non-zero **first**, and treat "no
  runs" as "keep waiting", never as "nothing is pending".

  It cost nothing here only because the answer was already "there is no run":
  `ci.yml` triggers on `push` for `main` alone, plus `pull_request`, so pushing
  a branch with no pull request open starts nothing at all. **CI begins when the
  PR opens**, and a branch pushed ahead of time buys no overlap.

- `gh pr checks` reporting the **previous** commit's completed run. A poll loop
  that broke when "every check is non-pending" saw a full set of passes seconds
  after a push, before GitHub had registered the new run at all — so a green was
  reported for code CI had never seen. Ask for the run belonging to a *revision*
  (`gh api "…/actions/runs?head_sha=$(git rev-parse HEAD)"`) rather than for the
  pull request's current check list, and re-confirm immediately before merging
  rather than trusting the earlier read.

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

**`python` in every command above means the project's virtualenv, and bare
`python` is usually not it.** `.venv/` is gitignored, so nothing in the tree says
it exists, and `which python` answers the system one unless it has been
activated -- which is how half a session of pytest spikes came to be run against
the wrong interpreter. Measured here on 2026-08-30:

| | `.venv/bin/python` | `/usr/bin/python` |
|---|---|---|
| pytest | **9.1.1** -- the floor `pyproject.toml` pins | 8.4.2 |
| hypothesis, mypy, ruff | present | absent |

The version is not a detail: `tools/verdict.py` classifies pytest *report
objects*, and `pyproject.toml` records the measured 8-against-9 difference in
`TestCase.subTest`'s report shape that the floor exists for. So when a pytest
behaviour looks surprising, ask
`python -c "import pytest; print(pytest.__version__)"` before believing it --
the answer is the difference between a spike that means something and one that
does not.

`python -m pytest -q` runs the same tests serially, and is the one to reach for
when a parallel run's output is confusing -- it is what a batch runs, without
the batching. **It is now the only serial fallback.** `python -m unittest
discover -s . -t . -p 'test_*.py'` used to work too, and stopped with Phase B's
first cluster: it silently loads *nothing* from a pytest-native module. Measured
across B1 -- 1614 tests before, **1499 after, `OK` both times**, exactly the 115
in the eight converted modules gone with nothing said. That is the flattering
failure §8 collects, so reach for `pytest -q`.

### Layout

| | |
|---|---|
| `tupferl/` | the package. `__main__.py` is the CLI and the only entry point |
| `tupferl/manifest.py` | what may be managed and what is. Read its docstring before touching the admission rules — five of the seven are there to stop the wrong file being pushed, and the newest of them (`SECRETS`, #35) is a short list of famous filenames rather than a scanner, on purpose |
| `tupferl/gitrepo.py` | every call to git. The only other subprocess in the package is the user's `$EDITOR`, in `conflicts.edit` |
| `tupferl/copies.py` | what a stored copy is: bytes, the one mode bit that travels, and the single rule for "the target is already this file". Below `manage` and `sync`, because both write the same snapshots |
| `tupferl/sync.py` | the three-version comparison and everything it decides. `resolve` is pure, so plan §7.4's table is a test with no repository in it |
| `tupferl/merge.py` | the 3-way merge, over `git merge-file`. Bytes in, bytes out, and the conflict count is git's exit status |
| `tupferl/manage.py` | `init`, `add`, `remove`, `list`. `--host` on `add` and `remove` means the same thing in both: this machine's overlay rather than the shared tree |
| `tupferl/inspection.py` | `status` and `diff`, the two commands that only look. Both read `sync.examine`, so what `status` promises about the next sync is computed by the code that performs it |
| `tupferl/conflicts.py` | what a conflict is (`Sides`) and the six ways a person settles one. Returns an `Answer`, never a decision about disk — which is what keeps it out of an import cycle with `sync`, and what lets `--ours`/`--theirs`/`--no-input` be settlers that answer without asking |
| `tests/` | **pytest-native, and run by pytest**: `tools/verdict.py` classifies pytest reports, so the harness does not care how a test is written. A new test module has to be named `test_<module>.py` or `test_<module>_<aspect>.py`, or `tools/mutants.py` resolves no target for that source file and `test_mutants.TestChoosingTheTests` goes red. Phase B converted all 33 modules that had to convert; **some modules are still taken by the `unittest` loader and none is arrears**, and *which* and *how many* is [`docs/pytest-plan.md`](docs/pytest-plan.md)'s status line rather than anything here — Phase C changed the number and something else will, so typing it here is the rot this file opens with, and `tests/test_pytest_plan.py` recomputes it from the tree where nothing recomputes a figure typed here. Write a new module pytest-native: a plain `def test_...` is discovered, packed by its module, run and accounted for |
| `tools/` | the test infrastructure, ported from `martinus/woswoar` — except `paint.py` and `settings.py`, which are this repository's. Its own tests came later (#4): `test_verdict.py` and `test_paint.py` were written here, `test_reached.py` and `test_watch.py` ported (`test_watch.py` has since gained `TestEveryAnswerIsColoured`), `test_mutants.py` ported with four assertions re-pointed at this project's layout. `verdict.py` + `test_verdict.py` are the classifier, and the only one — `verdict_unittest.py` and its tests were the `unittest` backend it replaced, kept behind `TUPFERL_MUTATE_VERDICT=unittest` while the conversion was measured against it and deleted with that switch in Phase C. **Nothing here spells a project's name**: since Phase D every one of them is a key in `pyproject.toml`'s `[tool.mutate]`, read by `settings.py` and documented in [`tools/README.md`](tools/README.md) |
| `docs/plan.md` | the plan this is built from |
| `docs/pytest-plan.md` | the phased conversion of the suite to pytest, and the measured spike results Phase A depends on. **Its status line says which cluster is next, and `tests/test_pytest_plan.py` asserts it against the tree** -- so "continue the plan" is a safe instruction and the line cannot go stale. It did, within a day of being written |

Five things are not where a newcomer would guess, all on purpose:

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
- **A conflict carries its own three versions.** `sync.resolve` builds a
  `conflicts.Sides` and hangs it on the `CONFLICT` outcome, so `home` is a
  `Blob` rather than `Blob | None` wherever a conflict is settled, and
  `outcome.sides is not None` is both the test for "is this a conflict" and the
  narrowing that follows from it. There is no second place to keep in step.
- **`status` and `diff` do not have their own walk of the managed files.**
  `sync.examine` is the loop `settle` uses with the writing taken out, and both
  read it. That is what makes `status` a preview of the next sync rather than a
  second opinion about it — a row added to plan §7.4's table reaches `status` by
  existing. It costs `diff` a `git merge-file` per file both sides changed,
  which it then discards; that is accepted, and the alternative is the
  duplication the extraction removed.
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
- **An overlay fixture needs *both* copies of the file.** A test that stores a
  file with `add --host` and never shares it leaves one version of that name in
  the repository, so "the overlay won" is unobservable — measured: inverting
  `manifest.managed`'s merge so the shared file wins **survives every test that
  drives a real sync** except `tests/test_overlays.py`, which asserts the two
  differ before it asserts anything else. The general shape is §2's "two
  symmetric inputs"; this is the spelling it takes here.
- **A test wanting a throwaway directory uses `tests/support.py`'s `tempdir`,
  never pytest's `tmp_path`.** `tmp_path` keeps the last three numbered roots
  per user under `/tmp/pytest-of-<user>`, and a sweep runs thousands of probes
  as separate processes racing over that numbering. `support.tempdir` removes
  its own tree in a `finally` and names what survived if the delete fails. This
  is written here rather than only beside `tests/test_config.py`'s `box`
  fixture, because it binds every module Phase B has yet to convert and nobody
  writing one of those will read that docstring first.
- **A converted test class keeps `@pytest.mark.usefixtures("...")` for its
  sandbox even when no test in it names the fixture.** A test can depend on a
  base class for a *side effect* and never mention it: the sandbox patches
  `os.environ`, so a test that sets `PATH` and reads the result looks,
  in its own text, like a test needing nothing. Converted by giving each test
  the fixtures its body mentions, that test gets none -- and runs against the
  developer's real environment, which is the failure `tests/support.py`'s
  docstring exists to prevent, arriving by a new route. Measured in B3: it
  broke `PATH` for the rest of the process and took nine later tests with it,
  which was luck. A test that merely *read* `$HOME` would have passed.

  So the decorator goes on the class and is the load-bearing statement -- *this
  class runs in a sandbox* -- rather than an inference from whether some method
  still happens to use the value. The mark states a property, so a class
  *without* one has to be a class the property is false of -- B4b's four
  modules have exactly one, `test_sync_cli`'s `TestTheRemoteLine`, which is
  pure and touches no sandbox.

  **The three `unittest` adapters this rule was written about no longer exist.**
  `SandboxCase`, `MachineCase` and `TwoMachinesCase` were deleted in B4b with
  their last users, so the counts that used to sit here -- 20 classes naming one
  directly, 38 reaching one through a module-local base -- are now zero by
  construction rather than by progress. That is the shape §0 warns about at its
  most flattering: a grep coming back empty reads as work finished.

  **No count of converted classes is kept here, and no list of what is left**,
  which is the correction the first version of this paragraph needed: it carried
  both, and the class count was wrong by one on the day it was written.
  `docs/pytest-plan.md`'s status line is the number to read, because
  `tests/test_pytest_plan.py` recomputes it from the tree and nothing recomputes
  a figure typed here.

  **The leak half is guarded rather than trusted.** `tests/conftest.py`'s
  `_every_test_puts_the_environment_back` is autouse and fails the test that
  left `os.environ` changed, instead of the nine downstream ones that then
  cannot find git. It does not catch a test that merely *reads* the real
  environment, and nothing cheap does -- that half is what the marks are for.
  A session-wide replacement of `$HOME` would look like the deeper fix and is
  not: it turns a loud failure into every test silently sharing one home.
- **Every `raise TupferlError` is checked by a test, not by habit.**
  `tests/test_errors.py` reads them all out with `ast` and asserts plan §5's
  shape: one semicolon (what happened; what to do next), one full stop, one
  sentence. Those three are a proxy for "is this actionable?", which is not
  decidable — and measured against the tree they identified exactly the four
  messages that had drifted to what-happened-only, and no others. If a new
  message legitimately cannot take that shape, argue it in the PR and change
  the check; do not add an exception list, which is how the rule stops meaning
  anything.
- **Every row that is not `caught` has a disposition, and it is a comment
  beside the code.** A whole-tree sweep found 557 survivors. Triaging them in
  prose does not survive to the following Sunday: the next sweep produces the
  same rows with nothing to say which were already understood, so either
  somebody reads all of them again or nobody reads any. Both have happened here.

  ```python
  # survivor: branch -- equivalent: `Path.cwd() / an_absolute_path` discards the
  #   left side, so taking the branch anyway is the same answer.
  if not expanded.is_absolute():
  ```

  **A tag guards a *statement*, not a physical line.** Trailing the statement,
  or in the comment block directly above it; the block is joined, so a reason
  may wrap, and several tags may sit in one block. Both the reader and
  `--accept` normalise a mutation's line to the line its statement opens on --
  anything inside brackets is a *continuation*, and a comment inserted there
  splits the expression and leaves `ruff format --check` wanting to reflow the
  file. `python -m tools.mutate --all --accept` writes a `TODO` tag above every
  unread row for a person to finish.

  **The operator is required, and that is the whole design.** Measured on a
  whole-tree table: mutations average 2.1 per source line and reach 13, and
  **53% of the lines carrying a survivor also carry a row that is caught**. A
  bare tag would excuse a live guard about half the time it was used — and would
  go on excusing operators `mutants.py` has not learnt yet, which is the
  flattering direction arriving through the record's own syntax.

  Five things about it are load-bearing, each a way it could quietly become a
  mute list instead of a record:

  - **`--accept` is a flag, never automatic.** Recording a survivor is saying
    somebody read it and decided; a run that did that by itself would be
    deciding on their behalf.
  - **A new tag says `TODO`, and that is the point.** A reason nobody wrote is
    not a reason. Seeing `TODO` in a diff, next to the line it excuses, is the
    review.
  - **Nothing is ever removed by the tool.** A tag is deleted by deleting a line
    of code, which is a person's job and shows up as one.
  - **A tag that has stopped earning its place is reported.** Today that means a
    tag every row of which the suite has learnt to *catch* — good news, and good
    news nobody is told is exactly how a mute list forms. Spent is judged per
    *tag*, not per row: one operator covers mutations that need not have the
    same answer, and `conflicts.somewhere_in`'s range arithmetic is equivalent
    widened and caught narrowed, so a check that fired on the first caught row
    reported a live tag as dead on its first real sweep.
  - **The unfinished ones are counted out loud, every run.** A `TODO` tag
    silences its row exactly as a written reason does — that is what makes
    `--accept` usable — so without the count a green sweep is a claim nobody
    made. As of 2026-08-31 there are **109** of them, all in `tools/mutate.py`,
    where the pool orchestration and `_Lanes` signal handling resist testing for
    the reasons four dead ends below already record. That number is debt, not
    progress; a sweep exits 0 over all of it.

    It read 115 until this line was re-counted and the tree said 114, so the
    figure had been wrong by one since it was written — which is what a
    hand-maintained count does. The five that went with #96 are the shape this
    number exists to make visible: four `TODO`s replaced by written reasons, and
    two collapsed into one when `_borrow` and `_attempt` came to share `_lent`.

  **Historical — this replaced a file of sha256 keys
  (`known-survivors.json`), and none of the names below still exists.** The
  reason it was replaced is what is worth keeping: It was not kept up to date: twelve
  equivalences proved in one sitting went into commit messages instead, because
  editing a JSON blob keyed on a hash was further away than writing a sentence
  nobody would read again. Seventeen of its 213 entries had come to match
  nothing the tree generated. Three pieces of machinery went with it, and every
  one existed only to compensate for the key being content-addressed rather
  than positional:

  - `Accepted.seen`, an occurrence count, because two identical mutations in one
    file collapsed to one key — 557 survivors to 432 — so a count was needed to
    tell the 126th from the 125th. Two lines carry two tags.
  - `complete`, which decided whether "this key matched nothing" was evidence. A
    `--base` run generates rows for the changed lines alone, so it reported 206
    of 210 entries stale — and `_accept` *dropped* what `stale` named, so the
    documented recording command destroyed the record it recorded into. A tag is
    judged where it sits, so a narrowed run judges exactly the tags it reached.
  - a collision `_resume_key`'s docstring still records: three unrelated `0`/`1`
    literals in one file shared a key, so a reason written for one was absorbed
    by the others rather than read.

  **`BROKE` and `TIMEOUT` are excused on the same terms** (#57). They were the
  one category a sweep could not settle: 33 came back every whole-tree run with
  nothing to say which had been read, and worse than a survivor's version of it,
  because such a row is never `caught` — so the line it appears to guard is
  guarded by nothing while the summary shows it in neither of the two numbers a
  reader looks at. `verify()` is unchanged and still counts `survived` alone: a
  hand-written table has no disposition to consult, and a row it cannot answer
  is a mistake in the table.

  What the tags *cannot* do is stop the row happening. `"a" + "b"` becoming
  `"a" - "b"` is a `TypeError` every time, so `tools/mutants.py` refuses to
  generate it when either operand is a string literal — 9 of this tree's 45
  `arith` rows. The other 36 include `paint.GOOD + paint.HEAD`, which is two
  *attributes*: proving those string-valued means resolving a name across a
  module boundary, which is a type checker rather than a guard. Getting the
  check wrong permissively costs one `BROKE` row; getting it wrong strictly
  stops mutating real arithmetic, and nothing would report that.

- **A red baseline reports every row as `caught`, and that reads like a perfect
  sweep.** The failing test notices every mutation, so the harness credits it.
  `BASELINE NOT GREEN` is printed, once, above rows that all say `caught` --
  and a table of 51 rows came back 51 for 51 twice before anyone read the line
  rather than the rows. The truth was 39.

  What made it invisible for so long is that the verdict layer could not be
  measured at all: a shard runs as `python -c <the source of the verdict
  layer>`, where `sys.path[0]` is `''` -- resolved against the *current*
  directory at each import, where `python -m` fixes it at startup -- and
  `test_a_flat_selection_looks_beside_itself` imported `tools` from inside an
  `os.chdir` block. Green under the suite, red under any shard that selected
  only that module. **Never issue an import from inside a chdir**, and read the
  baseline line before the verdicts.

  The test it happened in lived in `tests/test_verdict_unittest.py` and went
  with that file in Phase C. The rule did not: `tests/test_verdict.py`'s
  `TestWhereTheWalkLooks` issues its import *before* any `chdir` and says in its
  docstring that this is why, which is where the lesson is now enforced rather
  than only recorded.

- **`verify()` is the strict wrapper; a generated table needs `run(...,
  strict=False)`.** `run`'s own docstring draws the line: stopping at an
  unanswerable row is right for a table somebody wrote by hand, and wrong for a
  generated one, where "a single non-viable mutant out of two hundred would
  throw away every answer already paid for." Three of this repository's own
  mutations cannot be answered at all -- two force `verdict.collect` down its
  whole-suite group (`_groups` yields `[]`, which is `mutate.WHOLE_SUITE`),
  running the entire suite nested inside a memory-capped sandbox, and
  `run_tests`'s `if args.worker:` becomes a fork bomb -- so a generated table
  over `tools/` stops dead under `verify`. The three were counted under the
  `unittest` backend, where the same two rows reached `loader.discover(".")`;
  the mechanism transferred with the layer and the count has not been re-taken.

  And **a spec's `if __name__ == "__main__":` block never fires**: `mutate.main`
  loads the file with `runpy.run_path`, where `__name__` is not `"__main__"`,
  then runs the module-level `MUTATIONS` under its own rule. To choose your own
  arguments, call `run` at module level and name the list something else.

- **`sorted` over a *set* is only probabilistically guarded.** A set iterates in
  hash order, which Python randomises per run, so `sorted` becoming `list` is
  caught only when that order happens to differ from sorted. With two elements
  that is a coin flip -- a guard that sometimes guards, and it reads exactly
  like one that always does. Size such a fixture for the odds you want: eight
  keys is 1 in 40320.

- **A test that reads the repository's own source is not a test of behaviour,
  and inside a probe it reads the *mutated* copy.** `tests/test_mutants.py`
  asserts that every `# survivor:` tag names an operator its statement can
  produce. Under a probe that property is false by construction -- the mutation
  changed the statement -- so the test failed for the mutation rather than for
  the code, and the row was filed `caught` with nothing behavioural having
  noticed. Measured over a 2621-row `--only tools/` table, with and without the
  gate: **caught 2394 -> 2228, survivors 205 -> 370, mutation score 92.1% ->
  85.8%.** 166 lines read as guarded and were not.

  `mutate._run` sets `TUPFERL_MUTATE_MUTATED` and `support.over_a_mutated_tree`
  reads it, so such an assertion can stand down. **It is for source-shaped
  claims only**: a test about *behaviour* that stood down would turn a real
  survivor into a row nobody looked at, which is the same flattering direction
  one step further on.

  Two things about finding it are worth more than the fix. The signal was a
  **spent-tag report inviting a deletion that would have been wrong** -- an
  `equivalent:` tag reported "now caught", which is a contradiction rather than
  good news, because an equivalent mutant is the same program and cannot
  honestly be caught. And the first count was **226**, from reading recorded
  killers; the true figure is **166**, because `Killers.ahead_of` runs a
  recorded killer first so it stays recorded. **A killer census counts rows
  whose first killer is X, never rows only X can catch** -- the honest number
  needs a run with X deselected.

- **Never read a raw survivor list as a bug count.** Cross it with coverage
  (`python -m tools.reached results.json coverage.json --list`): a survivor on a
  line no test executes is a missing test; a survivor on a line the suite does
  execute is a weak fixture or an equivalent mutant. The two halves mean
  opposite things.

### The dependency surface is a claim with a test behind it

On 3.11+ the package imports nothing outside the standard library; on 3.10 it
imports `tomli`, and only inside `config.toml`'s shim. `tests/test_packaging.py`
asserts both, and asserts `pyproject.toml` agrees in both directions — an import
that is not declared crashes only on a machine that does not already have the
package, and a declaration nothing imports is what `rich` would be if it were
listed.

A dependency arrives as one import in one commit, and nothing else in the suite
notices: the code works, the tests pass, and the supply chain grew. If a new
dependency is genuinely wanted, that test is the place the argument for it gets
written down.

**That test governs `[project] dependencies` — what a *user* installs — and not
the optional extras**, which is why `pytest>=9.1.1` could join `test` without it
going red. An extra ships to nobody, so the check that matters there is a
different one: the floor has to be a version somebody actually ran. Both
extras carry that argument as a comment beside them, and pytest's says what
measurement pins it.

### The harness knows nothing about tupferl, and that is a claim with a test

Every name under `tools/` that used to say `tupferl` is a key in
`pyproject.toml`'s `[tool.mutate]` table since Phase D — the mutable prefixes,
the `TUPFERL_MUTATE_*` family, the temporary directory prefixes, the Hypothesis
profile, the tests directory, the naming convention that predicts a test module,
the width `--accept` wraps a tag to, and what a sandbox copy leaves out.
`tools/settings.py` reads it; `tools/README.md` documents it.

The last two were found by the review rather than by the plan, and both are the
same shape: **a constant that is this repository's answer, applied to somebody
else's tree.** `--accept` writes tag comments into the *host's* source at
`_COLUMNS`, so a harness carrying 100 into a project formatted at 88 makes every
tag it writes illegal there. `_SKIP` decides what is copied into a sandbox once
per lane per row, and named `.venv` and `sweeps` — a spelling. Ask that question
of anything new under `tools/`: not "does this say tupferl", but "whose tree does
this touch".

**The defaults are generic and this project's answers are in the table**, which
is the opposite of what `docs/pytest-plan.md` specified and is the one decision
in that phase worth arguing. Had the defaults been tupferl's, a reader that
opened the file and then threw the result away would produce a byte-identical
sweep, and every test written for it would pass — the flattering green §8
collects. Measured, by deleting the table and running the suite: **58 tests
across five modules go red** (it was 13 before the review pass that this section
also records). Three knobs stay green there because their default really does
equal this project's value (`unmutable`, `probe_plugins`, `tests_dir`), and what
covers those is the scratch project in
`tests/test_settings.py`, which drives a copy of `tools/` inside a tree whose
every knob differs and asks the harness — not the settings — what it thinks.

**A dict of keys to add cannot turn a variable off**, and that is worth more
than the instance. `Settings.sandbox` set `PYTEST_DISABLE_PLUGIN_AUTOLOAD` when
the project wanted autoload off and said nothing when it wanted it on — and
`_run` spreads it over `os.environ`, so an ambient one from a nested sweep was
inherited and the knob silently did nothing. Making a constant configurable
introduced a hole the constant did not have, because "unconditionally set" and
"set unless configured otherwise" differ only when something else already set
it. `Settings.environment` is the one place that inherits, overrides and
removes, and the removals are derived from the additions so a name cannot be
added to one and forgotten in the other.

Two rules follow for anything added under `tools/`:

- **a new project-specific name goes in the table, not in the module.** The
  grep that says so is `grep -rn "TUPFERL" tools/*.py`, and it should come back
  with one line: a docstring in `verdict.py` naming a switch Phase C deleted.
- **`tools/settings.py` imports nothing but the standard library.**
  `tests/support.py` imports it — that is what makes `support.ALARM` and
  `mutate._ALARM` one spelling instead of two literals a typo could part — so
  anything it pulled in would be pulled into every test process and every probe.

**A negative assertion about a configured walk needs its positive half**, and
this is where that was measured rather than recited: with the reader disabled
the walk comes back *empty*, so `"src/dangerous.py" not in walked` — the test
for `unmutable` — was satisfied by there being nothing to look at. It asserts
the sibling is present first.

### Gotchas

Fifty of them, and each is here because it cost somebody an afternoon.
Grouped rather than run together: as one flat list of 461 lines this was a
section a reader scanned past. The entries themselves are unchanged and none
has been dropped.

#### Python, and the versions this supports

- **Backticks inside a double-quoted shell string are executed, and this
  repository's prose is full of backticks.** Not a `git commit -m` problem: it
  is every double-quoted argument, and it has silently corrupted content here
  twice — once eating a line from a commit message, once running `ruff` and
  `ruff format --check` and splicing their *output* into a source comment,
  which then read "The line limit  enforces here … and 59 files already
  formatted stays green after ." Nothing failed; the text was simply wrong.

  Use a quoted heredoc for anything carrying prose — `<<'MSG'` and `<<'PY'`
  disable substitution entirely — or write the file with Python. Single quotes
  work too, but this project's sentences contain apostrophes. Since the damage
  is silent, the check is to read back what landed rather than to trust the
  exit status.

- **`AssertionError: Cannot find component 'X' for 'tupferl.old_module.X'` from
  inside mypy** — moving a name between modules leaves `.mypy_cache` wrong.
  `rm -rf .mypy_cache` and re-run; it is not your change.
- **`ruff format --check` fails on code you did not touch** — the formatter's
  output changes between versions. The floors in `pyproject.toml` are the
  versions the tree is actually formatted by, not the oldest that would work.
- **`tomllib` is 3.11+, and this project supports 3.10.** `tupferl/config.py`
  falls back to `tomli`; the 3.10 CI leg is what proves the fallback is
  reachable, so do not drop that leg to save a minute.
- **A stale `.pyc` can survive an edit of the same size in the same second.**
  Python invalidates cached bytecode on mtime *and size*, so rewriting
  `PROMPTED = 60.0` as `PROMPTED = 20.0` -- identical length -- within one second
  left the old bytecode in place, and a test read the value the file no longer
  had. It cost a wrong diagnosis: the constant was correct on disk and wrong in
  memory. Any script that edits a file, runs the suite and edits it back is
  exposed; `find . -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +`
  settles it. `tools/mutate.py` is not exposed -- it passes `-B` and sets
  `PYTHONDONTWRITEBYTECODE`, and `_clear_bytecode` runs over each sandbox.
- **`RLIM_INFINITY` is `-1` — a sentinel, not a large number.** So `min(want,
  hard)` against an unlimited ceiling returns `-1` and *raises* the limit to
  unlimited, which is the opposite of the clamp it reads as. It is silent: the
  test then reads back whatever the code under test chose rather than what the
  fixture set, and passes or fails for reasons unrelated to its name. Written
  once, as an `under(want, hard)` helper, wherever a fixture composes limits.
  The same sentinel is why a raise is spelled `(hard, hard)` and never
  `(RLIM_INFINITY, hard)`: macOS reports unlimited as `sys.maxsize`, so asking
  for `-1` there is "current limit exceeds maximum limit" and the child dies.
- **A broken module is classified the same way however it was reached, and
  `tests/test_verdict.py`'s `TestABrokenModuleIsClassifiedTheSameWayTwice` is
  what keeps it that way.** pytest collects identically whether a module was
  named or found, so both routes report the failure through
  `pytest_collectreport` and agree. The test exists so that a difference
  reappearing is loud, rather than surfacing as a difference in what a *walk*
  concludes.

  **Historical — `unittest` did not have that property, and two tests were
  written with the fixtures exactly backwards before anyone measured it.**
  `discover` wrapped everything into `loader.errors`; `loadTestsFromNames`
  wrapped only what derived from `Exception`, so a syntax error or a
  module-scope `SystemExit` escaped to the verdict layer's own handler and came
  back `loaded: False` instead. Both refused to credit a test, which was the
  only thing that mattered — but a fixture written for one proved nothing about
  the other. The measured table lived in
  `tests/test_verdict_unittest.py`'s `TestABrokenModuleTakesTwoDifferentPaths`,
  deleted with its subject in Phase C; `tools/run_tests.py` stopped collecting
  with `unittest` at Phase A2, so nothing in the tree reads the distinction now.
- **Patching `sys.version_info` does not conjure the module.** A test that fakes
  3.11 and then lets the code `import tomllib` fails on a real 3.10 interpreter,
  where that module does not exist -- so it passes on every leg *except* the one
  that exists to check the branch. Stub the module in `sys.modules` instead: the
  claim under test is "this branch imports that name", and standing something
  there and watching it come back is exactly that claim.
- **The preflight passing locally does not mean CI's will.** `hypothesis`'s
  `blacklist_categories` is a compatibility shim whose stub is typed in newer
  releases; the same file passed `mypy` here and failed it on the runner. Prefer
  the modern spellings (`exclude_characters`, `exclude_categories`, `codec`),
  and when a lint fails only in CI, suspect the dependency's version before the
  code. Clearing `.mypy_cache` was the first guess here and it was wrong.
- **`str.splitlines()` splits on far more than `\n`** — `\x0b \x0c \x1c \x1d
  \x1e \x85 \u2028 \u2029` as well. A generated line containing one of those
  becomes two lines, and anything indexing by line number is then off by one for
  the rest of the file. It bit the sync state machine's *model*, not the code:
  git splits on `\n` alone, so the file under test was right and the expectation
  was wrong. Use `split("\n")` wherever the line count is load-bearing.
- **`tty.setcbreak` behaves differently on 3.10 and 3.12.** Python 3.12 stopped
  clearing `ECHO`, so the same call swallows the keypress on one and echoes it
  on the other. `conflicts.one_key` sets `ICANON` and `ECHO` itself and echoes
  the key deliberately, which is the same on every supported interpreter.
- **Historical — `unittest`'s display string changed in 3.11, and the retired
  `verdict_unittest.Verdicts.noticed` held it.** 3.10 rendered
  `test_it (test_a.T)` where 3.11+ renders `test_it (test_a.T.test_it)`. Four
  tests asserting on the *content* of `noticed` passed locally and turned the
  `test (3.10)` leg red. The fix was to assert on `killers` instead -- the same
  test as `module.Class.method`, which is what a loader takes back -- and that
  layer said why the two were recorded separately: "a display format is not an
  API". Both the file and the hazard went in Phase C. The durable half is why
  the trade was worth making:

  **`tools/verdict.py` does not have this problem, and that is the argument for
  the trade rather than an accident.** A pytest nodeid *is* the id a later run
  feeds back, so `noticed` and `killers` are filled from one list and there is
  no display format to drift. What it costs is that every id in
  `sweeps/killers.json` changed shape; the cache is machine-local and
  disposable, and `mutate._loadable` drops what pytest cannot name.
- **Historical — `TestCase.enterContext` is 3.11**, so on the 3.10 leg it was
  an `AttributeError` and the tests reached for `contextlib.ExitStack` instead.
  Moot now: no test in this tree writes a `TestCase`, and a fixture entering a
  context manager around its `yield` needs no version check. The live form of
  the same lesson is the `tomllib` entry above, which has a branch behind it;
  what the conversion did to this one is recorded where it is useful, beside
  `support.bounds`.
- **`text=True` encodes stdin and argv by different rules.** `subprocess`
  encodes an argv list with the filesystem encoding and `surrogateescape`, so a
  path that is not valid UTF-8 goes through; it encodes `input=` with the
  *stream's* handler, which is strict by default and raises
  `UnicodeEncodeError`. That is a `ValueError`, so it sails past every `except`
  arm in `gitrepo.git` — the class of escape #3 exists to close, reintroduced
  by #3's own fix when `stage` moved its pathspecs to stdin. `git()` passes
  `errors="surrogateescape"`, which answers the decoding half too. A dotfile
  name need not be valid UTF-8 on Linux, and `TestAPathThatIsNotUtf8` has both
  directions.

#### git

- **A test that makes `git commit` fail must not do it by removing the git
  identity.** git falls back to `user@hostname`, and whether that *works*
  depends on the machine: in a Linux container the hostname is `(none)` and git
  refuses, on a macOS runner it is a real name and the commit succeeds. Three
  tests written that way were green on every Linux leg and red on macOS. Use
  `support.break_commits`, which installs a `pre-commit` hook that exits 1.
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
- **git writes CRLF conflict markers into a CRLF file.** `split(b"\n")` leaves
  the `\r` attached, so a marker arrives as `b"<<<<<<< … (this computer)\r"` and
  matches nothing spelled without it. That made `conflicts.leftover` inert for
  every CRLF dotfile — an `[e]` the user quit without resolving was accepted and
  the markers reached `$HOME`, the repository *and* the snapshot on both
  machines, with `sync` exiting 0. `conflicts.bare` is the one place that strips
  it. Every fixture in the suite was LF until the review, which is why the run
  was green with the bug in it.
- **`=======` has no label, so it cannot be matched the way the other two
  markers can.** A line of a dotfile that is exactly seven equals signs ends the
  local side of a hunk whether it was meant to or not, and the prompt then shows
  that side empty and attributes its lines to the other computer — a display bug
  whose consequence is the user pressing the other key and destroying their own
  edit. `conflicts.trustworthy` checks the parse against the two real files and
  `describe` shows nothing rather than showing it swapped.
- **`git merge-file` honours `merge.conflictStyle` from a config *file* and
  ignores `-c`.** This entry used to say it ignored the setting entirely --
  measured against git 2.43, where `merge`, `diff3` and `zdiff3` all gave the
  two-section form -- and `gitrepo.merge_file` carried
  `-c merge.conflictStyle=merge` against the day a git started honouring it.
  That day came, and **the pin did not work**. Measured on git 2.55:

  | config | argv | base section? |
  |---|---|---|
  | isolated | *(none)* | no |
  | isolated | `-c merge.conflictStyle=zdiff3` | **no** |
  | file `zdiff3` | `-c merge.conflictStyle=merge` | **yes** |
  | file `zdiff3` | `--no-diff3` | no |

  So the override a reader would reach for is the one spelling that does
  nothing, and the guard was inert in exactly the case it existed for. A user
  with `merge.conflictStyle = zdiff3` in `~/.gitconfig` got a base section that
  `conflicts.hunks` attributed to *this computer*, so the prompt showed the
  wrong side and `[m]`/`[t]` could discard the edit they meant to keep. It is
  `--no-diff3` now (older than `--zdiff3`, which arrived in git 2.35), and
  `tests/test_merge.TestTheConflictStyleCannotComeFromTheUser` writes a real
  config file and points `GIT_CONFIG_GLOBAL` at it, because `-c` reproduces
  nothing.

  The general lesson is bigger than the flag: **a guard written for a future
  that has not arrived cannot be tested, and this one was wrong from the day it
  was written.** Nothing could have told the difference on 2.43.
- **git's merge stages are 1 base, 2 ours, 3 theirs — and "ours" is the branch
  being merged *into*.** During a `tupferl sync` that is this computer's commits
  and stage 3 is the repository's, which lines up with `--ours`/`--theirs` by
  luck rather than by construction: read the wrong way round, every conflict
  still settles cleanly and silently keeps the side the user asked to discard.
  `tests/test_sync_commits.py` names the content it expects on both sides —
  swapping the two constants turns 6 of its 14 tests red.
- **Read a stage with `cat-file`, not `show`.** `git show :2:path` is porcelain
  and applies the repository's filters, so a `.gitattributes` with `text=auto`
  hands back bytes that are not what was committed. And not through
  `gitrepo.git` at all, which is `text=True` and returns `stdout.strip()` — that
  decodes a dotfile on the user's behalf and eats its trailing newline and any
  leading blank line, which is the same loss `merge_file`'s docstring records as
  its reason for rewriting a file in place rather than using `-p`.
- **`git merge-file` needs three lines of agreement to call two disagreements
  two hunks**, and the five-line fixture most of this suite uses has exactly
  three between its first and last lines. A test about a *count* of conflicts
  therefore wants a longer file; written on `START` it reports 1 and reads as
  a bug in the counting.

#### Terminals, signals and processes

- **`_born` spawns `ps` where there is no `/proc`, so calling it puts a
  subprocess into whatever called it -- and on macOS that is a different
  program.** `mutate._stamp` read the sweep's own birth time through `_born`;
  every Linux leg was green, and all four `macos` shards failed all seven tests
  of `TestWhatEveryProbeIsHandedOnItsCommandLine` with `'int' object has no
  attribute 'name'`, because that class patches `subprocess.Popen` and the fake
  intercepted `ps` instead of the probe. `_my_birth` reads `/proc/self/stat` and
  answers `None` elsewhere.

  The general shape is worth more than the instance: **a helper with a
  platform-dependent *implementation* has a platform-dependent cost, and a
  fallback that shells out is not interchangeable with one that reads a file.**
  Ask what a helper does on the other platform before calling it somewhere a
  spawn would matter. The guard is asserted against `_born` rather than against
  `subprocess`, so it fails on Linux too -- a test that watched for a spawn
  could only go red on the leg nobody runs before pushing.

- **`Path.resolve()` moves a macOS temporary directory, and nothing on Linux
  says so.** `/var/folders/.../T` is a symlink to `/private/var/folders/.../T`,
  so `tempfile.mkdtemp()` hands back the first and anything that resolves the
  same path reports the second. A test comparing a fixture's directory against a
  path the code under test resolved is green on all three Linux legs and red on
  `macos` -- measured, on `tools/settings.py`'s `_root`, which calls `.resolve()`
  for the reason every root-finder does. Resolve **both** sides or neither; and
  since the two agree everywhere else, the assertion that would catch it is the
  one no local run can.

- **A unix socket cannot be bound at an arbitrary path.** `sun_path` is 104
  bytes on macOS, and a sandbox path plus `.local/share/tupferl/repo/…` exceeds
  it, so `bind` raises `OSError` and the test errors instead of testing. Use
  `os.mkfifo` where a "not a regular file" fixture is needed; it is the same
  class with no length limit.
- **A prompt in a test must fail, not block.** `conflicts.ask` loops, so a test
  that types one fewer key than the prompt asks for reads an empty terminal and
  waits for ever — a suite that hangs in CI rather than one that goes red. Both
  fixtures that type keys append `support.FALLBACK` (`s`), so an unexpected
  extra question is answered "skip", the run exits 1, and the test fails on its
  own assertion instead. `run_cli`'s subprocess path also passes
  `communicate(timeout=60)`, because a child that ignores its stdin entirely
  would otherwise outlive the suite.
- **One keypress can be several bytes.** A press of the Down arrow is `\x1b[B`,
  and read a byte at a time that is three answers — the last of which is `b`,
  *keep both*. Reading the whole sequence is not simply `os.read(fd, 8)` either:
  that returns everything the terminal holds, which includes the key pressed
  *after* it. `conflicts.rest_of_escape` reads to the end of the sequence and no
  further.
- **`tcsetattr` with `TCSADRAIN` can block for ever on a pty.** It waits for the
  terminal's pending *output* to drain, and a pty starts with `ECHO` on — so
  every key a fixture types is echoed into an output queue that, in these tests,
  nobody reads. When it fills, the drain never completes. `conflicts.one_key`
  sets only *input* flags, so it uses `TCSANOW`, which cannot wait; and
  `support.hush` clears the pty's echo before anything is typed. The symptom was
  a `macos` CI leg running for twenty minutes while every other leg finished in
  under one — Linux's pty buffer is large enough to hide it. Every job now has a
  `timeout-minutes`, and `tests/test_ci.py` asserts it, because a running job's
  log is a 404: a hang is the one failure with nothing to read.
- **A leaked `SIGALRM` is a failure with no owner, and it takes two modules to
  see one.** `tests/test_support.py` left `ITIMER_REAL` armed at **29.99s**
  (measured, by asking `getitimer` after `pytest.main` returned) because one
  class that arms a real timer was missing the `_alarm_put_back` mark its
  sibling twelve lines below had; `tests/test_mutate.py` left `verdict._ring`
  installed as the handler, because `verdict.each_test` installs it and one test
  calls that in-process. **Either alone is inert.** Together, `SIGALRM` fired
  thirty seconds later into `_ring` and raised `Hung` inside whatever unrelated
  test was running -- so *which* test failed depended only on what was executing
  at T+30s, each module was green alone, and the pair was red at a different
  place each run (#115).

  Two things this cost that are worth more than the fix. The conftest fixture's
  own docstring **named the class it did not cover**, which is §0's shape at its
  most flattering -- a reader checking whether the hazard was handled finds a
  sentence saying it is. And the failure it produced was a *timeout*, so every
  instinct says "something is slow or deadlocked" and the answer was neither.

  `tests/conftest.py`'s `_every_test_leaves_the_alarm_no_louder_than_it_found_it`
  is the guard, autouse beside the environment one and for its reason: the
  failure lands on the test that caused it rather than on the next nine. It
  asserts the timer never gets **louder** rather than that it is unchanged --
  an alarm that legitimately fires leaves it at zero, and demanding equality
  would add a spurious second failure to a test that already reported the real
  one. Handler identity is the half that catches a leaked `lambda`.

- **A whole `termios` structure does not round-trip portably.** Asserting
  `tcgetattr(fd)` is byte-identical before and after a raw-mode read passed on
  every Linux leg and failed on macOS: `VMIN` and `VTIME` are meaningless once
  `ICANON` is back on, so a driver may normalise them on restore. Assert the
  flags the user would actually miss — `ICANON` and `ECHO` — and assert
  separately that they really were cleared in between, or "unchanged before and
  after" is trivially true of a function that changes nothing.
- **The suite must never inherit the developer's stdin.** `sync` asks
  `sys.stdin.isatty()` to decide whether anyone is there to answer a conflict,
  so a test that inherits a terminal *prompts* and blocks, and the same test in
  CI skips silently. `support.run_cli` passes `DEVNULL` and `support.typing`
  patches `sys.stdin`; a real pty is opted into with `keys=`.
- **`ARG_MAX` is not a constant, so no fixture may be sized against it.** On
  Linux the whole argv is bounded by `RLIMIT_STACK / 4` — 2 MiB against this
  container's 8 MiB stack, and **larger on a GitHub runner**. Measured the hard
  way: a fixture that built 3 MB of argv was refused here and *accepted* there,
  turning the three Linux legs red while macOS passed. The worse half of the
  same mistake was silent — a 2.2 MB `stage` fixture that was **green on the
  runner with the fix reverted**, a test that could not fail on the machine that
  matters, invisible from its own text.
  - For "a spawn was refused", use **one argument** over `MAX_ARG_STRLEN` — a
    fixed 32 pages (128 KiB) on Linux whatever the stack is, and `ARG_MAX` on
    macOS. 2 MiB clears both, everywhere, in one spawn.
  - For "the paths no longer go on the command line", assert *that* — watch the
    call and read its argv. It is the thing that changed, and no kernel limit is
    involved in checking it.
  - And if a test does build many real paths: **macOS's `PATH_MAX` is 1024**, a
    quarter of Linux's 4096, so a component chain sized for Linux cannot be
    *created* on the macos leg and the test errors rather than tests there.

#### The mutation harness

- **The suite a probe runs is run by pytest, and a killer is a nodeid.**
  `tools/verdict.py` hands each group to `pytest.main` in one process and
  classifies at `pytest_runtest_makereport`, so a killer reads
  `tests/test_sync.py::TestX::test_y` where it used to read
  `tests.test_sync.TestX.test_y`. Three consequences, and the last is the one
  that bites:

  - `sweeps/killers.json` written before this is *dropped*, not migrated —
    `mutate._loadable` asks pytest what it collects and keeps the intersection.
    The cache is machine-local and gitignored, so the cost is one slow sweep.
  - a **selection** is still dotted, because `mutants.targets_for` builds it out
    of module names. The two spellings meet in three reachability filters, and
    `mutate._reaches` is the one place that reconciles them. A nodeid compared
    raw against a dotted selection matches *nothing* — which turns off every
    ordering mechanism at once, costs the measured 3.9% and 6–10%, and **fails
    nothing**.
  - **Historical — `TUPFERL_MUTATE_VERDICT=unittest` selected the classifier
    that was here before**, so a row the two disagreed about could be graded by
    the old one rather than argued about. The switch and
    `tools/verdict_unittest.py` were both deleted in Phase C; `_probe` reads
    `tools/verdict.py` and nothing else. Two things it taught are kept because
    they are about *any* second instrument, not about that one: a value naming
    no layer was **refused rather than defaulted**, since a typo that silently
    fell back would report the pair as agreeing when only one ever ran; and what
    the retired layer could still grade **shrank with every Phase B cluster**,
    because `unittest`'s loader refuses a pytest-native module with `calling
    <class ...> returned <object>, not a test` — a `broke` row, which is never
    `caught`, so it appeared in neither of the two numbers a reader looks at
    while reading exactly like a harness fault. **A compatibility switch has a
    half-life, and it wants a stated end** — this one had Phase C written into
    its own docstring, which is why removing it was a checklist item rather than
    an archaeology exercise.

- **`unittest` loads a module's classes alphabetically; pytest collects them in
  definition order.** So the conversion changed which test reaches a mutated
  line *first*, and where one test hangs under a mutation and a sibling fails
  fast, that decides `caught` against `BROKE` -- and `BROKE` is never `caught`.
  Measured on the Phase A acceptance sweep: five rows of `tools/mutants.py` that
  the old runner caught came back `BROKE`, because `TestCappingTheTable`
  (unittest index 2, file line 1406) and `TestLineEndingsThatAreNotNewline` both
  arm `support.deadline` while their siblings `TestTheCap` (index 13, line 926)
  and `TestTheOperators` (line 158) did not. Alphabetically the bounded ones ran
  first and `failfast` stopped; in definition order the unbounded ones are
  first. **The defect was always there** -- the ordering only decided whether
  anything reached it.

  The general form is the one this file already states five times, in its
  newest spelling: *the killer a sweep reports is one route to the line, not all
  of them* -- and **changing the runner changes which route is first**. So when
  a row moves to `BROKE`, look for an unbounded sibling of a class that is
  bounded, before suspecting the harness.

  **It also silently repairs claims, which is the direction nobody looks.**
  `tests/test_mutants.py` carried a docstring saying six `line_starts` rows
  could not be answered at all: `TestChoosingTheTests.setUpClass` built the real
  import index, hung there before any test ran, and `setUpClass` is not a test,
  so the row came back `TIMEOUT` at 300s rather than `BROKE` at 30. Both halves
  had stopped being true at Phase A2 and nothing said so -- alphabetically
  `TestChoosingTheTests` ran before `TestLineEndingsThatAreNotNewline` and in
  definition order it runs long after, and `verdict.py` arms its alarm in
  `pytest_runtest_protocol`, which brackets setup as well as call. Measured in
  B6 on `at += 1` becoming `at -= 1`, with the generated selection and the
  `failfast=True` a sweep passes: **`caught` in 45s, on the branch and on a
  `main` worktree alike** -- which is what separates "the conversion fixed it"
  from "it was fixed already and the docstring did not know".

  **Historical -- a per-case bound inside a `subTest` loop did not bound the
  test, and `parametrize` is what removed the trap rather than a better bound.**
  `subTest` *catches* the `TimeoutError`, records a failure and carries on, so a
  loop over twenty operators cost twenty times the bound: measured on
  `TestTheOperators`, whose `mutate` helper already armed `support.deadline` per
  call, past 60s under one mutation against a 30s alarm. The fix was a class
  bound **as well**, which worked because `deadline` restores the outer alarm
  with only its remaining time.

  **One case is one test now, so the class fixture arms the bound afresh for
  each and `failfast` stops at the first that trips.** B6 met the same trap
  twice in one cluster -- `test_mutants.TestTheOperators` and
  `test_mutate`'s `test_a_row_that_asked_nothing_is_excused_on_the_same_terms`,
  the second measured past 120s -- and converted both, so there is no
  `subTest` loop left in the suite outside the probe *fixtures* that use one
  on purpose. The lesson survives its instance: **a construct that swallows
  the exception your bound raises is not bounded**, and it is worth asking of
  any new harness what it does with a `BaseException` from inside.

- **A test's own timeout must *beat* the harness's, not merely exist.**
  `tools/mutate.py` arms a per-test alarm (30s by default) and files anything
  that trips it as `BROKE` — which is never `caught`, so the line it was
  guarding ends up unguarded. `tests/test_watch.py` bounded each subprocess at
  30s too, so the two raced and the alarm won: seven mutants of `watch.main`
  and `watch.alive` came back `BROKE`. Pick a bound above the longest honest
  wait in the file and comfortably below the alarm, and say both numbers where
  you write it.

  **Where to arm it is the part that keeps being got wrong — five times, all
  the same way: the bound went where the sweep pointed, and the hang was
  somewhere else.** The rule that survives all five is *the killer a sweep
  reports is one route to the line, not all of them.* So find the callers of
  the hang-prone function and bound each entry point. The five, because the
  shapes differ:

  1. **Around the call** rather than the class. `TestTheHarnessAnswersBothWays`
     was bounded in `test_the_walk_catches_what_the_selection_missed`; two of
     six rows stayed `BROKE`, because `if not walk:` inverted hangs the two
     tests that pass `walk=False`.
  2. **In a helper** rather than the class. `line_starts` was bounded in the
     helper its class goes through and three of four rows stayed `BROKE` — the
     tests that kill them do not call that helper.
  3. **In the class the sweep named**, when the killer is in *another*:
     `mutants.py:170` is killed by `TestWhatIsNeverMutated`, reaching the same
     line through a module-level helper.
  4. **In this process**, when the route that hangs is a **subprocess** —
     `TestABoundedCallStillReturns` — where no `SIGALRM` here can reach. That
     one wants `subprocess.run(timeout=…)` *and* a memory ceiling in the
     child, because a mutant that loops **while appending** takes the machine
     before any clock speaks — the argument `verdict.cap`'s docstring makes.
     `tests/test_mutants.py`'s `returns` sets `RLIMIT_AS` in the child it
     generates and bounds it at 5s rather than 20: the honest wait is a spawn,
     and the bound is paid per test, under a sweep, on a machine already
     running thirty-eight lanes.
  5. **Around a `subTest` loop.** A bound is one shot: `subTest` *catches* the
     `TimeoutError`, records a failure and carries on with nothing armed.
     Measured — the first case of a two-case loop was covered and the second ran
     past 120s under the very mutation the bound was written for. Arm it
     **inside** the `subTest` — or, as B6 did to the last two of these, write
     the cases as `parametrize` and let one case be one test, where the class
     fixture arms the bound per case and no second copy is needed.

  For a whole class, `_bounded = support.bounds(seconds, why)` in the class
  body — `deadline` as an autouse fixture, entered and left with every test.
  **A bound around one call covers that call and reads as though it covered the
  class.**

  It was a `contextlib.ExitStack` entered in `setUp` until B6, because
  `TestCase.enterContext` is 3.11 and this project supports 3.10; a fixture
  makes that question moot. `bounds` exists because the rule was written down
  seven times instead of once — four lines per class, two pairs of them
  byte-identical including the comment — which is this file's own B5 lesson
  arriving in the paragraph that states it.

  **And check what the bound's exception collides with.** `TimeoutError` *is* an
  `OSError`, so a `deadline` inside an existing `pytest.raises(OSError)` is
  swallowed: the hang is accepted as the error under test and the bound turns
  one unguarded line into a test that cannot fail, which is worse than the hang
  it replaced. `tests/test_manage.py`'s fifo test reads the exception type back
  explicitly for that reason.

  **Write it through `support.bounded`, which knows the alarm actually armed.**
  Comparing against `mutate.EACH_TEST` is the obvious spelling and it guards
  only the *default*: `--each-test` is a flag, so a sweep at `--each-test 10`
  puts a 20s bound back above the alarm and the test written to prevent that
  cannot see it, because the constant still reads 30. `_run` passes the armed
  value to the child in `TUPFERL_MUTATE_EACH_TEST`, and `bounded` takes `SHARE`
  (two thirds) of it — the ratio `PROMPTED` and `EACH_TEST` already had, so
  stating the rule left every measured number where it was and changed only what
  happens when the alarm moves. It is a floor, never a ceiling: with nothing
  armed, or with `--each-test 0`, the fixture's own number stands unchanged.

  **Writing the rule down did not apply it, and counting the instances in prose
  is what a person does instead of a check.** `support.PROMPTED` and `PATIENCE`
  went through `bounded` when it was written and nothing else did. B5 set out to
  route the two `tests/test_watch.py` and `tests/test_reached.py` carried -- both
  a bare `20`, with a comment saying they beat "the 30s alarm", true of the
  default and false of `--each-test 10` -- and named the pair in a tuple. The
  review found **four more**, including `tests/test_mutate.py`'s `BOUND = 20`,
  three screens above a docstring reading *"that is the third instance of one
  mistake here"*. A hand-written list is a record of what somebody remembered.

  **`test_support.TestEveryWaitOnAChildIsBounded` is the guard, and it walks.**
  Every `timeout=` handed to `run`, `Popen`, `communicate` or `wait` anywhere
  under `tests/`, following a name to what it was assigned -- including a
  parameter default, which is how `test_mutate.py` hid its one -- and insisting
  it reaches `support.bounded`. 21 sites on 2026-08-31, with a `FLOOR` under the
  count for the reason `tests/test_errors.py` has one: a resolver that matched
  nothing would report no unrouted waits and read as a clean bill of health.

  Two things about its shape are load-bearing. Asking *what is being called*
  keeps `argparse.Namespace(timeout=60.0)` out with no exception list -- that
  `timeout` is the harness's own setting in a fake `args`, not a wait. And the
  one shape it must let through is recognised structurally rather than listed: a
  `timeout=` inside a `with pytest.raises(...)` is the *assertion*, as
  `running.wait(timeout=0.5)` is, and bounding it would bound the subject.

  Beside it, `test_a_driven_bound_follows_the_alarm_that_was_armed` asks a
  *fresh interpreter* what a routed constant comes out as under a patched
  `TUPFERL_MUTATE_EACH_TEST`. The walk checks the spelling; this checks that the
  spelling has the effect, which a source check never could.

- **A soft rlimit is not a cap: any descendant can raise it back.** `RLIMIT_AS`
  has a soft and a hard half, and `setrlimit` lets an unprivileged process raise
  soft up to hard freely. `verdict.cap` used to lower only soft and pass the
  inherited hard straight back — so under a sweep every probe ran with
  `soft = 4 GiB, hard = RLIM_INFINITY`, and one `resource.setrlimit(AS, (hard,
  hard))` anywhere below it bought an unbounded process. `tests/test_verdict.py`
  did exactly that on purpose, to reach a known state, and its docstring
  explained why it was safe.

  It was not. Measured, from the kernel log rather than inferred: `Killed
  process (python) total-vm:63940536kB, anon-rss:54020240kB` — **one process at
  51.5 GiB** on a 62 GiB machine, during a sweep whose lane ceiling was 4096 MiB.
  A single process an order of magnitude over the per-lane ceiling is proof that
  no ceiling was in force, which is what told the two apart: it is *not* lanes
  adding up, so `_COMMIT` is not the thing to look at, and lowering it would have
  cost parallelism and prevented nothing.

  `cap` lowers both halves now. Raising a hard limit needs a privilege none of
  this has, so the ceiling survives every `fork` and `exec` beneath it. The cost
  is that a descendant cannot undo the cap — which is right, and the one test
  that wanted to now asks for a **bounded** number instead: "clear the inherited
  cap" and "have no cap" are different asks and only the second can take a
  machine with it.
- **Coverage understates `tools/` badly, and the reason is the tool's own
  thesis.** `tests/test_verdict.py`, `test_reached.py` and `test_watch.py` all
  drive their subject as a *subprocess*, which in-process coverage cannot see —
  so `verdict.py` reads 30% while its classification is exercised end to end.
  Read the mutation numbers instead; that gap is precisely what
  `tools/reached.py` was written to repair.
- **The sweep mutates the code that decides what to kill, and then runs it.**
  `_lane` answers "which pids are this lane"; `_end_lane` `SIGKILL`s that
  answer. A generated table contains `row.group == leader` becoming `!=`, which
  selects every process the user owns *except* the lane — and the harness
  killed all of them. A real desktop session died that way on 2026-08-30
  (#91), and it was diagnosed as an OOM for hours because `killed by SIGKILL`
  reads as memory pressure. It was `os.kill` in a loop.

  **A guard on the walk cannot fix this, because the walk is what is being
  mutated.** `_permitted` is therefore a *second* fact read somewhere else:
  a process that started before this one cannot belong to a lane this one
  started. It reads `/proc/<pid>/stat` field 22 directly rather than through
  `_processes`, so one mutation cannot disable both — and when the table
  reader *is* the thing mutated, the guard refuses nothing, which is the old
  behaviour rather than a new hazard.

  The general shape, and it is worth more than the instance: **a harness that
  mutates itself must not route a destructive operation through mutable
  code.** Ask of any new one — a delete, a kill, a push — what the second,
  independent fact is that vetoes it.

- **The other half of that: a probe cannot answer a row that disables the bound
  it runs under — and the answer is a written disposition, not an exclusion.**
  #91 fixed what a mutated harness *kills*; this is what it *counts*. A probe
  runs this suite, this suite drives nested harnesses, so a probe carrying a
  mutated `_lane` or `_born` hosts a sweep whose `_end_lane` takes the probe
  with it, and one carrying a mutated sandbox pool hosts one that waits on an
  empty queue for ever. `_permitted`'s trick does not transfer: a veto works
  because *some* unmutated code is left to veto with, and here the code that
  would have to hold the bound is the code being mutated.

  **Measured, whole table, 2026-08-31** — `--all --only tools/mutate.py`, 1030
  rows, green baseline: 795 caught, 216 survived, 13 `BROKE`, 6 `TIMEOUT`. The
  19 unanswered rows sit in seven scopes, and 12 of them had no disposition, so
  the sweep's own section read *"12 asked nothing, so the table is that much
  smaller than it looks"*.

  | scope | rows | how it dies |
  |---|---:|---|
  | `_lane` | 7 | SIGKILL, 5–6s |
  | `_born` | 4 | SIGKILL, 4–5s |
  | `Work.take` | 2 | 300s, the per-row bound |
  | `_sandboxes`, `_borrow`, `_attempt`, `run` | 1 each | 300s |
  | `_Lanes.release` | 1 | `MemoryError`, 29s |
  | `_born_from_proc` | 1 | 0.3s |

  **`SIGKILL` and `TIMEOUT` mean different things here**, and the summary's two
  numbers hide that: the thirteen `SIGKILL`/`MemoryError` rows are answerable on
  an idle machine and the six `TIMEOUT` rows are not answerable at all. Reading
  them as one category is what made the first attempt at this reach for a single
  mechanism.

  **#96 proposed refusing to *generate* these, and the measurement refutes it.**
  Built and measured before being taken back out: an exclusion naming
  `_Lanes`, `_lane`, `_born*`, the pool and `Work.take` as *scopes* removes 99
  rows to repair 19 — and **57 of the 99 are rows the suite catches today**,
  24 more are read survivors. That is 3 caught rows destroyed per unanswered
  row repaired, and it is this file's own recorded mistake at a coarser
  granularity: *"the operator is required, and that is the whole design … a
  bare tag would excuse a live guard about half the time it was used"*, 53%
  measured there against **58% here**.

  Nor is there a cost argument for it. The 19 rows are **1890 lane-seconds of
  92401, 2.0%** — and the SIGKILL ones die in five seconds, not in a runaway.

  **The rows are unanswerable *under a sweep*, and the qualifier is measured
  rather than hedging.** Run alone on an idle machine, `_lane`'s `found.add`
  row and `_born`'s `branch` row are both `caught` in 42.8s -- warm cache and
  cold, identically, so it is not an ordering effect. With 49 GiB free the
  unguarded nested harness fits; with 36 lanes sharing the machine it does not.
  That is `_Lanes`' own split showing itself: `_BUDGET` shrinks an *honest*
  nested harness and `_Lanes` answers a dishonest one, so mutating `_Lanes`,
  `_lane` or `_born` leaves the probe with neither.

  Two consequences worth writing at the tag: a **narrow** run will report these
  tags spent, which is correct rather than a reason to delete them; and the
  distinction does not apply to the six `TIMEOUT` rows, where a drained sandbox
  queue cannot be recovered by any amount of free memory and nothing has ever
  caught them.

  **So the fix is 19 `# survivor:` tags with reasons written in them**, which is
  the mechanism this file already documents for exactly this (`BROKE` and
  `TIMEOUT` are excused on the same terms, #57). It costs no coverage, it is per
  `(line, operator)` rather than per scope, and the sweep already counts it —
  7 of the 19 were excused that way before this and only the other 12 were
  loud. Two further reasons it beats refusing to generate:

  - **a future unanswerable row in those scopes still shows up.** An exclusion
    is permanent and silent, so a later change could make a *new* row in
    `_Lanes` unanswerable and nothing would ever say. That matters most for
    exactly the cluster the exclusion was wanted for.
  - **the reason lives beside the line**, so the next reader of `_lane` learns
    why a sweep cannot speak for it, which a constant in another module does
    not tell them.

  What an exclusion would still be right for is a row that is *dangerous* rather
  than merely unanswerable — `mutants.UNMUTABLE` exists for that and is empty.
  These are not: `_permitted` keeps a mutated `_lane` inside its own lane.

- **Never launch a mutation sweep with `nohup`.** It sets SIGHUP to `SIG_IGN`,
  and a process started that way passes the *ignored* disposition to every
  descendant — so `tests/test_merge.py`'s stub, which killed itself with SIGHUP
  to produce an exit status of `-1`, silently exited 0 instead. The sweep's
  baseline then went red on a file the change never touched, and every verdict
  in it was void. Use `setsid`, or the shell's own backgrounding with output
  redirected. The stub restores `SIG_DFL` itself since v0.5, so this particular
  test no longer cares — but the general hazard stands for any fixture about
  signals, and a POSIX shell **cannot** reset a signal it inherited as ignored.
- **A redirected stream is *block* buffered, so `flush=True` at one call site
  is not a fix.** Every documented way of running a sweep sends stdout to a
  log, and `print` to a non-terminal holds ~8 KiB before writing. `_attempt`
  learned this and flushes its progress line; the *header* prints did not, and
  a detached sweep then showed **0 bytes for its first five minutes** — checked
  at 35s and again at 2:25 with forty lanes working the whole time — because
  `slowest_first` puts the survivors first and no row completed to carry the
  header out.

  That is the ambiguity [`tools/watch.py`](tools/watch.py) exists to remove:
  silence reads identically to progress. `mutate.main` now sets
  `sys.stdout.reconfigure(line_buffering=True)` **once**, because remembering
  `flush=True` per call site is the thing that already failed. One write
  syscall per line against a job measured in minutes.

  The general form: **when a lesson is written down as a per-call-site habit,
  check whether it can be set once instead.** The habit is what rots.

- **A mutation sweep is minutes to hours.** Launch it detached, record the pid,
  and watch it with `tools/watch.py` — never identify the job by pattern.
  `pgrep -f` matches the asking shell's own command line, which reported a dead
  sweep alive twice in one session; and run a moment after `setsid`, it matched
  a transient pid instead of the sweep's, so a watcher armed with it announced
  a live sweep dead at 78 of 806 rows. **A pattern is not an identity, and it
  fails in both directions** — the second is the one that reads as a real
  failure and sends you looking for a crash that never happened. `setsid …
  &` does not hand back the sweep's pid in `$!` the way plain `&` does, which is
  exactly when reaching for `pgrep` is tempting; read the pid out of the sweep's
  own output or `--json` path instead, and verify `kill -0` on it before
  trusting a watcher. Point `--done` at `<json>.done`, never at the `--json`
  report, which under `--batch` and `--all` is rewritten after every file and so
  exists long before the run ends.

  **The pid is in `<json>.pid`, written by the sweep itself.** That is what "read
  it out of the `--json` path" means, and not knowing the file existed is how one
  session came to scan `/proc` for an exact argv-and-cwd match instead — which
  worked, and was answering a question the tool had already answered. Two
  corollaries, both learned the same session:

  - **a stale `<json>.pid` from a killed run outlives it.** A pid file beside no
    log reads exactly like a sweep that started and produced nothing, and the
    number may since have been handed to something else. Delete it with the
    `--json` file when you abandon a run, and `kill -0` before believing it.
  - **waiting on `<json>.done` alone cannot tell *finished* from *died*.** A
    sweep that is killed never writes it, so the wait is identical in both
    cases — and the notification when the *waiter* is reaped reads like the
    sweep completing. Wait on `[ -f <json>.done ] || ! kill -0 $pid`, and say
    which one happened.
- **A killed sweep used to leak its probes for ever, and the probe is the only
  thing that can notice.** Every teardown path in `tools/mutate.py` runs *inside*
  the sweep, so a `SIGKILL` left probes with nothing that would ever reap them:
  #114 measured four alive **36 hours** later, one holding 3.2 GiB, and every
  sweep in that window sized itself from a `MemAvailable` they were eating --
  invisible to `_report_crowding`, which sums *lane* RSS, and an orphan is not a
  lane. `verdict.watch_for_orphaning` is the fix, and three things about it are
  the design rather than detail:

  - **not `os.getppid() == 1`.** Measured here: a killed sweep's probes
    reparent to **1522**, the systemd user manager, so the obvious test would
    have watched them live for ever.
  - **the sweep names itself, in `MUTATE_OWNER_PID`.** Asking `getppid()` at
    startup loses a race in the leaking direction: a probe orphaned before it
    finishes importing records the *reaper* as its owner and then waits for a
    change that has already happened. Found by writing the test that kills the
    sweep with no pause; a one-second sleep made it pass.
  - **it `killpg`s its own group, and refuses unless it leads that group.**
    `_run` passes `start_new_session=True`, so the group holds nothing but the
    probe's descendants -- which is what has to die, since a probe exiting alone
    would orphan the `git` its suite forked. There is no process table to walk
    and no membership test to invert, which is #91's whole lesson; and the veto
    means a mutation that breaks the check makes it do *less*.

  The temporary trees are the other half and cannot be fixed the same way: they
  belong to the sweep, not the probe. `_owned_temp` stamps each one with the
  sweep's pid *and birth time*, and `_collect_abandoned` removes only those
  whose owner is provably gone.

  **It is `--collect`, never automatic, and the first version was automatic --
  which is how this file's own rule got proved again.** A probe runs a *mutated*
  copy of `tools/mutate.py`, so the very first row of the very first sweep taken
  over that change mutated the liveness test and the probe deleted the live
  sweep's own sandbox out from under it: `FileNotFoundError:
  .../tree3/tools/verdict.py`, and the run died on row 1 of 39. That is exactly
  *a harness that mutates itself must not route a destructive operation through
  mutable code*, which #91 had already paid for once with `_end_lane`, arriving
  in a new place within a week. **When you add a delete, a kill or a push to
  this harness, that rule is the first thing to check, not the last.**

  Two things make it acceptable now. It is a flag, for `--accept`'s reason --
  removing things is a decision, and a run that made it by itself would be
  making it on somebody's behalf. And the veto is a fact the code cannot talk
  itself out of: a tree holding `Path.cwd()`, or holding a parent of it, is a
  tree *this process is standing in*, and the kernel answers that rather than
  anything computed here. A mutation to the liveness test can now at worst
  delete somebody else's abandoned tree.

  **It cannot collect what predates the stamp** -- nothing distinguishes an old
  tree from a live one -- so `unstamped` names those and a person runs one
  `rm -rf`. A tool that deletes on your behalf should not be the one guessing.

- **The sweep sizes itself from what is actually free, and says so.**
  `tools/mutate.py` reads `MemAvailable` out of `/proc/meminfo`, takes the
  smaller of that and any cgroup limit, leaves a gibibyte, and divides. So a
  machine with an editor and a browser on it yields a small budget and an idle
  one a large budget, with nothing to set. The line every run prints names the
  rule it used:

  ```
  32 lane(s) at 2529 MiB each, from 53962 MiB of usable memory
  (54986 MiB unclaimed, less 1024 MiB spare), committing 150% -- see tools.mutate._share.
  ```

  The last clause is `_COMMIT`, and it is said out loud for the reason the rest
  of the line is: over-committing is a judgement that lane peaks do not
  coincide, and a reader has to be able to see it was made -- especially on the
  run that does get killed.

  Two things follow, and both bit before this existed:

  - **The old rule halved visible memory** on the guess that someone else wanted
    the other half. On this container that cost more than half the parallelism,
    because `_share` gives up *lanes* once each one's ceiling would fall under
    `_FLOOR`: 3 lanes where 7 fit, and a 12-row table at 11.2s against 7.6s over
    three interleaved pairs. It survives only as the fallback for a machine with
    no `/proc/meminfo` -- macOS, and the `macos` CI leg is what keeps that arm
    reachable.
  - **Every run also says how close it came.** The last line of a sweep names
    what its heaviest lane process held against what it was allowed:

    ```
    heaviest lane process held 1892 MiB of its 2053 MiB ceiling (92%, sampled, ~3% under)
    ```

    That number is why `_FLOOR`'s comment was corrected. It claimed a lane peaks
    at 838 MiB with a 2x margin — measured in woswoar, and it travelled here with
    the word "here" attached. On this machine four sweeps gave 1766, 1828, 1892
    and 1901 MiB, so the real margin is about **1.1x**. `_FLOOR` stays at 2 GiB
    because nothing has ever been killed for memory, but the figure behind it is
    now measured rather than inherited, and the line is yellow above 90% so a
    machine where it does not fit says so on the first run rather than after a
    wall of `BROKE`.

    **Do not divide the budget by a *resident* figure to pick a lane count.** A
    lane's tree holds ~73 MiB resident while one of its processes reaches
    ~1.85 GiB of address space — 25x apart, and only the second is what the
    ceiling caps. An issue filed on that confusion (#52, since rewritten) would
    have given every lane a 1032 MiB ceiling and killed all of them.
  - **`TUPFERL_MUTATE_TOTAL` is still there and should now be rare.** It was the
    documented way to say "this machine is mine"; that question is measured
    rather than asked. Reach for it to *reproduce* a small machine, not to
    unlock a large one.
- **The generated sweep goes last.** Implement, preflight, review and *apply*
  the review, and only then `python -m tools.mutate --base main`. The table is
  generated from the lines as they stand, so any edit after it invalidates every
  row.

#### CI, and fixtures that pass for the wrong reason

- **A Hypothesis profile means something different in CI than on your machine.**
  Hypothesis registers and loads a derandomised profile of its own when it sees
  `CI` in the environment, and a profile that leaves a field unstated inherits
  whatever is default when it is registered. That is why every profile in
  `tests/profiles.py` states every field it cares about. To reproduce a CI-only
  failure of this shape, run the preflight the way CI does:

  ```sh
  CI=true TUPFERL_HYPOTHESIS_PROFILE=ci python -m tools.run_tests
  ```
- **A platform skip turns a CI leg red, and it is not only the `macos` one.**
  Every job that runs tests passes `--no-skips` -- the `test` matrix's three
  legs and all four `macos` shards -- which is exactly what the flag is for: a
  leg with every optional tool installed, where a skip means something is
  missing rather than absent by design, so a platform-gated test is a failure
  there rather than a skip.

  **This entry said "that job", singular, and was wrong from the day it was
  written.** The `test` matrix has passed `--no-skips` since the bootstrap
  commit; the entry arrived in #61 and named `macos` alone, because the instance
  it was written from happened to fire only there. Same family as the
  `merge.conflictStyle` entry below: a claim nothing could have contradicted,
  because the fixture that would have shown it did not exist.
  Measured: one `skipUnless(hasattr(os, "sched_getaffinity"))` added in this
  repository's own review pass took that leg from green to red on the next
  push.

  Spelled `@pytest.mark.skipif` since B6 converted the last real `skipUnless`.
  **The polarity inverts and nothing checks it**: pytest has no `skipunless`, so
  `skipUnless(shutil.which("git"))` becomes
  `skipif(shutil.which("git") is None)`, and the wrong way round skips on every
  machine that *has* git -- green, silent, and testing nothing. `--no-skips` is
  the leg that would say so, which is a second reason not to drop it.

  A test whose *stronger* half is platform-specific should assert the part that
  holds everywhere and add the rest under a plain `if`, labelled at the
  assertion. §2 asks for the label either way; this is the spelling that keeps
  the leg green.
- **A test that greps a config file must read it with the comments stripped,
  and this has now cost three tests in two files.** A workflow that explains
  itself quotes the setting it is explaining, so `"if: always()" in gate_block`
  is satisfied by the comment `# - \`if: always()\`, because a job whose
  dependency failed is *skipped*`. Measured: deleting the real `if: always()`
  line from `ci.yml` left **all 33 tests in `tests/test_ci.py` green**, and the
  test that could not fail was the one guarding the single required status
  check.

  The other two are already recorded beside their own code —
  `test_release.py`'s `settings()` ("the first version of that test failed on
  its own explanation") and `test_ci.py`'s `mutation.yml` class ("two of four
  hand-made edits survived"). Three instances is a rule: **strip once, in the
  parser, and give the stripping its own test with both halves** — a setting
  that is still there, and a phrase that is only ever prose. Strip too much and
  everything passes by finding nothing; strip too little and it passes by
  finding a comment.

  The stripping rule has to be exact rather than approximate, and it is not the
  same rule in both files: `ci.yml` and `release.yml` have no trailing comments,
  so whole comment *lines* is exact there, while `mutation.yml`'s class cuts at
  the first `#` and its comment says why that is exact for it. Check the file
  before copying either.

  **The way to find one of these is to perturb the file and watch**, which is
  §2's revert-and-verify with the "fix" being a setting: copy the tree aside,
  delete the setting, and confirm the test goes red. Seven such probes over
  `ci.yml` found this one and cleared the other six.

- **A fingerprint of "nothing was written" needs the file's bytes in it.**
  Path, size and mode is the obvious spelling and it cannot fail here: the edit
  a sync test makes is usually one line to upper case, so the file before and
  the file after are the same length with the same mode. `tests/test_status.py`
  had exactly that, and its *own* second half — run a real `sync` and insist
  the fingerprint moves — is what caught it. Leave mtime out; a read can move
  it on some filesystems.
- **A test that hand-rolls what the code under test does can diverge from it,
  and only another machine may notice.** A precondition for #15 drove
  `gitrepo.fetch` then `gitrepo.merge` itself instead of running `sync`; on the
  runner's git 2.55 that pair left **nothing** unmerged, while the same fixture
  through `integrate` conflicted exactly as expected — three tests beside it
  passed in the same run. The mechanism is not established and is not worth
  guessing at; the lesson is §2's "prefer driving the real thing", now with an
  instance where the copy and the original disagreed. Where a precondition is
  wanted, look for one the real path already proves: the refusal message names
  the path, and nothing but a real conflict could put it there.
- **Colour is decided per stream, and a captured stream is not a terminal.**
  `tools/paint.py` asks `isatty` of the stream being written to, so everything
  a test captures — `support.quiet`, a `subprocess` pipe, `> sweep.log` — comes
  back exactly as it did before colour existed. That is why adding it moved no
  existing assertion, and it is the same property `tools/watch.py --match`
  depends on. Two rules when you paint a new line: **pad before painting**
  (`f"{painted:9}"` counts the escape bytes as columns) and put the code around
  whole words, never inside one. `support.Screen` is the capture that claims to
  be a terminal, for the half a captured run cannot show.

### Decisions from the plan's open questions (§9)

Recorded here as well as in the README because a later change is likelier to
read this file:

| question | answer | why |
|---|---|---|
| `argparse` or `click` | `argparse` | the plan says prefer fewer dependencies, and the command set is eight verbs with a handful of flags |
| snapshot format | plain copies under `.tupferl/state/<hostname>/` | the plan sanctions it for v1; content-addressing buys deduplication nobody has measured a need for |
| merge implementation | `git merge-file` | git is a hard requirement already, and its 3-way merge is battle-tested where a hand-written one would be the most defect-dense file in the project |

### Measured, and kept

- **Copy the two-machine fixture rather than building it — 120.4ms to 4.3ms,
  24% off the serial suite** (#19). `support.template()`
  builds the tree once per *process* and `copy_template` copies it; **190 of
  the suite's 1991 tests** take it, all of them through the `two_machines`
  fixture since B4b converted the last `TestCase` user. That count was 146 when
  #19 was measured and is re-counted here because this entry was edited without
  re-checking it: `pytest --collect-only --fixtures-per-test`, 2026-09-01. The
  numerator is unchanged at 190; the denominator said 1920, which was already
  wrong before Phase C deleted 100 tests and made it wrong differently -- a
  ratio written as two hand-typed numbers goes stale on whichever one somebody
  is not thinking about. The durable half of this entry is the 120.4 ms against
  4.3 ms below; both counts are a moving target and are dated for that reason.

  | | median |
  |---|---|
  | build from scratch | 120.4 ms |
  | `copytree` of a built one | 4.3 ms |

  Interleaved A/B, three pairs, the six affected modules run serially: **19.5 s
  saved of 82.4 s**, about 24%. On the *parallel* suite the same change is only
  4.6 s, because wall-clock there is bounded by the slowest shard —
  `test_sync_properties`, which is 2 tests and 19% of the serial total. Both
  numbers are the median of paired differences; three pairs is not many, and the
  parallel one is inside the run-to-run spread.

  Two things in a copy still name the tree it came from, and both are fixed at
  the copy: `.git/config`'s `remote.origin.url` (without which every test pushes
  to the template's remote and sees other tests' commits) and `.git/FETCH_HEAD`
  (inert — nothing reads it — but a stale absolute path in a fixture).
  `test_support.TestTheTwoMachineTemplate` drives the contamination case rather
  than comparing URLs, and greps a copy for the template's path so that a *third*
  such file is caught rather than waited for.

- **Order each file's rows by what they cost last sweep — 6% at 16 lanes, 10%
  at 32** (`slowest_first`).
  Four interleaved pairs over `--only tupferl/`, 1309 rows, one binary and one
  variable — the control is the same `killers.json` with its `seconds` map
  stripped, since an empty map makes `slowest_first` a no-op and no second code
  path is needed:

  | lanes | pair | unordered | slowest-first | |
  |---|---|---|---|---|
  | 16 | 1 | 306.81s | 286.97s | −6.5% |
  | 16 | 2 | 300.83s | 282.90s | −6.0% |
  | 32 | 1 | 214.06s | 192.42s | −10.1% |
  | 32 | 2 | 214.68s | 192.64s | −10.3% |

  Median paired difference **−18.9s at 16 lanes and −21.8s at 32** — an almost
  constant saving, so it is a bigger *share* of a shorter run. That is the
  mechanism showing itself rather than a coincidence: it predicts, and the runs
  confirm, that more lanes means more idle capacity for a late 90s row to waste.

  **It thins the tail rather than removing it.** The last survivor finishes dead
  last in all eight runs; what moves is the median survivor's completion, 1198 →
  1043 of 1309 at 16 lanes and 1295 → 1189 at 32. The residual is structural:
  the sort is *within* a file and `by_size` runs the largest file **last**, so
  that file's survivors are dispatched near the end however well its own rows
  are ordered. Ordering *files* by predicted cost would reach them and would
  keep every row contiguous — at the price of #49's reason for smallest-first.
  Not attempted.

  **And it costs one row, reproducibly.** `tupferl/conflicts.py:635 in ask()`
  went `caught` in 4 of 4 unordered runs and `BROKE` in 3 of 4 ordered ones,
  always as `test_b_keeps_both ... did not finish within 30s`. The landmine is
  pre-existing and is the one CLAUDE.md already names — a prompt in a test must
  fail, not block — but `Killers.ahead_of` sets `first` identically in both
  arms, so what differs is `Learned`, whose front is fed by completion order.
  Reordering changes which test is tried first, and one candidate blocks rather
  than failing. A `BROKE` row is never `caught`, so that line is unguarded on
  the runs where it fires.

  **A second instance was claimed here and then withdrawn, and the withdrawal is
  the more useful entry.** `tools/mutate.py`'s `_attempt`, the `drop-call` on
  the write that applies the mutant, was reported as `TIMEOUT` at 300s in "5 of
  5 attempts across two trees" and filed as #107. **Every one of those five runs
  omitted `failfast`.** `mutate.run`'s signature is `failfast: bool = False` and
  `main` passes `failfast=True` for a generated table, so a hand-driven
  `mutate.run([row], ...)` keeps going after the first failing test where a
  sweep stops at it. Re-run with the flag a sweep sets, on the same row and the
  same tree:

  | arm | without `failfast` | with it |
  |---|---|---|
  | selection as generated | `TIMEOUT` 300.0s | **`caught` 0.8s** |
  | recorded killer on `first` | `caught` 1.7s | `caught` 1.7s |
  | killer's module as the selection | `TIMEOUT` 300.0s | **`caught` 0.8s** |

  So the rule, and it is not specific to this flag: **a single-row reproduction
  has to be driven with the arguments the sweep uses, or it is answering a
  different question.** `run`'s defaults are tuned for a hand-written spec —
  `failfast=False` is right there, because "a red baseline is a thing you want
  the whole of" — and every one of them is a way for a reproduction to diverge
  from the run it is meant to explain. Read `main`'s call, not the signature.

  What is left unexplained is one observation, not five: a whole-table sweep of
  a branch reported that row `TIMEOUT` while the same sweep of `main` reported
  it `caught` in 3.5s, both with `failfast` on. `failfast` stops at the first
  *failing* test and not at a slow passing one, so a front that puts slow
  passers ahead of the fast failure would spend the budget — which is a
  hypothesis and is recorded as one. #107 carries it, at P3.

- **Historical — the lane count was held at 16 by a constant (`_LANES`, now
  removed), and lifting it was worth 30%.** `_LANES = 16` sat in `run`'s `wanted` expression with nothing behind
  its comment ("the most lanes worth running, whatever the machine reports"). On
  a 32-core machine it was the *only* binding term — `usable_cpus() * 2` gave
  64 and memory gave 25 — so the tool used half the machine. Measured over the
  1309 rows of `--only tupferl/`, two interleaved pairs each: **214.1s and
  214.7s at 32 lanes against 306.8s and 300.8s at 16.**

  Removed. What bounds the ask now is the work there is and the cores there
  are, and nothing else.

  **And the ceilings may now add up to `_COMMIT` (150%) of the budget.** A
  ceiling is headroom for a pathological row, not what an honest one spends, so
  requiring `lanes x ceiling <= budget` prices every lane as though all were
  pathological at once. The evidence it was already too strict: `--workers 32`
  had been committing **126%** for dozens of sweeps, whole-tree included, and
  nothing has ever been killed for memory. On this machine the default goes from
  16 lanes to 39.

  Three things to keep straight, each a way it could go wrong quietly:

  - **`_COMMIT` is not applied to `_affordable`**, which divides by what a lane
    is *measured* to use rather than by its ceiling. That number already prices
    peaks as independent, so scaling it too would spend one allowance twice.
  - **The allowance must buy lanes, not headroom.** Applying it to the ceiling
    alone gives the same lane count a bigger ceiling nobody reaches — no more
    parallel than before, and it passes every obvious assertion. Measured: that
    mutation survived every other test in the class until
    `test_the_commitment_buys_lanes_rather_than_headroom` was written for it.
  - **Measured, and the constant is vindicated by an order of magnitude
    (#90).** `_report_crowding` samples the *sum* of lane RSS at one instant —
    what the host actually feels, where `_report_headroom` watches the heaviest
    single process. Three whole-table runs of `--all --only tools/mutate.py` at
    40 lanes, ceilings summing to **80 GiB on a 62 GiB machine**:

    | | |
    |---|---:|
    | peak held by every lane between them | **5.1–5.7 GiB**, 7–10% of budget |
    | lowest `MemAvailable`, watchdogged independently | **47 GiB** |
    | headroom against the commitment | **15×** |

    So 150% is not generous, it is conservative — on the one table where peaks
    were argued to correlate. Both instruments *sample* (1s and 2s), so a
    sub-second spike is invisible to both; what is established is that no
    *sustained* aggregate pressure exists, which is the claim the constant
    rests on.

  - **A lane at 100% of its ceiling is a runaway being capped, not a starved
    lane — and reading it the other way costs an afternoon.** It reads like
    "the ceiling is too small", so #94 was filed on it. The A/B refuted that:
    same table, same 40 lanes, ceiling raised 2048 → 3072 MiB, and the heaviest
    lane came back at **100% of both** (2046/2048, 3071/3072) while `BROKE` got
    *worse*, 3 → 8. A process with no bound of its own fills any bound you give
    it, so no ceiling is ever enough and there is nothing here to tune.

    What those rows actually are is #96: **the sweep mutates its own memory
    guard and pool** — `_Lanes.release`, `_Lanes._sample`, `_sandboxes`,
    `_borrow`, `Work.take` — so the probe's guard is disabled and its nested
    harness runs unbounded whatever the outer lane was given. 8 of 9
    not-answered rows recurred across both arms, which is what tells a
    structural cause from noise.

  - **A `SIGKILL` row is a question, not an answer.** It reads as "the machine
    ran out of memory", and the harness's own message says so *first* — but it
    offers something else second, which was the true half on 2026-08-30: "or a
    harness running inside it killed the session it was in". A sweep killed a
    desktop session that day and `_COMMIT` was the obvious suspect and the
    wrong one; the cause was #91, a mutant of `_lane` inverting the membership
    test that `_end_lane` then `SIGKILL`s. `os.kill` in a loop.

    So cross a `SIGKILL` row against the summed figure before believing
    memory, and look at *which* rows died — seven of the thirteen mutated the
    guard machinery, which no memory story explains.

  The counter-argument, which is measured and which `slowest_first` makes worse:
  woswoar#232 was not lanes drifting up independently, it was *three of four
  lanes running away simultaneously on the same source line*, because a
  generated table walks a file in order and its expensive rows are adjacent.
  Peaks are correlated by position, and ordering rows dearest-first concentrates
  them further — peak lane memory measured 466 MiB unordered against 528 MiB
  ordered, both trivial against a 3363 MiB ceiling on this table, but the
  whole-tree figure is 92%.

- **Run a row's recorded killer *before* the learned front, not after: worth
  3.9%.** `Killers.ahead_of` puts the test recorded as catching *this
  row* on `Mutation.first`; `Learned` (#43) is move-to-front over the last 8
  killers seen during the run. `_attempt` composed them as
  `f"{ahead} {mutation.first}"` — so up to `LEARNED - 1` general tests ran ahead
  of the one test known to catch the row, on **1105 of a 1309-row table**.

  `Killers.ahead_of` already argues the opposite one function away: it drops the
  cheap prefix entirely for a row whose killer is known, because "exact beats
  general, the prefix would only be work before the answer". `Learned` is
  general in the same way — what caught the *previous* rows is a proxy for what
  catches this one, and here the thing being proxied is in hand.

  Measured over `--only tupferl/`, 1309 rows, warm cache, 32 lanes:

  | ordering | wall |
  |---|---|
  | none | 215.09s |
  | by recorded cost, `Learned` first | 192.42s / 192.64s |
  | by recorded cost, **killer first** | **184.98s / 185.46s** |

  The mechanism is visible per row, which is what the recorded `spent` is for:
  the median **caught** row falls from 0.67s to 0.33s and caught work from 1711
  to 1137 lane-seconds, while survivor cost is unchanged (3040 → 3089). That is
  the shape to expect — a survivor runs everything by construction, so nothing
  about `first` can reach it.

  **`Learned` follows the killer rather than being dropped.** A recorded killer
  can be stale — the code moved, the test no longer sees the mutation — and the
  learned front is then the next guess before the whole selection. It costs
  nothing when the killer is right, because the killer has already answered. And
  removing `Learned` outright would gut the sweep that matters most: its own
  docstring is right that `Killers.known` "misses by construction on `--base
  main`", whose rows are new text, so on a diff sweep the move-to-front is the
  only adaptive ordering there is.

  **`Mutation.exact` is what carries the distinction**, because `first` holds
  either kind and they are indistinguishable once written into one string. The
  flag's *producer* needs its own test: dropping `exact=True` from
  `Killers.ahead_of` sends every row down the `else` arm and silently restores
  the old order — measured, that mutation survived every test written against
  the composition itself.

### Measured dead ends — do not re-attempt without new evidence

- **Sorting the whole table by cost across files loses to sorting within each
  one — 205.2s and 204.2s against 185.0s and 185.5s, on identical total work.** Tried twice, for two
  different reasons, and it lost both times.

  The first attempt rested on two beliefs, both wrong. That `sweep` counted a
  file down to zero before writing its `--json` -- untrue since #46 made that
  write per row, though `by_size`'s docstring still said it and the claim was
  quoted as a reason. And that a *timed* row does not need `Learned`, since
  `Killers.ahead_of` has put its own killer on `first` -- true in principle and
  false in fact, because `_attempt` ran the learned front **before** that
  killer. Result: 222.55s against a 215.09s no-ordering control, the whole gain
  cancelled.

  The second attempt was after that inversion was fixed, when the premise
  finally held. It still lost: **205.23s / 204.20s against 184.98s / 185.46s**
  for the within-file sort on the same binary.

  What makes it a real dead end rather than a tuning problem is that the total
  *work* is identical -- 4233 lane-seconds global against 4226 within-file, 0.2%
  apart, with global's caught rows marginally *cheaper*. It is purely worse
  packing: ideal makespan for both is ~132s over 32 lanes, and within-file lands
  at 185s where global lands at 205s. Better per-row ordering, more idle time,
  which is the opposite of what LPT promises.

  **The mechanism is not established**, and that is stated rather than guessed
  at: it would need a completion timeline the logs do not carry.

  **What would justify re-opening it**: that timeline. Instrument each row's
  start as well as its finish and look at where lanes actually idle. Without it
  any further attempt is the same guess again.

- **Shuffling the outward walk to break up the survivor herd is 12.7% slower
  *and* reported 24 false `caught` verdicts.** Every lane
  resolves the same module list through the same loader, so survivors dispatched
  together march through the suite in lockstep -- all in the same module at the
  same instant. The effect is real and measured: bunching them costs 3040 ->
  3181 survivor lane-seconds, **1.6%**.

  Seeding the walk from `_key(row)` -- selection untouched, walk beyond it
  shuffled -- was **12.7% slower** (208.56s / 208.43s against
  184.98s / 185.46s). But the timing is the least of it.

  **It reported 24 false `caught` verdicts, reproducibly.** Both runs came back
  `1300 caught / 6 SURVIVED` where the truth is `1276 / 30` -- a mutation score
  of 99.5% against 97.7%. A survivor runs the whole suite by construction, so no
  reordering can honestly change that outcome. (Measured against the `unittest`
  backend, in what was then `verdict._reached` and became
  `verdict_unittest._reached` before Phase C deleted it. `tools/verdict.py`'s
  `_groups` is the same walk and this has not been re-attempted against it --
  which is the point of a dead end, not a gap.) All 24 were "caught" by
  `tests.test_mutate.TestTheHarnessAnswersBothWays.test_the_walk_catches_what_the_selection_missed`,
  on rows mutating `config.py`, `merge.py`, `manifest.py` and `sync.py`.

  Two things follow, and the second is the one worth carrying forward:

  - **`_unbaselined` did not catch it**, because it cannot. A `tupferl/config.py`
    row's selection never names `tests.test_mutate`, so no baseline shard had
    proved that module green in the sandbox -- the walk reached it, it failed,
    and the failure was recorded as a kill. The guard covers a killer no shard
    *could* have covered only when the shard existed.
  - **The mechanism is not established, and "the suite has order-dependent
    tests" was written here on a hypothesis that then failed to confirm.** The
    offending test reproduces in *no* smaller setting: not standing alone, not
    with the seed set, not with a mutated sandbox, not after the exact
    eleven-module prefix the shuffled walk would have run before it (reproduced
    from the row's own seed), not under a lane-sized `RLIMIT_AS`, and not as a
    one-row sweep with the shuffle applied -- where the row correctly survives.
    Only a full 32-lane sweep produces it, and then reproducibly.

    What that leaves pointing at is something that exists only there: many lanes
    sharing a sandbox pool while one of the tests being run is itself a nested
    mutation harness. **What would settle it is the traceback, and a sweep now
    prints it.** `Verdict.why` is recorded for every `caught` row and was read
    by nothing but the baseline branch; `_loose_evidence` prints it for each
    unbaselined killer, one row per killer with the count of what it caught.
    Deliberately on the *green* path as well as the red one — a killer that
    fails untouched is diagnosed by the check that follows, while a killer that
    *passes* untouched and still caught rows is corrected by nothing and leaves
    nothing to read, which is exactly the shape of this result. Re-open the
    shuffle only with that output in hand; six attempts to reproduce it failed
    and none of them had it.

  So the walk order is load-bearing for *correctness* and not only for speed,
  by a route not yet understood. 1.6% was never going to pay for that.

- **Interleaving a mutation table round-robin across files cuts `Learned`'s hit
  rate from 72.7% to 27.3%, and nothing fails to say so** (#49). Proposed so
  that an interrupted sweep would have partial coverage of every file rather
  than complete coverage of some. It breaks `Learned` (#43), whose docstring
  states the premise it rests on: rows arrive sorted by file and line, so
  consecutive mutants sit in the same function and are usually caught by the
  same test.

  Replayed over the 906 caught rows of a recorded whole-tree report, 13 files,
  `LEARNED = 8`:

  | ordering | move-to-front hit rate | adjacent rows sharing a killer |
  |---|---|---|
  | contiguous by path — today | 72.7% | 44.1% |
  | size-first, whole files | 72.8% | 44.1% |
  | round-robin across files | 27.3% | 0.1% |

  Sorting whole *files* costs nothing, because each file's rows stay together.
  Interleaving *rows* turns about 45% of them from "the killer is already at the
  front" into "walk the selection to find it again".

  Replayed sequentially from a recorded report rather than timed: the real list
  is shared across lanes and `Learned.ahead` also filters by reachability, so
  the ratio is the claim and not the absolute figures.

  **What would justify re-opening it**: a `Learned` keyed per file rather than
  one list per run, which would hold a front for each and make the interleave
  free. That is a bigger change than the ordering it would enable, and nobody
  has needed it.

  The failure mode is why this is written down at all: nothing fails. Same
  verdicts, no counter on the hit rate, just a slower sweep — which reads as
  "mutation testing is slow", a conclusion already reached here once for a
  different reason.

- **Four cleverer mutation dispatches, all of them losing to the plain stride**
  (#49 follow-on). A `ThreadPoolExecutor` fed the whole table hands rows out
  first-come, so with N lanes each lane walks a *stride* of N and no lane ever
  gets two consecutive rows. `Learned` is move-to-front over adjacency, so that
  dispatch is structurally the round-robin ordering the entry above measures at
  a 27.3% hit rate. Four attempts to fix it, each built behind an environment
  switch **in one binary** so the arms could be attributed:

  | dispatch | `--only tupferl/` (1309 rows) | `tools/mutants.py` (dense) |
  |---|---|---|
  | stride — what `main` does | 357s | **236s** |
  | equal segments + steal the widest half | **318s** | 288s |
  | segments + suspect a survivor's next 4 | — | 242s |
  | segments + suspect a survivor's next 8 | — | 239s |
  | segments + suspect anything past 15s | 364s | 237s |

  Contiguity does exactly what it was predicted to do — replayed at `keep=8`
  over 2811 caught rows, 32 lanes: stride+shared front 37.2%, contiguous+per-lane
  **70.7%**, against a 72.9% sequential ideal — and winning that proxy was worth
  **11% at most, on one of the two tables, and −22% on the other.**

  The two tables disagree because they differ in the one thing that decides what
  a move-to-front hit is *worth*: `tools/mutants.py` has **1 distinct test
  selection** for every row, so the front has nothing to discriminate and a hit
  saves almost nothing; `tupferl/` has 11, one of them spanning 21 modules over
  133 rows, where a hit means running one test instead of walking all 21. **A
  benchmark table was chosen wrongly twice here, in opposite directions**, and
  each time the conclusion reversed. Any future claim about dispatch has to name
  its table's selection count.

  Three further things measured along the way, each worth more than the
  scheduling was:

  - **A survivor is ~71s against ~7.3s for a caught row**, so survivors are
    ~12% of a whole-tree table's rows and **~49% of its lane-seconds**. That is
    the tail, and no dispatch can fix it: by the time a row is *known* to be a
    survivor it has already cost its 71s, and under a stride every neighbour it
    might have warned has long since been claimed. Both suspect variants fired
    `0 early` on the table they were written for.
  - **An absolute "slow" threshold is the wrong shape.** At 15s it fired on
    ~22% of `tupferl/`'s rows — 287 flags for 30 real survivors — shredding
    contiguity for nothing and costing 46s. The measured move-to-front rate
    fell 63.9% → 55.0% while the tail was *identical*, which is how it was
    settled. A threshold that must self-calibrate wants a multiple of a running
    median, not a percentile: the *fraction* of dear rows differs between tables
    (2.3% and 11%) while the *ratio* dear-to-typical is ~10x on both.
  - **The handout is not the cost.** `Work.take` measured 209ns uncontended and
    265ns with 32 lanes on it — 0.85ms across a 3199-row sweep.

  **What would justify re-opening it**: nothing about the *scheduler*. Every one
  of the four was an attempt to infer during a run something a previous run
  already knew, so the thing to try instead is recording it — which is what
  `Killers.seconds` and `slowest_first` do, measured below. Re-open the
  scheduler only with a table whose selection count is stated and a mechanism
  that is not a within-run guess.

  Kept from the branch, because they were measured free (236s in the new binary
  against `main`'s 232s and 234s): streaming per-row output with a `[n/total]`
  counter and a lane tag, and the closing statistics block.
