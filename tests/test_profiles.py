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

import os
import subprocess
import sys

import pytest
from hypothesis import settings

from tests import profiles, support
from tools import mutate


class TestTheProfilesExist:
    @pytest.mark.parametrize("name", ["dev", "ci", "mutation"])
    def test_all_three_are_registered(self, name: str) -> None:
        assert settings.get_profile(name) is not None

    def test_the_mutation_profile_is_the_cheap_one(self) -> None:
        """Cheaper than `ci`, which is the point of having it."""
        cheap = settings.get_profile("mutation").max_examples
        assert cheap < settings.get_profile("ci").max_examples

    @pytest.mark.parametrize("name", ["ci", "mutation"])
    def test_the_non_interactive_profiles_are_derandomised(self, name: str) -> None:
        assert settings.get_profile(name).derandomize

    def test_the_default_is_not(self) -> None:
        """A developer's run should find new falsifying examples; that is the
        whole reason to run these at all locally."""
        assert not settings.get_profile("dev").derandomize


#: Asks a fresh interpreter what each profile says, so the answer is the module's
#: import-time behaviour rather than this process's already-loaded state.
ASK = (
    "from hypothesis import settings;"
    "from tests import profiles;"
    "print({n: (settings.get_profile(n).derandomize, settings.get_profile(n).max_examples)"
    " for n in ('dev', 'ci', 'mutation')})"
)


def ask(**env: str) -> str:
    """What a fresh interpreter says the profiles are, under `env`."""
    done = subprocess.run(
        [sys.executable, "-c", ASK],
        cwd=support.ROOT,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


class TestTheProfilesDoNotDependOnTheAmbientEnvironment:
    """The failure this file was written for, found by CI rather than here.

    Hypothesis registers and loads a derandomised profile of its own when it sees
    `CI` in the environment, and a profile that leaves a field unstated inherits
    whatever is default when it is registered. So `dev` was randomised locally
    and derandomised on a runner -- one name, two meanings, decided by a variable
    nothing in this project mentions.

    Run in a subprocess because the interesting behaviour happens at import, and
    `tests.profiles` is imported long before any test runs.
    """

    def test_the_profiles_are_the_same_with_and_without_ci(self) -> None:
        assert ask(CI="") == ask(CI="true")

    def test_and_they_are_the_ones_this_module_asserts(self) -> None:
        """The precondition: two identical answers prove nothing if the answer
        itself is wrong, or if the subprocess failed to say anything useful."""
        assert "'dev': (False, 200)" in ask(CI="true")


class TestTheWiring:
    def test_the_harness_and_the_profiles_agree_on_the_variable(self) -> None:
        """The drift that would cost hours per sweep and fail nothing."""
        assert profiles.ENV == mutate._PROFILE

    def test_the_harness_asks_for_a_profile_that_exists(self) -> None:
        """The name the harness actually passes, not one retyped here.

        `load_profile` on an unregistered name raises inside the probe, where it
        surfaces as `BROKE` on every row rather than as the typo it is.
        """
        assert settings.get_profile(mutate._MUTATION_PROFILE) is not None
