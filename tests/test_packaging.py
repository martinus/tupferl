"""What tupferl depends on at runtime, asserted rather than intended.

**On Python 3.11 and newer this package imports nothing outside the standard
library.** On 3.10 it imports exactly one thing, `tomli`, which is the library
`tomllib` was taken from -- so the single dependency is a backport of a stdlib
module, and it disappears on the interpreters that have it.

That is worth a test rather than a sentence. A dependency is added by one import
in one commit, and nothing else in this suite would notice: the code works, the
tests pass, and the supply chain grew. The claim in the README decays silently
and in the flattering direction, which is the shape CLAUDE.md section 8 is about.

Two directions, because each catches what the other cannot:

- an import that is *not* declared crashes on a fresh install, and only on a
  machine that does not happen to have the package already;
- a declaration that is *not* imported is a dependency nobody notices going
  stale -- which is the reason `pyproject.toml` does not list `rich` even though
  plan section 5 sanctions it.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from tools import mutate
from tupferl import config

#: The repository root: this file's parent's parent.
ROOT = Path(__file__).resolve().parent.parent

#: The one third-party name the package may import, and the version floor below
#: which it must be declared.
#:
#: `tomli` rather than "no dependencies at all" because 3.10 has no `tomllib`
#: and this project supports 3.10 -- see `config.toml`, whose branch the 3.10 CI
#: leg exists to prove reachable. It is the same code under a different name.
BACKPORT = "tomli"
BACKPORT_UNTIL = (3, 11)

#: Standard library on *some* interpreter this project supports, which is not
#: the same set as `sys.stdlib_module_names` on the one running the test.
#:
#: The walk below reads source rather than importing, so it sees both arms of
#: `config.toml`'s version shim whichever interpreter it runs on -- and on 3.10
#: `tomllib` is not in `stdlib_module_names`, so the stdlib module tupferl
#: imports on 3.11 looks third-party there. Measured: this test passed on 3.11
#: and 3.12 and took the `test (3.10)` leg red on its first push.
#:
#: Named here rather than by widening the assertion, because the expectation
#: must not depend on which leg is running: a test whose answer changes with the
#: interpreter is one whose green means something different on each machine.
ALSO_STDLIB = frozenset({"tomllib"})


def imported(where: Path) -> dict[str, set[str]]:
    """Every top-level module name `where`'s Python files import, by name.

    Read with `ast` rather than by importing: an import that only happens on
    another interpreter -- which is exactly the case under test -- would not
    execute here, and a test that could not see the 3.10 branch is a test that
    passes on 3.11 for the wrong reason.
    """
    found: dict[str, set[str]] = {}
    for path in sorted(where.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                found.setdefault(name, set()).add(path.name)
    return found


def pyproject() -> dict[str, Any]:
    """`pyproject.toml`, read through the package's own shim.

    `config.toml()` rather than a direct `tomllib` import, so this reads TOML
    the way tupferl does -- including on the 3.10 leg, where the module under
    discussion is the one doing the reading.
    """
    with (ROOT / "pyproject.toml").open("rb") as handle:
        loaded: dict[str, Any] = config.toml().load(handle)
    return loaded


@pytest.fixture(scope="module")
def imports() -> dict[str, set[str]]:
    """What the package reaches for, by name.

    Module-scoped, and safe to be: `imported` is an `ast.parse` of every
    `tupferl/*.py` -- measured at 16.4ms -- and it ran four times per pass of
    this file. Every consumer only reads the dict, and the sandbox's sources
    cannot change during a probe, so one walk serves all four. Measured saving
    ~49ms of this module's ~150ms."""
    return imported(ROOT / "tupferl")


@pytest.fixture
def project() -> dict[str, Any]:
    """`pyproject.toml`'s `[project]` table."""
    table: dict[str, Any] = pyproject()["project"]
    return table


def declared(project: dict[str, Any]) -> list[str]:
    """The declared requirement names, without their markers or versions."""
    return [re.split(r"[<>=!;\s\[]", one, maxsplit=1)[0] for one in project["dependencies"]]


class TestNothingButTheStandardLibrary:
    def test_the_package_imports_nothing_third_party_but_the_backport(
        self, imports: dict[str, set[str]]
    ) -> None:
        """The whole claim, in one assertion. `sys.stdlib_module_names` is the
        interpreter's own list, so this cannot drift from what "standard
        library" means the way a hand-kept set would."""
        outside = {
            name: sorted(files)
            for name, files in imports.items()
            if name not in sys.stdlib_module_names and name not in ALSO_STDLIB and name != "tupferl"
        }
        assert outside == {BACKPORT: ["config.py"]}

    def test_the_fixture_can_see_imports_at_all(self, imports: dict[str, set[str]]) -> None:
        """The precondition. A walk that found nothing would satisfy the
        assertion above by finding no third-party imports either -- which is
        an equality against an empty set, silently."""
        assert "pathlib" in imports
        assert "subprocess" in imports
        assert len(imports) > 10, imports

    def test_the_one_exception_is_confined_to_the_toml_shim(
        self, imports: dict[str, set[str]]
    ) -> None:
        """`tomli` is allowed because 3.10 has no `tomllib`, and nowhere else
        has that excuse. A second module importing it would mean the shim had
        been copied rather than called."""
        assert imports[BACKPORT] == {"config.py"}


class TestTheDeclarationAgreesWithTheImports:
    """`pyproject.toml` against what the package actually reaches for."""

    def test_exactly_one_runtime_dependency_is_declared(self, project: dict[str, Any]) -> None:
        assert declared(project) == [BACKPORT]

    def test_it_is_declared_only_below_the_version_that_has_it(
        self, project: dict[str, Any]
    ) -> None:
        """The marker is the half that makes this a *disappearing* dependency:
        without it, 3.11 and 3.12 would install a backport of a module they
        already ship."""
        (one,) = project["dependencies"]
        assert f"python_version < '{BACKPORT_UNTIL[0]}.{BACKPORT_UNTIL[1]}'" in one

    def test_everything_declared_is_something_the_package_imports(
        self, project: dict[str, Any], imports: dict[str, set[str]]
    ) -> None:
        """The other direction. A dependency nobody imports is one nobody
        notices going stale, which is why `rich` is sanctioned by the plan and
        still absent from this list."""
        for name in declared(project):
            assert name in imports, f"{name} is declared and never imported"


class TestTheTagWidthMatchesTheFormatter:
    """`mutate._COLUMNS` is `pyproject.toml`'s `line-length`, and must stay so.

    `--accept` writes `# survivor:` tags into real source files, wrapped to
    `_COLUMNS`. If the two drift, every tag the tool writes is a line
    `ruff format --check` rejects -- so the preflight goes red on generated text
    in a file the change never touched, which is a long way from the edit that
    caused it.

    Asserted here rather than read from `pyproject.toml` by `tools/mutate.py`:
    `tools/` may not import the package, and parsing TOML there would drag
    `tomli` in on 3.10 for a constant that changes once a decade. This file
    already opens `pyproject.toml` for the dependency surface, so the comparison
    costs nothing new.
    """

    def test_the_wrap_width_is_the_formatter_s_line_length(self) -> None:
        assert pyproject()["tool"]["ruff"]["line-length"] == mutate._COLUMNS
