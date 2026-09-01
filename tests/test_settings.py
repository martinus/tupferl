"""What `tools/settings.py` reads, refuses, and derives.

Three questions, and they are separate on purpose:

- **the table this repository writes reproduces the constants Phase D
  replaced.** That is the phase's acceptance gate spelled as a test, and it is
  the only place in the tree where those literals still appear;
- **a wrong table is refused rather than half-applied**, because a knob that
  silently kept its default is a config file that lies about what is in force;
- **a different table really changes what the harness does.** Asserted against a
  *scratch project* driven in a subprocess, not against `SETTINGS` in this
  process -- the whole claim is that a second project can configure this, and
  only a second tree can show it.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from tests import support
from tools import mutants, mutate, settings
from tools.settings import Settings
from tupferl import config

#: What this project's `[tool.mutate]` table has to come back as: the constants
#: that stood in `tools/` before Phase D, character for character. If a change
#: here is deliberate it is a change to what every sweep since has measured, so
#: it belongs in a PR that says so.
TODAY = Settings(
    mutable=("tupferl/", "tools/"),
    unmutable=(),
    env_prefix="TUPFERL",
    tmp_prefix="tupferl-",
    hypothesis_profile_env="TUPFERL_HYPOTHESIS_PROFILE",
    hypothesis_profile="mutation",
    probe_autoload=False,
    probe_plugins=(),
    tag_columns=100,
    sandbox_ignore=("sweeps", ".venv"),
    tests_dir="tests",
    test_module_patterns=("test_{stem}", "test_{stem}_*"),
)

#: A table with **every** knob different from both the defaults and this
#: project's answers. Both halves matter: a value equal to the default would
#: pass against a reader that never opened the file, and a value equal to
#: tupferl's would pass against one that read the wrong tree's.
OTHER = """
[tool.mutate]
mutable = ["src/"]
unmutable = ["src/dangerous.py"]
env_prefix = "OTHER"
tmp_prefix = "other-"
hypothesis_profile_env = "OTHER_HYPOTHESIS"
hypothesis_profile = "quick"
probe_autoload = true
probe_plugins = ["myplugin", "otherplugin"]
tag_columns = 72
sandbox_ignore = ["node_modules"]
tests_dir = "checks"
test_module_patterns = ["check_{stem}"]

# Stated a second time, in pytest's own vocabulary, because the harness has two
# readers of "which files are test modules" and only one of them is this table:
# `verdict.collect`'s outward walk asks the host's pytest for `python_files`
# (Phase A) and the *selection* asks `test_module_patterns`. A project that set
# only the second would get a selection its own pytest cannot collect -- which
# costs a longer walk rather than a wrong verdict, and is still a foot-gun a
# demonstration should not model. Defaulting one from the other is recorded in
# `docs/pytest-plan.md` as considered and declined: nothing in tupferl exercises
# it, because tupferl declares neither.
[tool.pytest.ini_options]
python_files = ["check_*.py"]
"""

#: What the subprocess in `TestASecondProjectConfiguresIt` prints back. Every
#: line asks the *harness* a question rather than reading `SETTINGS` back, so a
#: reader that parsed the file and then went unread would fail every one.
ASKS = """
import json
from pathlib import Path
from tools import mutants, mutate, settings

