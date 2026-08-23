"""Every Hypothesis profile, in one place, selected by one environment variable.

Plan §7.2 asks for exactly this and gives the reason: without a `mutation`
profile, every mutant pays the full example budget and a sweep takes hours for no
extra signal. The other half of that reason is less obvious -- a mutation run
compares a *baseline* against a mutant, so the two must draw the same examples.
A randomised profile makes "the baseline passed and the mutant failed" mean
"they were given different inputs" some of the time, and that is indistinguishable
from a catch. So both non-interactive profiles are derandomised.

Import this module for its side effect and then use `settings` normally:

    from tests import profiles  # noqa: F401  -- registers and loads the profile

One module rather than a `register_profile` call at the top of each property
test: with a call per file, the file that is *missing* one still runs, at
whatever profile a sibling happened to load, and nothing says so.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

#: Which profile to use. Named for the project so it cannot collide with another
#: tool's variable in a shell where both are set.
ENV = "TUPFERL_HYPOTHESIS_PROFILE"

#: The default. Randomised, because on a developer's machine finding a new
#: falsifying example is the whole point of running these at all.
settings.register_profile("dev", max_examples=200, deadline=None)

#: CI: derandomised, so a failure a contributor is asked to reproduce reproduces.
#: `deadline=None` on every profile, and this is not laziness -- these properties
#: drive real `git` subprocesses, so a per-example deadline measures the runner's
#: load rather than the code.
settings.register_profile("ci", max_examples=200, deadline=None, derandomize=True)

#: Under `tools/mutate.py`. Few examples and no health checks: a mutant that
#: makes generation slow would otherwise fail the `too_slow` health check, which
#: reports as an *error* rather than as a test noticing the mutation -- the
#: harness would call that `BROKE` and the row would go unanswered.
settings.register_profile(
    "mutation",
    max_examples=20,
    deadline=None,
    derandomize=True,
    suppress_health_check=list(HealthCheck),
)

settings.load_profile(os.environ.get(ENV, "dev"))
