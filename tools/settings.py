"""What the mutation harness knows about the project it is run from.

Everything in this module was a constant somewhere under ``tools/`` spelling
``tupferl``. That was fine while the harness had one user and is the thing that
stops it having a second, so each of those names is now a key in a
``[tool.mutate]`` table in the host project's ``pyproject.toml``. The rest of
``tools/`` reads `SETTINGS` and holds no project name of its own.

**The defaults are generic and tupferl's values are in the table, which is the
one decision here worth arguing.** `docs/pytest-plan.md`'s Phase D asked for the
opposite -- defaults equal to today's constants, so an absent table changes
nothing -- and that is the arrangement in which nothing can tell a working
config reader from one that is never consulted. If ``mutable`` defaulted to
``("tupferl/", "tools/")`` *and* the table said the same thing, a bug that
dropped the table on the floor would produce an identical sweep, which is
exactly the flattering green CLAUDE.md §8 collects. With the values only in the
table, deleting it changes the answer, so every test written here can fail.

The gate the phase actually cares about -- a whole-tree sweep unchanged -- is
met either way, because the table restores today's values.

**The root is the tree this file lives in, not the current directory.** Two
reasons, and the second is the one that decides it:

- the suite chdirs into scratch trees constantly (`support.tempdir`), and a
  setting that changed under `os.chdir` would make `mutants.mutable` answer
  differently depending on where a caller happened to be standing;
- a probe runs with ``cwd`` set to a *sandbox*, and the plan's requirement is
  that a mutation must not be able to edit its own budget. Reading ``__file__``'s
  tree does not by itself buy that -- inside a probe this file *is* the sandbox's
  copy -- so what buys it is that ``pyproject.toml`` is not a ``.py`` file under
  a ``mutable`` prefix and so is never mutated. The sandbox's copy is byte
  identical to the running tree's, and both spellings agree. It is said here
  because the reason is not the one the phrase "read the running tree" suggests.

**Unknown keys and wrong types are refused, never ignored.** A misspelled knob
that silently kept its default is a config file that lies about what is in
force, and the failure would surface as a sweep with different numbers and
nothing to explain them.

**It imports nothing from ``tools``**, deliberately: `tests/support.py` is the
bottom of the test tree and now imports this to spell one environment variable,
so anything this pulled in would be pulled into every test process.

**Why a mutation here is BROKE, and why 13 tags below say so.** `SETTINGS =
load(ROOT)` runs at *import*, and every consumer reads it at *their* import --
`mutants.MUTABLE`, `mutate._ALARM`, and `tests/support.py`'s `ALARM` and
`CARRIES`, which `tests/conftest.py` pulls in before any test runs. So a mutation
that makes the answer wrong or absent does not fail a test; it makes the module
unimportable, pytest exits `USAGE_ERROR` over a broken conftest, and the harness
files the row `BROKE` -- which is never `caught`. **Measured 2026-09-01: 20 of
this file's 64 rows, 31%.**

Every one of them has a test that would catch the behaviour, and none of those
tests can be collected once the configuration is unreadable. That is a cost of
reading configuration once at import, which is the right shape for a value read
in loops -- the alternative is a call at every use, which would also stop
`mock.patch.object(mutants, "UNMUTABLE", ...)` working. It is excused per
`(line, operator)` rather than by excluding the file, for the reasons CLAUDE.md
gives about `_lane` and `_born`: an exclusion is permanent and silent, so a
*future* unanswerable row here would never be reported.

One thing this rules out, and it was tried: routing `tests/support.py`'s `ROOT`
through this module. It is one expression fewer and it cost two more `BROKE`
rows, because a fixture's idea of where the tree is must not come from the thing
under test.

What extraction still changes, so the next reader is not surprised: an installed
harness has no tree of its own to read, and would take the root from pytest's
``rootdir`` or the invoking directory instead. That is one function, `_root`,
and it is the only thing here that knows where a project is.
"""

from __future__ import annotations

import fnmatch
import sys
from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