root = Path.cwd()
print(json.dumps({
    "root": str(settings.ROOT),
    "mutable": list(mutants.MUTABLE),
    "unmutable": list(mutants.UNMUTABLE),
    "walked": sorted(mutants.every_line(root)),
    "targets": mutants.targets_for("src/thing.py", root),
    "alarm": mutate._ALARM,
    "budget": mutate._BUDGET,
    "total": mutate._TOTAL,
    "mutated": mutate._MUTATED,
    "profile_env": mutate._PROFILE,
    "profile": mutate._MUTATION_PROFILE,
    "sandbox": settings.SETTINGS.environment({}),
    "unset": list(settings.SETTINGS.unset),
    "tmp": settings.SETTINGS.tmp("verdict-"),
    "columns": mutate._COLUMNS,
    "skipped": sorted(mutate._SKIP("x", ["node_modules", "sweeps", ".git", "src"])),
    "refusal": mutate._mutable_prefixes(),
}))
"""

#: How long the one subprocess here may take. It is an import of `tools` and a
#: walk of four small files -- under a second unloaded -- and the number is what
#: a loaded sweep machine needs. Through `bounded` because every wait on a child
#: under `tests/` is, and because 20s against a `--each-test 10` sweep would
#: lose the race to the alarm and file the row `BROKE`.
ASKING = support.bounded(20.0)

#: What `ASKS` prints back, parsed.
Answers = dict[str, Any]

#: What `tmp_table` hands a test: a root, optionally with a `pyproject.toml` in
#: it. Named because four signatures spell it.
Table = Callable[[str | None], Path]


@pytest.fixture(scope="class")
def elsewhere() -> Iterator[Path]:
    """A whole second project: its own config, its own layout, its own copy.

    `tools/` is copied in rather than imported from here, and that is the point
    of the fixture: `settings.ROOT` is the tree the harness *lives* in, so a
    scratch project that merely sits beside this one would be configured by this
    one's `pyproject.toml`. Copying is also what extraction looks like -- an
    installed harness sits inside the project it measures.

    Class-scoped, and `answers` caches: nothing here mutates the tree, and at
    function scope the five tests below paid the same 110 ms subprocess five
    times over five identical copies -- 0.43 s of the module's 0.59 s, in a class
    whose first test argues in its own docstring that the spawn is the cost.
    `mutate._SKIP` rather than a second ignore list, for the reason this whole
    change exists.
    """
    with support.tempdir(prefix="tupferl-elsewhere-") as box:
        shutil.copytree(support.ROOT / "tools", box / "tools", ignore=mutate._SKIP)
        (box / "pyproject.toml").write_text(OTHER, encoding="utf-8")
        (box / "src").mkdir()
        (box / "src" / "thing.py").write_text("VALUE = 1\n", encoding="utf-8")
        (box / "src" / "dangerous.py").write_text("VALUE = 2\n", encoding="utf-8")
        (box / "checks").mkdir()
        (box / "checks" / "check_thing.py").write_text(
            "def check_it():\n    pass\n", encoding="utf-8"
        )
        (box / "checks" / "test_thing.py").write_text(
            "def test_it():\n    pass\n", encoding="utf-8"
        )
        yield box


class TestThisProjectsTableIsYesterdaysConstants:
    """The gate. Every literal Phase D took out of `tools/` is here, once."""

    @pytest.mark.parametrize("knob", list(TODAY.__dataclass_fields__))
    def test_the_table_reproduces_it(self, knob: str) -> None:
        """One case per knob rather than one comparison of two dataclasses, so a
        failure names the knob instead of printing two ten-field objects and
        leaving the reader to diff them."""
        assert getattr(settings.SETTINGS, knob) == getattr(TODAY, knob)

    def test_every_knob_is_written_down_in_the_file(self) -> None:
        """The table is the documentation of what the table takes, so a field
        added to `Settings` and left out of `pyproject.toml` is a knob nobody
        knows to reach for. Read out of the file rather than off `SETTINGS`,
        which would be satisfied by the defaults.

        **Parsed, not grepped**, which is CLAUDE.md's thrice-recorded rule about
        reading a config file in a test and was got wrong here on the first
        attempt: the original walked the raw text and split every line on `=`,
        so a comment inside the table quoting a setting -- and the table has six
        of them -- would have been read as a stated key.

        Through `config.toml()` rather than `import tomllib`, which is 3.11: a
        module-scope import of it here is red on the 3.10 leg, and mypy said so
        before CI could. `tools/` may not import the package and carries its own
        copy of the shim; `tests/` may, and `tests/test_packaging.py` reads this
        same file the same way.
        """
        table: Any = config.toml().loads((settings.ROOT / settings.FILE).read_text("utf-8"))
        for step in settings.TABLE.split("."):
            table = table[step]
        assert set(table) == set(TODAY.__dataclass_fields__)

    def test_the_defaults_are_not_this_project(self) -> None:
        """**The reason the defaults are generic**, and the test that makes
        every other one in this class able to fail: if `Settings()` already
        answered `tupferl`, a reader that never opened `pyproject.toml` would
        pass the whole file above."""
        assert Settings() != TODAY

    @pytest.mark.parametrize("name", ["ALARM", "MUTATED"])
    def test_the_fixtures_derive_these_names_rather_than_spelling_them(self, name: str) -> None:
        """`tests/support.py` assigns each of these from `settings`, not from a
        literal, and this reads the source to say so.

        `ROOT` is deliberately **not** in the list: `tests/support.py` derives
        its own, because a fixture's idea of where the tree is must not come
        from the thing under test. Routing it here cost two `caught` rows --
        `settings._root` mutations became `BROKE`, which is never `caught`. The
        two values are compared in `TestWhereItReads` instead, which is a real
        check because they are computed apart.

        **Comparing the values instead is what cannot fail.** Both ends are one
        expression apart now, so `support.ALARM == mutate._ALARM` holds however
        either is written -- and it would go on holding if somebody typed
        `"TUPFERL_MUTATE_EACH_TEST"` back into `support.py`, which is precisely
        the state Phase D removed. The two value comparisons in `test_support`
        are kept as a tripwire against a literal with a *different* spelling;
        this is the one that sees a literal with the same spelling, and that is
        the one which then rots the next time `env_prefix` moves.

        Reading the repository's own source is a source-shaped claim, and
        `tests/` is not mutable, so no `over_a_mutated_tree` guard applies: a
        probe's copy of this file is the same file.
        """
        source = ast.parse((support.ROOT / "tests" / "support.py").read_text(encoding="utf-8"))
        assigned = [
            node.value
            for node in source.body
            if isinstance(node, ast.Assign)
            and any(isinstance(at, ast.Name) and at.id == name for at in node.targets)
        ]
        assert len(assigned) == 1, name
        assert isinstance(assigned[0], ast.Attribute), ast.unparse(assigned[0])
        assert isinstance(assigned[0].value, ast.Name)
        assert assigned[0].value.id in {"settings", "SETTINGS"}


class TestWhatItRefuses:
    """A table that is wrong says so. Half-applying it is the failure mode."""

    def test_a_misspelled_key_is_named_with_the_ones_it_takes(self) -> None:
        with pytest.raises(ValueError) as refused:
            settings.parse({"mutible": ["src/"]})
        assert "mutible" in str(refused.value)
        assert "mutable" in str(refused.value)

    def test_several_misspelled_keys_are_named_in_a_settled_order(self) -> None:
        """Sorted, so two runs over the same table produce the same sentence.

        **Eight of them, and the count is the test.** The unknown keys come out
        of a `set` difference, whose iteration order Python randomises per run,
        so `sorted` becoming `list` is caught only when that order happens to
        differ from sorted -- with two keys a coin flip, and a guard that
        sometimes guards reads exactly like one that always does. Eight is 1 in
        40320, which is the ratio CLAUDE.md names for this shape.
        """
        wrong = ["hh", "gg", "ff", "ee", "dd", "cc", "bb", "aa"]
        with pytest.raises(ValueError) as refused:
            settings.parse(dict.fromkeys(wrong, "x"))
        assert "aa, bb, cc, dd, ee, ff, gg, hh" in str(refused.value)

    def test_a_list_where_a_string_belongs_is_refused(self) -> None:
        with pytest.raises(ValueError, match="env_prefix"):
            settings.parse({"env_prefix": ["OTHER"]})

    def test_a_string_where_a_list_belongs_is_refused(self) -> None:
        """`"src/"` is iterable and every character of it is a string, so a
        check written as "all items are strings" accepts it and `mutable`
        becomes eleven one-character prefixes."""
        with pytest.raises(ValueError, match="mutable"):
            settings.parse({"mutable": "src/"})

    def test_a_number_where_a_flag_belongs_is_refused(self) -> None:
        """`isinstance(True, int)` is true, so the obvious spelling of this
        check takes `1` and `0` for booleans and would take `2` as well."""
        with pytest.raises(ValueError, match="probe_autoload"):
            settings.parse({"probe_autoload": 1})

    def test_a_flag_where_a_string_belongs_is_refused(self) -> None:
        with pytest.raises(ValueError, match="tmp_prefix"):
            settings.parse({"tmp_prefix": True})

    def test_a_table_that_is_not_a_table_is_refused(self, tmp_table: Table) -> None:
        with pytest.raises(ValueError, match="must be a table"):
            settings.load(tmp_table('[tool]\nmutate = "yes"\n'))

    def test_a_file_that_is_not_toml_is_refused(self, tmp_table: Table) -> None:
        """Loudly, rather than falling back to the defaults: a project whose
        `pyproject.toml` does not parse has a bigger problem than this table,
        and silently sweeping the wrong prefixes would hide it."""
        with pytest.raises(ValueError, match="not valid TOML"):
            settings.load(tmp_table("[tool.mutate\n"))


@pytest.fixture
def tmp_table() -> Iterator[Table]:
    """A throwaway root holding one `pyproject.toml`, or none at all.

    `support.tempdir` rather than pytest's `tmp_path`, which keeps three
    numbered roots per user and is raced by every probe of a sweep.
    """
    with support.tempdir(prefix="tupferl-table-") as box:

        def written(text: str | None) -> Path:
            if text is not None:
                (box / settings.FILE).write_text(text, encoding="utf-8")
            return box

        yield written


class TestWhereItReads:
    def test_a_tree_with_no_pyproject_gets_the_defaults(self, tmp_table: Table) -> None:
        """Not an error. The harness has to be importable from a tree that has
        not configured it yet; what it refuses is *running* with nothing
        mutable, which says so in one sentence."""
        assert settings.load(tmp_table(None)) == Settings()

    def test_a_pyproject_with_no_table_gets_the_defaults(self, tmp_table: Table) -> None:
        assert settings.load(tmp_table('[project]\nname = "elsewhere"\n')) == Settings()

    def test_it_reads_the_root_it_is_given(self, tmp_table: Table) -> None:
        """The one thing `load` is for. Handed another tree it answers about
        that tree, which is what makes `ROOT` a choice rather than a constant --
        and what the scratch-project class below rests on."""
        assert settings.load(tmp_table(OTHER)).mutable == ("src/",)

    def test_the_repository_is_what_the_module_read(self) -> None:
        """`SETTINGS` is `load(ROOT)` and `ROOT` is this tree, so a `ROOT` that
        pointed anywhere else would give the whole suite someone else's
        prefixes."""
        assert settings.ROOT == support.ROOT
        assert settings.load(support.ROOT) == settings.SETTINGS


class TestTheNamesItDerives:
    """The arithmetic, asked of `Settings` directly so the cases are cheap."""

    def test_a_prefix_is_prepended_with_one_underscore(self) -> None:
        assert Settings(env_prefix="OTHER").env("MUTATE_BUDGET") == "OTHER_MUTATE_BUDGET"

    def test_no_prefix_leaves_the_bare_name(self) -> None:
        """Not `_MUTATE_BUDGET`. A leading underscore is legal in a shell and
        invisible in a diff, which is the kind of name that gets set once and
        read never."""
        assert Settings().env("MUTATE_BUDGET") == "MUTATE_BUDGET"

    @pytest.mark.parametrize(
        ("knob", "suffix"),
        [
            ("budget_env", "MUTATE_BUDGET"),
            ("total_env", "MUTATE_TOTAL"),
            ("alarm_env", "MUTATE_EACH_TEST"),
            ("mutated_env", "MUTATE_MUTATED"),
        ],
    )
    def test_each_variable_takes_the_prefix(self, knob: str, suffix: str) -> None:
        assert getattr(Settings(env_prefix="OTHER"), knob) == f"OTHER_{suffix}"

    def test_the_four_names_are_distinct(self) -> None:
        """Two of them colliding would make a probe's budget its alarm, and the
        `_run` that set both would simply keep the second."""
        made = Settings(env_prefix="OTHER")
        assert len({made.budget_env, made.total_env, made.alarm_env, made.mutated_env}) == 4

    def test_a_temporary_directory_takes_the_prefix(self) -> None:
        assert Settings(tmp_prefix="other-").tmp("verdict-") == "other-verdict-"

    def test_a_nested_tests_directory_becomes_a_dotted_package(self) -> None:
        assert Settings(tests_dir="src/checks").tests_package == "src.checks"

    def test_bytecode_is_always_off_in_a_sandbox(self) -> None:
        """Not a setting and not conditional. A sandbox that leaves a `.pyc`
        behind is the stale-bytecode trap CLAUDE.md records, where a mutation of
        the same size in the same second is read past."""
        assert Settings().sandbox["PYTHONDONTWRITEBYTECODE"] == "1"
        assert Settings(probe_autoload=False).sandbox["PYTHONDONTWRITEBYTECODE"] == "1"

    def test_autoload_is_left_alone_unless_the_project_turns_it_off(self) -> None:
        """The default is a stranger's suite running the way their suite runs.
        `PYTEST_DISABLE_PLUGIN_AUTOLOAD=0` would *not* do this: pytest tests the
        variable for being set at all."""
        assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in Settings().sandbox
        assert Settings(probe_autoload=False).sandbox["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"

    def test_named_plugins_travel_through_pytests_own_variable(self) -> None:
        """`PYTEST_PLUGINS` rather than a `-p` slot on the probe's command line,
        because `tools/verdict.py` is read as source text into the sandbox and
        may import nothing from `tools` -- so it cannot be told this any other
        way without growing an argv position for it."""
        made = Settings(probe_plugins=("one", "two")).sandbox
        assert made["PYTEST_PLUGINS"] == "one,two"

    @pytest.mark.parametrize(
        ("env", "value", "want"),
        [
            ("OTHER_HYPOTHESIS", "quick", {"OTHER_HYPOTHESIS": "quick"}),
            ("", "quick", {}),
            ("OTHER_HYPOTHESIS", "", {}),
            ("", "", {}),
        ],
    )
    def test_the_hypothesis_hook_needs_both_halves(
        self, env: str, value: str, want: dict[str, str]
    ) -> None:
        """All four combinations, because three of them are the same answer for
        three different reasons and a test of one proves nothing about the
        others. An empty variable *name* is not a variable; an empty *value* is
        a profile `load_profile` would try to find and fail on inside a probe,
        where it surfaces as `BROKE` on every row rather than as the typo it
        is; and a project with no Hypothesis has neither."""
        assert Settings(hypothesis_profile_env=env, hypothesis_profile=value).profile == want

    @pytest.mark.parametrize(
        ("made", "want"),
        [
            (Settings(), ("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "PYTEST_PLUGINS")),
            (Settings(probe_autoload=False), ("PYTEST_PLUGINS",)),
            (Settings(probe_plugins=("one",)), ("PYTEST_DISABLE_PLUGIN_AUTOLOAD",)),
            (Settings(probe_autoload=False, probe_plugins=("one",)), ()),
        ],
    )
    def test_what_is_removed_is_exactly_what_was_not_set(
        self, made: Settings, want: tuple[str, ...]
    ) -> None:
        """Derived from `sandbox` rather than listed beside it, so the two
        halves cannot part -- and asked of all four configurations, because a
        test of one arm is satisfied by a `unset` that returns a constant."""
        assert made.unset == want

    def test_an_inherited_variable_the_project_did_not_ask_for_is_removed(self) -> None:
        """The hole the knob opened, and the reason `environment` exists at all.

        `_run` spreads the contract over `os.environ`, so before this a
        `probe_autoload = true` project running under a sweep -- which exports
        `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` to its probes, and those probes run
        this suite, which starts more sweeps -- inherited the ambient one and
        the knob silently did nothing. Both directions here: the name the
        project did not set is gone, and one it did not mention is kept.
        """
        got = Settings().environment(
            {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_PLUGINS": "ghost", "PATH": "/bin"}
        )
        assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD" not in got
        assert "PYTEST_PLUGINS" not in got
        assert got["PATH"] == "/bin"

    def test_the_project_that_asked_for_them_keeps_them(self) -> None:
        """The other half: `unset` must not remove what `sandbox` just set."""
        got = Settings(probe_autoload=False, probe_plugins=("one",)).environment(
            {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "0", "PYTEST_PLUGINS": "ghost"}
        )
        assert got["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
        assert got["PYTEST_PLUGINS"] == "one"

    def test_extras_win_over_the_contract_and_the_environment(self) -> None:
        """`_run` passes the budget, the alarm and the mutated marker as extras
        over a base that may already carry a stale one from the sweep that
        started it -- an inner harness reading the outer's budget is how 4,340
        processes came to be alive at once."""
        got = Settings().environment({"X": "old"}, X="new")
        assert got["X"] == "new"

    def test_no_plugins_sets_nothing(self) -> None:
        """An empty `PYTEST_PLUGINS` is a plugin named the empty string, which
        pytest tries to import and fails on -- inside the probe, where it
        surfaces as `BROKE` on every row rather than as the typo it is."""
        assert "PYTEST_PLUGINS" not in Settings().sandbox

    @pytest.mark.parametrize(
        ("patterns", "want"),
        [
            (("test_{stem}",), {"test_sync"}),
            (("test_{stem}", "test_{stem}_*"), {"test_sync", "test_sync_cli"}),
            (("check_{stem}",), {"check_sync"}),
            (("{stem}_test",), {"sync_test"}),
        ],
    )
    def test_the_patterns_pick_the_test_modules(
        self, patterns: tuple[str, ...], want: set[str]
    ) -> None:
        """`test_syncing` is in every fixture and in no answer: an anchored
        pattern has to mean an exact match, or the convention picks up every
        module whose name merely starts the same way."""
        found = ["test_sync", "test_sync_cli", "test_syncing", "check_sync", "sync_test", "test_ci"]
        assert Settings(test_module_patterns=patterns).test_modules("sync", found) == want

    @pytest.mark.parametrize(
        ("patterns", "name", "want"),
        [
            (("test_{stem}", "test_{stem}_*"), "test_sync", True),
            (("test_{stem}", "test_{stem}_*"), "support", False),
            (("{stem}_test",), "sync_test", True),
            (("{stem}_test",), "test_sync", False),
        ],
    )
    def test_a_helper_is_told_from_a_test_module_by_the_same_patterns(
        self, patterns: tuple[str, ...], name: str, want: bool
    ) -> None:
        """`importers` follows a helper's imports one level and indexes a test
        module's directly, so getting this backwards would make `tests/support.py`
        a test module that imports nothing and lose every edge through it."""
        assert Settings(test_module_patterns=patterns).is_test_module(name) is want


@pytest.fixture(scope="class")
def answers(elsewhere: Path) -> Answers:
    """What the harness inside `elsewhere` says about itself, asked once.

    A class-scoped fixture rather than a cached method, and the difference is
    measured: `functools.lru_cache` on the method was the first spelling and it
    cached nothing, because pytest builds a fresh instance for every test and
    `self` is part of the key. Seven tests, seven 90 ms subprocesses, and the
    `maxsize=1` beside it read as though the question were settled -- §8's
    "distrust a pass you cannot explain" applied to a speed-up rather than to a
    verdict. `--durations` is what said so.
    """
    done = subprocess.run(
        [sys.executable, "-c", ASKS],
        cwd=elsewhere,
        capture_output=True,
        text=True,
        timeout=ASKING,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return dict(json.loads(done.stdout))


class TestASecondProjectConfiguresIt:
    """The claim the phase exists for, asked of a project that is not this one.

    Everything above could pass against a harness that read the file and then
    ignored it. This drives the real `tools/` from inside a tree whose every
    knob differs, and asks the harness -- not the settings -- what it thinks.
    """

    def test_every_knob_arrives_overridden(self, answers: Answers, elsewhere: Path) -> None:
        """One test rather than thirteen, because each assertion names its knob
        and a failure still says which. The subprocess is no longer the argument
        for grouping them -- `answers` is class-scoped, so every test in this
        class shares one -- but a reader comparing a whole configuration against
        a whole answer is still better served by one place to look."""
        got = answers
        # `.resolve()`, because `settings._root` does and **macOS is where that
        # shows**: `/var/folders/...` is a symlink to `/private/var/folders/...`,
        # so `tempfile` hands back the first and the harness reports the second.
        # Green on all three Linux legs and red on `macos (3)`, which is the
        # shape CLAUDE.md collects -- a test that could only fail on one
        # platform, and this one had no label saying so because nobody knew.
        assert got["root"] == str(elsewhere.resolve())
        assert got["mutable"] == ["src/"]
        assert got["unmutable"] == ["src/dangerous.py"]
        assert got["alarm"] == "OTHER_MUTATE_EACH_TEST"
        assert got["budget"] == "OTHER_MUTATE_BUDGET"
        assert got["total"] == "OTHER_MUTATE_TOTAL"
        assert got["mutated"] == "OTHER_MUTATE_MUTATED"
        assert got["profile_env"] == "OTHER_HYPOTHESIS"
        assert got["profile"] == "quick"
        assert got["tmp"] == "other-verdict-"
        assert got["columns"] == 72
        assert got["sandbox"] == {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_PLUGINS": "myplugin,otherplugin",
        }

    def test_what_a_probe_there_must_not_inherit_follows_the_knobs(self, answers: Answers) -> None:
        """`probe_autoload = true` has to *remove* an inherited
        `PYTEST_DISABLE_PLUGIN_AUTOLOAD`, not merely decline to set one -- a
        sweep started from inside another sweep would otherwise hand its own
        down and the knob would silently do nothing. `probe_plugins` is named,
        so that half is set rather than removed."""
        assert answers["unset"] == ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"]

    def test_the_sandbox_copy_leaves_out_what_that_project_named(self, answers: Answers) -> None:
        """`node_modules` is nobody's business but that project's, and `sweeps`
        is this one's -- so both halves are here: the configured name is skipped
        and tupferl's is not, which is what separates "it read the table" from
        "the list is still hardcoded"."""
        skipped = answers["skipped"]
        assert "node_modules" in skipped
        assert ".git" in skipped
        assert "sweeps" not in skipped
        assert "src" not in skipped

    def test_the_walk_takes_the_configured_prefix_and_not_this_ones(self, answers: Answers) -> None:
        """`--all` over a project that has no `tupferl/` and does have a
        `tools/`: the copied harness is *there*, so a `mutable` that had not
        moved would come back with `tools/**.py` in it and read as a working
        sweep."""
        assert answers["walked"] == ["src/thing.py"]

    def test_the_unmutable_entry_is_left_out_of_the_walk(self, answers: Answers) -> None:
        """`src/dangerous.py` is under `mutable` and named in `unmutable`, so
        it is the one file that separates the two knobs. Empty in this
        repository, which is why nothing here can show it.

        **The sibling is asserted first, and that is not decoration.** Measured:
        with the config reader disabled the walk comes back empty, and "this
        file is not in the answer" is then satisfied by there being no answer --
        the negative assertion whose precondition was never established that
        CLAUDE.md §2 lists. The positive half is what establishes it.
        """
        walked = answers["walked"]
        assert "src/thing.py" in walked
        assert "src/dangerous.py" not in walked

    def test_the_selection_comes_from_the_configured_tests_directory(
        self, answers: Answers
    ) -> None:
        """`checks/check_thing.py` matches the configured pattern and
        `checks/test_thing.py` does not, so this separates "it found the
        directory" from "it kept the old convention" -- both files are there and
        only one is an answer."""
        assert answers["targets"] == "checks.check_thing"

    def test_the_refusal_names_the_configured_prefixes(self, answers: Answers) -> None:
        """The message a stranger sees first. Naming `tupferl/` at somebody
        else's project is the whole of the problem this phase is about."""
        assert answers["refusal"] == "src/**.py"


