"""`tools/cpus.py`, which had no test module at all.

One function, and both tools that size a pool read it -- `run_tests` doubles the
answer and `mutate` divides its memory budget by it. So a wrong answer here is
not a wrong answer *here*: it is every lane count and every batch count on every
machine, silently, in the direction of doing less work than the machine can.

There was no `tests/test_cpus.py` before, which also means `mutants.targets_for`
resolved no target for `tools/cpus.py` and its rows ran the whole suite for want
of anywhere better to look.

Every test stands a **stand-in module** in place of `os` rather than patching
attributes on the real one, which the whole interpreter shares. The code looks
`process_cpu_count` up by name rather than checking a version, so the claim
under test is "it asks for that name and falls back when it is absent" -- and
standing something there with or without it is exactly that claim. Patching
`sys.version_info` would not do: on a real 3.10 interpreter, faking the version
cannot conjure the function.
"""

from __future__ import annotations

import os
import types
import unittest
from unittest import mock

from tools import cpus


class TestHowManyCpusAreUsable(unittest.TestCase):
    def test_it_answers_what_the_interpreter_says(self) -> None:
        """`counter() or 4`, and the `or` is what makes the fallback a
        *fallback*. Read as `and`, a truthy count is discarded and every machine
        answers 4 -- a two-core container oversubscribes, a sixty-four-core build
        box runs at a sixteenth of its width, and neither says anything.
        """
        counter = getattr(os, "process_cpu_count", os.cpu_count)
        self.assertEqual(counter(), cpus.usable_cpus())

    def test_an_interpreter_that_will_not_say_falls_back(self) -> None:
        """The other half, and the branch the `or` exists for. Deliberately not
        1: a wrong small answer serialises the suite silently, where a wrong
        large one is merely slower."""
        stand_in = types.SimpleNamespace(process_cpu_count=lambda: None, cpu_count=lambda: None)
        with mock.patch("tools.cpus.os", stand_in):
            self.assertEqual(4, cpus.usable_cpus())

    def test_it_prefers_the_count_that_knows_about_affinity(self) -> None:
        """`os.cpu_count()` ignores an affinity mask and a container's quota, so
        on a two-core-limited runner on a sixteen-core host it answers sixteen,
        and a pool sized from that spends its time context-switching.

        The two numbers differ on purpose: equal ones cannot say which was read.
        """
        stand_in = types.SimpleNamespace(process_cpu_count=lambda: 2, cpu_count=lambda: 16)
        with mock.patch("tools.cpus.os", stand_in):
            self.assertEqual(2, cpus.usable_cpus())

    def test_without_that_name_it_uses_what_there_is(self) -> None:
        """The 3.10 path: the stand-in has no `process_cpu_count` at all."""
        stand_in = types.SimpleNamespace(cpu_count=lambda: 7)
        with mock.patch("tools.cpus.os", stand_in):
            self.assertEqual(7, cpus.usable_cpus())


if __name__ == "__main__":
    unittest.main()