# survivor: boundary -- equivalent, and measured in `tupferl/config.py` for the same line:
#   `sys.version_info` on a real 3.11.0 is `(3, 11, 0, 'final', 0)`, which is already `> (3, 11)`.
#   The two spellings can only differ for a version tuple of exactly `(3, 11)`, which no
#   interpreter reports.
# survivor: branch -- `if True` is this program on every interpreter a sweep has ever run on,
#   because a sweep runs one interpreter and every one in CI's matrix except the 3.10 leg takes
#   this arm anyway. The 3.10 leg is what proves the other arm, and it is the one place no
#   mutation runs. Dropping the check there is an `ImportError` at every import of this module.
if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - the 3.10 CI leg is what proves this branch
    import tomli as tomllib


def _root() -> Path:
    """Where the project is -- the only thing in this module that knows.

    The tree this file lives in, not `Path.cwd()`, for the two reasons the
    module docstring gives. A function rather than one expression inline because
    it is the single seam extraction moves: an installed harness has no tree of
    its own to measure and would answer pytest's ``rootdir`` or the invoking
    directory here, and nothing else would change.

    `parents[1]` rather than `parent.parent` for the reason `tests/support.py`
    spells it that way: one index to read instead of two hops.
    """
    # survivor: off-by-one, return-value -- unanswerable; see "Why a mutation here is BROKE"
    #   above. a wrong root finds no `pyproject.toml`, so `mutable` is empty and `tools/mutants.py`
    #   has nothing to import -- and `tools/run_tests.py` takes its `--rootdir` from here.
    return Path(__file__).resolve().parents[1]


#: This project, resolved once at import.
ROOT = _root()

#: The table, in the file. Both spelled once because the error messages below
#: quote them, and a message naming a different table than the reader reads is
#: worse than no message.
FILE = "pyproject.toml"
TABLE = "tool.mutate"

#: Every environment variable `Settings.sandbox` may set. `unset` subtracts
#: what a given configuration did *not* set from this, so a probe never
#: inherits a half of the contract the project turned off.
_PROBE_NAMES = ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_PLUGINS")