class TestWhatARunWithNothingMutableSays:
    def test_an_empty_setting_says_so_rather_than_blaming_the_diff(self) -> None:
        """ "nothing mutable changed" and "you have not said what is mutable"
        are the same exit status, and only one of them is a fact about the
        tree."""
        with mock.patch.object(mutants, "MUTABLE", ()):
            said = mutate._mutable_prefixes()
        assert settings.TABLE in said
        assert settings.FILE in said

    def test_the_prefixes_are_named_as_a_glob(self) -> None:
        with mock.patch.object(mutants, "MUTABLE", ("src/", "extra/")):
            assert mutate._mutable_prefixes() == "src/**.py and extra/**.py"


class TestTheTemporaryDirectoriesAreNamed:
    """Two of the three sites, driven. The third is `Settings.tmp` itself.

    Written this way round because CLAUDE.md's own lesson about a rule spelled
    per call site is that the habit rots: `tmp` is the one place the prefix is
    joined, and these two say it really reaches the code.
    """

    def test_the_sandbox_pool_names_its_holder(self) -> None:
        """`count=0`, so no tree is copied and the test costs a `mkdtemp`. The
        pool is a generator context manager and the copy loop is what is
        expensive about it."""
        made: list[str] = []
        #: Bound before the patch, so the stand-in below calls the real thing
        #: rather than itself. `tools/mutate.py` does `import tempfile`, so
        #: patching the attribute on the module object patches what it reads.
        real = tempfile.TemporaryDirectory

        def watched(**how: Any) -> Any:
            made.append(str(how["prefix"]))
            return real(**how)

        with (
            mock.patch.object(tempfile, "TemporaryDirectory", watched),
            mutate._sandboxes(0),
        ):
            pass
        assert made == ["tupferl-mutate-"]
