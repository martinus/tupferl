# `tools/` — the test infrastructure

Ported from [martinus/woswoar](https://github.com/martinus/woswoar), which is
Apache-2.0, as this repository is. **The files here have been modified from
their originals** — the list of what changed is at the bottom.

Each module's docstring carries the full argument for its shape; that is the
point of them, and it is why they were ported rather than replaced with
off-the-shelf equivalents. Read the docstring before changing a module.

## When to reach for which

| when | tool |
|---|---|
| a fix needs a test that fails without it (CLAUDE.md §2) | [`mutate.py`](mutate.py) |
| a mutation run left more survivors than you can read | [`reached.py`](reached.py) |
| the suite is slow enough that you are tempted to run a subset | [`run_tests.py`](run_tests.py) |
| a long job is running detached and silence is ambiguous | [`watch.py`](watch.py) |
| a tool's output needs a colour, and its log must not get one | [`paint.py`](paint.py) |

```sh
python -m tools.mutate --base main --json sweeps/r.json   # generated from the diff
python -m tools.mutate <spec>.py          # a table you wrote
python -m tools.reached sweeps/r.json sweeps/c.json   # survivors missing tests
python -m tools.watch $PID --log sweeps/r.log --done sweeps/r.json.done --match 'caught|SURVIVED'
```

`mutants.py`, `verdict.py`, `cpus.py` and `paint.py` are not run directly: they
are what `mutate.py` and `run_tests.py` are built from. `verdict.py` in particular is a
*standalone* file on purpose — it is read as source and executed inside each
mutation's sandbox, so it must not import anything from this package.

`verdict_unittest.py` is the same file's predecessor, classifying `unittest`
result objects where `verdict.py` classifies pytest reports. It is reached only
with `TUPFERL_MUTATE_VERDICT=unittest`, which exists so a row the two disagree
about can be re-run against the classifier that was here before rather than
argued about, and it is deleted with that switch at the end of the conversion
([`docs/pytest-plan.md`](../docs/pytest-plan.md), Phase C).

## Why these and not `mutmut` / `pytest-xdist`

Three properties, each of which cost a real debugging session to learn:

- **`BROKE` is never counted as `caught`.** A mutation that turns a working
  import into a failing one exits non-zero and leaves a plausible count of
  tests behind, exactly like a test catching it. A harness that reads the exit
  status reports a test that never executed as a test that noticed. Under
  pytest there is a second, sharper version of the same trap: a failing
  `self.subTest(...)` leaves the owning test's *own* report reading `passed`,
  so a classifier that read finished reports would report a real kill as a
  survivor.
- **Every row is run against the whole suite until something notices** -- its
  selection first, then the rest, stopping at the first test that catches it.
  So a survivor has run everything by the time it is called one, and a
  selection that misses the killing test costs a longer walk rather than a
  wrong answer. There is no second pass over the survivors and no
  `--no-confirm`; both were removed once the walk made them redundant.
  Per-file test selection is then a speed decision rather than a correctness
  one: a survivor found against a narrow target may just have been run against
  the wrong tests, and that error points the expensive way — it sends the author
  to rewrite a test that was never weak.
- **The working tree is never edited.** Every mutant runs in a throwaway copy,
  so a kill at the wrong moment cannot leave mutated source behind (CLAUDE.md
  §6).

`run_tests.py` earns its place with one accounting check that plain `unittest`
has no need for: `ids discovered == ids reported`. A parallel run can be green
because a batch died before it reported anything.

## What was changed in the port

- **Paths and names.** `MUTABLE` is `("tupferl/", "tools/")`; the temporary
  directory prefixes and the `TUPFERL_MUTATE_BUDGET` variable were renamed.
- **`UNMUTABLE` is empty.** In woswoar it excluded a script that wrote a real
  store into a sandbox; nothing here needs it yet. The mechanism is kept, with
  the rule that earns it recorded at the constant.
- **`usable_cpus` moved to `cpus.py`.** It was in a `sandbox.py` whose other
  contents are specific to woswoar's `bench` and `compare`, neither of which was
  ported. `run_tests.py` had a second, slightly different copy; both now call
  the one function.
- **The Hypothesis profile is pinned per probe.** `mutate.py` sets
  `TUPFERL_HYPOTHESIS_PROFILE=mutation`, which `tests/profiles.py` reads. Without
  it every mutant pays the full example budget; without the *derandomisation*
  that profile also carries, a baseline and a mutant draw different examples and
  "it failed" stops meaning "a test noticed".
- **Evidence is attributed.** These docstrings are mostly measurements and
  incident reports, and every one of them was collected in woswoar rather than
  here. Numbers now say so, and `woswoar#123` is an issue in *that* repository.
  A claim about a file this project does not have was either rewritten to the
  equivalent file here or dropped — see CLAUDE.md §0 for why a stale claim is
  worse than none.
- **`paint.py` is new here, and the four tools print through it.** woswoar's
  print in one colour. The module is mostly about the half that prints
  *nothing*: a sweep is launched detached with its output redirected, and
  `watch.py --match 'caught|SURVIVED'` greps that file, so an escape sequence
  inside the word `caught` would make a healthy run read as a stalled one.
  Colour is decided by `isatty` per stream and goes around whole words.
- **`bench.py`, `compare.py` and `sandbox.py` were not ported.** They compare
  two revisions of woswoar and are built around its store; the versions this
  project needs will be written when there is something to measure.
