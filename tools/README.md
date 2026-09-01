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
| a name here says `tupferl` and should not | [`settings.py`](settings.py) |

```sh
python -m tools.mutate --base main --json sweeps/r.json   # generated from the diff
python -m tools.mutate <spec>.py          # a table you wrote
python -m tools.reached sweeps/r.json sweeps/c.json   # survivors missing tests
python -m tools.watch $PID --log sweeps/r.log --done sweeps/r.json.done --match 'caught|SURVIVED'
```

`mutants.py`, `verdict.py`, `cpus.py`, `paint.py` and `settings.py` are not run
directly: they are what `mutate.py` and `run_tests.py` are built from.
`verdict.py` in particular is a *standalone* file on purpose — it is read as
source and executed inside each mutation's sandbox, so it must not import
anything from this package. `settings.py` is the other end of that rule: it
imports nothing but the standard library, because `tests/support.py` imports it.

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

`run_tests.py` earns its place with one accounting check that a serial run has
no need for: `ids discovered == ids reported`. A parallel run can be green
because a batch died before it reported anything. It drives pytest since
Phase A2 of `docs/pytest-plan.md`; its module docstring says why not
`pytest -n auto --dist loadscope`, and what would justify re-opening that.

## What was changed in the port

- **Paths and names.** They were renamed in the port and are configuration
  since Phase D: `MUTABLE`, the temporary directory prefixes and the
  `<PREFIX>_MUTATE_BUDGET` family all come out of `[tool.mutate]` — see
  [Configuring it for another project](#configuring-it-for-another-project).
- **`UNMUTABLE` is empty.** In woswoar it excluded a script that wrote a real
  store into a sandbox; nothing here needs it yet. The mechanism is kept, with
  the rule that earns it recorded at the constant.
- **`usable_cpus` moved to `cpus.py`.** It was in a `sandbox.py` whose other
  contents are specific to woswoar's `bench` and `compare`, neither of which was
  ported. `run_tests.py` had a second, slightly different copy; both now call
  the one function.
- **The Hypothesis profile is pinned per probe.** `mutate.py` sets the variable
  and value `[tool.mutate]` names — here `TUPFERL_HYPOTHESIS_PROFILE=mutation`,
  which `tests/profiles.py` reads; a project that names neither gets no hook. Without
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

## Configuring it for another project

Everything the harness knows about the project it measures is a key in a
`[tool.mutate]` table in that project's `pyproject.toml`, read by
[`settings.py`](settings.py). Nothing under `tools/` spells a project's name.

| key | what it decides | default |
|---|---|---|
| `mutable` | path prefixes whose `.py` files are mutated | none, and a run with none refuses |
| `unmutable` | never mutated, whatever `mutable` says | none |
| `env_prefix` | the `<P>_MUTATE_BUDGET` / `_TOTAL` / `_EACH_TEST` / `_MUTATED` family | none, so the bare names |
| `tmp_prefix` | every temporary directory the harness makes | `mutate-` |
| `hypothesis_profile_env`, `hypothesis_profile` | the variable and value a probe sets; either empty disables the hook | none |
| `probe_autoload` | whether a probe lets pytest autoload installed plugins | `true` |
| `probe_plugins` | plugins a probe force-loads through `PYTEST_PLUGINS` | none |
| `tag_columns` | what `--accept` wraps a `# survivor:` tag to | `88` |
| `sandbox_ignore` | names a sandbox copy leaves out, on top of the universal list | none |
| `tests_dir` | where the test modules are | `tests` |
| `test_module_patterns` | how a source stem predicts its test module's | `["test_{stem}", "test_{stem}_*"]` |

The defaults are generic and this project's answers are in its own table, which
is deliberate: had the defaults been tupferl's, a reader that never opened the
file would produce an identical sweep and every test written for it would pass.
An unknown key or a wrong type is refused with a message naming it, rather than
silently keeping a default.

`tag_columns` has to equal whatever the host's formatter enforces, because
`--accept` writes comment lines into the host's own source files; here
`tests/test_packaging.py` asserts it against `[tool.ruff] line-length`.

`sandbox_ignore` is where a sweep's largest avoidable cost is decided: a sandbox
is copied once per lane per row, so a virtualenv or a `node_modules` left in it
is paid thousands of times. The universal names — `.git`, `__pycache__`, the two
linter caches, `*.egg-info`, `.hypothesis` — are in `mutate._SKIP` and need no
configuration.

`tests_dir` and `test_module_patterns` are an **ordering** heuristic and not a
gate — `verdict.collect` walks whatever the selection missed — so a wrong value
costs a longer walk, never a wrong verdict. The walk itself already respects the
host's own pytest configuration (`python_files`, `testpaths`, conftest
hierarchies).

Two limits of that pair, both stated rather than fixed, because neither can be
exercised by a project with tupferl's flat layout:

- **a host states its test-module convention twice** — here, and in pytest's
  `python_files`. Defaulting one from the other is possible (`settings.load`
  already parses the file that holds both) and is declined for now: tupferl
  declares neither, so nothing here would prove the defaulting works.
- **`tests_dir` is one directory and is not searched recursively.** A suite laid
  out as `tests/unit/test_x.py` gets an empty selection for every row, so every
  row runs the whole suite — slower, never wrong, and silent about it. It is the
  knob most likely to be wrong for the second consumer.

## What extraction still needs

Phase D made the harness project-agnostic; it did not package it. What is left,
recorded here rather than filed, because none of it is work anybody should start
before there is a second consumer:

- **a package name and an entry point.** `python -m tools.mutate` becomes
  `python -m <name>`, and `settings.ROOT` — today "the tree this file lives in",
  which is what makes a copied `tools/` configurable — becomes pytest's `rootdir`
  or the invoking directory. That is one function, `settings._root`, and it is
  the only place that knows where a project is.
- **the `tests/support.py` pieces the harness's own tests borrow.** `tempdir`,
  `bounded`, `bounds`, `deadline` and `over_a_mutated_tree` are used by
  `test_mutate`, `test_mutants`, `test_verdict`, `test_reached`, `test_watch`
  and `test_settings`. They are tupferl's file today and half of it is about
  tupferl's sandbox.
- **the read-source contract as package data.** `mutate._probe` reads
  `verdict.py` off disk beside itself; an installed wheel has to ship it as a
  file rather than only as a module.
- **licence headers and attribution.** The woswoar Apache-2.0 provenance is
  documented above and would need to travel with the files.
- **a woswoar dry-run as the real acceptance test.** Everything here is measured
  against one project. The scratch project in `tests/test_settings.py` proves the
  knobs are read; it does not prove the set of knobs is the right set, and only a
  second real suite can.
- **whether `run_tests.py` ships or stays per-project.** It is a suite runner
  with an accounting check, not part of the mutation harness, and `mutate.py`
  imports it for exactly one thing.