@dataclass(frozen=True)
class Settings:
    """One project's answers. Frozen because nothing should be able to edit it.

    Every default is what a stranger's project would want, which for four of
    them is "nothing": a harness that guessed which directories were worth
    mutating would guess wrong quietly.
    """

    #: Path prefixes whose ``.py`` files are worth mutating. Never a tests
    #: directory: breaking a test proves nothing about the fix. Empty by default,
    #: and an empty answer makes `tools/mutate.py` refuse to run rather than
    #: sweep nothing and report a clean table.
    mutable: tuple[str, ...] = ()

    #: Never generated for, whatever `mutable` says. See `mutants.mutable` for
    #: what filled it in the project this came from.
    unmutable: tuple[str, ...] = ()

    #: Prepended to every environment variable the harness sets for a probe, so
    #: two projects sweeping at once cannot read each other's answers. Empty
    #: means the bare names -- ``MUTATE_BUDGET`` and friends.
    env_prefix: str = ""

    #: Prepended to every temporary directory the harness makes, so a leaked one
    #: says whose it was. `tools/mutate.py` and `tools/run_tests.py` append their
    #: own second word.
    tmp_prefix: str = "mutate-"

    #: The variable the host's own suite reads to pick a Hypothesis profile, and
    #: the profile a probe asks for. **Not derived from `env_prefix`**: this
    #: names a variable the *host project's tests* own, and deriving it would let
    #: a change of prefix rename someone else's variable silently. Either empty
    #: disables the hook, which is what a project without Hypothesis wants.
    hypothesis_profile_env: str = ""
    hypothesis_profile: str = ""

    #: Whether a probe lets pytest autoload installed plugins. Off is faster and
    #: reproducible across machines -- measured at 79.5 ms a probe here -- and is
    #: wrong for a project whose tests need an autoloaded plugin, which is why it
    #: is a knob rather than the constant it was. On by default: a stranger's
    #: suite should run the way their suite runs.
    probe_autoload: bool = True

    #: Plugins a probe force-loads through pytest's own ``PYTEST_PLUGINS``, which
    #: is what makes ``probe_autoload = false`` usable rather than a cliff: a
    #: project can turn autoload off and name the two plugins it actually needs.
    probe_plugins: tuple[str, ...] = ()

    #: Where the test modules are, as a path relative to the root.
    tests_dir: str = "tests"

    #: What `--accept` wraps a `# survivor:` tag to, which has to be the host's
    #: own formatter width. It was `mutate._COLUMNS = 100` -- this repository's
    #: `[tool.ruff] line-length`, in the harness -- and `--accept` writes those
    #: tags into the *host's* source: at 88 every tag the tool writes is a line
    #: the host's `ruff format --check` rejects, which is exactly the failure the
    #: constant existed to prevent, arriving in the project that cannot see the
    #: guard. 88 by default because that is what ruff and black use unasked.
    tag_columns: int = 88

    #: Names never copied into a mutation's sandbox, **on top of** the universal
    #: list in `mutate._SKIP`. A sandbox is copied once per lane per row, so this
    #: is the single largest cost a sweep can carry and it surfaces only as
    #: slowness. `.git` and the bytecode and tool caches are everybody's;
    #: `sweeps` and `.venv` are a spelling, and a host using `venv/`, `.tox/` or
    #: `node_modules/` has no way to say so without this.
    sandbox_ignore: tuple[str, ...] = ()

    #: How a source file's name predicts its test module's, ``{stem}`` being the
    #: source file's stem and the rest an `fnmatch` pattern against a test
    #: module's stem. **A wrong value costs a longer walk, never a wrong
    #: verdict** -- `mutants.targets_for` is an ordering, and `verdict.collect`
    #: walks everything the selection missed.
    test_module_patterns: tuple[str, ...] = ("test_{stem}", "test_{stem}_*")

    def env(self, suffix: str) -> str:
        """One environment variable's name, under this project's prefix."""
        # survivor: return-value -- unanswerable; see "Why a mutation here is BROKE"
        #   above. `None` here is a `None` in `tests/support.py`'s `CARRIES` tuple, which
        #   `os.environ` is filtered by before any test runs.
        return f"{self.env_prefix}_{suffix}" if self.env_prefix else suffix

    @property
    def budget_env(self) -> str:
        """How much memory a harness a probe starts may assume it has."""
        return self.env("MUTATE_BUDGET")

    @property
    def total_env(self) -> str:
        """What the whole run may spend, when the caller says so outright."""
        return self.env("MUTATE_TOTAL")

    @property
    def alarm_env(self) -> str:
        """The per-test alarm this run armed, for the suite under it to read."""
        # survivor: return-value -- unanswerable; see "Why a mutation here is BROKE"
        #   above. `support.ALARM` is this, read at the import of every test module.
        return self.env("MUTATE_EACH_TEST")

    @property
    def mutated_env(self) -> str:
        """Set when the tree under the suite is a mutated copy."""
        # survivor: return-value -- unanswerable; see "Why a mutation here is BROKE"
        #   above. `support.MUTATED` is this, and `over_a_mutated_tree` reads it at import.
        return self.env("MUTATE_MUTATED")

    @property
    def tests_package(self) -> str:
        """`tests_dir` as an import spells it -- ``tests``, ``src.tests``."""
        return self.tests_dir.strip("/").replace("/", ".")

    def tmp(self, what: str) -> str:
        """A temporary directory's prefix, under this project's own.

        One method rather than an f-string at each of the three sites, which is
        CLAUDE.md's own rule about a habit that has to be remembered per call
        site: two of the three are observed by a test and the third is this
        method, so there is one thing to get right instead of three.
        """
        return f"{self.tmp_prefix}{what}"

    @property
    def sandbox(self) -> dict[str, str]:
        """The environment every pytest a sweep starts runs under.

        Derived rather than written down, because it is a *contract*: `_run`
        spreads it into a probe's environment and `_collected` into its own, so
        what a sweep is graded against and what its cache is validated against
        cannot drift apart.

        ``PYTHONDONTWRITEBYTECODE`` is unconditional and is not a setting: a
        sandbox that leaves a ``.pyc`` behind is the stale-bytecode trap
        CLAUDE.md records, and no project wants it.
        """
        made = {"PYTHONDONTWRITEBYTECODE": "1"}
        if not self.probe_autoload:
            made["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        if self.probe_plugins:
            made["PYTEST_PLUGINS"] = ",".join(self.probe_plugins)
        return made

    @property
    def profile(self) -> dict[str, str]:
        """The Hypothesis hook a probe sets, or nothing at all.

        Empty unless the project named **both** a variable and a value. An empty
        variable name is not a variable, and an empty *value* is a profile
        `load_profile` would try to find and fail on inside the probe -- where a
        typo surfaces as `BROKE` on every row rather than as the typo it is.

        A property beside `sandbox` rather than a conditional spread inside
        `_run`'s `Popen` call, which is where it was first written: there its
        empty arm was reachable only from a project configured differently to
        this one, so no test in the tree could execute it.
        """
        if not (self.hypothesis_profile_env and self.hypothesis_profile):
            return {}
        return {self.hypothesis_profile_env: self.hypothesis_profile}

    @property
    def unset(self) -> tuple[str, ...]:
        """Names a probe must *not* inherit, given what `sandbox` did not set.

        **A dict of keys to add cannot turn a variable off**, and until this
        existed the default configuration had a hole the old constant did not:
        `PYTEST_DISABLE_PLUGIN_AUTOLOAD` used to be set unconditionally, so
        `probe_autoload = true` was expressed as *leaving it out* -- and `_run`
        spreads `sandbox` over `os.environ`, so an ambient one (a nested sweep,
        a CI that exports it) was inherited and the knob silently did nothing.
        A stranger's probes would then run without the plugins their suite
        needs, which is precisely what the knob exists to prevent.

        Derived from `sandbox` rather than listed beside it, so a name added
        there conditionally cannot be forgotten here.
        """
        return tuple(name for name in _PROBE_NAMES if name not in self.sandbox)

    def environment(self, base: Mapping[str, str], **extra: str) -> dict[str, str]:
        """`base`, with the sandbox contract applied and `extra` on top.

        The one place the three operations meet -- inherit, override, remove --
        because two spellings of a contract are two contracts, which is the
        argument `sandbox` already carried for the additions alone.
        """
        made = {name: value for name, value in base.items() if name not in self.unset}
        return {**made, **self.sandbox, **extra}

    def is_test_module(self, name: str) -> bool:
        """Whether a module stem under `tests_dir` is a test module or a helper.

        Derived from `test_module_patterns` with the stem left open rather than
        asked as a second knob: ``test_{stem}`` with anything for ``{stem}`` is
        exactly ``test_*``, so a project spelling its convention ``{stem}_test``
        gets ``*_test`` here for free and never states it twice.
        """
        return bool(self.test_modules("*", [name]))

    def test_modules(self, stem: str, names: list[str]) -> set[str]:
        """Which of `names` -- test module stems -- `test_module_patterns` picks.

        The patterns are matched against the stem rather than the file name so
        that a pattern says nothing about an extension, and `fnmatch` rather than
        `str.startswith` so that ``test_{stem}`` alone means an exact match and
        needs no second spelling for the anchored case.
        """
        wanted = [pattern.format(stem=stem) for pattern in self.test_module_patterns]
        return {name for name in names if any(fnmatch.fnmatch(name, want) for want in wanted)}


def _tuple(key: str, value: Any) -> tuple[str, ...]:
    """A TOML array of strings, or a message naming the key that was not one.

    `isinstance(value, list)` first, and not only "every item is a string":
    `"src/"` is iterable and every character of it is a string, so the shorter
    check accepts it and turns one prefix into eleven one-character ones.
    """
    # survivor: branch, drop-not, order -- unanswerable; see "Why a mutation here is BROKE"
    #   above. accepting a bad `mutable` or refusing this project's good one both end at the same
    #   place. `TestWhatItRefuses` is what would catch it, and cannot be collected.
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"[{TABLE}] {key} must be a list of strings, not {value!r}")
    # survivor: return-value -- unanswerable; see "Why a mutation here is BROKE"
    #   above. `mutants.MUTABLE` is `None` then, and `str.startswith(None)` at its import.
    return tuple(value)


