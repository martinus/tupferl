# Converting tupferl to pytest — phased implementation plan

Status: **Phase 0 executed** (2026-08-30); Phases A onwards not started. The
measured answers are in [Spike results](#spike-results--measured-2026-08-30),
which corrects three expectations this plan was written with — read it before
Phase A.

## Context for the executing agent

You are likely reading this in a fresh session with no memory of how it came to
be. The background you need:

tupferl's suite is stdlib `unittest` (1505 tests, 33 modules -- Phase 0 counted
them; the estimate here was ~1557), a choice
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

## Phase B — Convert the 33 test modules, in clusters

**Goal:** every test module becomes pytest-native. One cluster per PR. A
module converts *whole*; support machinery converts to `tests/conftest.py`
fixtures in the first cluster that needs it, with the unittest base classes
kept alive in `support.py` until their last user converts (then deleted in
that same PR).

**Per-PR conversion contract (every cluster):**

1. Before editing: record the module's collected item count and test list
   (`python -m pytest --collect-only -q tests/test_X.py`).
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
| B1 | `test_cpus`, `test_packaging`, `test_errors`, `test_merge`, `test_config`, `test_ci`, `test_release`, `test_paths` | creates `tests/conftest.py` (initially near-empty) | no support bases; `test_paths`' local `Environment` base → fixture. Also updates CLAUDE.md's "Build & test" serial-fallback line (`python -m unittest discover…` stops covering these modules) to `python -m pytest -q`. |
| B2 | `test_config_properties`, `test_merge_properties`, `test_sync_properties`, `test_profiles` | none new | Hypothesis-native. Delete the `__module__`/`__name__`/`__qualname__` dunder hack in `test_sync_properties.py` (it existed for unittest id round-trip in sharding; pytest nodeids come from collection) — keep the `X = Machine.TestCase` assignments, which are the pytest-idiomatic spelling. `profiles.py` untouched. The pyproject mypy-override list stays valid (module names unchanged). |
| B3 | `test_conflicts`, `test_gitrepo`, `test_cli`, `test_manifest`, `test_doctor` | `SandboxCase` → `sandbox` fixture (throwaway `$HOME`; `mock.patch.dict(os.environ, sandbox_env(...), clear=True)` as a yield-fixture); `requires_git` → `pytest.mark.skipif` | pty/`run_cli` tests live here; S0's capture findings apply. `sandbox_env` and the `CARRIES` allowlist are untouched — the poison test in `test_support` still guards the `ENV_KEYS` linkage. |
| B4a | `test_sync`, `test_status`, `test_diff`, `test_manage` | `TwoMachines` → `two_machines` fixture (copytree of the cached template + remote-url repair; the `template()`/`two_machines()` functions themselves unchanged) | the overlay both-copies rule and `TestTheSnapshotIsWrittenLast` transfer as-is. |
| B4b | `test_overlays`, `test_sync_cli`, `test_sync_commits`, `test_sync_conflicts` | per-module bases (`Conflicted`, `TwoCommits`, `OneMachine`, …) → module-local fixtures | after this PR, delete `TwoMachines`/`SandboxCase` classes from `support.py` if no user remains (grep, don't assume). |
| B5 | `test_support`, `test_paint`, `test_watch`, `test_reached` | local bases (`Boxed`, `Fixture`) → fixtures | `test_watch`'s bound-vs-alarm numbers (the 30s trap) re-checked against `bounded` after conversion. |
| B6 | `test_run_tests`, `test_mutants`, `test_verdict`, `test_mutate` | `Probe`, `Tree`, table bases → fixtures | hardest: these drive *nested* harnesses; every recorded walk/BROKE gotcha applies. `test_mutate.py`'s mid-file `if __name__ == "__main__"` block (~line 349; the file continues for thousands of lines) is dead under pytest — delete it with a note. `tests/test_verdict_unittest.py` is left unittest-style *deliberately* (it dies in Phase C; pytest runs unittest tests either way, so leaving it costs nothing — say so in the PR). The `TODO` survivor tags in `tools/mutate.py` are not this phase's debt: leave them, count unchanged. |

**Size:** 7 PRs, each roughly 1–4 sessions. **Failure protocol:** FP per
cluster; a newly-surviving row means the conversion weakened a test — fix the
test, never the disposition; a newly-BROKE row is almost always a bound/alarm
race — apply the five-lessons checklist before touching anything else.

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
