"""The Hypothesis profiles, and the wiring that makes the `mutation` one apply.

Two of these assertions look like restatements of the constants and are not. The
`mutation` profile only ever takes effect if `tools/mutate.py` sets the same
environment variable `tests/profiles.py` reads -- two strings in two files with
nothing but a convention between them. If they drift, every mutant silently pays
the full example budget, a sweep takes hours instead of minutes, and *nothing
fails*: the run is still correct, just slow enough that nobody runs it.

The derandomisation is the half that would be wrong rather than slow. A
randomised baseline and a randomised mutant draw different examples, so "the
baseline passed and the mutant failed" stops meaning "a test noticed".
"""

from __future__ import annotations

import unittest

from hypothesis import settings

from tests import profiles
from tools import mutate


class TestTheProfilesExist(unittest.TestCase):
    def test_all_three_are_registered(self) -> None:
        for name in ("dev", "ci", "mutation"):
            with self.subTest(profile=name):
                self.assertIsNotNone(settings.get_profile(name))

    def test_the_mutation_profile_is_the_cheap_one(self) -> None:
        """Cheaper than `ci`, which is the point of having it."""
        self.assertLess(
            settings.get_profile("mutation").max_examples,
            settings.get_profile("ci").max_examples,
        )

    def test_the_non_interactive_profiles_are_derandomised(self) -> None:
        for name in ("ci", "mutation"):
            with self.subTest(profile=name):
                self.assertTrue(settings.get_profile(name).derandomize)

    def test_the_default_is_not(self) -> None:
        """A developer's run should find new falsifying examples; that is the
        whole reason to run these at all locally."""
        self.assertFalse(settings.get_profile("dev").derandomize)


class TestTheWiring(unittest.TestCase):
    def test_the_harness_and_the_profiles_agree_on_the_variable(self) -> None:
        """The drift that would cost hours per sweep and fail nothing."""
        self.assertEqual(profiles.ENV, mutate._PROFILE)

    def test_the_harness_asks_for_a_profile_that_exists(self) -> None:
        """The name the harness actually passes, not one retyped here.

        `load_profile` on an unregistered name raises inside the probe, where it
        surfaces as `BROKE` on every row rather than as the typo it is.
        """
        self.assertIsNotNone(settings.get_profile(mutate._MUTATION_PROFILE))


if __name__ == "__main__":
    unittest.main()