def _scalar(key: str, value: Any, want: type) -> Any:
    """One value of the type the field declares, or a message saying so.

    `type(value) is not want` rather than `isinstance`, because `bool` is a
    subclass of `int` and TOML has both: an `isinstance` check would let a
    string field take nothing extra but would let ``probe_autoload = 1`` past,
    and a config that half-works is the thing this module refuses to be.
    """
    # survivor: branch, negate -- unanswerable; see "Why a mutation here is BROKE"
    #   above. refusing every scalar refuses this project's own table; accepting every one is caught
    #   by `TestWhatItRefuses`, which cannot be collected once the table is refused.
    if type(value) is not want:
        raise ValueError(f"[{TABLE}] {key} must be a {want.__name__}, not {value!r}")
    return value


def parse(raw: dict[str, Any]) -> Settings:
    """A ``[tool.mutate]`` table as a `Settings`, or a `ValueError` saying why.

    Split from `load` so a test can hand it a table without writing a file, and
    so the refusals below are reachable without one.
    """
    declared = {spec.name: spec for spec in fields(Settings)}
    # survivor: arith -- unanswerable; see "Why a mutation here is BROKE"
    #   above. `set(raw) + set(declared)` is a `TypeError` at import.
    unknown = sorted(set(raw) - set(declared))
    # survivor: branch -- unanswerable; see "Why a mutation here is BROKE"
    #   above. always-taken refuses this project's own table at import; the never-taken half is
    #   caught.
    if unknown:
        raise ValueError(
            f"[{TABLE}] has no key(s) {', '.join(unknown)}; "
            f"the ones it takes are {', '.join(sorted(declared))}."
        )
    made: dict[str, Any] = {}
    for key, value in raw.items():
        # The type of the field's own **default**, not of its annotation.
        # `from __future__ import annotations` makes every `Field.type` the
        # *source text* -- `"tuple[str, ...]"` -- so dispatching on it means
        # string-matching, and the first version did: `startswith("tuple")`,
        # then `bool` if the text was exactly `"bool"`, and `str` for everything
        # else. That last arm is the problem. It is not a default, it is a
        # silent mis-type: the first numeric knob added here would be refused
        # for every legal value with a message naming the wrong type, and no
        # test could see it until the knob existed. Every field has a plain
        # default, so `type(...)` of it is exact and needs no map to maintain.
        want = type(declared[key].default)
        # survivor: negate -- unanswerable; see "Why a mutation here is BROKE"
        #   above. every list is then checked as a scalar and every scalar as a list, so the table
        #   is refused at import.
        made[key] = _tuple(key, value) if want is tuple else _scalar(key, value, want)
    # survivor: return-value -- unanswerable; see "Why a mutation here is BROKE"
    #   above. `SETTINGS` is `None`, and `SETTINGS.mutable` at `tools/mutants.py`'s import.
    return Settings(**made)


