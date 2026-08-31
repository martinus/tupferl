"""Fixtures more than one test module needs.

**Deferred until a cluster genuinely shared one, on purpose.** B1's plan entry
said to create this file "initially near-empty"; it did not, because after
converting eight modules no *fixture* in them was shared and an empty
`conftest.py` makes a claim -- shared fixtures live here -- that nothing backs.
B3 is the cluster that backs it: five modules want the same throwaway `$HOME`,
and six more do in B4a and B4b (`test_diff`, `test_manage`, `test_status`,
`test_sync`, `test_sync_cli`, `test_sync_commits`).

**A fixture goes here only when a second module wants it.** The alternative is
the shape every large suite ends up regretting, where `conftest.py` is a
grab-bag nobody can delete from because no one call site owns anything in it.
Where a fixture has exactly one user it stays in that module -- `test_paths`'
`only`, `test_config`'s `box` and `test_merge`'s `merged_under` are all still
where B1 left them.

**What a sandbox *is* lives in `tests/support.py`, not here.** `support.sandbox`
is the definition; this file is the pytest adapter over it and
`support.SandboxCase` is the `unittest` one. Both adapters exist until B4b
converts the last `TestCase` user, and neither holds setup of its own, so they
cannot drift apart in the meantime.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from tests import support


@pytest.fixture(autouse=True)
def _every_test_puts_the_environment_back() -> Iterator[None]:
    """Fail the test that leaves `os.environ` changed, rather than the next nine.

    **Written because B3 produced exactly this and it was diagnosed backwards.**
    A test that used `SandboxCase` only for its side effect -- the patched
    environment -- named no fixture when it was converted, so it got none and set
    `PATH=/nonexistent` in the real one. What went red was nine *later* tests
    that could not find git, in three other classes.

    This is not a replacement for the sandbox: it catches a **leak**, not a
    read, and a test that quietly *read* the developer's `$HOME` still passes.
    That half stays a convention, enforced by the `usefixtures` marks on every
    class that needs a sandbox.

    Nor is a session-wide replacement of `$HOME` the deeper fix it looks like.
    It would turn a loud failure -- nine tests broken, and the developer's own
    dotfiles at risk -- into a quiet one, where every test shares one `$HOME`
    and passes anyway. That is the green run CLAUDE.md §8 is about.

    One dict copy per test; measured below measurement against a 1828-test run.

    `PYTEST_CURRENT_TEST` is excluded because **pytest writes it itself** -- the
    value carries the phase, so it reads `... (setup)` when this fixture starts
    and `... (teardown)` when it finishes. Found on this guard's first run,
    where it failed 1711 of 1828 tests: a whole-environment comparison cannot
    pass, and a guard that always fires is no guard. It is the only exclusion,
    and it is named rather than pattern-matched so that a second one has to be
    argued for.
    """

    def ours() -> dict[str, str]:
        return {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}

    before = ours()
    yield
    assert ours() == before, "this test changed os.environ and did not put it back"


@pytest.fixture
def sandbox() -> Iterator[support.Sandbox]:
    """A throwaway `$HOME` with `os.environ` pointed inside it, for one test.

    Function-scoped, and that is not the default being accepted quietly: the
    fixture *patches `os.environ`*, so a module- or session-scoped one would
    leak one test's `HOME` into the next and make an ordering bug look like a
    fixture bug. It costs a `mkdir` and a seeded `~/.gitconfig`.
    """
    with support.sandbox() as made:
        yield made


@pytest.fixture
def machine() -> Iterator[support.Machine]:
    """A sandboxed `$HOME` with a bare remote beside it, and the CLI pointed there.

    Function-scoped for `sandbox`'s reason, which it inherits by composition,
    plus one of its own: `init` and every command after it write into a real git
    repository, so a shared one would carry a test's commits into the next.
    """
    with support.machine() as made:
        yield made


@pytest.fixture
def two_machines() -> Iterator[support.TwoMachines]:
    """Two `$HOME`s and the bare remote they share, `.bashrc` already synced.

    A `copytree` of a template built once per process -- 4.3 ms against the
    120.4 ms a real `init`/`add`/`sync` costs (#19), which is what makes this
    affordable per test for the 146 tests that take it.

    It does **not** patch `os.environ`: each machine carries its own environment
    and applies it per command, which is how two hostnames coexist in one
    process. So a test that takes this and then reads `os.environ` is reading the
    developer's, and wants `sandbox` as well or instead.
    """
    with support.two_machines() as made:
        yield made
