# Converting tupferl to pytest — phased implementation plan

Status: **Phases 0, A, A2, B, C and D executed** (2026-08-30 to 2026-09-01) —
so the conversion is finished, and so is the extraction-readiness work that
followed it. **There is no Phase E: the plan is done.**
**Of 35 test modules, 1 still runs through pytest's `unittest` adapter**, and
it is not arrears: `tests/test_sync_properties.py` is converted but exposes a
class Hypothesis builds inside `hypothesis.stateful`, which the plan keeps as
the pytest-idiomatic spelling.

Both numbers are asserted by `tests/test_pytest_plan.py`, which asks the
`unittest` loader what it takes back from each module — so this line cannot
quietly go stale and "continue the plan" is a safe instruction.

**The module total fell by two and that is a deletion, not a conversion.**
Phase C removed `tests/test_verdict_unittest.py` and `tests/test_unassert.py`
with their subjects. Both numbers in the line above therefore dropped for a
reason that is not progress, which is exactly the reading a falling count
invites — said here because no test can tell the two apart.

**"Still run as `unittest`" is the number to state, and it took two tries to
find a predicate that means it.** "Converted" was the first, and the guard
written for it failed on its own first run: a module born pytest-native — such
as that guard — raises the native count with no conversion behind it. The second
asked `issubclass` of each module's attributes, filtered by `__module__`, and
cluster B2 then edited exactly that attribute: deleting
`test_sync_properties.py`'s dunder rewrite dropped the count by one for free. A
number a `__module__ = __name__` can lower is not a number of work done, so the
guard asks the loader instead — what pytest's own `unittest` adapter runs, and
what `tools/verdict_unittest.py` ran until Phase C deleted it.
The measured answers to the spikes are in
[Spike results](#spike-results--measured-2026-08-30), which corrects three
expectations this plan was written with. What each executed phase did
differently from what it says below is in
[Phase A as built](#phase-a-as-built--2026-08-30),
[Phase A2 as built](#phase-a2-as-built--2026-08-30),
[B1 as built](#b1-as-built--2026-08-30),
[B2 as built](#b2-as-built--2026-08-30),
[B3 as built](#b3-as-built--2026-08-31),
[B4a as built](#b4a-as-built--2026-08-31),
[B4b as built](#b4b-as-built--2026-08-31),
[B5 as built](#b5-as-built--2026-08-31) and
[B6 as built](#b6-as-built--2026-08-31) and
[Phase C as built](#phase-c-as-built--2026-09-01) and
[Phase D as built](#phase-d-as-built--2026-09-01). **Read every "as built"
section before the next phase** — said that way rather than as a count, because a count is one
more thing to hand-maintain per cluster and this one was already wrong once.

**A pytest-native test module is safe to write as of A2**, which was the whole
point of doing it before Phase B: `tools/run_tests.py` collects with pytest now,
so a plain `def test_...` is discovered, packed by its module, run, and counted
by the accounting check.

## Context for the executing agent

You are likely reading this in a fresh session with no memory of how it came to
be. The background you need:

tupferl's suite *was* stdlib `unittest` (1505 tests, 33 modules -- Phase 0
counted them; the estimate here was ~1557), a choice
[`docs/plan.md`](plan.md) §7.1 made because the mutation tooling in `tools/`
classifies unittest result objects. The maintainer has since decided to convert
the whole suite to pytest, **because the end goal is to open-source the
mutation framework**: pytest is where its audience is, and its pytest support
must be generic — respect a host project's own pytest configuration
(`python_files`, `testpaths`, conftest hierarchies), work with fixtures,
parametrize, markers and skips, and hardcode nothing about tupferl. tupferl is
the testbed; `martinus/woswoar` converts to the same framework next; extraction
into its own repository comes after that and is out of scope here (Phase D
prepares for it).

Decisions already made by the maintainer — do not relitigate them:

- **End state is pytest-only.** The unittest verdict layer is deleted at the
  end (Phase C). No dual-backend maintenance.
- **One phase = one PR**, merged before the next phase starts. Open the PR,
  report CI, stop — merging is the maintainer's call, per PR (CLAUDE.md §1).
- **Harness first, tests second.** pytest runs unittest-style `TestCase`s
  natively, so the verdict layer is rebuilt on pytest while every test stays
  unittest-style; only after that is a mixed suite safe. The reverse order
  loses tests silently: the current unittest loader loads an *empty* suite
  from a pytest-native module, so its tests vanish from every sweep — the
  flattering failure CLAUDE.md §8 is about.
- **The mutation sweep is the acceptance instrument.** Mutations target
  `tupferl/` and `tools/` source, never `tests/`, so a test conversion that
  weakens a test shows up as a newly-surviving mutant. Inline `# survivor:`
  comments in source are versioned dispositions and are untouched by test
  conversion; `sweeps/killers.json` is a gitignored machine-local cache and
  churning it is free.

## 0. Ground rules for every phase

These bind every PR in this plan. Read this section plus your own phase before
touching anything.

- **Read `CLAUDE.md` first**, in full. §0 (stale claims), §2 (test bar), §6
  (never discard uncommitted work), §8 (never trust an unexplained green),
  "Testing rules this project adds", and the "mutation harness" gotcha group
  are all load-bearing here.
- **Every PR leaves the tree green under the full preflight**:
  `ruff check . && ruff format --check . && mypy tupferl tests tools && python -m tools.run_tests`.
  The preflight *command* is deliberately kept stable through the whole
  conversion (see Phases A2 and C), so the `PREFLIGHT` tuples in
  `tests/test_ci.py` and `tests/test_release.py` never need touching unless a
  phase says so explicitly.
- **Never sed/mass-regex over the real tree** (CLAUDE.md §2, §6). Conversions
  are per-module, by hand, whole-module at a time — a half-converted module is
  the worst state, because a pytest-native test in a module the unittest
  loader still reads is silently dropped.
- **CLAUDE.md corrections land in the same PR that stales the claim** (§0).
  Each phase lists the claims it stales; also grep for more
  (`grep -n unittest CLAUDE.md`) before opening the PR, and say in the PR body
  which entries were corrected.
- **Standard failure protocol** (referenced as "FP" below): if a phase's
  acceptance gate fails, do not merge, and do not widen scope to force green.
  Reduce to the smallest failing case (one row / one module / one probe),
  record the measured evidence (commands, output, numbers) in the PR, and
  either (a) fix within scope and re-run the full gate, or (b) stop and report
  to the maintainer with the evidence, naming what the next session needs.
  Never fix a red gate by weakening the gate.
- **Never report a mutation row fixed from a hand-written spec.** The evidence
  is `python -m tools.mutate --all --only <file>` — the selection a sweep
  actually uses (CLAUDE.md §2).
- Sweeps are launched detached with `setsid`, watched with
  `python -m tools.watch <pid> --log … --done <json>.done` — never `nohup`,
  never `pgrep` (CLAUDE.md mutation-harness gotchas).
- Where this plan says "spike decides", Phase A names a primary design and a
  fallback; the spike picks between them. It does not invent a third without
  recording why.

## Phase 0 — Spikes: measured answers before design freezes

**Goal:** answer the questions the Phase A design depends on, with numbers, on
this machine, against a pinned pytest version.

**PR scope:** one PR updating this document with a "Spike results" section
containing the measured answers (number or observed behavior + pytest version
+ interpreter version, each naming primary-or-fallback for the design it
decides), plus `pytest` (and, if S3 says so, `pytest-subtests`) added to
`[project.optional-dependencies] test` in `pyproject.toml` with a floor that
is the version actually spiked (the repo's floors-are-real-versions stance —
and note `tests/test_packaging.py` asserts the dependency surface in both
directions; the *runtime* package is untouched, but check what it says about
dev extras before assuming). No harness or test changes. Spike scripts live in
scratch space, never committed; only their results are.

Pin one pytest version for all spikes: the latest stable that supports Python
3.10, verified on the 3.10 interpreter too — that CI leg exists precisely to
catch 3.10-only breaks.

### S0 — Does the whole existing suite pass under plain pytest?

*Question:* do all 1505 unittest-style tests pass, unmodified, under
`python -m pytest -q`? *Experiment:* run it; compare pass/fail/skip counts
against `python -m tools.run_tests` and
`python -m unittest discover -s . -t . -p 'test_*.py'`. Run again with
`-p no:cacheprovider` and confirm no `.pytest_cache` and no new `__pycache__`
contents appear (`git status --ignored` before/after). *Decides:* whether
Phase A can assume pytest's unittest integration handles this suite at all,
and which tests need pre-work. Expected suspects: anything reading
`sys.stdin` (pytest's captured stdin answers `isatty()` False — the safe
direction here, but verify), the pty tests, and
`tests/test_run_tests.py`/`test_mutate.py`, which spawn nested harnesses.
Record every delta with the test name and mechanism. This spike is the
precondition for everything else; if it fails broadly, stop and report (FP).

### S1 — Walk strategy: repeated in-process `pytest.main()` vs one subprocess per module group

*Question:* what does each walk step cost, and is repeated `pytest.main()` in
one process safe for this use? *Cost arithmetic to judge against:* ~88% of
rows are caught and pay only their selection (median caught row 0.33s); a
survivor walks ~33 modules and already costs ~71s; survivors are ~12% of rows
and ~49% of lane-seconds. A subprocess per walk group adds interpreter+pytest
startup per module (expect 0.3–0.5s × 33 ≈ 10–16s per survivor ≈ +7–11% of
sweep lane-seconds — acceptable if correctness demands it). Repeated
`pytest.main()` pays startup once and keeps `sys.modules` warm across calls —
but calling it repeatedly from one process is known-delicate (module caching,
plugin re-registration, assertion-rewrite hook stacking). *Experiment:* in one
`python -c` process in a sandbox copy, call
`pytest.main([...], plugins=[collector])` for one module, then for 33 modules
one call each; measure per-call wall time (first vs subsequent), check for
duplicate-plugin errors, a growing `sys.meta_path`, and verdict correctness on
a seeded failure in module 20. Compare against subprocess-per-group timing.
Also measure the *fixed* per-probe overhead of the first `pytest.main()` —
it lands on every caught row, so at ~1300 rows even 0.2s ≈ +260 lane-seconds;
get the real number with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 -p no:cacheprovider -q`.
*Decides:* primary = repeated in-process `pytest.main()` (matches today's
lazy-load architecture; a probe is a throwaway process, so cross-call
contamination is bounded to one row); fallback = subprocess-per-group if state
leaks produce a single wrong verdict in the experiment.

### S2 — The per-test alarm under pytest

*Question:* when a SIGALRM handler raises `Hung(BaseException)` during a
test's call phase under pytest, is it reported per-test (and classifiable via
`call.excinfo`), or does it abort the session? Does the run continue to the
next test? *Experiment:* a throwaway module with a `time.sleep(60)` test
followed by a passing test; a plugin that arms `signal.setitimer` in a
`pytest_runtest_protocol` hookwrapper (covering setup+call+teardown, as
`startTest`/`stopTest` do today) and inspects `pytest_runtest_makereport`'s
`call.excinfo`. Verify: which phase reports the failure, whether
`issubclass(call.excinfo.type, Hung)` is checkable there, whether the second
test runs, and that an `except Exception` inside the test does *not* swallow
it (the reason `Hung` is a `BaseException`). Also verify it is not reported as
a plain failure that Phase A's classifier would file as "noticed". *Decides:*
primary (per-test report, classified `broke` in `makereport`) vs fallback
(catch `Hung` around `pytest.main()`, attribute via the last
`pytest_runtest_logstart` nodeid the plugin recorded, classify the group
`broke` — verdict-equivalent, since a hang is `broke` either way).

### S3 — `subTest` under pytest's unittest integration

*Question:* does a failing `self.subTest(...)` make the owning test fail under
the pinned pytest, without any plugin? If not (the historical behavior:
silently passing — exactly the flattering failure this repo forbids, across
84 uses in 23 modules), does `pytest-subtests` restore it, and what do its
reports look like — owner-attributable nodeid? which `when` phase? does the
parent also emit a passed report? *Experiment:* one TestCase with a
two-iteration subTest loop failing on the second; run under plain pytest and
with pytest-subtests; inspect reports from a collecting plugin. *Decides:*
whether pytest-subtests is a Phase A test-dependency (expected yes; added in
Phase 0's PR, removed in Phase C after all subTest conversions land — or kept
if woswoar will want it; record the choice). Phase A's gate cannot pass if
subTest failures vanish: seed one deliberate subTest failure and confirm the
probe reports it `noticed` with the owning test's id.

### S4 — Sandbox hygiene

*Questions:* (i) does `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` plus an explicit `-p`
for Hypothesis's plugin work — Hypothesis complains when running under pytest
without its plugin, so verify the exact `-p` entry-point name; (ii) does the
`TUPFERL_HYPOTHESIS_PROFILE=mutation` profile still load (it comes via
`tests/profiles.py` import side effect, so it should be plugin-independent —
verify); (iii) does assertion rewriting write `.pyc` into the sandbox when
`python -B` + `PYTHONDONTWRITEBYTECODE=1` are set (the rewriter should skip
caching under `sys.dont_write_bytecode` — verify with
`find <sandbox> -name __pycache__` after a probe), and what does
`--assert=plain` cost/buy (import time saved per module — relevant to walk
laziness — vs. poorer `reasons` tracebacks); (iv) confirm
`-p no:cacheprovider` leaves no `.pytest_cache` in the sandbox. *Experiment:*
run a hand-built probe (the S1 harness) in a sandbox copy under each
combination; diff the sandbox tree before/after; time module import with and
without rewriting. *Decides:* the exact env + argv the Phase A probe uses.
Primary: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, explicit `-p` list,
`-p no:cacheprovider`, `-q`; rewriting on iff it writes nothing and costs
<10% of a one-module walk step, else `--assert=plain`.

### S5 — Collection cost

*Question:* what does pytest collection cost, whole-suite vs one module,
against unittest's measured 621ms-for-29-modules / 0–1ms-for-one?
*Experiment:* time `python -m pytest --collect-only -q` (whole suite, and one
module), and time a fileset enumeration that pays no imports at all: read
`python_files`/`testpaths` out of the project's pytest config and glob.
*Decides:* the walk-enumeration design (Phase A): whether the generic "every
other test module" list can come from a config-respecting glob (cheap,
primary) or needs a cached `--collect-only` pass (expensive; only if globbing
provably misses files the config would collect). Also decides whether
`tools/run_tests`'s parent can afford one full collect per run.

### S6 — `MemoryError` classification

*Question:* when `verdict.cap`'s rlimit makes an allocation fail mid-test,
does the resulting `MemoryError` arrive at `pytest_runtest_makereport` with
`call.excinfo.type` a `MemoryError` subclass (classifiable as `broke`, never
`noticed`), and does pytest's reporting survive memory pressure well enough to
keep running? *Experiment:* a test that allocates unboundedly, run in a probe
with a small cap; check the report; also the kill-test: confirm that when
reporting itself dies, the probe's outer `except BaseException` →
`{"loaded": false}` path still fires and mutate files the row `broke`.
*Decides:* whether the `_carrier` equivalent lives in `makereport` (primary)
or needs a belt at the logreport level too.

**Acceptance gate for Phase 0:** the "Spike results" section exists with a
measured answer for S0–S6; `pip install -e '.[dev]'` brings in the pinned
pytest; full preflight green (nothing else changed). **Size:** 1 PR, roughly
2–4 sessions of experiments, small diff. **Failure protocol:** FP; if S0
reveals broad incompatibility, the whole plan stops for maintainer review.

## Spike results — measured 2026-08-30

**Every S0-S6 question below is answered from a measurement on this machine, not
from documentation.** Where a spike contradicts the expectation the plan was
written with, the plan text above is left as written and the correction is
here — three of them do (S3, S5, and half of S4).

**Pinned for every spike:** pytest **9.1.1** (the newest stable; its
`Requires-Python >= 3.10` matches this project's own floor exactly),
pytest-subtests 0.15.0 (spiked, **not adopted** — see S3), hypothesis 6.165.10.

| | |
|---|---|
| primary interpreter | CPython **3.14.7**, Fedora, kernel 7.1.8 |
| machine | AMD Ryzen 9 7950X, 32 logical cores, 62 GiB (≈53 GiB available) |
| 3.10 leg | CPython **3.10.21** in a `python:3.10-slim` container under podman, `git` and `procps` installed, run as a non-root user |

Timings are medians of interleaved repetitions; the count is stated at each.

### S0 — The whole existing suite passes under plain pytest, unmodified

**Yes, on both interpreters, with no pre-work needed.** Not one test needed
touching, and every expected suspect (stdin, the pty tests, the nested
harnesses in `test_run_tests.py` / `test_mutate.py`) passed untouched.

| | tests | wall |
|---|---|---|
| `python -m tools.run_tests` | 1505, 0 failures / 0 errors / 0 skipped | 20.7 s |
| `python -m unittest discover -s . -t . -p 'test_*.py'` | 1505 | 89.0 s |
| `python -m pytest -q` | **1940 passed**, 0 failed, 0 skipped | 89.6 s |
| `python -m pytest --collect-only -q` | **1505 nodeids** | — |

**The 1940-vs-1505 delta is not extra tests, and explaining it was the whole
of this spike.** pytest collects exactly the same 1505 items; it emits 435
additional *reports*, one per `subTest` iteration. Accounted exactly by a
plugin counting reports by class:

```
call/passed/SubtestReport   435      <- 77 tests use subTest at runtime
call/passed/TestReport     1505
setup/passed/TestReport    1505
teardown/passed/TestReport 1505
                           1505 owners + 435 subtests = 1940
```

The plan's "~1557 tests" is superseded: the real number is **1505**.

**Tree hygiene:** `git status --ignored --short` is byte-identical before and
after a whole-suite run, and no `.pytest_cache` appears anywhere with
`-p no:cacheprovider`.

**stdin, measured against a real pty rather than assumed.** `sync` asks
`sys.stdin.isatty()` to decide whether anyone is there to answer a conflict, so
this one is load-bearing. Handing the child a real terminal with `pty.openpty()`:

| | `sys.stdin` | `isatty()` |
|---|---|---|
| pytest, default capture | `DontReadFromInput` | **False** |
| pytest, **`-s`** | `TextIOWrapper` | **True** |
| unittest (today) | `TextIOWrapper` | **True** |
| control: bare interpreter on that pty | `TextIOWrapper` | True |

pytest replaces stdin whether or not a terminal is there, which is the safe
direction and *safer than today* — but **`-s` undoes it exactly**, and a probe
that prompts on a developer's machine does not fail, it blocks. So the probe
must never pass `-s`. That is a measured rule, and it collides with the capture
finding under "One finding that belongs to no single spike" below: `-s` is
precisely what one reaches for when plugin output goes missing. Reach for a
pre-`dup`'d fd instead.

**3.10:** identical — 1505 nodeids, 1505 owner reports + 435 subtest reports =
1940, 0 failed, 0 collection errors, no cache. One skip
(`test_watch.TestWhetherAProcessIsThere::test_pid_one_is_alive_though_it_is_not_ours`),
which `python -m unittest discover` also skips in the same container:
environment, not framework.

**One false alarm, recorded because it is the shape §8 warns about.** The first
3.10 run showed *four* failures, all in `tests/test_mutate.py`
(`TestWhereThereIsAProc`, `TestWhatPsIsAskedFor`). They are not a pytest delta
and not a 3.10 delta: `ps` is absent from `python:3.10-slim`, and the same four
fail identically under `python -m unittest` and under `python -m tools.run_tests`
in that same container. Installing `procps` removed all four. **The check that
settled it was running the other framework in the same container** — comparing
against the 3.14 run would have "confirmed" a pytest regression that does not
exist.

### S1 — Walk strategy: repeated in-process `pytest.main()` wins

**Primary wins on both counts, speed and correctness.**

Fixed per-probe overhead — subprocess spawn included, since that is what a
probe costs `mutate`; median of 9 each:

| | median |
|---|---|
| bare interpreter | 8.0 ms |
| `import unittest` | 27.7 ms |
| `import pytest` | 75.8 ms (autoload on) / 74.1 ms (off) |
| **unittest: load + run one module** | **42.5 ms** |
| **`pytest.main` one module, autoload on** | **193.1 ms** |
| **`pytest.main` one module, autoload off** | **113.6 ms** |
| `pytest.main` one module, autoload off + `-p hypothesispytest` | 169.9 ms |

So a probe costs **+71 ms** over unittest, with autoload off (S4).

The walk, all 33 groups, interleaved A/B, two pairs:

| | run 1 | run 2 |
|---|---|---|
| in-process repeated `pytest.main()` | 80.98 s | 80.19 s |
| one subprocess per group | 85.42 s | 85.60 s |
| *(today)* in-process repeated unittest load+run | 78.43 s | 78.48 s |

Subprocess-per-group is **6.1% slower** (~150 ms per group) — well under the
plan's 10–16 s estimate, but it buys nothing, because in-process showed no
contamination at all. Against today's unittest walk, pytest is **+2.9%**.

**Estimated whole-sweep cost of the move: about +2%** — survivors are ~49% of
lane-seconds and pay +2.9%; caught rows are the rest and pay the +71 ms fixed
overhead once each.

Safety, which is what actually decides this:

- `sys.meta_path` is **3 before and 3 after** 33 consecutive `pytest.main()`
  calls. No duplicate-plugin errors, no assertion-rewrite hook stacking.
- Four unmutated 33-group walks (2 in-process, 2 subprocess) agree **exactly**,
  per module, on `(exit code, number noticed)`. No module differs.
- Three seeded source mutations — `paths.META` renamed, the first `return` in
  `copies.py` negated, the first `not` in `manifest.py` dropped — stop the walk
  at groups **7, 16 and 29** respectively, and **both arms agree exactly** on
  the stopping group, the module and the number noticed. The walk stops at the
  first notice, as designed.

The plan asked for the seed to be "in module 20"; where a seeded mutation is
first noticed is not something the seeder chooses, so this was run as three
seeds landing early, middle and late instead.

### S2 — The per-test alarm: reported per test, and classifiable

**Primary wins.** A `Hung(BaseException)` raised from a `SIGALRM` handler armed
by `signal.setitimer` inside a `pytest_runtest_protocol` hookwrapper:

- is reported **per test**, in the `call` phase;
- arrives at `pytest_runtest_makereport` with `call.excinfo.type` equal to
  `Hung`, so `issubclass(call.excinfo.type, Hung)` is checkable exactly where
  the plan wanted it;
- **does not abort the session** — with five tests and two 60-second sleeps,
  all five started and the two tests following the hangs ran and passed;
- is **not swallowed by `except Exception:` inside the test body** (which is
  the reason `Hung` derives from `BaseException`); the test that wraps its
  sleep in `except Exception` still reports `Hung`, not the `AssertionError`
  its handler would have raised;
- is distinguishable from a plain failure — a normal `assertEqual` failure in
  the same run reports `AssertionError`, `is_hung` false — so a hang is
  classified `broke` and never `noticed`.

At `EACH=1.0` the whole five-test run took **2.07 s**, which is the alarm
firing rather than the sleeps completing.

**Identical on 3.10.**

### S3 — subTest: pytest 9 does this natively, and pytest-subtests is not needed

**The plan expected the opposite, and the correction matters.** It anticipated
that a failing `self.subTest(...)` would silently pass under plain pytest and
that `pytest-subtests` would be a Phase A dependency. Measured:

| pytest | call-phase reports for a two-case subTest owner whose second case fails | run |
|---|---|---|
| 8.4.2 | one `TestReport`, outcome **failed** | exit 1 |
| **9.1.1** | `SubtestReport` passed, `SubtestReport` failed, `TestReport` **passed** | exit 1 |

Both versions fail the run, so the historical silent-pass is not the hazard
here. `SubtestReport` comes from `_pytest.subtests` — a module inside pytest's own
package, not the plugin — and is emitted with `-p no:subtests` in force.

Each `SubtestReport` carries the **owning test's nodeid** and a
`report.context` of `SubtestContext(msg=..., kwargs={...})`, so attribution to
the owner is direct and no parametrized carrier id is involved.

**The trap this creates is the one to write down.** On 9.1.1 the owning test's
*own* `TestReport` says `passed` even when one of its subTests failed. A
classifier that reads only `TestReport` objects — which is the obvious port of
today's `addSubTest` handling — would file a subTest kill as "nothing
noticed", and report a mutation SURVIVED that the suite actually caught. That
is the flattering direction. **Classify on `report.outcome == "failed"` across
every report object, not on the owner's report.**

**pytest-subtests 0.15.0 is not adopted.** Installed, it changes the report
stream for `TestCase.subTest` not at all — same objects, same classes, same
order — and changes only the summary wording (`1 failed, 2 passed, 1 subtests
passed` against `1 failed, 3 passed`). Revisit in Phase B only if a converted
pytest-native test wants the `subtests` *fixture*, which is a different
feature; Phase C's "remove it if unused" step is then moot.

**Identical on 3.10.**

### S4 — Sandbox hygiene, and the probe's exact env and argv

**Decided:** `python -B`, `PYTHONDONTWRITEBYTECODE=1`,
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`, `-p no:cacheprovider`, `-q`, assertion
rewriting **on**, and **no** `-p` for Hypothesis.

- **No `.pyc`, in any combination.** `-B` plus `PYTHONDONTWRITEBYTECODE=1`
  gives zero `.pyc` files in the sandbox even with assertion rewriting on: the
  rewriter honours `sys.dont_write_bytecode`.
- **No `.pytest_cache`** with `-p no:cacheprovider`; one is created without it.
- **Autoload off saves 79.5 ms per probe** (193.1 → 113.6 ms).
- **Hypothesis needs no `-p`, contrary to the plan's expectation.** With
  autoload off and no Hypothesis plugin: the `mutation` profile is still in
  force (`max_examples=20`, `derandomize=True`, `STATEFUL,STEPS = 3,4`),
  because it arrives through `tests/profiles.py`'s import side effect and not
  through the plugin; there is **no complaint or warning**; and a deliberately
  failing `@given` test still **fails**, in all three combinations. Adding
  `-p hypothesispytest` costs **56 ms per probe** — most of the autoload saving
  — and changed nothing measured. The entry-point name, for the record, is
  `hypothesispytest` (module `_hypothesis_pytestplugin`).
- **`--assert=plain` is not worth taking.** It saved 0.15 s and 0.80 s on ~80 s
  walks — below resolution — and produced a byte-identical `longrepr` for
  unittest-style assertions. Rewriting stays on, because Phase B's
  pytest-native `assert` statements need it and it costs nothing now.
- **`.hypothesis/` is written into the sandbox — by both frameworks.** Measured
  under a pytest probe and under today's unittest probe: both create it. It is
  Hypothesis's own behaviour and is gitignored. Not a regression; noted so a
  Phase A reader does not chase it.

**Genericity caveat, which the plan should carry into Phase D.**
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` is right for tupferl and *wrong* for a host
project whose suite needs an autoloaded plugin — it would silently change what
that project's tests do. It belongs with the `probe_plugins` knob as a setting
with a default a host can change, not as a constant.

**And one trap found the hard way:** pluggy validates hookimpl *argument
names*. A hook spelled `pytest_collectreport(self, r)` raises
`PluginValidationError` at plugin registration, naming the argument. The
parameter must be `report`.

### S5 — Collection cost: glob, by a factor of 123

| | median of 7, whole process |
|---|---|
| `pytest --collect-only -q`, whole suite | **500.8 ms** |
| `pytest --collect-only -q`, one module | 116.0 ms |
| `unittest discover`, whole suite (imports all 33) | 250.5 ms |
| `unittest loadTestsFromNames`, one module | 41.2 ms |
| **glob only, no imports** | **12.0 ms** (4.0 ms over a bare interpreter) |

**Primary wins: a config-respecting glob, not a cached `--collect-only` pass.**
The plan's condition for the fallback was "only if globbing provably misses
files the config would collect", and it does not: reading `rootdir`,
`python_files` and `testpaths` out of `config` and globbing names **the same 33
files** pytest collects — no misses, no additions. `rootdir` resolves to the
tree correctly with no `[tool.pytest.ini_options]` table present; the defaults
in force are `python_files = ["test_*.py", "*_test.py"]` and `testpaths = []`.

**`norecursedirs` is load-bearing and is easy to leave out.** A naive
`rglob` of those two patterns over the real tree finds **71** files — 38 of
them inside `.venv`, i.e. it would walk pytest's own test suite. Applying
`norecursedirs` gives exactly **33**. Phase A owes the enumeration a test that
it agrees with `--collect-only`, because a *missed* file turns a caught row
into a reported survivor, which is the flattering direction.

`tools/run_tests`'s parent can afford one full collect: 500.8 ms against a
20.7 s parallel suite is **2.4%**.

*Observation for Phase A, not acted on here:* `tools/verdict.py`'s docstring
argues laziness from "621 ms to import all 29 modules against 0–1 ms for one".
That number does not reproduce on this machine — `discover` over all 33 costs
250.5 ms. The *argument* is unchanged and in fact stronger under pytest
(500.8 ms against 116.0 ms), but the figure travelled from another machine and
should be re-measured rather than copied when that docstring is rewritten.

### S6 — MemoryError: classified per test, with the outer belt demonstrated

**Primary wins: the classification lives in `pytest_runtest_makereport`, and no
belt at the logreport level is needed.** Three regimes, driving a test that
allocates without bound under `RLIMIT_AS` with **both** halves lowered (as
`verdict.cap` has done since #74):

| ceiling | what happens |
|---|---|
| **≥ 320 MiB** | `MemoryError` arrives at `makereport`, phase `call`, `call.excinfo.type` a `MemoryError`, report `failed`. **The session continues** and the next test runs. Classifiable `broke`, never `noticed`. |
| **256 MiB** | pytest cannot reach collection. The probe's outer `except BaseException` fires with `MemoryError`, zero tests started → the `{"loaded": false}` path, which `mutate` files `broke`. **The belt is demonstrated, not assumed.** |
| **≤ 192 MiB** | the process dies before writing anything; `mutate` sees no report file, exactly as today. |

Address-space floor to run one trivial module at all: **unittest 242 MiB,
pytest 278 MiB** (binary search, both halves of the rlimit lowered). pytest
costs **+36 MiB**, which against `mutate._FLOOR`'s 2048 MiB is **1.8%** —
**`_FLOOR` needs no change**, and a real sweep's per-lane ceiling puts every
probe in the first regime.

### One finding that belongs to no single spike

**Anything a verdict plugin `print`s during `pytest.main()` is eaten by
pytest's own capture.** Measured on a run whose plugin printed once per
call-phase report: 3 of 8 lines survived with default `fd` capture, 8 of 8 with
`-s`, and 4 of 4 (the correct count) when written to a file descriptor
`os.dup`'d **before** `pytest.main()` was entered. The report JSON is written
to a file so it is unaffected — but a probe that diagnoses itself by printing
would lose most of its output, silently and partially, which is worse than
losing all of it. Diagnose from a file or a pre-duplicated fd.

This also explains a scare during S1: a hook that printed nothing looked like a
state leak between repeated `pytest.main()` calls, and was capture. **The two
look identical from outside**, which is worth knowing before Phase A debugs a
walk — and the obvious remedy, `-s`, is the one thing the probe may not do,
because it hands the suite a real stdin again (S0).

### What this changes in the plan above

| | the plan said | measured |
|---|---|---|
| suite size | ~1557 tests | **1505** (1940 pytest reports) |
| S3 | pytest-subtests expected to be a Phase A dependency | **not needed**; pytest 9 is native. The hazard is the *opposite* one: the owner's own report reads `passed` |
| S4 | Hypothesis "complains without its plugin"; explicit `-p` list | **no complaint**, profile loads anyway, `-p` costs 56 ms and buys nothing |
| S5 | glob primary "unless it provably misses files" | glob confirmed exact; but `norecursedirs` must be applied — a naive glob finds 71 files, not 33 |
| S1 | subprocess fallback would cost +7–11% of sweep lane-seconds | it costs **6.1% of a walk**, ≈2% of a sweep; in-process is safe, so it is not needed |
| Phase C step 2 | "remove pytest-subtests if Phase B eliminated all subtests uses" | moot — it was never added |

Nothing here changes a phase boundary or the order of the plan.

## Phase A — Rebuild the verdict layer on pytest (tests stay unittest-style)

**Goal:** the mutation harness's probe runs the suite under pytest instead of
unittest, with equivalent *verdicts* on a whole-tree sweep. All tests remain
unittest-style TestCases.

**PR scope** (one PR, the largest of the plan):

1. `git mv tools/verdict.py tools/verdict_unittest.py` and
   `git mv tests/test_verdict.py tests/test_verdict_unittest.py` (update
   imports/paths inside; the naming keeps `tools/mutants.py`'s `test_<stem>`
   convention resolving and keeps `test_mutants.TestChoosingTheTests` green —
   verify, don't assume).
2. New `tools/verdict.py` — the pytest verdict layer (design below).
   Standalone-by-read, like its predecessor.
3. New `tests/test_verdict.py` — ports the *claims* of the old test file
   against the new backend: broken-module classification, Hung classification,
   MemoryError classification, subTest attribution, cap arithmetic, walk
   semantics, report JSON shape. Written unittest-style (the suite still is).
4. `tools/mutate.py`: `_probe()` selects which verdict source to read via
   `TUPFERL_MUTATE_VERDICT` (`pytest` default; `unittest` kept for diagnosis
   until Phase C); `_run()` passes `first` as one JSON-encoded argv slot (see
   below) and adds the probe env/argv hygiene S4 decided — these go in
   `_run`'s env, not in the verdict source, so mutate owns the sandbox
   contract. `Killers`' cache validation must drop stale unittest-format ids
   gracefully (it already validates each id per run — extend to reject
   non-nodeid entries); `sweeps/killers.json` is disposable.
5. CLAUDE.md corrections this PR stales, at minimum: the `tests/` layout row
   ("stdlib `unittest`, not pytest — the mutation tooling classifies unittest
   result objects"), and the "discover vs loadTestsFromNames" gotcha gains a
   note that it now describes `verdict_unittest.py` only. The `run_tests.py`
   docstring's pytest-refusal paragraph is *not* yet stale (run_tests is still
   unittest here) — do not touch it in this PR.

### Pytest verdict layer design

**Execution model** (unchanged property): `mutate._run` reads
`tools/verdict.py` source from *its own* tree and hands it to
`python -B -c <source>` with `cwd=<sandbox>` — the sandbox's `tools/` copy is
never consulted, so a mutation cannot grade its own exam. `-c` still puts the
sandbox cwd on `sys.path`. The module imports `pytest` (from the venv, not
the sandbox — safe) and must import **nothing from `tools/`**. Do not switch
to an installed runner module: the read-source property is the isolation
guarantee and costs nothing to keep.

**argv protocol:** `report-path, failfast, memory-cap, each-test-seconds,
first (JSON list), walk ("1"/"0"), *names` — the same slots as today, with
`first` JSON-encoded because pytest nodeids of parametrized tests can contain
spaces, which the current space-joined slot would shred. This is a protocol
change inside one repo revision; `_run` and `verdict.main` always ship
together.

**One probe = one process, plugin object passed to
`pytest.main(plugins=[…])`** — never conftest injection: the sandbox's
conftest belongs to the project under test and must keep working untouched
(that is the genericity requirement). Per S1, the walk is repeated
`pytest.main()` calls in that one process (or subprocess-per-group if S1's
fallback won): the first call runs `first + selection`, then one call per
remaining module until something notices.

**Classification** — the noticed/broke line, drawn where exception objects
still exist (a `pytest_runtest_makereport` hookwrapper inspecting
`(item, call)`):

| pytest event | verdict bucket |
|---|---|
| call-phase failure (assertion or any `Exception` in the test body) | `noticed` — killers gets `item.nodeid` |
| subTest failure (via pytest-subtests reports, per S3) | `noticed`, attributed to the **owning** test's nodeid, never a parametrized carrier |
| setup-phase failure (fixture error; unittest `setUpClass`/`setUpModule` surface as pytest-generated fixtures) | `broke` |
| teardown-phase failure | match current semantics: an item's own `tearDown` error counts `noticed` (it arrives at `addError` with a real TestCase today); a class/module-scoped teardown counts `broke` (`_ErrorHolder` today). S0/S3 verify where each lands under pytest. |
| collection/import error (`pytest_collectreport` failed) | `broke`, first line of the error as the message; the group is not run |
| `call.excinfo.type` ⊆ `Hung` | `broke`, `"{nodeid} did not finish within {each:g}s"` |
| `call.excinfo.type` ⊆ `MemoryError` | `broke`, `"{nodeid} ran out of memory"` |
| skip / xfail | neither, as today: a skip is not an answer. A *strict xpass* is a failed report and will read `noticed`; there are zero xfails today; the semantic is documented in the module docstring. |

The Hung/MemoryError carrier check runs **before** the noticed
classification, exactly as `_carrier` does today — both raise inside a real
test and would otherwise be filed as a test noticing.

**Report JSON:** identical keys — `loaded, ran, noticed, killers, reasons,
times, broke` — so `mutate._run`'s consumer changes minimally. `noticed`
holds display strings (nodeids are fine; CLAUDE.md's own gotcha already says
only `killers` may be asserted on); `killers` holds nodeids, which round-trip:
they are what later runs' `first` and selection feed straight back to
`pytest.main`. `times` = per-nodeid sum of setup+call+teardown
`report.duration` (feeds `slowest_first`; the cache is disposable). `reasons`
= the first failing report's `longrepr` string. `failfast` = the plugin sets
`session.shouldstop` after the first notice (it needs that hook anyway for
the walk's "has anything noticed yet" check between calls).

**Alarm:** `each_test()` semantics preserved (0 when no `SIGALRM`; returns
what was actually armed — `support.bounded`'s floor mechanism reads
`TUPFERL_MUTATE_EACH_TEST` from the env `_run` sets, which is untouched).
Armed per-item in a `pytest_runtest_protocol` hookwrapper — `setitimer(each)`
on entry, `setitimer(0)` on exit; the cancel matters for the same
misattribution reason `stopTest`'s comment records. Primary/fallback per S2.

**Walk enumeration, generic:** the walk must respect the host project's
pytest config, not glob `test_*.py`. Design: the plugin's `pytest_configure`
(first call) captures `config.rootdir`, `config.getini("python_files")`,
`config.getini("testpaths")`. The `every_module` replacement enumerates
candidate *files*: for each directory containing a selection member (falling
back to `testpaths`, then rootdir), glob each `python_files` pattern; subtract
the selection; sort. Each walk group is one file path handed to the next
`pytest.main` call — file-level laziness, no imports paid until a group is
reached (the point of the 621ms-vs-1ms measurement; S5 confirms the pytest
equivalents). Conftest hierarchies come along free because pytest loads them
itself per invocation. Selection names arrive from `mutants.targets_for` as
dotted modules (`tests.test_x`); the verdict layer maps them to paths under
rootdir (`tests/test_x.py`) — the one translation point, parameterized in
Phase D.

**Semantics that must not drift** — each gets a test in the new
`tests/test_verdict.py`: baseline (`walk=False`) runs the selection only, one
group per name; empty selection + `walk=False` = whole suite (`pytest.main`
with no path args, rootdir collection); `first` runs ahead of the selection
but an empty selection stays "whole suite" (the "first in its own argument"
trap the current code documents); the walk stops at the first notice;
`mutate.run` still baselines `WHOLE_SUITE` for walking tables (unchanged;
that logic is in mutate). Port `_loose_evidence` and the `Verdict.why`
recording — the unbaselined-killer traceback printing exists because of a
failure mode visible only at full-sweep scale (see the shuffled-walk dead end
in CLAUDE.md); it must survive the rewrite.

Also write a small **"harness assumptions" test class** in the new
`tests/test_verdict.py`: one test per depended-on pytest behavior (subTest
reporting, `BaseException` handling, setup/call/teardown phase mapping,
rewrite-vs-`dont_write_bytecode`), so a future pytest upgrade goes red loudly
instead of flattering the sweep.

### Acceptance gate (the sweep-equivalence instrument)

On the PR branch, at a committed checkpoint, on an idle machine:

```sh
setsid env TUPFERL_MUTATE_VERDICT=unittest python -m tools.mutate --all --json sweeps/old.json > sweeps/old.log 2>&1 &
# watch to completion with tools.watch, then:
setsid env TUPFERL_MUTATE_VERDICT=pytest   python -m tools.mutate --all --json sweeps/new.json > sweeps/new.log 2>&1 &
```

**As built, the control was run on the tree before the change instead, and the
reason is worth keeping.** A killer id has a shape, `tests/test_mutate.py`
asserts it, and it must — a cache full of ids nothing can select is a wall of
`BROKE`, so something has to hold the format. Under
`TUPFERL_MUTATE_VERDICT=unittest` those assertions fail, the baseline is red,
and a red baseline reports every row as `caught`: the command above would have
produced a control that agreed with everything. The same question asked
correctly is "did rebuilding the layer change a verdict", and its control is a
whole-tree sweep of the parent commit, where the old classifier is the one the
tests expect. Rows are then compared on `(path, line, operator, label)`, which
is only meaningful for files the change did not touch — every `tupferl/**` file
and every `tools/**` file but `mutate.py` and the two verdict layers.

Compare per `(path, span/line, operator)`: the verdict outcome
(`caught`/`SURVIVED`/`BROKE`/`TIMEOUT`) must be identical, modulo killer
*names* (formats differ by design), `times`, `reasons` text, and written
explanations. **Known-flaky rows protocol:** any differing row is re-run 3×
under *both* backends (`python -m tools.mutate --all --only <file>`); a row
that flips within one backend (the documented `conflicts.py` caught/BROKE
flake; `sorted`-over-set probabilistic guards) is excluded with the evidence
attached. Anything else differing = gate failed. Evidence in the PR: both
summary blocks, the diff table (a throwaway comparison script may be written,
but is not committed), baseline-green confirmation for both — read the
baseline line, not the row colours (CLAUDE.md's red-baseline gotcha) — both
`TODO`-tag counts, and the seeded-subTest-failure check from S3 demonstrated
once against the new backend. Plus the full preflight.

**Size:** 1 large PR — new verdict ~500–800 lines, new tests ~1000+ lines,
mutate diff small; the sweep pair is hours of wall clock. **Failure
protocol:** FP; per-row disagreements are triaged row-by-row with single-row
reproductions *before* touching the classifier — a fix built on the wrong
mechanism is worse than none (§5). If the disagreement class is "pytest
cannot express X", stop and report; do not paper over with a verdict-side
special case.

## Phase A as built — 2026-08-30

What landed, where it differs from the section above, and the evidence. Read
this before A2: three of the decisions below are ones A2 inherits.

### The classification moved from a class name to a phase

`tools/verdict.py` is a pytest plugin handed to `pytest.main(plugins=[...])`,
one process per probe, one `pytest.main` call per walk group. The
`noticed`/`broke` line is drawn in a `pytest_runtest_makereport` hookwrapper,
where the exception object still exists:

| what died | phase | bucket |
|---|---|---|
| the test body, by assertion or by any exception | `call` | `noticed` |
| a case inside `self.subTest(...)` | `call` | `noticed`, against the **owner** |
| the instance's own `tearDown` | `call` | `noticed` |
| `setUpClass` / `setUpModule` | `setup` | `broke` |
| `tearDownClass` / `tearDownModule` | `teardown` | `broke` |
| a module that will not import | no phase; `pytest_collectreport` | `broke` |

That table is the whole of what replaced an `isinstance` against
`unittest.suite._ErrorHolder`, and it is a *measurement* rather than a
documented guarantee — so `tests/test_verdict.py`'s
`TestWhatThisAssumesOfPytest` asserts every row of it against pytest directly,
with a plugin that records what it is handed. A pytest that moved one would
otherwise turn `broke` silently into `caught`.

### Classifying at `makereport` rather than at `logreport` is the load-bearing choice

S3 warned that on pytest 9 the owning test's own report reads `passed` when one
of its `subTest` cases failed. At `makereport` there is no such split: the
failure arrives once, carrying `item.nodeid` — the owner — so attribution is
free and there is no carrier to unwrap. Measured, and asserted both ways in
`test_a_failed_subtest_reaches_makereport_but_not_its_owners_report`.

### Four things the section above did not anticipate

- **A usage error fires no hook at all.** A stale `first` naming a renamed
  test, or a module the selection names and the tree does not, makes pytest
  refuse the whole invocation and say so on *stdout*, which `mutate._run` sends
  to `DEVNULL`. Without an exit-status arm the report would read "nothing ran"
  and the row would be filed as holding no tests. `Watcher.over` reads the
  status and records a `broke` when no hook did; `SystemExit` at module scope
  reaches it too, as `INTERNAL_ERROR`.
- **`NO_TESTS_COLLECTED` is not a failure.** The walk steps over an empty
  module all the time; read as an error it would end the walk at the first one.
- **A killer id is a nodeid, and a selection is still dotted.** They meet in
  three reachability filters, and a nodeid compared raw matches nothing —
  turning off every ordering mechanism at once and failing nothing.
  `mutate._reaches` is the single reconciliation point.
- **`mutate._loadable` asks pytest what it collects**, once per run in a
  subprocess, and keeps the intersection. That is also what drops a
  `sweeps/killers.json` written by the other backend — 601 such ids on the
  first run here.

### What was deliberately not done

- **`Mutation.first`, `Killers.known` and `Learned.recent` still hold ids
  space-joined into one string.** `_run` JSON-encodes the argv slot, which is
  the half that this phase's rewrite owns; the other half has no trigger until
  a parametrized nodeid exists, which is Phase B's first cluster. Phase B's
  contract now has it as step 1a.

  **Closed since, by that step** — see
  [Step 1a as built](#step-1a-as-built--2026-08-30), which also corrects the
  list: only `Mutation.first` was ever space-joined, and the field this
  paragraph does not name — `baseline_shards`' extra shard — was the one where
  it would have been silent.
- **The walk is not recursive, so `norecursedirs` is not consulted.** It looks
  in the directories the selection's own files live in, which is what the
  `unittest` layer did and what the sweep pair compares against. Anyone
  widening it owes that filter in the same change: a naive `rglob` of the
  default patterns over this tree finds 71 files, 38 of them inside `.venv`.
- **`pytest-subtests` is still absent**, as S3 concluded.

### Evidence

Two whole-tree sweeps, on an idle machine, compared on
`(path, line, operator, label)`. The control is the parent commit `b95db93`,
for the reason the acceptance-gate section above now gives.

| | control (`b95db93`) | branch |
|---|---|---|
| rows | 3410 | 3599 |
| wall | 1895 s | 4479 s |
| `caught` | 3047 | 3177 |
| `SURVIVED` | 339 | 385 |
| `BROKE` | 9 | 21 |
| `TIMEOUT` | 15 | 16 |
| baseline | green | green |

Both read from the baseline *line*, not the row colours.

**The wall-clock column is not an A/B and must not be quoted as one.** The two
runs differ in table size (3410 against 3599 rows), in row ordering (the control
had no `killers.json` at all, so no `slowest_first`; the branch had the recorded
costs but no prefix and no exact killers, since every cached id was dropped), and
in machine load — a browser, Steam, and an OOM event that took seven lanes.
CLAUDE.md §5 asks for interleaved runs and this was two sequential ones. So the
+136% here is **not** evidence that pytest is 2.4× slower, and Phase 0's S1
estimate of about +2% is not refuted by it: the two have not been compared under
conditions that would let them disagree. **A2 must not inherit this as a
measurement.** Answering it honestly needs an interleaved pair on one tree with
one cache state, which is a day of wall clock and is not the question this gate
was run to ask.

**Comparable rows: 2246.** `tupferl/**` is **1298 rows with 0 differences** —
the package the harness exists to measure is graded identically by both
backends. `tools/**` is 948 rows with 13 differences, every one of them
accounted for:

- **4 were a real defect this change exposed** — the collection-order finding,
  now fixed and confirmed `caught` under `python -m tools.mutate --all --only
  tools/mutants.py`. See the CLAUDE.md gotcha; the cause is the suite's, not
  the verdict layer's.
- **6 are improvements**: `survived → caught` ×3 (`mutants.py:640`,
  `mutants.py:1621`, `watch.py:446`) and `broke → caught` ×3
  (`mutants.py:1475`, `mutants.py:1479`, `watch.py:178`) — two of them rows the
  control could not answer at all.
- **1 is dispositioned in the tree already**: `mutants.py:1438`'s `sorted` →
  `list` carries a `# survivor: order` tag, written before this PR, naming
  `rglob`'s filesystem-defined order as the reason. The two sweeps ran in
  different directories, so the control caught it by luck.
- **2 are the same flaky line**, `mutants.py:376`, and it flips *within* the
  backend — which is the known-flaky protocol's own definition. Six post-fix
  runs:

  | row | caught | broke |
  |---|---|---|
  | `+=` → `-=` | 5 | 1 |
  | `1` → `0` | 2 | 1 |
  | `1` → `2` | 3 | 0 |

  The mechanism is a runaway allocation racing the memory cap, and which test
  module the walk reaches first decides it. It is not new:
  `TestLineEndingsThatAreNotNewline`'s docstring already records "four rows came
  back `BROKE` that way on the whole-tree sweep — two of them 'ran out of
  memory'" under the old runner. The class bound added here improves it (5 of 6
  where the control had 1 of 1) rather than fixing it.

**And CI caught one thing the sweep could not.** The memory-cap class was
written with two constants calibrated against this machine's measured 278 MiB
pytest floor, and four legs went red on the first push: a runner's interpreter
and site-packages are leaner, so a cap this machine cannot start under is one
the runner starts fine under, and an allocation this machine refuses is one it
grants. `tests/test_verdict.py` measures the floor now (`VmPeak` of one real
pytest run, 267 MiB here) and derives its cap from it, and the test aimed at
`main`'s outer belt is **gone** rather than re-tuned -- the only honest trigger
for it is a cap inside a band that is a property of one interpreter's address
space. Its portable half, "a report always exists and says whether it loaded",
is asserted instead. Same shape as `mutate._FLOOR`'s recorded mistake, in a new
place.

**One caveat on the branch sweep's own summary.** Seven of its 21 `BROKE` rows
are void: the host OOM killer took seven consecutive lanes at 09:53, in the same
event that killed the desktop session, and each reports *"the probe was killed by
SIGKILL"*. All seven are in `tools/mutate.py`, which this PR changes, so none
entered the compared set — checked, not assumed. Both sweeps also closed with
`heaviest lane process held … 100% of its ceiling`, the control included, so
that is not new to pytest. It is the first evidence bearing on `_COMMIT`'s
"150% is calibrated against '126% has never been killed' and no more", and it is
recorded here as an observation rather than a proposal: the sum-of-lane-RSS
sampler CLAUDE.md asks for first still does not exist, and the machine was also
running a browser and Steam.

**What keeping the retired backend costs, measured, so Phase C knows what it
buys back.** `tools/verdict_unittest.py` generates **155 mutation rows** of its
own, and `tests/test_verdict_unittest.py` takes the verdict layer's share of a
whole-suite walk from 3.18 s to 12.97 s — which every survivor pays by
construction. Not a reason to remove it early; a reason to remove it on time.

## Phase A2 — Port `tools/run_tests.py` to drive pytest

**Goal:** the sharded parallel runner and its accounting check
(`ids discovered == ids reported`) drive pytest, keeping the exact CLI
(`--jobs/--no-skips/--only/--exclude/--shard I/N/--worker/--out`) and module
path, so the `PREFLIGHT` tuples, ci.yml, release.yml and CLAUDE.md §7 all stay
true unmodified. This must land **before any test module converts**: a
pytest-native module under unittest discovery loads as an empty suite and
vanishes from the count, and the accounting check cannot see it — both sides
come from the same discover.

**Design:**

- **Discovery:** the parent runs one in-process collect
  (`pytest.main(["--collect-only", "-q", …], plugins=[collector])`) capturing
  nodeids; cost per S5. Unimportable modules: collection errors map to
  today's `unloadable` dict (name → first error line), same reporting, same
  red exit.
- **Packing key:** *scope unit* = `file::Class` for class-bound items, `file`
  for plain functions — preserves today's class-keyed largest-first packing
  (the two real `setUpClass` uses keep their fixture sharing) and answers
  "what happens when tests become functions" with module-keyed packing
  automatically. Record in the docstring, for Phase B: class-scoped fixtures
  replace `setUpClass` cost-sharing; module-scoped fixtures may run once per
  batch that touches the module, so they must be cheap or idempotent (the
  `lru_cache`'d template already is).
- **Workers:** unchanged shape —
  `python -m tools.run_tests --worker <nodeids…> --out <json>`; the worker
  calls `pytest.main([*nodeids, "-p", "no:cacheprovider", …],
  plugins=[recorder])` and writes the same five-key JSON
  (`ran, failures, errors, skipped, unloadable`), where `errors` =
  setup/teardown-phase failures and collection errors of named items,
  `skipped` = `[nodeid, reason]` pairs. Parent accounting, dead-batch
  synthesis, `--no-skips`, and duplicate detection: unchanged logic, over
  nodeids.
- `--only`/`--exclude` keep their dotted-module/class spellings (translated
  to nodeid prefixes) so ci.yml's four macos `--exclude` values keep working
  verbatim — check them against the translation, by name.
- **xfail semantics** (zero uses today, decided here so the first future use
  doesn't decide it silently): xfail counts as neither pass nor skip;
  strict xpass is a failure. Say so in the docstring.
- **Docstring rewrite:** the module's docstring currently argues *against*
  pytest in its last paragraph — rewrite it in this PR (§0), keeping the
  accounting argument, which is the part that survives.

**Acceptance gate:** `python -m tools.run_tests` green with a total equal to
Phase A's count (state both numbers in the PR); deliberately break one
module's import locally and show the unloadable path plus red exit;
deliberately kill one worker (temporary crash, not committed) and show "batch
died without reporting" + "N discovered tests never ran"; `--no-skips`,
`--only tests.test_sync`, `--exclude`, and `--shard 1/2` each demonstrated;
`CI=true TUPFERL_HYPOTHESIS_PROFILE=ci python -m tools.run_tests` green; full
preflight green; and one mutation check over the runner itself:
`python -m tools.mutate --all --only tools/run_tests.py` — zero
newly-surviving/newly-BROKE rows against Phase A's sweep for that file
(`tests/test_run_tests.py` is *not* converted yet; it must still kill them).
**Size:** 1 PR, ~400–600 line diff. **Failure protocol:** FP.

## Phase A2 as built — 2026-08-30

The design above survived in every part a reader would check: one in-process
`--collect-only` in the parent, `file::Class` scopes from `item.parent`, the
same worker command line, the same five-key JSON, dotted `--only`/`--exclude`,
and the docstring rewritten.

**One of those went wrong before it was put right, and the correction is worth
more than the design note.** A2 as merged handed each worker its scope *names*
rather than the nodeids the design specified, as an argv saving. A scope name is
a packing key and not a selector: `tests/x.py::TestY` selects exactly its own
tests, but `tests/x.py` -- the scope a test outside any class packs under --
selects the whole file, classes included. A module with a bare function beside a
class therefore ran that class twice. See "The dispatch that was not a
selection" below; the design's `<nodeids…>` was right and the optimisation was
not.

Two further details came out differently from the letter of it, both
deliberately:

- **the translation goes the other way.** The design said `--only`/`--exclude`
  would be "translated to nodeid prefixes"; the build translates each *scope*
  into the dotted spelling instead. A dotted pattern cannot become a nodeid
  prefix without already knowing whether `tests.test_x` names a directory, a
  module or a class, so that direction is the lossy one — and translating the
  scope is what lets `selects` stay exactly as it was, anchored at a dot, with
  the property `test_only_is_anchored_at_a_dot` unchanged.
- **collection errors of *named* items stay in `unloadable`**, where the design
  put them in `errors`. `errors` is a list of test ids the parent prints under
  an `ERROR:` label; a module name in it would be counted as a test that failed,
  which is the distinction `unloadable` was added to keep. `run_batch` says so
  where it writes the key.

What follows is what the section did not anticipate, and the numbers.

### The packing key was free, and that decided a design question

`item.parent.nodeid` **is** the scope unit -- `tests/test_sync.py::TestX` for a
class-bound test, `tests/test_sync.py` for a plain function -- so nothing here
parses a nodeid to find it. That matters beyond tidiness: a string cut would
have to know that a parametrized id ends in `[...]`, which is exactly the
knowledge Phase B would have invalidated.

### Four things the section above did not anticipate

- **A failed `subTest` reaches `pytest_runtest_logreport` as a `SubtestReport`
  and the owning test's own `call` report reads `passed`** -- the pytest 9
  behaviour pyproject.toml's floor is there for, now met a second time in a
  second layer. The obvious way to avoid double-counting a subcase is to drop
  every report carrying a `context`, and that would have made a batch whose
  only failure is a subcase report itself **green**: no id in `failures`,
  `run_batch` exiting 0, and pytest's own exit status the only dissent. The
  failed arm therefore does not filter on `context` at all, and `_settled`
  deduplicates instead -- both reports carry the owner's nodeid.
  `test_a_failing_subtest_is_reported_once_and_not_lost` drives two failing
  subcases so that the deduplication is exercised rather than assumed.
- **A batch naming a scope inside a module that will not import exits
  `USAGE_ERROR`, not `INTERRUPTED`.** pytest reports the import failure through
  `pytest_collectreport` and *then* says "found no collectors for
  tests/test_x.py::TestY". The first draft of `_unexplained` accepted
  `INTERRUPTED` alone as explained-by-`unloadable`, so that batch threw its
  report away as unbelievable. The rule is now "anything already in
  `unloadable` explains any status", which is wider and is the honest reading:
  the entry is what makes the batch red either way.
- **`errors` changed meaning, and the new one is better.** `unittest` split
  failures from errors by exception type -- `AssertionError` against everything
  else -- so a test raising `RuntimeError` in its own body was an "error". The
  split is by *phase* now: `call` is the test saying no, `setup`/`teardown` is
  the fixture around it, which is what decides whether a reader looks at the
  test or at what set it up. The same change fixes an accounting wart: a class
  whose `setUpClass` raises used to produce one synthetic
  `setUpClass (module.Class)` id while every test under it surfaced as "never
  ran" under a name nobody could run. pytest starts each test and files a
  `setup` error against its real nodeid.
- **Everything pytest prints is moved to stderr**, which is where a
  `TextTestRunner` put it and where the parent's contract already assumed it
  was. `contextlib.redirect_stdout(sys.stderr)` around `pytest.main` is enough
  -- pytest builds its terminal writer from `sys.stdout` inside the call --
  measured at 0 bytes on stdout against 1980 on stderr, and it leaves
  `run_batch` callable in-process without wrecking its caller's streams.
  `os.dup2(2, 1)` would work too and cannot be done in-process at all.

### What was deliberately not done

- **Plugin autoload is not disabled**, which is the opposite of what
  `tools/mutate.py` does to its probes and is deliberate: a sweep has to be
  reproducible across machines, and this is the developer's own suite runner.
  Nothing a plugin can do escapes the accounting check, because discovery runs
  under the same plugins the batches do.
- **`run_tests.dotted` and `mutate._dotted` are two spellings of one
  translation**, kept apart. They differ only in `mutants.module_of`, which
  additionally collapses a package's `__init__` -- a case that cannot arise
  from a scope, since `__init__.py` matches none of pytest's `python_files`
  patterns. Sharing would mean importing `mutants` into every worker for one
  line. Both docstrings name the other.
- **`docs/plan.md` §7.1 still says "Framework: stdlib `unittest`, not
  pytest".** That is Phase C's line to correct, together with the rest of the
  documentation settling; it is named here so the next session does not have to
  rediscover it.

### What `/simplify` found, after the PR was open and CI was green

Four review agents over the diff. One real defect, several cleanups, and two
issues filed rather than fixed here.

**The defect: a broken module excused *any* collect failure, and the run went
green.** `_unexplained` treated anything in `unloadable` as accounting for any
exit status — widened deliberately, because a batch naming a scope *inside* an
unimportable module exits `USAGE_ERROR` rather than `INTERRUPTED`. Too wide for
discovery, which names no scopes. Measured with the guard reverted, on a tree
holding a broken module and a `conftest.py` hook that raises once the collection
passes three items:

```
=== with the fix: exit 1
    ::error::pytest exited INTERNAL_ERROR (3) while collecting /tmp/hole-...
=== reverted:     exit 0
    Ran 2 tests in 1 batches on 64 workers | OK (0 failures, 0 errors, 0 skipped)
```

A green run over a collect that raised — the failure this file exists to refuse,
one level up from where it refuses it. The set of excused statuses is now chosen
by the caller (`BY_A_BROKEN_MODULE` for discovery, `BY_ONE_A_BATCH_NAMED` for a
batch), and `test_a_broken_module_does_not_excuse_a_collect_that_blew_up` holds
it. **The `> 3` in that fixture is the test, not a detail**: it makes the parent
and the batch see different collections, so the batch is honestly green and
nothing downstream can notice. A hook that raised unconditionally takes the
worker down too, and the accounting check then catches it for the wrong reason —
which reads exactly like the right one, and is how this stayed invisible.

Applied besides: `pack` returns no batches for no scopes rather than one empty
one its only caller had to undo; the `dotted` side-table went, since it was
derived state built before the filters that shrink it; the two plugins'
identical `pytest_collectreport` bodies became one `_note_unloadable`, because
that key is the wire protocol and was written twice; `Batch` in the tests keeps
`CompletedProcess`'s own field names instead of renaming two streams; and
`tests/test_run_tests.py`'s throwaway trees now run with
`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` — they contain two hand-written `TestCase`s
and no plugin can matter to them, measured 0.21 s → 0.12 s per process across
33 call sites, and that module's own runtime fell from ~13 s to ~9 s.

**A duplication that was accepted at a cost nobody was paying.** `_stated` is
four lines in both `tools/verdict.py` and `tools/run_tests.py`, deliberately not
shared because `verdict.py` is read as source text into a sandbox. Only the
`run_tests` copy had a test. `tests/test_verdict.py` now runs the same table
against both and asserts they agree, so a divergence is red rather than a
discovery. `_named` is the same pair; its docstring now says the two render
differently on purpose and which audience each serves, because
`tools/cpus.py` already records what an unremarked copy of one number costs.

Not fixed here, filed instead: `--exclude` cannot name a module that will not
import and refuses with "matches nothing" (#84, predates the port), and
`tests/support.py`'s module-scope hypothesis import costs 20 of 35 modules
~60 ms each, about 7 s of CPU per run (#85, and the efficiency pass ranked it
the cheapest way to buy back this PR's +5%).

Skipped with reasons: dropping the `int(pytest.ExitCode.…)` casts (they match
`verdict.ANSWERED`, and consistency across the three copies is worth more than
four characters); memoising the tests' worker subprocesses (the one-claim-per-
test granularity is deliberate); re-keying `unloadable` by nodeid (its keys are
what a person types after `--only` and what "could not import …" prints);
`--jobs 32` against `--jobs 64` now that a batch start costs 0.20 s rather than
0.10 s (a measurement to run, not a change to make — `--jobs` already exposes
it); and narrowing the parent's collect under `--only` (0.42 s → 0.20 s, which
matters only to the interactive loop).

### The dispatch that was not a selection — 2026-08-30, after A2 merged

**A2's headline claim had no test behind it.** "A pytest-native module is safe
to write now" was asserted by `dotted`'s unit tests and demonstrated by nothing:
every fixture in `tests/test_run_tests.py` is a `unittest.TestCase`, so no batch
was ever handed a *module* scope. Driving one by hand, immediately before
Phase B:

| module shape | result |
|---|---|
| bare functions + `parametrize`, no class | green |
| classes only (the suite as it stands) | green |
| **a bare function *and* a class in one file** | **red — `::error::1 tests ran more than once`** |

The cause is above: a scope name is a packing key, not a selector. `main` hands
each batch the *ids* of its scopes now, which partition by construction, and the
module docstring states that distinction where the dispatch happens.

Three things worth keeping from it:

- **It failed loudly.** The duplicate check at the bottom of `main` caught it and
  turned the run red. Its `# survivor:` tag said the branch was "unreachable
  through `pack` … there for a future batching rule that overlapped" — true of an
  all-classes suite, and wrong. The tag now records that it was a *past* rule,
  and that this guard is the only thing that ever saw the bug.
- **The claim is driven now, not asserted.** `TestAPytestNativeModule` runs a
  file holding a bare function, a `parametrize`, and a plain non-`TestCase`
  class, and checks all five tests run once. Two of its three tests fail with
  the dispatch reverted; the third guards a different thing — a parametrized id
  can contain a space, which is why `--worker` takes a list and nothing joins
  these into a string.
- **An in-process `discover` over a *throwaway* tree does not work**, which is
  the tempting way to assert the partition directly. Those trees also call their
  package `tests`, and the process running the test imported the real one long
  ago, so the collect resolves `tests.test_x` against the original and reports
  `ModuleNotFoundError`. `discover(root=...)` is a seam for pointing at a real
  tree, not at a second copy of this one. Both docstrings say so.

### Evidence

| | HEAD (`23fb988`, `unittest`) | this branch (pytest) |
|---|---|---|
| tests run | 1589 | **1607** |
| result | 0 failures, 0 errors, 0 skipped | 0 failures, 0 errors, 0 skipped |
| scopes packed | 339 classes | 339 scopes |

The +18 is exactly this PR's own test arithmetic and nothing else: three tests
of the `unittest` loader's two spellings of an import failure removed, and
twenty-one added -- four on `_stated` and `_why`, three more running the same
`_stated` table against `verdict.py`'s copy, four on `dotted`, one on ci.yml's
`--exclude` values, one on the collect traceback reaching stderr without the
node listing, two on a collect that cannot be believed (one of them the
false-green above), and six on the worker (its stdout, a dead fixture's ids, a
failing subtest, a status nobody can believe, and both halves of the xfail
rule).

**Wall clock costs about 5%, and this one *is* an A/B** -- same machine, same
interpreter, two `git worktree` trees differing only in the commit, runs
interleaved A B A B A B, which is what CLAUDE.md §5 asks for:

| pair | HEAD | branch | |
|---|---|---|---|
| 1 | 28.58 s | 29.44 s | +0.86 s |
| 2 | 28.68 s | 30.11 s | +1.43 s |
| 3 | 28.29 s | 30.26 s | +1.97 s |

Median paired difference **+1.43 s on ~28.5 s, about +5%**, and every pair is
positive, so it is a real cost rather than noise. Three pairs is not many and
the differences drift upwards across them while HEAD stays flat, so treat +5%
as the shape and not as two significant figures.

**The mechanism is not established.** Two terms are known to exist and neither
has been measured on its own: the parent now pays one whole-tree collect
(Phase 0's S5 measured that at 500 ms, which is a third of the difference), and
each of 128 batches pays pytest's startup instead of `unittest`'s. Both are
per-run constants rather than per-test, so the share should fall as the suite
grows. Nothing here was optimised.

**One paragraph that used to stand here was wrong twice over, and it is left in
corrected rather than deleted.** It said that handing each worker its ids rather
than its scopes was not taken because "it would put a nodeid per test on a
command line that today carries one per scope, and `ARG_MAX` is a limit
CLAUDE.md already records being got wrong twice". Wrong about the cost: the
argv is *per batch*, and 1607 ids over 128 batches is about 13 each, ~1.2 KB for
the largest single scope in this tree, against a 2 MiB bound. And wrong about
the trade, because it was never a speed question at all -- passing scope names
was a correctness bug, and the ids are what the runner passes now. Invoking a
recorded hazard is not the same as measuring one.

Phase 0's 20.7 s figure for the `unittest` runner does not reproduce here at
all -- the same binary measures ~28.5 s on this machine today -- which is the
reason the interleave was run rather than the earlier number quoted. Comparing
against it would have reported a 45% regression that does not exist.

**The mutation gate, `python -m tools.mutate --all --only tools/run_tests.py`**,
against Phase A's whole-tree sweep as the control for that file. Both baselines
green, read from the baseline *line*:

| | control (Phase A) | this branch |
|---|---|---|
| rows | 172 | 211 |
| `caught` | 159 | **200** |
| `SURVIVED` | 12 | 10 |
| `BROKE` | 1 | 1 |
| unread | 12 survived + 1 broke, all tagged | **0** |

Every non-`caught` row carries a written reason beside the code and **none says
`TODO`**. The single `BROKE` is `if args.worker:` -- forcing that branch off
makes every worker run the whole suite and spawn more, and its tag has said so
since before this PR.

**And CI found a Phase A test that could not fail.** The `macos` leg went red
on `test_verdict.py::TestWhatTheBaselineNeeds::test_a_subtest_is_not_charged_to_its_owner_twice`,
which compared a wall clock against `SLEPT * 2` -- *exactly* the value the
double-counting it named would produce, so no margin at all -- and three 0.067 s
sleeps took 0.419 s on a loaded runner. That leg's own wall clock varies 128 s
to 230 s across four consecutive green runs of `main`, and this branch's was
211 s, inside that spread; the threshold was never going to hold there.

The deeper half is that it could not have failed for the reason it gave.
Measured on pytest 9.1.1, a `SubtestReport`'s `duration` is **0** -- three
subcases sleeping 0.067 s each report 0, 0, 0 against the owner's `call` report
of 0.2017 -- so removing `verdict.Watcher`'s `context` filter changes that
number by nothing, and **no fixture can make it change**. The filter's docstring
claimed the subcase duration was "already inside the owning test's `call`
report", which is true in principle and not why the filter matters. Corrected in
place; the guard is kept against a pytest that starts timing subcases, and both
the docstring and the test now say it is untestable and that removing it is
Phase C's call. Same family as CLAUDE.md's `merge.conflictStyle` entry: a guard
written for a future that has not arrived cannot be tested.

**Five sweeps, and every one after the first was earned.** The first reported 13
unread survivors; ten of them were killable rather than equivalent, and killing
them is where six of this PR's new tests come from -- `_why` driven directly
(three rows), a collect that cannot be believed (five rows), and `_stated`'s
`else` arm. The last sweep's one remaining survivor was the *same fixture
weakness one subscript along*: a one-line rendering cannot tell `spoken[-1]`
from `spoken[0]`, and the two-line rendering written to fix that cannot tell it
from `spoken[1]`. Three lines is the first length at which no other index gives
the answer. CLAUDE.md §2's symmetric-fixture entry, met twice in a row in the
same four lines of code. The fourth confirmed the `test_verdict.py` correction
above, and the fifth this section's own cleanups.

## Phase B — Convert the 33 test modules, in clusters

**Goal:** every test module becomes pytest-native. One cluster per PR. A
module converts *whole*; support machinery converts to `tests/conftest.py`
fixtures in the first cluster that needs it, with the unittest base classes
kept alive in `support.py` until their last user converts (then deleted in
that same PR).

**Per-PR conversion contract (every cluster):**

1. Before editing: record the module's collected item count and test list
   (`python -m pytest --collect-only -q tests/test_X.py`).
1a. **Done, as its own PR, before B1** — see
   [Step 1a as built](#step-1a-as-built--2026-08-30). It is listed here because
   it is a precondition of every cluster after it, not because it recurs: no
   later cluster has to repeat it.
2. Convert by hand. `subTest` → `pytest.mark.parametrize` where the cases are
   static; where the case list is computed (e.g. `test_errors`' ast-walk over
   every `raise TupferlError`), compute it at module level and parametrize
   over it — **and keep the assert-the-count-first discipline**: a parametrize
   over a computed list needs a companion test that the list is non-empty
   (§2's zero-iteration trap, now at collection time). Where a loop is
   genuinely per-fixture-state, use the pytest-subtests fixture or
   restructure. `addCleanup` → yield-fixtures or `request.addfinalizer`;
   ExitStack-in-`setUp` → yield-fixtures (`enterContext` was banned for 3.10
   reasons; fixtures make the idiom moot). `skipUnless`/`skipIf` (5 sites) →
   `pytest.mark.skipif` with the same reason strings — the macos `--no-skips`
   leg semantics survive because A2's runner counts mark-skips as skips
   (verify once against that leg's excludes). Timeout bounds: keep
   `support.deadline`/`bounded`/`PROMPTED`/`PATIENCE` exactly as they are
   (framework-neutral); additionally expose them as conftest fixtures for
   converted tests that want injection. The floor mechanism (env
   `TUPFERL_MUTATE_EACH_TEST` read at import time) is unchanged, and the five
   "where to arm it" lessons apply verbatim — with lesson 5 restated for
   parametrize: parametrize gives per-case bounds for free (one test = one
   case).
3. Gate, in order: full preflight; then the mutation check:
   `python -m tools.mutate --all --only <src>` for **every source file whose
   sweep selection includes this module** (compute the list from
   `tools/mutants.py`'s `targets_for`/`importers`; the PR body lists it),
   comparing rows against the most recent whole-tree sweep: **zero
   newly-surviving and zero newly-BROKE rows**. Killer-name churn is fine.
   The generated sweep runs *last*, after self-review (§1).
4. PR body: the before/after test-name mapping (renames from parametrize are
   expected; disappearances are not), the mutation evidence, and any
   CLAUDE.md entry the cluster staled.

**Cluster order** (leaf-most machinery first; harness self-tests last — they
drive nested harnesses and are the most alarm/timeout-sensitive):

| PR | modules | machinery converted | notes |
|---|---|---|---|
| B1 | **Done** — see [B1 as built](#b1-as-built--2026-08-30). `test_cpus`, `test_packaging`, `test_errors`, `test_merge`, `test_config`, `test_ci`, `test_release`, `test_paths` | creates `tests/conftest.py` (initially near-empty) — **not done, deliberately**; see B1 as built | no support bases; `test_paths`' local `Environment` base → fixture. Also updates CLAUDE.md's "Build & test" serial-fallback line (`python -m unittest discover…` stops covering these modules) to `python -m pytest -q`. |
| B2 | **Done** — see [B2 as built](#b2-as-built--2026-08-30). `test_config_properties`, `test_merge_properties`, `test_sync_properties`, `test_profiles` | none new | Hypothesis-native. Delete the `__module__`/`__name__`/`__qualname__` dunder hack in `test_sync_properties.py` (it existed for unittest id round-trip in sharding; pytest nodeids come from collection) — keep the `X = Machine.TestCase` assignments, which are the pytest-idiomatic spelling. `profiles.py` untouched. The pyproject mypy-override list stays valid (module names unchanged). |
| B3 | **Done** — see [B3 as built](#b3-as-built--2026-08-31). `test_conflicts`, `test_gitrepo`, `test_cli`, `test_manifest`, `test_doctor` | **creates `tests/conftest.py`**, which B1 did not; `SandboxCase` → `sandbox` fixture (throwaway `$HOME`; `mock.patch.dict(os.environ, sandbox_env(...), clear=True)` as a yield-fixture). **Not `requires_git`** — its only user is `test_mutants`, so it belongs to B6; `test_doctor`'s `skipIf` *was* converted | pty/`run_cli` tests live here; S0's capture findings apply. `sandbox_env` and the `CARRIES` allowlist are untouched — the poison test in `test_support` still guards the `ENV_KEYS` linkage. |
| B4a | **Done** — see [B4a as built](#b4a-as-built--2026-08-31). `test_sync`, `test_status`, `test_diff`, `test_manage` | `TwoMachines` → `two_machines` fixture, and `support.Machine` → a `machine` one, which this row did not anticipate: `test_manage` takes it for six classes. The `template()`/`copy_template()` functions are unchanged | the overlay both-copies rule transfers as-is; `TestTheSnapshotIsWrittenLast` is in `test_sync_cli` and so belongs to B4b, which this row had wrong. |
| B4b | **Done** — see [B4b as built](#b4b-as-built--2026-08-31). `test_overlays`, `test_sync_cli`, `test_sync_commits`, `test_sync_conflicts` | per-module bases (`Conflicted`, `TwoCommits`, `OneMachine`, …) → module-local fixtures | all three `unittest` adapters had no user left and were deleted here, which this row anticipated for two of them. |
| B5 | **Done** — see [B5 as built](#b5-as-built--2026-08-31). `test_support`, `test_paint`, `test_watch`, `test_reached` | local bases (`Boxed`, `Fixture`) → fixtures | `test_watch`'s bound-vs-alarm numbers (the 30s trap) re-checked against `bounded` after conversion — and they were wrong, in `test_reached` too. |
| B6 | **Done** — see [B6 as built](#b6-as-built--2026-08-31). `test_run_tests`, `test_mutants`, `test_verdict`, `test_mutate` | `Probe`, `Tree`, table bases → fixtures | hardest: these drive *nested* harnesses; every recorded walk/BROKE gotcha applies. `test_mutate.py`'s mid-file `if __name__ == "__main__"` block (~line 349; the file continues for thousands of lines) is dead under pytest — delete it with a note. `tests/test_verdict_unittest.py` is left unittest-style *deliberately* (it dies in Phase C; pytest runs unittest tests either way, so leaving it costs nothing — say so in the PR). The `TODO` survivor tags in `tools/mutate.py` are not this phase's debt: leave them, count unchanged. |

**Size:** 7 PRs, each roughly 1–4 sessions. **Failure protocol:** FP per
cluster; a newly-surviving row means the conversion weakened a test — fix the
test, never the disposition; a newly-BROKE row is almost always a bound/alarm
race — apply the five-lessons checklist before touching anything else.

### #96 is a prerequisite of B6, and of no other cluster — settled 2026-08-31

**What B6 needed was for every unanswerable row to carry a reason, and it now
does.** A sweep mutates its own memory guard, its process-identity readers and
its pool, so those rows come back `BROKE` or `TIMEOUT`, and neither is ever
`caught`. B6's gate is the 1030-row `tools/mutate.py` table, where the noise
sits; converting `test_mutate.py` while those rows are unanswerable means the
cluster's own acceptance check cannot distinguish "the conversion weakened a
test" from "the harness cannot answer this row", which is the one thing the gate
exists to tell apart.

Measured on the whole table, green baseline: **19 rows answered nothing**, in
`_lane` (7), `_born` (4), `Work.take` (2), `_Lanes.release`, `_sandboxes`,
`_borrow`, `_attempt`, `run` and `_born_from_proc` (1 each). Twelve had no
disposition at all. All 19 now carry a written `# survivor:` reason, so **B6's
gate reads "zero newly-surviving and zero newly-*unexcused*"** and a row that
becomes unanswerable during the conversion is loud rather than lost in a wall of
`BROKE`.

**Read those reasons before treating a spent-tag report as a finding.** Thirteen
of the 19 are unanswerable *under a sweep* and come back `caught` in 42.8s run
alone -- measured warm and cold alike, so it is not an ordering effect. A narrow
run over `tools/mutate.py` therefore reports their tags spent, which is the same
row answered under conditions B6's gate will not have. The other six are
`TIMEOUT`s on a drained sandbox queue and are not answerable at all.

**[#96](https://github.com/martinus/tupferl/issues/96) itself asked for
something else and is deliberately left open**: refusing to *generate* those
rows. That was built and measured before being taken back out — a scope-keyed
exclusion removes 99 rows to repair 19, and 57 of the 99 are rows the suite
catches today. CLAUDE.md's gotchas carry the numbers and the argument; whether
to close the issue on them is the maintainer's call, not this plan's.

**Measured, so this is a schedule rather than a worry.** Counting the caught
rows of `tools/mutate.py` that each cluster's modules actually kill:

| cluster | caught rows it kills | forces the 1030-row gate? |
|---|---:|---|
| B2 | 0 | no |
| B3 / B4a / B4b | 2 / 2 / 3 | check the handful individually |
| B5 | 0 | no |
| **B6** | **3105** | **yes, all of it** |

So B2–B5 are unaffected and should not wait. Re-run that count before B3, B4a
and B4b rather than trusting these three small numbers: they came from sweeps
taken on 2026-08-30 and a cluster that changes a module changes what kills what.

**And the reason for doing it immediately before B6 rather than earlier held
up, though not for the reason given.** It was expected to touch
`tools/mutants.py`, whose tests B6 converts; in the end it touched only
`tools/mutate.py` and CLAUDE.md, because the mechanism it needed already
existed. What it did need was a whole-table sweep of `tools/mutate.py` — which
is B6's gate, so the baseline was paid for once and serves both.

## Step 1a as built — 2026-08-30

Landed on its own, before B1, for two reasons. It touches only `tools/` and
converts no test module, so its own mutation sweep is a clean before-and-after;
and B1's gate is "zero newly-surviving and zero newly-BROKE rows against the
last whole-tree sweep", which a harness change landing in the same PR would
have muddied.

### Three of the four fields already held sequences

The plan named `Mutation.first`, `Killers.known`, `Learned.recent` and
`baseline_shards`' extra shard. Read against the code, only two of those are
places a space can be lost:

| named | what it actually holds | changed |
|---|---|---|
| `Mutation.first` | one space-joined string | **yes** — now `Sequence[str]`, and `Learned.ahead` returns a tuple to match |
| `baseline_shards`' extra shard | one space-joined string, re-`split()` by `run` | **yes** — a shard is now a sequence of names throughout, selection shards included |
| `Killers.known` | `dict[key, one test id]` | no — a single id per row, never joined |
| `Learned.recent` | `list[one test id]` | no — same |

`Killers.known` and `Learned.recent` were never the hazard: each element is one
id and nothing concatenates them. What did concatenate them is `Learned.ahead`,
which joined its result with spaces on the way out, and `_attempt`, which did
`f"{mutation.first} {ahead}".split()`. Both are gone; the composition is now
`(*mutation.first, *ahead)`.

`Mutation.tests` beside it is **deliberately still one space-joined string.** It
holds a *selection* — dotted module and class paths from `mutants.targets_for`
— and a dotted path cannot contain a space, so the hazard does not exist there.

**And the constraint that makes converting it anyway wrong**, which matters more
than the absent hazard, because "finish the job" is the obvious next PR:
`mutation.tests` is in the `--json` report. `_persist` writes it and `_recorded`
feeds `row["tests"]` straight back into `Mutation(...)`, so converting the field
changes an on-disk schema that every `sweeps/*.json` already carries — and an
**older** report read back would rebuild rows with a `str` in a
sequence-typed field, reintroducing exactly the shape this step removed, through
the resume path, with no type error to show for it. `first` has no such
constraint: it is never serialised.

### `str` is a `Sequence[str]`, so the type does not close the hole

Measured, not assumed: `Mutation(..., first="a whole string")` type-checks
clean. So does `row._replace(first="...")` — mypy does not check
`NamedTuple._replace`'s keywords **at all**, which is how two rows of this
project's own suite kept a string through the conversion and came back `BROKE`.

The failure is the flattering one. Iterating a string yields characters, so a
killer becomes fifty single-letter names, each selecting nothing; the row is
`BROKE`, and a `BROKE` row is never `caught`, so the line it appeared to guard
is guarded by nothing while the summary counts it in neither of the two numbers
a reader looks at.

`mutants.check` now refuses a string `first`, naming the tuple spelling in the
message. It goes there because `check` runs over the whole table before the
first sandbox is built: one loud death at row 0, not a wall of non-answers an
hour later.

### The fixtures were wrong in a way five of them could not show

`_unbaselined` takes the shard list, so its eight tests all built shards by
hand as `["tests.test_paths"]`. Under the new shape that is a list of one
*string*, which iterates into `t`, `e`, `s`… — and **three went red while five
went on passing.** The five are the dangerous half: they assert that a killer
is *not* covered by a shard, which a shard of single letters satisfies
perfectly.

They now go through one `shards()` helper, so a shard cannot be built wrongly
in a fixture and rightly in production. This is §2's "suspect the fixture"
arriving through a type change rather than through a review.

### Evidence

Two regression tests, and the revert that proves them. Reverting the *types* is
not the honest check — the tests then fail with `'tuple' object has no attribute
'split'`, which proves only that the shape moved. Reverting the **mechanism**
while keeping the types (restoring the join-and-re-split under
`first: Sequence[str]`) is the real one:

- `test_a_parametrized_id_survives_the_composition_from_both_sides` — both
  sides of `_attempt`'s composition carry a parametrized id, because both come
  from a previous verdict. Fails on the shredding: `…test_it[a b]` arrives as
  `…test_it[a` and `b]`.
- `test_a_parametrized_killer_reaches_its_shard_whole` — the same for the
  baseline shard, which is where it would have been silent.

Both fail under that revert; **the eight neighbouring tests in the same two
classes pass unchanged**, which is the point — the old mechanism was the
identity for every id without a space, so nothing already in the tree could
have seen it.

Plus two in `tests/test_mutants.py` for the `check` guard, and a sweep of
`tupferl/merge.py` — 31 rows, 30 caught, 1 survivor already tagged, baseline
green — to drive the shard path end to end.

## B1 as built — 2026-08-30

Eight modules, 115 collected items before and 237 after -- the growth is
`subTest` loops becoming `parametrize`, which is a rename of existing cases and
not new coverage. Nothing disappeared; the mapping is in the PR.

### `tests/conftest.py` was not created, and that is the divergence

The cluster table above says B1 creates it "initially near-empty". It does not,
because after converting all eight modules **no *fixture* in them is shared**.
That is narrower than "nothing is shared", and the distinction matters: what B1
did share it shared through `tests/support.py` and CLAUDE.md, which is where
those belong. The three fixtures B1 wrote are each used by exactly one file: `only` (an
`os.environ` holding precisely what a test names) in `test_paths.py`, `box` (a
throwaway directory) in `test_config.py`, and `merged_under` (a real
`~/.gitconfig` naming a conflict style) in `test_merge.py`.

An empty `conftest.py` would make a claim -- *shared fixtures live here* -- that
nothing yet backs, and §0 is about exactly that kind of sentence. B3 is the
first cluster with machinery two modules genuinely share (`SandboxCase` →
`sandbox`), so the file arrives in the PR that justifies it, and the table above
now says so.

**Two things B1 shared as prose rather than as code, and both are deliberate.**
The comment-stripping rule now has two spellings in two files, and the
"parametrize over a computed list needs a non-empty companion" invariant has
four. Both are named as follow-ups below rather than extracted here -- see
"Declined in the review, and why".

### Declined in the review, and why

The `/simplify` pass raised two structural findings that were not applied, and
CLAUDE.md §3 asks that a declined finding be argued rather than dropped.

- **A `support.over(argnames, cases)` wrapper refusing an empty case list.**
  The invariant is real and is currently written four ways (`test_ci`,
  `test_release`, `test_errors`, `test_config` each guard it differently), and
  25 modules follow. But it is a *new shared abstraction* introduced in a
  conversion PR, with only one cluster's worth of evidence about what shape it
  wants. **B2 is the right home**, with two clusters to validate it against;
  after B3 the retrofit becomes its own PR, so it should not slip further.

  **Overturned by B2, which is where it was sent.** B2 contributes no case to
  validate it with, and reading the four existing guards settled it against the
  wrapper outright — see [B2 as built](#b2-as-built--2026-08-30). The retrofit
  is not owed to B3; what is owed instead is an `ast` walk over `tests/`,
  filed as [#100](https://github.com/martinus/tupferl/issues/100).
- **Extracting `settings()` into one shared helper.** `tests/test_ci.py` and
  `tests/test_release.py` now hold byte-identical strippers. Extracting one
  invites extracting `jobs()` too -- and those genuinely are two different
  parsers (a line accumulator against `re.finditer`) for one YAML shape, which
  needs its own perturbation evidence over both workflows. That is a PR, not a
  paragraph. What *was* fixed here is the half that made a test unfailable:
  `test_release.jobs()` now strips in the parser rather than at four call
  sites, and has the both-halves test it never had.

### The `unittest` verdict layer lost its footing, loudly, and one test moved

`TUPFERL_MUTATE_VERDICT=unittest` drives `unittest`'s own loader, and that
loader cannot take a pytest-native module back: a plain `class TestX:` is found
by name and refused with

    TypeError: calling <class 'tests.test_config.TestRejectingAnUnknownKey'>
    returned <tests.test_config.TestRejectingAnUnknownKey object ...>, not a test

So every row whose *selection* names a converted module now answers `broke`
under that layer. `tests/test_mutate.py`'s
`TestWhichVerdictLayerGradesAProbe::test_either_layer_can_actually_grade_a_row`
was the one test that noticed, and it was right to: its row,
`UNKNOWN_KEY_GUARD`, selects `tests.test_config`.

**The fix was to move the row, not to weaken the test.** `EITHER_LAYER` mutates
`tools/verdict_unittest.py` and selects
`tests.test_verdict_unittest.TestATestThatNoticed` -- the one module this plan
keeps `TestCase`-style to the end, because it dies with its subject in Phase C.
So the row lives and dies with the layer it exists to exercise, and it is stable
through B2–B6. Measured: **0.35s per layer, against `UNKNOWN_KEY_GUARD`'s
0.66s**, so the move bought headroom rather than spending it.

Two candidate rows were timed before this one was picked; the other, mutating
`_carrier`'s "did not finish within" message, is also caught by both layers but
costs **3.4s per layer**, because its killer is a deliberately hanging test.
That is the kind of number the five "where to arm it" lessons are about, and the
cheaper row was chosen for it.

### `python -m unittest discover` is no longer a fallback, measured

It ran **1614 tests before this cluster and 1499 after, reporting `OK` both
times** -- exactly the 115 in the eight converted modules gone, with nothing
said. Pointed at one converted module alone it prints `Ran 0 tests` and
`NO TESTS RAN`. CLAUDE.md's "Build & test" line now says `python -m pytest -q`
and carries those numbers.

### What the conversion did, in one list

- `unittest.TestCase` bases dropped; the classes stay, because their docstrings
  are where this project keeps its arguments and pytest collects a plain
  `Test*` class the same way.
- `self.assertX(...)` → plain `assert`, and `assertRaises(...) as caught` →
  `pytest.raises(...) as caught` with `caught.exception` becoming
  `caught.value`.
- Every `subTest` loop → `pytest.mark.parametrize`. Where the case list is
  *computed* -- `test_errors`' ast-walk, `test_ci`'s and `test_release`'s hand
  parse of a workflow -- it is computed once at module level and the companion
  "the scan found them" test is restated to say what it now also guards: an
  empty list collects **no cases**, so those tests would not fail, they would
  cease to exist.
- `setUp` + `addCleanup` → yield-fixtures. Where the old base ran a helper the
  test called (`test_paths`' `Environment.only`, `test_merge`'s
  `merged_under`), the fixture yields a *callable* and holds an
  `contextlib.ExitStack` that unwinds at teardown.
- Three dead `if __name__ == "__main__": unittest.main()` blocks deleted,
  including the one sitting **mid-file** in `test_packaging.py` with a class
  after it.
- **No `skipUnless`/`skipIf` at all.** The contract above lists "5 sites →
  `pytest.mark.skipif`"; none of them is in this cluster -- they are in
  `test_doctor`, `test_verdict`, `test_run_tests`, `test_mutate` and
  `support.requires_git`, so B3, B5 and B6 inherit that item and the
  `--no-skips` check with it. `test_cpus` deliberately has none: its
  Linux-only half is a plain `if` with a label, because the `macos` leg turned
  red the one time it was a skip.
- `test_config`'s throwaway directory goes through `support.tempdir` rather than
  pytest's `tmp_path`, and the fixture says why: `tmp_path` keeps three numbered
  roots per user under `/tmp/pytest-of-<user>`, and a sweep races thousands of
  probe processes over that numbering.

### The review found a test that could not fail, and it was pre-existing

`test_ci.py`'s `test_it_runs_even_when_a_dependency_failed` asserted
`"if: always()" in gate_block` against the **raw** file, and the gate explains
itself in a comment that quotes the setting. Measured: deleting the real
`if: always()` line left all 33 tests in that file green -- a test that could not
fail, guarding the one required status check.

Found by perturbing a copy of the tree rather than by reading: seven settings
deleted one at a time from `ci.yml`, six of which went correctly red. `jobs()`
now parses `settings(workflow())`, the same comment-stripping `test_release.py`
already had, and `test_the_comment_stripping_leaves_the_settings_alone` states
both halves of the precondition. All seven probes now fail; the control is
green. CLAUDE.md has the general rule.

### Gate

Preflight green -- 1737 tests, 0 failures.

The mutation check ran `--all --only <src>` over the **13 source files whose
sweep selection includes one of the eight converted modules**, computed from
`mutants.targets_for`/`importers`. Three have no mutable rows
(`tools/__init__.py`, `tupferl/__init__.py`, `tupferl/errors.py`);
`tests.test_ci` and `tests.test_release` are reached by no source file at all,
because what they test is YAML.

**B1 changes no source, so the comparison against the last whole-tree sweep is
exact for every file that sweep's commit shares with this branch.**

| files | rows | result |
|---|---:|---|
| `conflicts.py`, `copies.py`, `gitrepo.py` | 409 | **0 newly not-caught**; one improvement, `copies.py:104` BROKE -> caught; 8 survivors, all tag-excused |
| `config.py`, `manifest.py`, `merge.py`, `paths.py`, `__main__.py`, `tools/cpus.py` | 230 | 0 newly not-caught |
| `tools/mutate.py` | 975 | 2 groups where caught fell, both explained below; broke 9 -> 2, timeout 8 -> 6, caught 744 -> 755 |

Baseline green on every run -- checked for the `BASELINE NOT GREEN` line, not
inferred from the verdicts.

**The two `tools/mutate.py` groups, and neither is B1's:**

- `connector: looked.outcome == "survived" or red` came back `broke` with
  `killed by SIGKILL`. A memory artefact -- see below.
- `drop-call: self._ceilings.pop(group, None) -> pass` came back `survived`
  where the baseline caught it. A/B'd the way step 1a's finding was: the
  mutation applied to a `main` worktree **and** to this branch, both run against
  the row's own generated selection. **426 passed on both.** So it survives on
  `main` too, and moved somewhere between the baseline sweep's commit
  (`b95db93`) and `main` -- step 1a and A2 rewrote that file heavily. It already
  carries a `# survivor: drop-call -- TODO` tag.

Nine further not-caught groups have **no baseline row at all**: they are
`_collected`, `_loadable`'s `_layer()` branch and `_report_headroom`, i.e. code
Phase A and step 1a added after the baseline sweep ran. They are that work's
debt, not this cluster's.

### The `/simplify` re-gate

The review changed `tests/test_merge.py` behaviourally, and that module is the
killer for 15 caught rows of `tupferl/gitrepo.py` and 8 of `tupferl/merge.py`.
Those two files were re-swept at 12 lanes: **156 rows, 150 caught, 6 survivors
all tag-excused, 0 BROKE, 0 TIMEOUT -- byte-identical to the baseline.**

Nothing else needed re-gating, and that was measured rather than assumed: of
every caught row in the three gate sweeps, the ones whose killer lives in a
converted module are `tools/cpus.py` (2, from `test_cpus`), `tupferl/config.py`
(27, from `test_config`), and those 23. **No row anywhere is killed by
`test_packaging`, `test_paths`, `test_ci`, `test_release` or `test_errors`** --
which is why the 975-row `tools/mutate.py` table did not have to be run again
for a change to `test_packaging.py`, the thing that would otherwise have made
this review expensive.

### A sweep killed the machine's desktop session -- and not for the reason recorded here

**The paragraphs below were written during B1 and their diagnosis is wrong.**
They are kept because the correction is worth more than the original: the cause
was #91, a mutant of `_lane` inverting the lane's membership test so that
`_end_lane` `SIGKILL`s every process the *user* owns. It was `os.kill` in a
loop, not the OOM killer, and the summed-lane sampler written for #90 is what
established it -- that table holds 924 MiB across 8 lanes, so ~3.2 GiB at the 28
that were running, on a 62 GiB machine that showed 55 GiB free afterwards.

What survives unchanged is the *reading* rule, which is why the section stays:
`broke` fell 12 -> 2 -> 0 as lanes came down on identical rows, so a `SIGKILL`
row is a question rather than an answer. What does not survive is blaming
`_COMMIT`, which #90 has since **measured and cleared**: the lanes hold
5.1-5.7 GiB between them against 80 GiB of ceilings, 15x headroom. The
rows that do fail are #96, the sweep mutating its own memory guard.

### The lane arithmetic, as it was recorded at the time

`tools/mutate.py`'s table was run three times before it was believed, and the
reason is CLAUDE.md's `_COMMIT` entry coming true:

| lanes | ceiling each | allowance | result |
|---|---|---|---|
| 40 | 2065 MiB | 83 GiB | 12 BROKE, all `SIGKILL`; closing line said the heaviest lane held **100% of its ceiling** |
| 20 | 4096 MiB | 82 GiB | clean where it ran; heaviest lane 14% |
| 28 | 2808 MiB | 79 GiB | **took the desktop session with it** |
| 6 | -- | -- | clean; finished the remainder |

The machine has 62 GiB. `_COMMIT` is 150%, so every one of those allowances is
over it by design -- the design being that lane peaks do not coincide. On this
table they do: it runs *nested* harnesses, and `slowest_first` dispatches its
expensive rows adjacently, which is exactly the correlation woswoar#232
measured and this file's "Measured, and kept" section already records.

CLAUDE.md said what to do about it before it happened -- "if a sweep is ever
OOM-killed, this is the first thing to look at, and the sampler is the thing to
write before arguing about the constant" -- so the finding is that the entry was
right, and the sampler (sum of lane RSS at one instant, which is what the host
feels) is now owed. Nothing about it is changed here; a lane count was chosen
by hand instead.

**Reading the ceiling line is not optional.** The first pass printed `heaviest
lane process held 2065 MiB of its 2065 MiB ceiling (100%)` and 12 `BROKE` rows,
and the two facts are one fact. Diagnosing the rows without reading the line
would have produced a guard against a conversion regression that never happened.

## B2 as built — 2026-08-30

Four modules, 16 collected items before and 19 after. The three extra are two
`subTest` loops in `test_profiles.py` becoming `parametrize`; every original test
name is still there, two of them now carrying a `[case]` suffix.

The smallest cluster in the plan, and it went as written. What is worth recording
is three things it decided rather than three things that went wrong.

### The dunder hack is gone, and pytest never needed it

`test_sync_properties.py` rewrote `__module__`, `__name__` and `__qualname__` on
both classes Hypothesis generates. That was not decoration: a `unittest` loader
asks a class what module it lives in, Hypothesis builds `TestCase` inside
`hypothesis.stateful`, and the id that came back could not be re-imported by
`tools/run_tests.py`'s sharding -- caught, when it happened, by that runner's
accounting check ("1 discovered tests never ran") and by nothing else.

pytest takes the name from the module namespace it collected, so the six lines
are simply not needed. Verified rather than assumed, because the thing they
guarded is exactly the thing that fails silently: the ids collect as
`tests/test_sync_properties.py::TestSyncIsIdempotentAndConverges::runTest`, and
the full sharded run reports **1780 tests, `OK`** with the accounting check
satisfied.

**It also moves the module out of the "still `TestCase`" count, and that is
honest rather than convenient.** `SyncMachine.TestCase` *is* a
`unittest.TestCase` subclass and the assignment stays -- the plan keeps it,
because it is the pytest-idiomatic spelling too. What
`tests/test_pytest_plan.py` counts is a `TestCase` a module *defines*, and with
`__module__` no longer overwritten this module defines none: the class is
Hypothesis's, built in Hypothesis's module. The same `value.__module__ ==
mod.__name__` filter that stops `support.py`'s bases being counted four times
is what reads this correctly.

### `support.over()` was declined a second time, with a better reason

B1's review raised a shared wrapper refusing an empty `parametrize` case list,
and B1 deferred it here on the grounds that "B2 is the right home, with two
clusters to validate it against". **B2 contributes no case to validate it
with**: all four modules are Hypothesis-native, their parametrizes are over
literal tuples, and the cluster adds no computed list at all.

Reading the four existing guards settled it against the wrapper on its own
terms. `test_ci`, `test_release`, `test_errors` and `test_config` do not assert
non-emptiness -- they assert *content*: `GATE in FOUND`, `"runs-on:" in
FOUND["check"]`, `len({m.module for m in FOUND}) >= 5`, `set(KNOWN) == {field
names}`. Each is strictly stronger than "not empty", each is specific to its
module, and a generic `over()` would replace none of them. It would add a
weaker fifth check beside four stronger ones.

And it would not close the hole it was proposed for, which is somebody adding a
parametrize over a computed list and *forgetting* the guard: nothing stops that
author writing `pytest.mark.parametrize` directly. **The shape that would
actually work is a test, not a helper** -- one that walks `tests/` with `ast`,
finds every `parametrize` whose case list is a name rather than a literal, and
insists the module also holds a test naming that name. That is `test_errors.py`'s
own technique pointed at the suite, it is about a page of code, and it is a PR of
its own rather than a paragraph in a conversion.

**The decline is recorded here; the test that should exist is
[#100](https://github.com/martinus/tupferl/issues/100).** The two are separable
and belong in different places -- the argument against the wrapper is about this
conversion and dies with the plan, while the guard outlives it and is something
somebody has to do. §4 asks for a filed issue when a finding is "a guard that
would catch a future regression", and this is one by its own description. The
issue carries both constraints that make the obvious implementation wrong, one
of which is that the new test is itself parametrized over a scan and needs its
own non-emptiness precondition. Nobody has been bitten yet, so it is a P3, and
the retrofit is not owed to B3.

### The review found the predicate behind the plan's own progress count

Not in the four converted modules, which came through the four `/simplify`
angles clean -- but in `tests/test_pytest_plan.py`, which this cluster falsified
without touching.

That guard counted a module as unconverted if it *defined* a
`unittest.TestCase`, asked as `issubclass` over the module's attributes and
filtered by `value.__module__ == mod.__name__`. **B2 edited exactly that
attribute.** `test_sync_properties.py` never wrote a `TestCase` of its own; it
was in the count only because the deleted dunder rewrite pointed Hypothesis's
class at this module. So the count fell 26 -> 22 in a PR that converted four
modules, and one of the four fell for three deleted assignments.

The number was honest and the mechanism was not, which is the distinction worth
recording: *a count a `__module__ = __name__` can lower is not a count of work
done*, and the same file's docstring already rejects a "converted" count for the
symmetric reason. The predicate is now the `unittest` loader itself --
`loadTestsFromModule(mod).countTestCases()` -- which is what
`python -m unittest discover` and `tools/verdict_unittest.py` actually run, is
immune to `__module__` (verified: re-applying the deleted hack in memory leaves
the answer at 2 tests), and needs no filter for imported bases, because a base
carries no `test_` methods to count. 159ms for all 35 modules.

Three consequences:

- **The number is 23, not 22**, and the status line now names *two* permanent
  exceptions rather than one. `test_sync_properties` is converted and will be
  unittest-backed for ever, because the class is Hypothesis's. Saying so is
  better than a count that quietly excluded it: a reader of "22 left" would have
  gone looking for 22 modules to convert and found 21.
- **The grep the docstring rejects is wrong in both directions**, measured: it
  misses `test_overlays`, `test_sync_commits`, `test_sync_conflicts` and
  `test_sync_properties`, and it counts `test_pytest_plan` itself, which names
  `unittest.TestCase` only to ask about it. 23 against grep's 20. That worked
  example had been made false by this cluster under the old predicate and is
  exact again under the new one.
- **Two of the new tests could not fail on their first run**, found by
  perturbation rather than by reading. "The plan names both permanent modules"
  searched the whole document head, and the paragraph explaining the predicate
  names `test_sync_properties` -- so a status line that had dropped it stayed
  green; it is scoped to the status paragraph now. And a probe that renamed the
  two class bindings to `_Hidden` did not go red, because `loadTestsFromModule`
  walks `dir(module)` and does not care what a name looks like -- the probe was
  wrong, not the test, and the correct probe (delete the bindings) fails two
  tests as it should.

Five perturbations now go red with a green control: total off by one, count off
by one, a module dropped from the cluster table, the status line dropping a
permanent module, and a permanent module genuinely converted.

### The gate: what was in selection, and the one file deliberately not swept

Selections were recomputed from `tools/mutants.py` rather than taken from B1's
notes, and the earlier estimate of "six `tupferl/` files" was wrong by four:
`tupferl/__init__.py`, `tupferl/errors.py` and `tools/__init__.py` are in scope
and generate **0 rows** between them, and `tools/mutate.py` is in scope because
`test_profiles.py` imports it.

`tupferl/` has not been touched since 2026-08-29, so the whole-tree report of
2026-08-30 10:04 is an exact row-for-row baseline. That made the whole package
cheaper to run than the six files separately -- **1309 rows in 441s at 38
lanes**, against six invocations each paying their own baseline shard -- so the
gate is wider than the contract asks for rather than narrower.

| | baseline | B2 |
|---|---:|---:|
| caught | 1271 | **1272** |
| survived | 26 | **26** |
| `BROKE` | 1 | **0** |
| `TIMEOUT` | 0 | 0 |

**Zero newly-surviving and zero newly-`BROKE`**, which is the contract. The
survivor *set* is identical row for row, not merely the same size -- all 26 are
excused by a tag beside the code, and the sweep exits 0 over them. One row moved
the other way: `tupferl/copies.py:104` was `broke` in the baseline and is
`caught` here.

Preflight: **1781 tests, 0 failures, 0 skipped**. The sweep's own load lines
read `heaviest lane process held 555 MiB of its 2068 MiB ceiling (27%)` and
`every lane held 3587 MiB between them at once, of 52297 MiB usable (7%)`, with
an independent watchdog seeing `MemAvailable` no lower than 48829 MiB -- worth
recording because this is the first sweep since [#91](https://github.com/martinus/tupferl/issues/91),
where the harness's kill list was the thing at fault rather than memory.

`tools/mutate.py` was **not** swept, on the rule the [#96
section](#96-is-a-prerequisite-of-b6-and-of-no-other-cluster--settled-2026-08-31)
already states, which says to re-run the count rather than trust it. Re-run against both of the
day's whole-table arms (1030 rows each, 21:38 and 22:08): **0 of its caught rows
is killed by any B2 module**, in either. So no change to these four modules can
turn one of those caught rows into a survivor -- there is nothing there that
depends on them -- and the 1030-row table is at present the noisiest in the tree
(#96), which is the second reason not to read a comparison out of it.

### Declined in the review, and why

- **Renaming `test_all_three_are_registered` now that it is parametrized.** The
  ids read `[dev]`, `[ci]`, `[mutation]` and each case asserts one profile, so
  the plural is odd. Kept anyway: the cluster's auditable claim is "16 items
  before, 19 after, every original name present", and a rename that is not
  forced by `parametrize` weakens exactly that. The failure message is one line
  of oddness against a mapping a reader can check.
- **`pytest.raises(TupferlError, match=key)` in `test_config_properties`.**
  Shorter by two lines, and it would be correct today because `KEYS`' alphabet
  is `a-z_-`, all regex-safe. That is the objection: the test would silently
  become weaker -- `re.search` matching more than the substring did -- the day
  somebody adds `.` or `*` to that alphabet, and nothing would say so. The
  explicit `assert key in str(caught.value)` has no such coupling.
- **Caching `ask(CI="true")`, which two tests spawn separately.** Measured at
  0.103s, 26% of the module's in-pytest wall, and paid on every probe that walks
  `tests.test_profiles`. Declined because the second of those tests is the
  *precondition* -- it exists to say that the identical answers the first test
  compares are the right answers -- and sharing one subprocess run would make it
  assert about the run the first test already made. 0.1s is not worth turning a
  precondition into a restatement.

## B3 as built — 2026-08-31

Five modules, 3556 lines, 61 classes, **506 assertions** -- more than B1 and B2
together, and the first cluster where the machinery moved rather than the tests
alone. 303 collected items before, 350 after; **every one of the 303 distinct
test names survives**, and the growth is `subTest` loops becoming `parametrize`.

| module | before | after |
|---|---:|---:|
| `test_conflicts` | 119 | 123 |
| `test_gitrepo` | 63 | 71 |
| `test_manifest` | 60 | 77 |
| `test_doctor` | 40 | 42 |
| `test_cli` | 21 | 37 |

### The machinery landed first, on its own, and that was the right call

`support.SandboxCase` has **13 classes naming it directly and 36 more reaching
it through `support.Machine`** left in B4a and B4b, so it cannot be deleted here -- and a `sandbox` fixture written beside it would have been a
second hand-maintained copy of what a sandbox *is*, free to drift for exactly as
long as it takes nobody to notice.

So `support.sandbox()` is the definition now and both are adapters over it:
`SandboxCase` holds no setup of its own, and `tests/conftest.py` yields the same
`Sandbox`. Landed as its own commit with **no test module touched** and the
suite green at 1781, which is what makes it checkable as behaviour-neutral
rather than merely believed to be.

`tests/conftest.py` arrives here rather than in B1, for the reason B1 gave when
it declined to create it empty: this is the first cluster where a fixture is
genuinely shared. A `sandbox_on` fixture for naming the host was written and
deleted before committing -- no module in B3 needs a non-default host, and that
file's own docstring forbids a fixture written for a caller that does not exist.

### The conversion hazard this cluster found, and it is the one to carry forward

**A test can depend on a base class for a side effect and never name it.**
`SandboxCase.setUp` patches `os.environ`; a test that only calls
`os.environ["PATH"] = "/nonexistent"` and asserts on the result reads, in its own
text, as needing nothing at all.

Converted by giving each test the fixtures its body mentions, such a test gets
**none** -- and then runs against the developer's real environment. That is
exactly the failure `tests/support.py`'s docstring is about, arriving by a new
route: "a test that writes there is not a flaky test, it is a lost afternoon".
Here it was loud (`PATH` stayed broken and nine later tests could not find git),
but it is loud by luck; a test that merely *read* `$HOME` would have passed.

**The rule that follows, and it is cheap: mark the class, not the method.**
Every class that was a sandbox case carries `@pytest.mark.usefixtures(...)`
naming the fixture that carries its sandbox, whether or not its tests take the
parameter. The decorator is the load-bearing statement -- *this class runs in a
sandbox* -- and it survives somebody later deleting the last reference from the
last test in it.

**The first version of this section stated that rule and the diff did not
follow it**, which is §0's failure mode inside the paragraph that argues against
§0's failure mode. `test_doctor`, `test_manifest` and `test_cli` had 17 marks
between them; `test_gitrepo` and `test_conflicts` had **zero**, across six
converted sandbox classes, and were safe only because every test in them
happened to take a fixture that depends on `sandbox`. Found by review, not by
the suite -- nothing could have gone red. There are 24 marks now, and the rule
names the *property* rather than the identifier `box`, which is a different
composite type in each module.

**And the leak half is guarded rather than trusted now.**
`tests/conftest.py`'s `_every_test_puts_the_environment_back` is autouse: it
fails the test that left `os.environ` changed, instead of the nine downstream
ones that then cannot find git. Its own first run failed 1711 of 1828 tests and
was right to -- **pytest writes `PYTEST_CURRENT_TEST` itself**, and the value
carries the phase, so it reads `(setup)` going in and `(teardown)` coming out.
That is the single named exclusion. Verified it can fail, against a test that
sets `PATH` and does not restore it, and that it stays quiet against one that
does.

It does not catch a test that merely *reads* the real environment, and nothing
cheap does; that half is what the marks are for. A session-wide replacement of
`$HOME` looks like the deeper fix and is not -- it turns a loud failure into
every test silently sharing one home, which is the green run §8 collects.

### What a fixture removed that a base class could not

Three notes worth keeping, because they are the argument for the conversion
rather than a description of it:

- `test_gitrepo.TestIsRepository` built its own sandbox in eleven lines --
  `TemporaryDirectory`, `seed_home`, `sandbox_env`, `mock.patch.dict` and two
  `addCleanup`s. It is one parameter now, and it was the *only* place with a
  second implementation of the sandbox.
- `ConflictedIndex`'s docstring warned that it must hold no tests of its own,
  "because subclassing one that *does* makes every test in it run again under
  the subclass's name". A fixture has nothing to inherit, so the rule that had
  to be remembered is now unstateable. The comment is kept, restated as history.
- `blank_before(case, text, marker)` took a `TestCase` purely so it could call
  `assertGreater` on it. Plain `assert` needs no such thing. That parameter
  existed only to reach the framework, and it is the clearest small win here.

### The assertion rewriting was mechanical, and checked as such

506 assertions is past what is honest to claim was hand-edited, and CLAUDE.md
forbids sed over the real tree for good reasons. What was done instead: a
throwaway rewriter that finds each `self.assertX(...)` by **scanning balanced
parentheses** rather than by regex -- a regex cannot match a nested call -- splits
arguments at top-level commas respecting strings and brackets, and **refuses
anything it does not recognise** rather than guessing, printing what it left.

It was tried on a copy first and read there, and it left 7 calls by hand (the
`assertRaises` context managers). The two things that make this not-a-sed: it
cannot silently half-convert, because anything unrecognised is reported and
still says `self.assert`, which does not run; and the whole diff was read
afterwards, which is where the four real mistakes below were caught.

**It is committed, as `tools/unassert.py`, and the first draft of this section
argued for the opposite** -- that a committed converter invites running it and
trusting it. The review took that apart and was right:

- §7 rules out exactly the storage `/tmp` is: "a note there lives on one
  machine, under one tool, for one person", and it does not survive a reboot.
  The realistic outcome was not "B4a reuses it" but "B4a writes it again", with
  a fresh set of the four mistakes below.
- §7's trigger is "tools exist because the same mistake was made twice". Four
  clusters remain, each with more assertions than this one.
- The precedent runs the other way. `mutate --accept` rewrites the tree in bulk
  and is committed; the answer here has always been *commit it, write the
  hazard down, checkpoint first* -- never *keep it out of the repository*.
- And the safety property is **testable**, which is the real answer to the
  objection: `tests/test_unassert.py` drives it with an unknown method, a
  wrong-arity call and an unbalanced expression, and asserts each comes back
  byte-for-byte and reported. A half-conversion still reads `self.assert`,
  which in a converted class is an `AttributeError`. That is the difference
  between "trust me, I read it" and "it cannot silently half-convert".

Verified against `main`'s five files that the committed tool reproduces what
the throwaway one did: 499 `assert` statements and 11 `assertRaises` correctly
refused.

### Gate

Preflight: **1861 tests, 0 failures, 0 skipped.**

`tupferl/` has not been touched since 2026-08-29, so B2's whole-package report
is an exact row-for-row baseline. **1309 rows in 273s at 38 lanes:**

| | B2 baseline | B3 |
|---|---:|---:|
| caught | 1272 | **1272** |
| survived | 26 | **26** |
| `BROKE` | 0 | **0** |
| `TIMEOUT` | 0 | 0 |

**Zero newly-surviving, zero newly-`BROKE`**, and the survivor *set* is
identical row for row rather than merely the same size. All 26 are excused by a
tag beside the code.

**`tools/mutate.py` was not swept, and the reason is stronger than B2's.** Its
generated selection is `tests.test_mutate tests.test_packaging tests.test_profiles
tests.test_support` -- **no B3 module is in it at all**. The two rows the #96
table records as "killed by a B3 module" are caught by the harness's *outward
walk*, not by their selection, so a B3 change cannot reach them by the route the
gate is about. Both killers still collect under identical nodeids and pass.

The residual risk is stated rather than waved away: the walk could in principle
turn `caught` into `BROKE` if a converted test started *hanging* under a mutant.
B3 added no new unbounded wait -- `support.deadline`, `PATIENCE` and `PROMPTED`
are untouched, and the pty fixtures kept their bounds -- so there is no mechanism
for it, but that is an argument rather than a measurement, and B6 sweeps that
whole table anyway.

### Four mistakes the rewriting made, all found by running the tests

Recorded because each is a shape the next cluster will meet:

1. **A blanket `self.X` -> `box.X` reaches inside the fixture class too.**
   `Managed.refusal` calls `self.check`, where `self` is the dataclass and not
   a test; rewritten, it looked for a fixture from inside the object the fixture
   returns.
2. **Detecting "which fixture does this test need" by searching the method text
   matches prose.** Four tests in `test_doctor` were given a `repository`
   fixture because the word appears in their docstrings, and one was given it
   because `found["repository"]` is a dict key. Strip docstrings first, and
   prefer `box.` with the dot over the bare name.
3. **A helper method that reached the terminal through `self` needs the fixture
   passed to it**, and the tests that call it need the parameter as well --
   which is only visible after the helper is fixed, so the parameter pass has to
   run twice.
4. **A decorator can land on the wrong test.** A `parametrize` written for the
   loop in `test_output_that_is_not_two_numbers_is_unknown` was attached to the
   test above it, which then failed with `NameError` on the parameter. Loud, and
   only because the parameter was read.

## B4a as built — 2026-08-31

Four modules, 3260 lines, 41 classes, **553 assertions** -- more than B3. 241
collected items before, 270 after; **every one of the 241 distinct test names
survives**, and the growth is `subTest` loops becoming `parametrize` plus two
new zero-iteration guards.

| module | before | after |
|---|---:|---:|
| `test_manage` | 94 | 94 |
| `test_sync` | 60 | 68 |
| `test_diff` | 49 | 63 |
| `test_status` | 38 | 45 |

### The machinery landed first again, and this time it renamed things

Two bases, not one: the row above anticipated `TwoMachines` and missed
`support.Machine`, which `test_manage` takes for six classes. Both got B3's
treatment -- the dataclass is the definition, and the `unittest` class and the
`tests/conftest.py` fixture are adapters over one contextmanager -- landed as
its own commit with no test module touched and the suite green at 1861 either
side.

**The definitions took the good names and the adapters were renamed**, which
is the one decision here worth arguing rather than recording:
`support.Machine` → `MachineCase` and `support.TwoMachines` → `TwoMachinesCase`,
matching the `SandboxCase` suffix already in the file, and the free function
`two_machines(into)` → `copy_template(into)`, which is what it does.

The alternative was to give the dataclasses odd names and touch nothing --
24 call sites cheaper, in modules this cluster otherwise does not open. It was
rejected because the *asymmetry* is what rots: with `Machine` meaning the
dataclass in one module and the `TestCase` in another, the next person to write
`support.Machine` gets whichever one their file happened to import. B4b deletes
both classes, so the rename is 24 lines that die in the next PR rather than a
name anybody has to live with.

### The tool this cluster reused had a defect, and only a test found it

`tools/unassert.py` came out of B3 with its safety property tested: an unknown
method, a wrong arity and an unbalanced expression each come back byte-for-byte
and reported. That property held. **The tool changed a claim anyway.**

Joining a multi-line call onto one line was `" ".join(arg.split())`, which does
not know a string literal from an expression:

```python
self.assertIn("host  .gitconfig", done.stdout)   # two spaces, a padded column
assert "host  .gitconfig" in done.stdout         # what it should have written
assert "host .gitconfig" in done.stdout          # what it wrote
```

Nothing was refused, because the call *was* recognised. The rewrite reads
correctly, passes `ruff` and `mypy`, and fails later against real output --
which is how it was found, three modules in, looking like a bug in `status`'s
column padding.

**The lesson is about where the safety property was drawn.** B3's argument for
committing the tool was "it cannot silently half-convert", and that was true and
insufficient: the hazard it left open is *fully* converting into a different
claim. `flatten` collapses whitespace outside strings only now, `_scan` was
already the one thing that knows where a literal ends, and
`TestWhitespaceInsideAStringSurvives` has four cases -- all four go red against
the old spelling, verified by putting it back.

The audit that followed is the part worth copying: every string literal holding
a run of two or more whitespace characters, in all nine modules the tool has
ever touched, compared before and against after. **One**, and the suite had
already caught it. B3's five files are clean.

### What the fixtures cost, and what they removed

Nine module-local fixture objects, all `@dataclass(frozen=True)` subclassing
either `support.Sandbox` or `support.TwoMachines`: `Shapes` and `Synced`
(`test_status`), `Synced`, `Paged` (`test_diff`), `Edited` (`test_sync`),
`Hosts`, `Keyed` (`test_manage`), plus plain fixtures where the object needed no
methods -- `started`, `ready`, `managed`, `holding`, `unshared`, `initialised`,
`root`.

Three notes worth keeping:

- **`test_manage`'s `TestTheExitStatusEachCommandReturns` wanted a machine that
  had *not* run `init`**, because the status `init` returns is what it asserts.
  Mapping it to the same fixture as its neighbours turned three of its four
  tests red at once -- loudly, which is the good case, and only because those
  tests call `init` themselves rather than depending on it having happened.
- **`test_sync`'s two review classes had identical setups**, and their bounds
  did not: `synced()` armed `support.PATIENCE` and `stored_it()` armed nothing.
  One fixture object now carries both helpers and both go through the bound,
  which is CLAUDE.md's fourth "where to arm it" lesson -- a bound around one
  call reads as though it covered the class.
- **`self.addCleanup((home / ".bashrc").unlink)` after `os.mkfifo`, twice,
  is simply gone.** The fixture's own `tempdir` removes the tree, and a fixture
  is one test; `rmtree` unlinks a fifo like any other non-directory. Said here
  rather than left as a silent deletion.

`test_sync.py` and `test_manage.py` each carried a mid-file
`if __name__ == "__main__": unittest.main()`, both dead since Phase A2. Deleted,
which is the same note B6 has for `test_mutate.py`'s.

### The gate found a hole in the verdict layer, and it is Phase B's, not B4a's

The first `tupferl/` sweep came back **1179 caught, 26 survived, 104 `BROKE`**
against B3's 1283 / 26 / 0 -- the survivor set identical row for row, and 104
rows that had been answers reduced to nothing. Every one of the 104 was a
`setup failed`, and every one was blamed on a module this cluster converted.

The cause is a line `tools/verdict.py` drew by *phase*:

| where the assertion lives | pytest phase | was |
|---|---|---|
| `unittest`'s instance `setUp` | ``call`` -- pytest runs it inside `runtest` | `noticed` |
| the same code as a function-scoped fixture | ``setup`` | **`broke`** |

So a fixture asserting its own precondition -- "the template's `init` failed" --
stopped being an answer the moment the module converted. A `BROKE` row is never
`caught`, so 104 lines of `tupferl/` read as guarded by nothing, from a change
that weakened no test.

**Phase cannot tell the two apart**, which is why the original rule was written
that way and why it was not simply wrong: pytest reports a session-wide
fixture's failure as the ``setup`` phase of every affected test, exactly as it
reports a per-test one. What separates them is *scope*, and
`pytest_fixture_setup` carries it. The rule is now: a **function-scoped**
fixture failing is that test noticing; `setUpClass`, `setUpModule`, their
teardowns and any wider fixture stay `broke`, with their existing tests
unchanged and still green.

Three things worth carrying to B4b, B5 and B6:

- **This was always going to happen at whichever cluster first converted a
  fixture that asserts.** B1 and B2 had no fixtures; B3's did not assert. It is
  not a B4a defect and the remaining clusters would each have met it.
- **It is invisible without a baseline to compare against.** 104 `BROKE` rows
  read as a harness having a bad day, which CLAUDE.md already records as their
  characteristic failure -- a `BROKE` row appears in neither of the two numbers
  a reader looks at. What made it legible was that B3's whole-package report
  existed and said 0.
- **`tests/test_verdict.py` gained the row it was missing**, in
  `TestWhatThisAssumesOfPytest`: that pytest calls `pytest_fixture_setup` for
  every fixture, lets a wrapper see the exception, and that `FixtureDef.scope`
  is the string the rule reads. Neither is a documented guarantee; both are
  measured against pytest 9.1.1. A release that moved either would send every
  function-scoped failure back to `broke` silently.

### Gate

Preflight: **1898 tests, 0 failures, 0 skipped**, from 1861.

`tupferl/` has not been touched since 2026-08-29, so B3's whole-package report
is an exact row-for-row baseline. **1309 rows in 279s at 37 lanes:**

| | B3 baseline | B4a |
|---|---:|---:|
| caught | 1283 | **1283** |
| survived | 26 | **26** |
| `BROKE` | 0 | **0** |
| `TIMEOUT` | 0 | 0 |

**Zero newly-surviving, zero newly-`BROKE`**, and the survivor *set* is
identical row for row rather than merely the same size.

Two `tools/` files changed as well, so `--base main` swept every changed source
line in the diff: **33 of 33 caught**, including all 20 rows of
`unassert.flatten` and the new `verdict.pytest_fixture_setup`.

**`tools/unassert.py` swept whole reports 21 survivors and 9 `BROKE`, and none
of them is this PR's.** They are on `_scan`, `convert` and `main` -- lines B3
wrote, which B3's gate could not have covered because it was `--only tupferl/`.
Said here rather than filed: the file dies with Phase C, and an issue closed by
a deletion is the backlog §4 warns about. Whoever sweeps `tools/` whole before
then should expect them.

### What `/simplify` found, after the PR was open and CI was green

Four reviewers over the diff. **Two of the findings were defects rather than
polish, and one of them was in code written earlier the same day** -- which is
the row of CLAUDE.md §3's table this cluster belongs in, and the argument for
not reading a quality pass as a formality.

**The scope latch (`tools/verdict.py`).** The fix above records the scope of a
fixture that raised, keyed by nodeid. It was *read* at every non-``call`` phase,
so once a test's own function-scoped fixture had failed in ``setup``, any later
failure of that test was credited too. Measured before the fix, on a class-scoped
fixture raising after ``yield``:

```
noticed: ['test_a.py::TestX::test_it', 'test_a.py::TestX::test_it']   # twice
broke  : []
```

and the flattering pair -- a function-scoped fixture hitting `MemoryError`,
which `_carrier` correctly refuses to credit, with a class teardown falling over
behind it -- came back with a killer nodeid for a row where nothing asserted
anything. That is the direction `tools/verdict.py`'s own docstring says every
bug in its class has erred. The scope is a fact about one *phase* and is read as
one now.

**The finder (`tools/unassert.py`), and the root beneath it.** The module
docstring claims the tool "finds a call by scanning balanced parentheses, not by
matching a regex". That was true of `close`, `split_args` and `flatten`, and
never of `CALL.search`. Chasing it found something worse: **`_scan` did not skip
comments**, so a `#` comment holding an odd number of quote characters -- "the
suite's", which is how English is written -- opened a string that never closed
and inverted every judgement after it. Measured on
`tests/test_verdict_unittest.py`: **12,379 characters of real string content
read as code**.

The consequence was concrete and aimed squarely at B6:

| module | string literals the tool would have rewritten | converts in |
|---|---:|---|
| `tests/test_unassert.py` | 31 | done |
| `tests/test_run_tests.py` | 6 | B6 |
| `tests/test_verdict.py` | 4 | B6 |
| `tests/test_verdict_unittest.py` | 4 | never |
| `tests/test_mutate.py` | 3 | B6 |

Every one is the source of a *probe module*, written as a literal precisely so
the harness can drive a `unittest`-style test. Rewriting one changes what the
harness is being driven with. **Zero, across every remaining module, after the
fix** -- and `_scan` skipping comments also stops a bracket or a comma inside a
comment being counted, which `close` and `split_args` were both exposed to.

A comment *inside* a call is now refused rather than flattened: it cannot
survive being joined onto one line, and dropping it would delete something a
person wrote.

**Precedence (`tools/unassert.py`).** `bracket` encoded the right rule --
"an unnecessary pair of parentheses is noise, a missing pair is a changed
assertion" -- and was wired to 1 of the 15 forms. The other fourteen spliced raw
argument text around `==`, `in` and `<`. The rule is `ast`'s now and applies to
all of them; it changes four real rewrites, all in modules B6 converts:

```
assert cache.cost == (found.times or {})           # was: == found.times or {}
assert "…" in (both[0][1] if both[0] else "")      # was: in x if c else ""
```

All four were equivalent **by luck** -- `{}` is falsy, and `X if C else ""`
fails both readings when `C` is falsy -- so this was exposure rather than a live
defect, and is recorded as such. The one that would not have been lucky is
`assertEqual(a == b, c)`, which spliced bare is the *chained* comparison
`c == a == b`.

**Efficiency was measured clean.** Interleaved A/B/A/B/A/B of the four converted
modules: 14.8 s either side, median paired difference **+0.5% for 29 more
tests** -- per-test cost down about 10%. The `pytest_fixture_setup` wrapper
costs **~0.67 µs per fixture setup** over a synthetic 60,000-setup table, which
is ~0.45 ms on a 14.7 s run. `Watcher.scopes` holds one entry per *failing*
setup, and a probe runs under `-x`.

**Declined, with reasons.** A `Computer.must()` collapsing the nine
`status, x = say(...); assert status == 0` pairs would reach `tests/support.py`
and four modules this cluster does not open. The `synced` fixtures in
`test_status` and `test_diff` share a five-line prelude but carry different
methods and different preconditions, and the duplication is carried from `main`
rather than introduced. `Hosts` re-implementing `support.Computer` is real and
pre-existing; only the false docstring claim beside it was fixed here (§0), and
it was false in a checkable way -- `Computer.__init__` asks `paths.repo_dir()`
under a patched environment, which the docstring said could not be done.

**Applied:** the two defects above, plus `added()` deleted (it was
`spoken("add", ...)`), `unshared` and `ready` derived from the fixtures they
repeated, `initialised` built off `machine` rather than re-making a remote,
`MachineCase`'s dead `log`/`stored` delegates removed, `fingerprint` hoisted to
`support.py` (`test_diff`'s `contents` was the same walk with a weaker
projection, and both docstrings named the same trap), `START` aliased to
`support.STARTS_AS` in both modules -- they were byte-identical to the template
they must match -- and two inline re-implementations of `Synced.diff` replaced.

**And the review's own new lines were swept, which found five more.** The
`--base main` run over them came back 76 caught, 4 `SURVIVED`, 1 `BROKE` --
every one in code written an hour earlier, which is the argument for the sweep
going *last* rather than for it being a formality.

The `BROKE` had a root worth removing. `_scan` skipped a comment by `find`ing
its newline, and `str.find` answers `-1` for "not there" -- which is exactly a
comment on the last line of a file with no trailing newline, one of this
module's own fixtures. Any mutation mishandling the sentinel assigned `i = -1`
and the loop ran backwards for ever. It is a flag and `i += 1` now, the way a
string already is, so `i` only ever increases. **CLAUDE.md's `RLIM_INFINITY`
lesson in a second spelling: a sentinel is not a number.**

That moved the hang rather than removing the class of it -- every `i += 1` in a
scanner loop is an infinite loop when mutated, and the file already carried 9
such rows from B3. An autouse module bound at `support.bounded(5.0)` makes those
tests *fail* instead, on a module that runs in 0.04s. On the module rather than
a class, which is CLAUDE.md's five "where to arm it" lessons taken together.

Of the survivors, one was a second `if not why:` a mutation could make
unconditional; it is an `else` now. One was `bracket`'s SyntaxError arm, which
no whole call can reach -- every argument of a call that parses is an expression
that parses -- so it is driven directly. Two were the arity window, which
**nothing was reaching**: the wrong-arity fixture uses `assertRaises`, which is
not in `FORMS`, so it stopped at "no rule for it" one branch earlier. And two
were parity: a comment is skipped one character at a time, so a mutation
stepping *two* lands on the newline or steps over it depending on the comment's
length, and one 17-character fixture answers for exactly one parity. Four
lengths answer for both.

**One equivalent survivor is tagged**, and one process failure is recorded with
it: the parity test appeared to fail and had not. Writing the file, running
`ruff format` and running pytest inside one second is the stale-`.pyc` gotcha
CLAUDE.md already carries -- and the probe that then "caught" an equivalent
mutation was reading the same stale bytecode, which is the flattering direction.
Every verdict here was re-taken with `__pycache__` cleared between runs.

Final gate: the `tupferl/` package unchanged at **1283 caught / 26 survived / 0
`BROKE`**, survivor set identical to B3 row for row; `--base main` over every
changed source line **95 of 96 caught, 1 excused by a tag, 0 `BROKE`**.

Preflight after: **1915 tests, 0 failures, 0 skipped.**

## B4b as built — 2026-08-31

Four modules, 2228 lines in and 2507 out, 37 classes, **408 assertions** --
comparable to B3's 506 and the last cluster of end-to-end sync tests. 127
collected items before, 132 after; **every one of the 127 distinct test names
survives**, and the growth is two `subTest` loops becoming `parametrize`.

| module | before | after |
|---|---:|---:|
| `test_sync_cli` | 42 | 47 |
| `test_sync_commits` | 40 | 40 |
| `test_sync_conflicts` | 26 | 26 |
| `test_overlays` | 19 | 19 |

**This is the cluster that deletes machinery rather than adding it.**
`SandboxCase`, `MachineCase` and `TwoMachinesCase` each carried a sentence
saying they die when their last user converts; that user was here, and all
three are gone -- 117 lines, in their own commit, with a grep across `tests/`,
`tools/` and the docs behind it rather than an assumption. What is left is the
extraction B3 and B4a did *for* them: `Sandbox`, `Machine` and `TwoMachines`
are the definitions and `tests/conftest.py` is the one remaining adapter.

Their docstrings are not deleted with them. B4a's rename argument -- "B4b
deletes both classes, so the rename is 24 lines that die in the next PR" --
is now a claim about the past, and each of the three places that made it says
what happened rather than being quietly cut, which is §0's rule applied to a
paragraph that has come true.

### 20 fixtures, and the one shape that repeats

Twenty module-local fixtures where there were twenty `setUp`s, plus four frozen
dataclasses over a `support` base where the `setUp` had methods beside it:
`OneMachine` (`test_sync_cli`), `Conflicted` (`test_sync_conflicts`),
`TwoCommits` and `Victimised` (`test_sync_commits`), and `Merging`, which
subclasses `Sandbox` rather than `TwoMachines`.

`Victimised` is the one worth naming: two fixtures in `test_sync_commits` set
`self.victim` -- a file **outside** the repository that a settled symlink could
have been written through -- and a frozen dataclass has nowhere to put it. One
field on one shared subclass, rather than a second dataclass per fixture.

Three method-to-function conversions, all in `test_sync_commits`, and all of the
same kind: a helper that read `self` only to reach the box.

- `raising(kind)` and `breaking(box)` are `@contextlib.contextmanager`s where
  they were `addCleanup` pairs. That is not cosmetic:
  `test_the_next_sync_still_works_afterwards` used to call `self.stack.close()`
  half way through to say "and now the settler works again", which is a `with`
  block ending exactly there.
- `collide(box, name)` takes what it acts on.

Four class attributes became module constants (`LINK`, `SWAPPED`, `TWIN`,
`SHARED`), because neither a `parametrize` decorator nor a fixture can see a
class attribute, and `test_sync_cli`'s `THERE` had to move for exactly that
reason when `test_every_line_names_the_ref` was parametrized.

### `assertContains` is gone, and it was `SandboxCase`'s only real method

Eleven call sites, all in `test_sync_cli.py`, and all now
`assert needle in haystack`. The helper's docstring argued that `assertIn`
"prints both sides, which for a multi-line report is a wall of text with the
interesting part in the middle" -- true of `unittest`, and pytest's assertion
rewriting prints the same two sides with the needle in the failure line. The
helper was work pytest already does.

`tools/unassert.py` refused all eleven by name (`assertContains: no rule for
it`), which is the property B3 committed it for, and the two `assertRaises`
with them. **13 of 408 left for a person** -- and, as in B4a, every defect this
cluster found came from a step the tool cannot check.

### Two dead entry points, and the second one was hiding between two classes

`test_sync_conflicts.py` and `test_sync_commits.py` each carried a mid-file
`if __name__ == "__main__": unittest.main()`, dead since Phase A2 -- the same
note B4a wrote for `test_sync.py` and `test_manage.py`, and B6 still has for
`test_mutate.py`. Both are deleted.

`test_sync_cli.py` also carried `NAME = PurePosixPath(".bashrc")` and its
import, read by nothing before this change either. Fixed here because the diff
already rewrote the block it was in (CLAUDE.md §4), and said out loud because a
silent deletion of an unused constant is indistinguishable from one that had a
user the grep missed.

### One thing ruff caught that the suite could not

`said(machine, *args)` was a method on `TestWhatSyncSaysAboutTheRemote` and is a
module-level function now -- and five of its callers assign to a local called
`said`. In a method that shadowed nothing; at module level it makes the *name*
local to the function, so `said = said(...)` reads an unbound local and raises.

ruff's F823 named all five before anything ran. Worth recording because the
class of mistake -- a method becoming a function, and its callers' locals
colliding with the new global -- is one every remaining cluster can make, and
the failure would have been an `UnboundLocalError` in a test whose text gives no
hint why.

### Gate

Preflight: **1920 tests, 0 failures, 0 skipped**, from 1915.

**No file under `tupferl/` or `tools/` is touched by this PR**, so `--base main`
generates no rows at all and the whole-package sweep is the entire acceptance
instrument -- which is the right shape for a pure test conversion, and worth
saying because a `--base` run reporting "0 rows, 0 survivors" would otherwise
read as evidence.

`tupferl/` has not moved since 2026-08-29, so B3's and B4a's reports are exact
row-for-row baselines. **1309 rows in 279s at 37 lanes**, baseline green:

| | B3 | B4a | B4b |
|---|---:|---:|---:|
| caught | 1283 | 1283 | **1283** |
| survived | 26 | 26 | **26** |
| `BROKE` | 0 | 0 | **0** |
| `TIMEOUT` | 0 | 0 | **0** |

The survivor *set* is identical to **both** earlier runs, label for label, not
merely the same size -- checked as a set difference in each direction rather
than by comparing the counts, which is the check that would have missed a
survivor swapping for another.

Two numbers worth keeping beside it: the heaviest lane held **556 MiB of its
2064 MiB ceiling (27%)**, against the 92% B4a's whole-tree figure records, and
the verdict-layer fix B4a landed is what keeps the 104 rows it recovered in the
`caught` column here -- this is the first cluster to convert fixtures *with*
that fix already in place, and it is the run that shows it holds for a second
cluster's worth of them.

### What `/simplify` found, after the PR was open and CI was green

Four reviewers over the diff. **The most useful finding was a number, not
code**, which is the second cluster running to say so.

**Two counts in `CLAUDE.md` were wrong, and this PR wrote one of them.** The
`usefixtures` entry said B4b converted "36 classes, which carry 35 marks"; the
tree has **37 and 36**. The arithmetic was right and the base was wrong -- and
the paragraph's own last sentence had already said the plan's status line is the
number to read "rather than a count kept here", two lines below a count kept
there. Both the count and the hand-written list of what B5 and B6 still convert
are gone; `tests/test_pytest_plan.py` recomputes the status line from the tree
and nothing recomputes a figure typed into `CLAUDE.md`.

The second was older. #19's entry says "146 of the suite's tests take"
the two-machine fixture -- true when it was measured and **190 of 1920** now,
across 45 classes rather than 40. This PR *edited that sentence* to drop its
`TwoMachinesCase` half without re-checking the figure beside it, which is §0's
rule missed inside the edit that §0 required. Re-measured with
`pytest --collect-only --fixtures-per-test`, dated, and corrected in all four
places that hand-copy it (`CLAUDE.md`, `tests/conftest.py`, and twice in
`tests/support.py`).

**A sixth `said` local.** The F823 section above records five call sites where a
method becoming a module function collided with a caller's local. There was a
sixth, in `TestTheRemoteLine`, which ruff does not flag because that method never
calls the global -- so the collision is inert until somebody adds a call. Renamed.
The lesson is narrower than the original one and worth having: **ruff finds the
collisions that already break, not the ones that are merely armed.**

**`subject()` was a fourth spelling of `Machine.log()`.** The helper was carried
faithfully from a method, and one of its three call sites still inlined the
`git log -1` *and* bound the result to a local called `subject`. It reads
`box.log()[0]` now, keeping the name because `log()[0]` asks the reader to know
which end is newest.

Two smaller ones: `test_sync_cli` retyped the template's own bytes where
`support.STARTS_AS` exists -- the call B4a made for the identical literal in
`test_diff` and `test_status`, so `test_overlays`' `SHARED` is aliased too -- and
two comments in `test_overlays` still said `setUp` in a file that has none.

### Declined, and why

- **Reverting `test_the_resolution_flags_are_accepted_when_there_is_no_conflict`
  to a loop.** Measured at 0.51s against ~0.29s: three fixtures rather than one,
  paid again for every mutant. Declined because the parametrized version is
  *stronger*, which the measurement does not show. `one_machine` leaves an
  `add`'s commit unpushed, so in a loop only the first flag met a sync with
  anything to do -- and `test_a_second_sync_writes_no_commit`, beside it, is the
  proof that a second run reports "0 changed". Three fixtures is three first
  syncs, one per flag. Written into the test.
- **Building `crlf` on `two_machines` instead of on `conflicted`**, saving one
  `diverge` (~60ms x 2 tests). The stacking is what `main` did and the end state
  is what the tests assert about; re-deriving the fixture is a redesign, and this
  cluster's rule is convert rather than redesign.
- **Folding `two_commits` and `executable` into one helper with a flag.** They
  are three lines each and the flag is the whole difference; a helper serving
  exactly two callers is not fewer lines and puts the `executable=True` a level
  away from the fixture that means it. The duplication is also deliberate --
  `main` spelled it as `support.TwoMachinesCase.setUp(self)`, skipping the base
  on purpose.
- **Hoisting `assert box.second.call("init", str(box.remote)) == 0` onto
  `TwoMachines`.** 29 occurrences across `tests/`, and `diverge`'s docstring
  already records the hazard of forgetting it. Real, and not this PR's: the count
  per file is identical to `main`, and it binds `test_diff`, `test_status` and
  `test_sync_properties` as well. A separate change.

### The gate, re-run after the review

The review edited tests, so every verdict above it was computed against a tree
that no longer exists -- CLAUDE.md §1's reason for putting a generated analysis
last. Re-run whole: **1309 rows in 278s, 1283 caught / 26 survived / 0 `BROKE` /
0 `TIMEOUT`**, baseline green, and the survivor set identical to B3's, B4a's and
this cluster's own pre-review run, label for label.

## B5 as built — 2026-08-31

Four modules, 2314 lines in and 2735 out. 147 collected items before, 211
after; **every one of the 147 distinct test names survives**, and the growth is
eight `subTest` loops becoming `parametrize` plus five new tests, all of them
guards the review asked for.

| module | before | after |
|---|---:|---:|
| `test_support` | 40 | 61 |
| `test_paint` | 22 | 59 |
| `test_watch` | 55 | 61 |
| `test_reached` | 30 | 30 |

Preflight: **1984 tests, 0 failures, 0 skipped**, from 1920.

### The cluster's real subject was two constants, and there were six

The B5 row asked for "`test_watch`'s bound-vs-alarm numbers (the 30s trap)
re-checked against `bounded`". They were wrong. `BOUND = 20` there and in
`test_reached` beats the *default* alarm and nothing else -- `--each-test` is a
flag, so at `--each-test 10` a 20s bound sits back above a 10s alarm, the two
race, the alarm wins, and the row is filed `BROKE`, which is never `caught`. It
was sitting in the file whose own docstring records the seven `main`/`alive`
rows it cost the first time.

Both were routed through `support.bounded`, a two-entry tuple named them, and
**the review found four more the tuple could not see**:

| | |
|---|---|
| `tests/test_mutate.py:60` | `BOUND = 20`, reached at `timeout=wait` through a parameter default |
| `tests/test_verdict.py:122` | `timeout=30` |
| `tests/test_verdict.py:179` | `timeout=60`, reached from a test body |
| `tests/test_verdict_unittest.py:126` | `timeout=30` |
| `tests/test_watch.py:738, 777` | `communicate(timeout=10)` -- in the file being fixed |

`test_mutate.py`'s is the one that settles the design question. Three screens
below that constant its `collect` docstring reads *"that is the third instance
of one mistake here"* and counts the other three by hand. **Counting instances
in prose is what a person does instead of a check**, so the check is written:
`test_support.TestEveryWaitOnAChildIsBounded` walks every `timeout=` handed to
`run`, `Popen`, `communicate` or `wait` under `tests/`, follows a name to what
it was assigned -- parameter defaults included -- and insists it reaches
`support.bounded`. 21 sites today, with a `FLOOR` under the count for
`tests/test_errors.py`'s reason.

Two decisions in its shape are worth keeping. Asking *what is being called*
keeps `argparse.Namespace(timeout=60.0)` out with no exception list. And the one
shape it must let through is structural rather than listed: a `timeout=` inside
`with pytest.raises(...)` is the assertion, which is what
`running.wait(timeout=0.5)` is.

Verified by reverting all four sites at once: `4 more items, first extra item:
'test_mutate.py:905 timeout=wait'`.

### `tests.support` is imported here, and it is not free

`bounded` lives in `tests/support.py`, so `test_watch` and `test_reached` import
it -- and `support` imports `tupferl.paths`, `manifest` and `__main__`, while
`tools/mutants.py`'s index is transitive. So those three sources' sweep
selections gained two modules. Measured, one run each rather than an interleaved
pair: `--only tupferl/` went **278s to 298s**.

The altitude review's deeper fix is a leaf module holding `ALARM`, `SHARE`,
`bounded`, `Spill` and `Screen`, which `support.py` re-exports -- `tests/profiles.py`
is the precedent. **Not done here**: it moves five names every converted module
already reads through `support`, and B6 converts the four modules that would
have to move with it. It is the right change to make *with* B6, not before it.

The same import let `test_watch` drop a nested `Screen` for `support.Screen` --
two copies of "a `StringIO` that claims `isatty`", one of them a paraphrase of
the other's docstring.

### `tools/unassert.py` was wrong about these two files, harmlessly

Its docstring claims "argument order is `actual == expected`". That is an
assumption about the *file*: it flips `assertEqual(a, b)` to `b == a`, right for
this repository's `(expected, actual)` convention and backwards for
`test_reached.py` and `test_watch.py`, which are `martinus/woswoar` ports written
the other way round. The output was yoda -- `assert 1 == split.total`.

Nothing changed meaning, and `ruff --fix` (SIM300) put **27 of them back in
`test_reached.py` alone**. It does not put all of them back: a dict, set or list
literal on the left is not a SIM300 constant, so four survived and were flipped
by hand. The tool now says to expect a ruff pass *and* a hand pass, and to check
which convention the file uses before reading the diff.

### The conversion mistake this cluster made, and what found it

The structural rewriter added a fixture parameter to any test whose body
*mentioned* the fixture's name -- and `running` is an ordinary English word, so
**five tests in `test_watch.py` took a fixture they never touch**, each spawning
a real `time.sleep(30)` child, blocking on a byte from it, then killing and
reaping it. None of the five called `self.running()` on `main`.

Nothing failed. It was found by an `ast` pass over the four modules asking which
test parameters are never read -- the same check that found B5's other
conversion defect, two locals named `watcher` shadowing the new module-level
`watcher()`, which is B4b's sixth-`said` lesson arriving in a new file. **Run
both passes on every remaining cluster**: unread parameters, and locals that
shadow a name the conversion moved to module scope.

The one unread parameter that is *correct* is `test_support`'s poison, and it is
CLAUDE.md's rule rather than an exception: it went onto the class as
`@pytest.mark.usefixtures("poisoned")`, because a test that reads the patched
environment and names no fixture would otherwise get none.

### Gate

`tupferl/` has not moved since 2026-08-29, so B3's, B4a's and B4b's reports are
exact row-for-row baselines.

**1309 rows in 297s at 36 lanes**, baseline green:

| | B3 | B4a | B4b | B5 |
|---|---:|---:|---:|---:|
| caught | 1283 | 1283 | 1283 | **1283** |
| survived | 26 | 26 | 26 | **26** |
| `BROKE` | 0 | 0 | 0 | **0** |
| `TIMEOUT` | 0 | 0 | 0 | **0** |

The survivor *set* is identical to all three, label for label, checked as a set
difference in each direction rather than by comparing counts.

**And a second gate this cluster needs and the earlier ones did not.** These
four modules are the killers for `tools/paint.py`, `tools/reached.py` and
`tools/watch.py`, which the `--only tupferl/` table does not reach. Their source
is byte-identical to `main`, so the control is a sweep of a `main` worktree,
same machine, run back to back:

| | `main` | branch |
|---|---:|---:|
| rows | 211 | 211 |
| caught | 197 | **198** |
| SURVIVED | 10 | **9** |
| `BROKE` | 4 | 4 |
| baseline | green | green |

**Zero newly-surviving and zero newly-`BROKE`**, compared on
`(path, line, operator, label)` -- 209 comparable keys, identical sets. Exactly
one row moved, and it moved the right way: `tools/watch.py:446`'s
`drop-call` went `survived` to `caught`, which the sweep reported as a **spent
tag**.

**The tag stays, and its own text says why.** It reads "Asserting that a process
*idled* means timing the watcher, which is the flakiest assertion this suite
could hold" -- and Phase A's evidence records this same row flipping
`survived -> caught` once before. A tag removed on one run of a row that is
known to flip would turn the next flip into a survivor with no disposition. That
is the loud failure, so it is not a disaster; it is also not an improvement
anybody measured.

**No `tupferl/` or `tools/` source is changed semantically** -- the only diff
under either is twelve lines of `tools/unassert.py`'s module docstring, which
generates no mutants -- so `--base main` would report nothing and these two
whole-file sweeps are the entire acceptance instrument.


### One macOS leg failed, and it was not this cluster

`test_mutate.py::TestTheHarnessAnswersBothWays::test_the_walk_catches_what_the_selection_missed`
tripped its 12s `NESTED` bound on the `macos` leg, once. Diagnosed before
re-running, because a re-run that passes proves nothing on its own:

- `tools/verdict.py` sorts the walk and it stops at the first module that
  notices, which is `tests/test_config.py`. All four of this cluster's modules
  sort after it, so however much slower they got, that test never runs them.
- Measured: the test is **1.33s on `main` and 1.38s on the branch**, two runs
  each.
- The identical branch content was green on `macos` one commit earlier, and
  `main` is green on its last fifteen runs.

The re-run passed. What is left is a real but pre-existing weakness -- a 12s
bound against a 0.66s honest wait *on this machine*, and a macOS runner is not
this machine. Filed rather than tuned blind (§5): raising it without knowing the
runner's honest wait would be a fix built on the wrong mechanism.

## B6 as built — 2026-08-31

Four modules, 11738 lines in and 11802 out. 663 collected items before, 761
after; **every one of the 663 distinct test names survives**, and 665 distinct
names come out — the two new ones are the companion guard a computed
`parametrize` needs, one per computed list. The rest of the growth is seventeen
`subTest` loops expanding into a case each.

| module | before | after |
|---|---:|---:|
| `test_run_tests` | 70 | 85 |
| `test_mutants` | 112 | 166 |
| `test_verdict` | 75 | 75 |
| `test_mutate` | 406 | 435 |

Preflight: **2089 tests, 0 failures, 0 skipped**, from 2059.

### The assertions went first, and that commit is green on its own

`python -m tools.unassert` rewrote 1105 of 1126 `self.assertX(...)` calls and
refused 21 (19 `assertRaises`, 2 `assertRegex`). A plain `assert` reads the same
inside a `TestCase`, so that landed as its own commit with every class still
unconverted — which made the structural work a diff a person can read instead
of eleven thousand lines of both at once.

The tool's B5 warning held: `tests/test_mutants.py` is the woswoar port, written
`assertEqual(actual, expected)` against this repository's `(expected, actual)`,
so its output was yoda. `ruff --fix` (SIM300) put most back and **eleven dict
and set literals** it does not see were flipped by hand — found with `ast`
rather than by reading, because "literal on the left" is not something a regex
can decide once list comprehensions are in play.

### Two claims the runner change had already falsified

Neither was found by looking for stale claims. Both turned up because the
conversion touched the code they described, which is §0's argument for fixing
them in the same change rather than filing them.

**`TestABoundedCallStillReturns` said six `line_starts` rows could not be
answered.** The argument was that `TestChoosingTheTests.setUpClass` builds the
real import index — parsing every file in the repository — so a `line_starts`
that never advances hung *this module's own fixture* before any test in it ran,
and the per-test alarm could not help because `setUpClass` is not a test:
`TIMEOUT` at 300s rather than `BROKE` at 30.

Both halves had gone at **Phase A2** and nothing said so. `unittest` loaded
classes alphabetically, so `TestChoosingTheTests` ran before
`TestLineEndingsThatAreNotNewline`; pytest collects in definition order, where
it runs long after. And `verdict.py` arms its alarm in
`pytest_runtest_protocol`, which brackets setup as well as call.

Measured on `at += 1` becoming `at -= 1`, driven with the selection
`targets_for` generates and the `failfast=True` a sweep passes — CLAUDE.md's own
rule about a single-row reproduction:

| tree | verdict |
|---|---|
| this branch | **`caught` in 45s** |
| a `main` worktree, same machine | **`caught` in 45s** |

So the conversion did not repair it; A2 did. The killer is
`TestTheOperators::test_each_operator_fires_on_its_own_fixture`, near the top of
the file, which `failfast` stops at. That is one of the six rows — the one the
old claim named — and the gate answers the rest.

The same paragraph called `TestCappingTheTable` "in-process with no bound",
which stopped being true when that class gained its own `deadline`.

**`support.deadline`'s docstring prescribed `ExitStack` in `setUp`** because
`TestCase.enterContext` is 3.11 and this project supports 3.10. An autouse
fixture makes that question moot, and every one of the seven such `setUp`s is
now one.

### `parametrize` removes the `subTest` bound trap rather than guarding it

CLAUDE.md's fifth "where to arm it" lesson is *arm it inside the `subTest`*,
because `subTest` catches the `TimeoutError` a bound raises, records a failure
and carries on with nothing armed — so twenty operators cost twenty times the
bound, and `TestTheOperators` ran past 60s against a 30s alarm.

One case is one test under `parametrize`, so the class fixture arms the bound
afresh per case and `failfast` stops at the first that trips. **B6 met the trap
twice in one cluster** — `test_mutants.TestTheOperators` and `test_mutate`'s
`test_a_row_that_asked_nothing_is_excused_on_the_same_terms`, the second
measured past 120s under the very mutation its bound was written for — and both
lose their second copy of the bound.

There is no `subTest` loop left in the suite outside `test_verdict_unittest.py`
and the probe *fixtures* that use one on purpose, as the thing under test.

### `requires_git` is the one skip, and its polarity is the hazard

B3's row deferred it here because `test_mutants` is its only user. pytest has no
`skipunless`, so `skipUnless(shutil.which("git"))` becomes
`skipif(shutil.which("git") is None)` — **and the wrong way round it skips on
every machine that has git**, which is every machine this runs on: green,
silent, and testing nothing. The argument is written at the definition rather
than at the call, and `--no-skips` on the macOS leg is what would say so.

### Three bases, three fixtures, and one factory

`Tree` (`test_run_tests`), `Probe` (`test_verdict`) and `GeneratedTable`
(`test_mutate`) become plain classes built by fixtures. `Probe.fresh` keeps its
meaning by holding an `ExitStack`: a second sandbox made mid-test does not
remove the first, which is what makes "the second has not been written to" true
rather than merely likely.

`test_mutate` had **fifteen** copies of `Path(tempfile.mkdtemp(prefix=...))`
plus `addCleanup(shutil.rmtree, box, True)`, and `test_mutants` three more of a
variant. They go through a `boxes` factory over `support.tempdir` — a factory
rather than one directory per test, because several helpers build a tree per
call and are called twice in a test.

**That pair was not what it looked like.** Its `True` is `ignore_errors`, so a
delete that failed left the tree behind and said nothing, where
`support.tempdir` names what survived. CLAUDE.md's rule ("a test wanting a
throwaway directory uses `support.tempdir`, never `tmp_path`") is written
against `tmp_path`; this is the *other* thing it was competing with, and the
rule turns out to be right about it for a second reason nobody had stated.

### `test_mutate.py`'s mid-file entry point, and what it actually did

The B6 row said to delete it with a note. The note is worth more than the
deletion: `if __name__ == "__main__": unittest.main()` sat **6000 lines above
the end of the file**, so running the module directly defined the classes above
it, ran *those*, and exited — reporting `OK` over a fraction of the file with
nothing to say which fraction. That is the flattering green §8 collects, and
pytest has no entry point to put it back at.

### The two conversion checks B5 asked for, and what they found

Both were run on all four modules. **Both found real instances**, which is the
argument for B5's instruction to run them every cluster rather than once:

- **unread parameters** — `test_mutants`' fixture rename left one
  `self.targets("tupferl/sync.py", index)` that `ruff` could not see, because
  `ruff format` had already wrapped it across two lines and the rewriter's regex
  was single-line. It also found a pre-existing one:
  `TestABatchSweepEndToEnd.truncated` took a `box` it never read.
- **locals shadowing a name the conversion moved to module scope** —
  `TestPacking` binds `batch` in two comprehensions, and the helper hoisted out
  of `TestWhatABatchReports` was going to be called `batch`. It is `worker` now,
  which is also what it drives. Two locals named `index` in `test_mutants` sent
  the new module-scoped fixture to `import_index` for the same reason.

Left alone and named rather than fixed: **twenty-five locals called `row`** in
`test_mutate` shadow the module-level `row()` helper. That predates the cluster
(#76 introduced the helper) and every one is a JSON record in a comprehension,
so no call is wrong — but it is exactly the shape the check exists for, and
renaming either side is a change of its own.

### One tool bug, recorded because the tool is kept

The structural threader that gives a fixture to every method using it, and to
every caller of one, ran to a fixpoint — and its call-site rewrite matched
`self.sleeper(` inside `self.sleeper(request, ...)`, so it re-added the argument
on every round: `self.sleeper(request, request, request, ...)`, eleven deep,
and 447 such insertions across the file before the loop gave up. Collapsed with
a second regex and the tool fixed to use a negative lookahead.

Worth writing down because the failure was **loud** — it did not settle, and
would have failed `ruff` and `mypy` had it — while the same bug in a tool that
ran once would have been silent. A rewriter over the real tree should be
idempotent and should be run twice to prove it.

### Gate

`tools/` and the three `tupferl/` files whose sweep selection names one of the
four converted modules — computed from `targets_for`, not guessed. The control
is a `main` worktree on the same machine, and **the selections are
byte-identical between the trees**, checked in both directions, so it is an
exact row-for-row baseline rather than a remembered number.

| `--only tools/`, 2621 rows | control (`main`) | branch |
|---|---:|---:|
| caught | 2393 | **2394** |
| SURVIVED | 204 | 205 |
| `BROKE` | 13 | **11** |
| `TIMEOUT` | 11 | 11 |
| baseline | green | green |

`tupferl/__main__.py`, `manifest.py` and `paths.py`, 168 rows: **168 caught, 0
survived, 0 unanswered — on both arms**, with identical spent-tag reports.
`tupferl/__init__.py` generates no rows.

**The first branch arm failed this gate, and the failure is the useful part.**
It came back 2379 caught, 25 `BROKE`, with **42 rows moved**. What follows is
what each was, because a gate whose numbers matched first time would have said
less than this one did.

#### The comparison itself was wrong twice before it was right

Keyed on `(path, line, operator, label)`, 2621 rows collapse to **2542 keys** —
77 keys occur twice, which is the collision CLAUDE.md records killing the old
hash-keyed survivor record. Keyed on *position* is no better: `slowest_first`
orders by recorded cost and the two arms have different machine-local caches,
so **2453 of 2621 positions differ**. Counting outcomes **per key** needs
neither and is exact.

#### Eight rows were a pre-existing hole, and they are fixed

`line_starts`' counter and `cap`'s round-robin are the two loops a one-line
mutation makes infinite, and `test_mutants` bounds the classes that call them
**directly**. Three classes reach them another way and carried no bound:
`TestEveryTagGuardsARowThatExists` and `TestFindingATagNoRowCanReach` through
`dead_tags`, which walks every mutable file and generates for each, and
`GeneratedTable` through `mutate.generated`. Every one of the eight rows named
one of those three as its killer.

**Unbounded on `main` too**, verified in the control worktree — so which arm
reported `BROKE` depended only on which route the sweep tried first, and the
arms have separate caches. CLAUDE.md's most-repeated lesson for the sixth time:
*the killer a sweep reports is one route to the line, not all of them.*

Bounded through `support.bounds`, one line per class. Re-swept
`--only tools/mutants.py`, 635 rows: **600 caught, 35 survived, 0 `BROKE`, 0
`TIMEOUT`** — where the first branch arm had 8 `BROKE` in that file.
`GeneratedTable` carries it on the base so both subclasses inherit it, proved
rather than assumed: a spy plugin reading `ITIMER_REAL` in
`pytest_runtest_call` finds 5.0s armed in **6 of 6** subclass tests, against an
honest wait of 0.02s per test.

#### Everything else was settled by re-running rows one at a time, on both trees

Driven with the selection `targets_for` generates and the `failfast=True` a
sweep passes — the arguments a sweep uses, because CLAUDE.md records four rows
reported fixed on a hand-driven `run` that used `run`'s own defaults instead.

| rows | branch | `main` | what it was |
|---:|---|---|---|
| 7 `caught → survived` | 3 caught / 8 survived | **identical** | the outward walk reaching past the selection |
| 17 unanswered | 15 caught | 16 caught | unanswerable *under a sweep*, not unanswerable |
| 3 newly unexcused | 3 caught | 3 caught | same |
| 1 (`_Lanes._sample`) | caught/caught/broke | broke/broke/broke | unstable on both trees |

The seven survivors were killed on `main` by tests **outside their selection**
— `test_watch.py` killing a `tools/mutants.py` row, `test_manage.py` killing a
`tools/mutate.py` row whose selection is `tests.test_mutate
tests.test_packaging tests.test_profiles tests.test_support`. How far the walk
gets depends on ordering and on what else is in flight, which is the mechanism
this plan's dead-end section records producing **24 false `caught` verdicts,
reproducibly**.

`tools/mutate.py:1211` in `_Lanes._sample` is the one row that looked
branch-specific on a single reading and is not: interleaved A/B/A/B/A/B, the
branch answered it twice and `main` never. It carries no `# survivor:` tag,
which is [#109](https://github.com/martinus/tupferl/issues/109).

#### One improvement is B6's own

`tools/mutants.py:1576` in `_imported()` went **broke → caught**. On `main` it
failed as `setup failed -- AttributeError` inside
`TestChoosingTheTests.setUpClass`; this cluster made that a module-scoped
fixture, which pytest enters inside the first requesting test's protocol — so
the failure is now an answer. Four `_lane` rows and `Work.take` also recovered,
which is what #96 predicted once those probes ran on an idle machine.

#### And the gate found something in the harness itself

Chasing a spent-tag report — `manifest.py:335`'s row "now caught, so the tag is
spent", inviting deletion of a disposition whose equivalence argument is still
correct — turned up what caught it: `TestEveryTagGuardsARowThatExists`, which
calls `dead_tags(REPO_ROOT)` where `REPO_ROOT` inside a probe is the
**mutated** sandbox. Any mutation on any tagged statement changes which
operators that statement generates, the tag reads as dead, and the test fails
without asserting anything about the line.

Measured over the control arm plus the `tupferl/` subset: **295 of 2789 rows —
10.6% —** record one of those two tests as their killer, 157 in
`tools/mutants.py` and 105 in `tools/mutate.py`, which carries ~128 tags. That
inflates `caught`, which is the flattering direction.
[#110](https://github.com/martinus/tupferl/issues/110), at P2.

Both issues are the gate paying for its two hours: neither is visible from the
suite, the preflight, or any CI leg.

## Phase C — Teardown: delete the unittest verdict layer, settle CI and docs

**Goal:** the pytest-only end state. **PR scope:**

1. Delete `tools/verdict_unittest.py`, `tests/test_verdict_unittest.py`, and
   `mutate._probe`'s `TUPFERL_MUTATE_VERDICT` switch (the default becomes the
   only path).

   **And ci.yml's two `tests.test_verdict_unittest` `--exclude` lines with
   them**, or `run_tests.main` refuses a pattern that matches nothing and the
   macOS leg goes red for the deletion rather than for a defect. Two tests in
   `tests/test_run_tests.py` say so first, locally, which is what they are for:
   `test_every_exclude_names_at_least_one_scope` over the two dead patterns,
   and `test_there_are_excludes_to_check`, whose count drops **6 → 4**. That
   module is not one Phase C otherwise touches, so a session that has not read
   this will diagnose a red test-runner as a regression — which is the whole
   cost of the item being missing, and why it is written at step 1 rather than
   in the doc sweep at step 3.
2. Remove pytest-subtests from deps if Phase B eliminated all subtests uses
   (grep decides; if woswoar will want it, keep it and say why).
3. CI/doc debt sweep. The `PREFLIGHT` tuples in `tests/test_ci.py` /
   `tests/test_release.py` are expected unchanged (the commands were kept
   stable by design) — verify against ci.yml/release.yml rather than assume.
   `mutation.yml`'s `python -m tools.mutate --all` — unchanged. CLAUDE.md:
   audit *every* unittest-specific claim (grep `unittest`, `subTest`,
   `TestCase`, `discover`, `setUpClass`): "discover vs loadTestsFromNames"
   becomes `Historical —`; "unittest's display string changed in 3.11"
   becomes Historical or is rewritten for nodeids; "`TestCase.enterContext`
   is 3.11" becomes Historical (fixtures made it moot); the subTest
   bound-arming lesson is rewritten for parametrize; "`skipUnless` turns the
   macos leg red" is respelled for `pytest.mark.skipif`; §7's preflight
   verified verbatim. **B6 did the last four of those already** — the
   enterContext prescription, the subTest lesson, the skip entry and the
   `tests/` layout row — so this list is now shorter than it reads; check
   rather than assume, which is the point of an audit. Per §0 most of these should already have been fixed by
   the PR that staled them — this phase is the audit that catches stragglers,
   and the PR body lists what it found (a straggler is also a process finding
   worth reporting).
4. Final whole-tree sweep as the phase gate:
   `python -m tools.mutate --all --json sweeps/final.json` — zero rows newly
   surviving/BROKE vs. the last accepted sweep; the `TODO`-tag count stated.

**Acceptance gate:** preflight green; the sweep above;
`grep -rn "verdict_unittest" .` empty; the CLAUDE.md audit table in the PR
body. **Size:** 1 PR, mostly deletions and prose. **Failure protocol:** FP.

> **The grep clause is wrong and was not met — see
> [Phase C as built](#phase-c-as-built--2026-09-01).** It is satisfiable only by
> deleting the history the audit deliberately keeps, which is the opposite of
> CLAUDE.md §0. What was checked instead: no *live* reference — no import, no
> `--exclude`, no environment variable, no table row — with every surviving
> mention prose that names the deletion as history.

## Phase C as built — 2026-09-01

**Done in one PR (#113).** Both files named in step 1 went, and so did
`tools/unassert.py` with its tests -- which step 1 did not list, because the
sentence saying it dies here was in *its own docstring* and in CLAUDE.md rather
than in this plan. That is the phase's first lesson and it is a cheerful one:
**a tool written for a finite job should say in its docstring what finishes
it**, and both of these did, which turned the deletion into a checklist item
rather than an archaeology exercise. The clause is now in CLAUDE.md §7 as a
standing rule rather than only as an instance.

### The gate, and why the headline numbers are not it

Two whole-tree sweeps, both with a green baseline, both on an idle machine:

| | baseline (`main` at `eba3970`) | branch |
|---|---:|---:|
| rows | 3930 | 3557 |
| caught | 3511 | 3201 |
| SURVIVED | 394 | 339 |
| BROKE | 15 | 7 |
| TIMEOUT | 10 | 10 |
| score | 89.9% | 90.4% |

**None of those deltas is evidence of anything.** 373 rows cease to exist with
the two deleted files, so every total moves for arithmetic reasons, and the
score improving by 0.5 points is the removal of two files with a poor ratio
rather than a suite that got better. The comparison that means something is per
`(path, scope, operator, old, new)` -- **not** per line, because five `tools/`
files had docstrings edited and a positional key reports nearly everything as
moved. 3337 keys exist on both trees; **20 differ, 10 each way.**

The symmetry is the tell, and all 20 resolve into three known classes:

- **8 are `broke` ⇄ `caught`** in `_Lanes._sample`, `run`, `_run`,
  `_unbaselined`, `run.announce`, `Killers.learn` -- the scopes CLAUDE.md
  already records as unanswerable under a sweep, where the mutation disables
  the bound its own probe runs under. 6 improved, 2 worsened. Re-run idle, both
  of the worsened pair came back `caught` in seconds.
- **3 are `order`** (`sorted` becoming `list`) -- `targets_for`, `_parse_ps`
  worse, `Watcher.beside` better. `sorted` over a *set* is only
  probabilistically guarded, because hash order is randomised per run.
- **the rest** are `off-by-one` / `arith` / `drop-call` singletons.

**Zero rows regressed.** Established twice, by different routes:

1. **A killer census over the baseline report.** Of 3511 caught rows, 305 have
   their recorded killer in a deleted test module -- and every one of those 305
   mutates `verdict_unittest.py` or `unassert.py` itself, so it ceases to exist
   too. Surviving rows that lose their killer: **none**. The census is sound in
   this direction: a row whose recorded killer survives is still caught by it,
   and only a row whose killer was deleted is at risk. (CLAUDE.md's warning that
   a census "counts rows whose *first* killer is X, never rows only X can catch"
   bites the *other* question and not this one.)
2. **Re-running the 10 worsened rows idle, on both trees, with `failfast=True`
   as `main` passes.** 3 came back `caught` on the branch; the other **7 survive
   on the baseline as well**.

### The seven rows nothing ever guarded

Those 7 are the phase's real finding, and they are about the suite rather than
about this change. Each was reported `caught` in a whole-tree sweep of *either*
tree and survives when run alone. They were never guarded: the outward walk
reached them, some unrelated test failed under the mutation, and the row was
credited. The recorded "killers" say so out loud --
`tools/mutate.py:_parse_ps`'s `all` becoming `any` was killed by
`tests/test_sync_cli.py::TestWhatStopsASync::test_an_unfinished_merge_stops_it`,
and `run`'s column arithmetic by another `test_sync_cli` test. A test about a
refused sync is not a guard on process-table parsing.

| row | operator |
|---|---|
| `mutate.py:_parse_ps` `all` -> `any` | order |
| `mutate.py:run` `+` -> `-` (`2 * width + lane_width`) | arith |
| `mutate.py:Killers.save` `1` -> `0` | off-by-one |
| `mutate.py:_resume_key` `1` -> `0` | off-by-one |
| `mutate.py:_persist` `1` -> `0` | off-by-one |
| `mutants.py:Tags.__init__` the `if` is never taken | branch |
| `watch.py:main` `time.sleep(args.interval)` dropped | drop-call |

Not fixed here -- Phase C is a deletion and this is pre-existing on both trees
-- but recorded so the next sweep does not rediscover them, and so that nobody
reads a whole-tree `caught` as proof a line is guarded. **The general form:
`caught` in a full sweep and `caught` alone are different claims, and only the
second is about a test.**

### What diverged from the plan

- **Step 1 was incomplete**: `tools/unassert.py` was not listed. See above.
- **Step 2 was a confirmed no-op.** `pytest-subtests` was never added --
  `pyproject.toml` says so beside the pytest floor. The plan's own table already
  recorded this; it was re-checked rather than re-decided.
- **The acceptance gate's `grep -rn "verdict_unittest" .` clause is wrong.** It
  cannot be empty: 48 references survive, every one of them prose that names the
  deletion as history, which is what CLAUDE.md §0 asks for. The only way to
  satisfy the clause as written is to delete the record. It is amended above to
  state what was actually checked -- **no live reference**: no import, no
  `--exclude`, no environment variable, no table row.
- **`assert len(EXCLUDES) == 6` became `>= FLOOR`, not `== 4`.** The plan
  prescribed the count dropping to 4 and devotes a paragraph to warning future
  sessions that a red test-runner here is not a regression. That paragraph *is*
  the cost of the equality. `tests/test_errors.py` and `tests/test_support.py`
  already use a floor for the identical hazard -- a parse that matched nothing --
  and the `parametrize` beside it checks each pattern found, so a new exclusion
  is checked by existing.

### Scope the next sweep before running it

**The census in (1) above took thirty seconds and reached the same conclusion as
fifty-seven minutes of whole-tree sweeping.** It should have been run *first*
and used to scope the arm. What a whole-tree sweep uniquely adds over
`--base main` plus a census is narrow, and worth stating so the next phase can
decide rather than default:

- rows the diff *created* -- `--base main` covers exactly those, in minutes;
- **walk-order effects, which are only visible at full-sweep scale.** This is
  the real reason and it is not hypothetical: the shuffled-walk dead end
  produced 24 false `caught` verdicts reproducible in no smaller setting, and
  the class-ordering entry records five rows flipping to `BROKE`. Deleting 100
  tests changes what the walk runs and in what order, which is precisely that
  class of change;
- a post-phase whole-tree number, which is what Phase D's own gate
  ("whole-tree sweep unchanged") is specified against.

For Phase D, whose changes are configuration with defaults equal to today's
constants, the census plus `--base main` is likely to be the honest instrument,
with one whole-tree run at the end to set the baseline rather than two to
compare.

### Numbers the phase owes

- **`TODO` survivor tags: 109**, all in `tools/mutate.py`, unchanged by this
  phase -- re-counted rather than copied. (A review reported 110; that count
  included `--accept`'s help string, which is not a tag.)
- **Survivor dispositions on the branch**: of 339 survivors, every one of the
  **26 in `tupferl/` has a written reason**. Zero `TODO`, zero untagged in the
  shipped package. The debt is entirely in `tools/`.
- **Suite size 2097 -> 1991 collected items**, every one of the 109 accounted
  for: 47 in `test_verdict_unittest.py`, 53 in `test_unassert.py`, 6 in the
  deleted layer class, 2 dead `--exclude` parametrize cases, 1 rename.

### One thing the sweeps exposed that is not about the sweeps

Four probe processes from a sweep **36 hours earlier** were still alive during
both arms, one holding 3.2 GiB resident, together with 2681 leftover sandbox
directories totalling 2.5 GiB. They pre-date this branch and were present for
both arms equally, so the comparison is unaffected -- but the absolute memory
lines every sweep prints were computed on a machine with ghosts on it. Filed
separately; `_end_lane` cannot run when the sweep itself is killed.

## Phase D — Extraction-readiness (parameterize, don't extract)

**Goal:** every tupferl-specific name in the mutation framework becomes
configuration with tupferl's values as the in-repo config, so extraction later
is a packaging exercise. woswoar's conversion is the intended first consumer.

**Config surface** — a `[tool.mutate]` table in `pyproject.toml`, read by a
small `tools/settings.py` (the name avoids colliding with
`tupferl/config.py`), with **defaults equal to today's constants** so an
absent table changes nothing:

| knob | today's value | notes |
|---|---|---|
| `mutable` | `["tupferl/", "tools/"]` | `mutants.MUTABLE` |
| `unmutable` | `[]` | mechanism already exists |
| `env_prefix` | `"TUPFERL"` | derives `<P>_MUTATE_BUDGET`, `<P>_MUTATE_EACH_TEST`, `<P>_MUTATE_TOTAL`; **`support.bounded` reads the same derived name**, so the floor mechanism survives renaming — `bounded` should take the name from one shared spelling, decided in this PR |
| `tmp_prefix` | `"tupferl-"` | sandbox/verdict/batch temp dirs |
| `hypothesis_profile_env` / `hypothesis_profile` | `"TUPFERL_HYPOTHESIS_PROFILE"` / `"mutation"` | optional — empty disables the hook for projects without Hypothesis |
| `probe_plugins` | per S4 (e.g. Hypothesis's plugin) | the `-p` list a probe passes under `PYTEST_DISABLE_PLUGIN_AUTOLOAD` |
| `test_module_patterns` | `["test_{stem}", "test_{stem}_*"]` + the tests dir | the `targets_for` naming convention. The walk itself already respects the host's pytest `python_files`/`testpaths` (Phase A); this knob is only the *selection-ordering* heuristic — a wrong value costs a longer walk, never a wrong verdict. |

Read from the **running** tree's pyproject, never the sandbox's — a mutation
must not edit its own budget. Each knob gets a test proving a non-default
value takes effect (drive a real probe with a scratch config, the way
`test_mutants` already drives scratch trees).

**"What extraction still needs" checklist** (recorded in `tools/README.md`,
not done here): a package name and entry point (`python -m <name>`); the
`tests/support.py` pieces the harness's own tests borrow; the read-source
contract as package data; license headers/attribution (the woswoar Apache-2.0
provenance is already documented); a woswoar dry-run as the real acceptance
test; docs for the `[tool.mutate]` table; and whether `run_tests.py` ships or
stays per-project.

**Acceptance gate:** preflight green; whole-tree sweep unchanged (the
config-default path); one demonstration probe run with every knob overridden
in a scratch project (PR body shows it); `grep -rn "TUPFERL" tools/` returns
only the settings defaults and prose. **Size:** 1 PR, ~300–500 lines.
**Failure protocol:** FP.

## Phase D as built — 2026-09-01

One PR. `tools/settings.py` is new (~250 lines with its argument),
`tests/test_settings.py` is new (53 tests), and `pyproject.toml` gained a
`[tool.mutate]` table. Every other file lost a name rather than gaining one.

### The one place it diverges from the plan above, and why

**The plan asked for "defaults equal to today's constants so an absent table
changes nothing". It was built the other way round: the defaults are generic and
tupferl's values are in the table.**

The plan's arrangement is the one in which nothing can tell a working config
reader from one that never opens the file. If `mutable` defaulted to
`("tupferl/", "tools/")` *and* the table said the same, a bug that dropped the
table on the floor would produce a byte-identical sweep and every test written
for the feature would pass — CLAUDE.md §8's flattering green, arriving through
the feature's own design.

Measured, by deleting the table from `pyproject.toml` and running the guards:
**13 tests across `test_settings.py`, `test_mutate.py` and `test_profiles.py`
go red.** Under the plan's arrangement the number would be zero.

The gate the phase actually names — a whole-tree sweep unchanged — is met either
way, because the table restores today's values exactly. `TODAY` in
`tests/test_settings.py` is a `Settings` holding every constant Phase D removed
from `tools/`, asserted one knob per parametrize case, and it is the only place
in the tree those literals still appear.

**Four knobs are green with the table deleted**, because their default really
does equal this project's value: `unmutable` (empty), `probe_plugins` (empty),
`tests_dir` (`tests`) and `test_module_patterns`. Nothing about the repository
can distinguish those, which is what `TestASecondProjectConfiguresIt` is for.

### The instrument that answers "does a second project configure it"

A scratch tree with **every** knob different from both the defaults and
tupferl's: `mutable = ["src/"]`, `unmutable = ["src/dangerous.py"]`,
`env_prefix = "OTHER"`, `tmp_prefix = "other-"`, `tests_dir = "checks"`,
`test_module_patterns = ["check_{stem}"]`, autoload back *on*, two named
plugins. `tools/` is copied into it — which is both what makes
`settings.ROOT` land on the scratch `pyproject.toml` and what extraction will
actually look like — and a subprocess asks the *harness* rather than the
settings:

```
{"root": "/tmp/check-5m_mnrec", "mutable": ["src/"],
 "unmutable": ["src/dangerous.py"], "walked": ["src/thing.py"],
 "targets": "checks.check_thing", "alarm": "OTHER_MUTATE_EACH_TEST",
 "budget": "OTHER_MUTATE_BUDGET", "total": "OTHER_MUTATE_TOTAL",
 "mutated": "OTHER_MUTATE_MUTATED", "profile_env": "OTHER_HYPOTHESIS",
 "profile": "quick", "tmp": "other-verdict-", "refusal": "src/**.py",
 "sandbox": {"PYTHONDONTWRITEBYTECODE": "1",
             "PYTEST_PLUGINS": "myplugin,otherplugin"}}
```

110 ms, measured. `checks/` holds `check_thing.py` **and** `test_thing.py`, so
the selection separates "it found the directory" from "it kept the old
convention" — both files are there and only one is an answer.

### What the perturbation found that the tests did not

`test_the_unmutable_entry_is_left_out_of_the_walk` passed with the config reader
disabled. With no `mutable`, the walk comes back **empty**, and "this file is
not in the answer" is satisfied by there being no answer — the negative
assertion whose precondition was never established that CLAUDE.md §2 lists. It
asserts the sibling is present first now. That is the only finding, and it came
from perturbing the file rather than from reading the diff.

### Decisions worth carrying forward

- **`probe_plugins` travels as `PYTEST_PLUGINS`, not as an argv slot.**
  `tools/verdict.py` is read as source text into the sandbox and may import
  nothing from `tools`, so it cannot be handed a settings object; pytest's own
  variable needs no plumbing and no new command-line position. The one thing
  that stays hardcoded in the probe is `-p no:cacheprovider`, which is sandbox
  hygiene rather than a project's business.
- **`hypothesis_profile_env` is not derived from `env_prefix`.** It names a
  variable the *host suite* owns (`tests/profiles.py` here), and deriving it
  would let a change of prefix silently rename somebody else's variable.
- **`tests/support.py` imports `tools.settings`.** Its docstring used to argue
  the opposite — "`support` is the bottom of the test tree and importing the
  harness into it to read one string is the wrong direction" — and that argument
  stands; this is not it. `settings.py` is the configuration, imports nothing
  but the standard library, and is what turns four hand-written literals in two
  files into one spelling. `test_support`'s two agreement tests are vacuous by
  construction now and were kept, with docstrings saying so: what they still
  catch is either end going back to a literal.
- **`settings._root` is the single seam extraction moves.** "The tree this file
  lives in" is what makes a copied `tools/` configurable and is immune to the
  `os.chdir` the suite does constantly; an installed harness would answer
  pytest's `rootdir` there and nothing else would change. Reading the *running*
  tree rather than the sandbox's is not what stops a mutation editing its own
  budget — inside a probe this file **is** the sandbox's copy. What stops it is
  that `pyproject.toml` is not a `.py` file under a `mutable` prefix.
- **An unknown key or a wrong type is refused, naming it.** `type(value) is not
  want` rather than `isinstance`, because `bool` is a subclass of `int` and TOML
  has both; and a list check that asks "are all items strings" accepts
  `"src/"` and turns one prefix into eleven one-character ones.

### Two things this phase did not do

- **`tools/README.md` carries the extraction checklist** — package name and
  entry point, the `tests/support.py` pieces the harness's own tests borrow, the
  read-source contract as package data, licence headers, a woswoar dry-run, and
  whether `run_tests.py` ships. None of it is work to start before there is a
  second consumer.
- **The set of knobs is proved read, not proved right.** Only a second real
  suite can say whether it is the right set, and woswoar's conversion is that
  test.

### A figure that was wrong and is now accidentally right

`tests/test_pytest_plan.py`'s docstring said "159ms for all 35" while the tree
held 34 modules. Phase D added `test_settings.py` and made it true. It is dated
now, and it is the third hand-typed count in this repository to be found wrong
by one — the first two are in CLAUDE.md's "Measured, and kept" and its `TODO`
tag count.

## Risks named up front

- **pytest version drift.** The repo's floors-are-real-versions stance
  applies doubly: the harness depends on *behavioral* details of pytest's
  unittest integration and report model, which change between majors.
  Mitigation: floor = the spiked version, plus Phase A's "harness
  assumptions" test class, so a pytest upgrade goes red loudly instead of
  flattering the sweep. If a future pytest drops 3.10 before this repo does,
  pin below it (the same shape as the tomli reasoning).
- **mypy strict on 3.10 over fixtures.** pytest ships `py.typed`;
  `@pytest.fixture`/`parametrize` are typed, but `disallow_untyped_decorators`
  may object to some mark/plugin decorators, and pytest-subtests is less
  thoroughly typed. Budget for narrow per-module overrides in the existing
  pyproject style — never a tree-wide relaxation — and remember the
  CI-only-mypy-failure gotcha: suspect the dependency version first.
- **pytest capture vs the stdin/pty discipline.** pytest's captured stdin
  answers `isatty()` False — the same safe direction as `run_cli`'s DEVNULL
  rule — but `-s`/`--capture` interactions with the pty fixtures, and
  capture's fd manipulation around `support.hush`, are exactly the class of
  thing S0 exists to find. Any surprise gets its own gotcha entry.
- **Repeated `pytest.main()` state** (if S1's primary wins): Hypothesis
  database/profile state, `warnings` filters, and anything leaking between
  the selection call and a walk call within one probe could flip a verdict.
  Phase A's equivalence sweep is the instrument that catches it at scale; the
  shuffled-walk dead end (24 false `caught`, CLAUDE.md) is the precedent that
  walk-order effects are real and only visible at full-sweep scale.
- **Sweep-comparison cost.** Phase A's gate is two whole-tree sweeps (hours
  each) plus flake triage; schedule it on an idle machine and interleave
  nothing (§5).

## Estimate

| phase | PRs | rough effort |
|---|---|---|
| 0 spikes | 1 | 2–4 sessions |
| A verdict layer | 1 | the largest single PR, plus two whole-tree sweeps |
| A2 runner | 1 | 1–2 sessions |
| B conversion | 7 | 1–4 sessions each |
| C teardown | 1 | 1 session |
| D extraction-readiness | 1 | 1–2 sessions |

Sequential by design; nothing here parallelizes across phases, because each
gate is the next phase's baseline.