def load(root: Path) -> Settings:
    """`root`'s settings, or the defaults where it has no table to read.

    A missing ``pyproject.toml`` and a missing table mean the same thing and are
    not an error: the harness has to be importable from a tree that has not
    configured it yet, and what it then refuses is running a sweep with nothing
    mutable, which says so in one sentence.

    ``tomllib`` is 3.11; ``tomli`` is the library it was taken from, and the
    3.10 CI leg is what proves that branch is reachable -- the same shim, and the
    same argument, as `tupferl/config.py`. **Unlike `config.py` it is imported at
    module scope**, and the first version of this deferred it with `config.py`'s
    reason copied across: that deferral buys nothing here, because `SETTINGS =
    load(ROOT)` runs `load` during import anyway. Measured: 0.9 ms of a 14 ms
    ``import tools.settings``, 1.9 ms median paired difference on the whole of
    `tests/support.py`'s 110 ms import -- 0.24 s of CPU across the suite's 128
    batches. `config.py`'s deferral is real because `toml()` is only reached when
    a file is actually read.
    """
    found = root / FILE
    try:
        raw = tomllib.loads(found.read_text(encoding="utf-8"))
    except OSError:
        return Settings()
    except tomllib.TOMLDecodeError as broken:
        raise ValueError(f"{found} is not valid TOML ({broken}); fix that line.") from broken
    # Walked from `TABLE` rather than spelled `raw["tool"]["mutate"]`, which is
    # the drift that constant's own comment says it prevents and which the first
    # version of this line committed anyway.
    table: Any = raw
    for step in TABLE.split("."):
        if not isinstance(table, dict) or step not in table:
            return Settings()
        table = table[step]
    # survivor: branch, drop-not -- unanswerable; see "Why a mutation here is BROKE"
    #   above. always-taken refuses this project's own table; the other half hands a string to
    #   `parse`.
    if not isinstance(table, dict):
        raise ValueError(f"[{TABLE}] in {found} must be a table, not {table!r}")
    # survivor: return-value -- unanswerable; see "Why a mutation here is BROKE"
    #   above. `SETTINGS` is `None`, as two entries above.
    return parse(table)


#: This project's answers, read once. A module-level singleton rather than a call
#: at each use, because `mutants.MUTABLE` and friends are read in loops and the
#: file does not change while a process runs.
SETTINGS = load(ROOT)
