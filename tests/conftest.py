"""Fixtures more than one test module needs.

**Deferred until a cluster genuinely shared one, on purpose.** B1's plan entry
said to create this file "initially near-empty"; it did not, because after
converting eight modules no *fixture* in them was shared and an empty
`conftest.py` makes a claim -- shared fixtures live here -- that nothing backs.
B3 is the cluster that backs it: five modules want the same throwaway `$HOME`,
and four more want it in B4a and B4b.

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

from collections.abc import Iterator

import pytest

from tests import support


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
