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
import unittest
from pathlib import Path

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


class TestNothingButTheStandardLibrary(unittest.TestCase):
    def setUp(self) -> None:
        self.imports = imported(ROOT / "tupferl")

    def test_the_package_imports_nothing_third_party_but_the_backport(self) -> None:
        """The whole claim, in one assertion. `sys.stdlib_module_names` is the
        interpreter's own list, so this cannot drift from what "standard
        library" means the way a hand-kept set would."""
        outside = {
            name: sorted(files)
            for name, files in self.imports.items()
            if name not in sys.stdlib_module_names and name != "tupferl"
        }
        self.assertEqual({BACKPORT: ["config.py"]}, outside)

    def test_the_fixture_can_see_imports_at_all(self) -> None:
        """The precondition. A walk that found nothing would satisfy the
        assertion above by finding no third-party imports either -- which is
        `assertEqual` against an empty set, silently."""
        self.assertIn("pathlib", self.imports)
        self.assertIn("subprocess", self.imports)
        self.assertGreater(len(self.imports), 10, self.imports)

    def test_the_one_exception_is_confined_to_the_toml_shim(self) -> None:
        """`tomli` is allowed because 3.10 has no `tomllib`, and nowhere else
        has that excuse. A second module importing it would mean the shim had
        been copied rather than called."""
        self.assertEqual({"config.py"}, self.imports[BACKPORT])


class TestTheDeclarationAgreesWithTheImports(unittest.TestCase):
    """`pyproject.toml` against what the package actually reaches for."""

    def setUp(self) -> None:
        # The package's own shim, so this reads TOML the way tupferl does --
        # including on 3.10, where the module under discussion is the one doing
        # the reading.
        with (ROOT / "pyproject.toml").open("rb") as handle:
            self.project = config.toml().load(handle)["project"]

    def names(self) -> list[str]:
        """The declared requirement names, without their markers or versions."""
        return [
            re.split(r"[<>=!;\s\[]", one, maxsplit=1)[0] for one in self.project["dependencies"]
        ]

    def test_exactly_one_runtime_dependency_is_declared(self) -> None:
        self.assertEqual([BACKPORT], self.names())

    def test_it_is_declared_only_below_the_version_that_has_it(self) -> None:
        """The marker is the half that makes this a *disappearing* dependency:
        without it, 3.11 and 3.12 would install a backport of a module they
        already ship."""
        (declared,) = self.project["dependencies"]
        self.assertIn(f"python_version < '{BACKPORT_UNTIL[0]}.{BACKPORT_UNTIL[1]}'", declared)

    def test_everything_declared_is_something_the_package_imports(self) -> None:
        """The other direction. A dependency nobody imports is one nobody
        notices going stale, which is why `rich` is sanctioned by the plan and
        still absent from this list."""
        reached = imported(ROOT / "tupferl")
        for name in self.names():
            self.assertIn(name, reached, f"{name} is declared and never imported")


if __name__ == "__main__":
    unittest.main()
