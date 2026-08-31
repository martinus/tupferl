# Converting tupferl to pytest — phased implementation plan

Status: **Phases 0, A and A2 executed** (2026-08-30), and Phase B's step 1a
and **clusters B1, B2, B3 and B4a** with them, which converted the first
twenty-one modules.
**Of 36 test modules, 14 still run through pytest's `unittest` adapter** — 12 of
those convert in B4b–B6. The other two never do, and are **not** arrears:
`tests/test_verdict_unittest.py` stays as it is until Phase C deletes it with
its subject, and `tests/test_sync_properties.py` is converted but exposes a
class Hypothesis builds inside `hypothesis.stateful`, which the plan keeps as
the pytest-idiomatic spelling.

Both numbers are asserted by `tests/test_pytest_plan.py`, which asks the
`unittest` loader what it takes back from each module — so this line cannot
quietly go stale and "continue the plan" is a safe instruction.

**"Still run as `unittest`" is the number to state, and it took two tries to
find a predicate that means it.** "Converted" was the first, and the guard
written for it failed on its own first run: a module born pytest-native — such
as that guard — raises the native count with no conversion behind it. The second
asked `issubclass` of each module's attributes, filtered by `__module__`, and
cluster B2 then edited exactly that attribute: deleting
`test_sync_properties.py`'s dunder rewrite dropped the count by one for free. A
number a `__module__ = __name__` can lower is not a number of work done, so the
guard asks the loader instead — what `python -m unittest discover` and
`tools/verdict_unittest.py` actually run.
The measured answers to the spikes are in
[Spike results](#spike-results--measured-2026-08-30), which corrects three
expectations this plan was written with. What each executed phase did
differently from what it says below is in
[Phase A as built](#phase-a-as-built--2026-08-30),
[Phase A2 as built](#phase-a2-as-built--2026-08-30),
[B1 as built](#b1-as-built--2026-08-30),
[B2 as built](#b2-as-built--2026-08-30),
[B3 as built](#b3-as-built--2026-08-31) and
[B4a as built](#b4a-as-built--2026-08-31). **Read every "as built" section before
the next phase** — said that way rather than as a count, because a count is one
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
| B4b | `test_overlays`, `test_sync_cli`, `test_sync_commits`, `test_sync_conflicts` | per-module bases (`Conflicted`, `TwoCommits`, `OneMachine`, …) → module-local fixtures | after this PR, delete `TwoMachines`/`SandboxCase` classes from `support.py` if no user remains (grep, don't assume). |
| B5 | `test_support`, `test_paint`, `test_watch`, `test_reached` | local bases (`Boxed`, `Fixture`) → fixtures | `test_watch`'s bound-vs-alarm numbers (the 30s trap) re-checked against `bounded` after conversion. |
| B6 | **Fix #96 first — see below.** `test_run_tests`, `test_mutants`, `test_verdict`, `test_mutate` | `Probe`, `Tree`, table bases → fixtures | hardest: these drive *nested* harnesses; every recorded walk/BROKE gotcha applies. `test_mutate.py`'s mid-file `if __name__ == "__main__"` block (~line 349; the file continues for thousands of lines) is dead under pytest — delete it with a note. `tests/test_verdict_unittest.py` is left unittest-style *deliberately* (it dies in Phase C; pytest runs unittest tests either way, so leaving it costs nothing — say so in the PR). The `TODO` survivor tags in `tools/mutate.py` are not this phase's debt: leave them, count unchanged. |

**Size:** 7 PRs, each roughly 1–4 sessions. **Failure protocol:** FP per
cluster; a newly-surviving row means the conversion weakened a test — fix the
test, never the disposition; a newly-BROKE row is almost always a bound/alarm
race — apply the five-lessons checklist before touching anything else.

### #96 is a prerequisite of B6, and of no other cluster

**Fix [#96](https://github.com/martinus/tupferl/issues/96) before starting B6.**
A sweep mutates its own memory guard and pool — `_Lanes.release`,
`_Lanes._sample`, `_sandboxes`, `_borrow`, `Work.take` — so those rows come back
`BROKE`, and a `BROKE` row is never `caught`. B6's gate is the 1030-row
`tools/mutate.py` table, where the noise sits; converting `test_mutate.py` while
those rows are unanswerable means the cluster's own acceptance check cannot
distinguish "the conversion weakened a test" from "the harness cannot answer
this row", which is the one thing the gate exists to tell apart.

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

**And do #96 last rather than first for a second reason:** its fix touches
`tools/mutants.py`, whose tests B6 converts. Adjacent is better than four
clusters apart.

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
section](#96-is-a-prerequisite-of-b6-and-of-no-other-cluster) already states and
which says to re-run the count rather than trust it. Re-run against both of the
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

### Gate

Preflight: **1894 tests, 0 failures, 0 skipped**, from 1861.

## Phase C — Teardown: delete the unittest verdict layer, settle CI and docs

**Goal:** the pytest-only end state. **PR scope:**

1. Delete `tools/verdict_unittest.py`, `tests/test_verdict_unittest.py`, and
   `mutate._probe`'s `TUPFERL_MUTATE_VERDICT` switch (the default becomes the
   only path).
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
   verified verbatim. Per §0 most of these should already have been fixed by
   the PR that staled them — this phase is the audit that catches stragglers,
   and the PR body lists what it found (a straggler is also a process finding
   worth reporting).
4. Final whole-tree sweep as the phase gate:
   `python -m tools.mutate --all --json sweeps/final.json` — zero rows newly
   surviving/BROKE vs. the last accepted sweep; the `TODO`-tag count stated.

**Acceptance gate:** preflight green; the sweep above;
`grep -rn "verdict_unittest" .` empty; the CLAUDE.md audit table in the PR
body. **Size:** 1 PR, mostly deletions and prose. **Failure protocol:** FP.

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
