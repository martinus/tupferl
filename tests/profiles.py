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
#:
#: `derandomize=False` is stated rather than left out, and that is not
#: redundancy. **Hypothesis registers and loads a profile of its own when it
#: sees `CI` in the environment**, and that one is derandomised -- so a field a
#: profile here does not state is inherited from whatever is default at
#: registration time. Left implicit, `dev` was randomised on a laptop and
#: derandomised on a runner: the same name for two different things, decided by
#: an environment variable nobody here mentions. CI caught it on the first run.
settings.register_profile("dev", max_examples=200, deadline=None, derandomize=False)

#: CI: derandomised, so a failure a contributor is asked to reproduce reproduces.
#: The name collides with the profile Hypothesis registers for itself when `CI`
#: is set, deliberately: registering it again replaces theirs, and the
#: `load_profile` at the bottom of this module is what decides which one is in
#: force either way.
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

#: How many examples the *stateful* sync machine runs, and how many rules each
#: one fires. Not `max_examples` on the profile, because one example there is one
#: `three_way` call and one here is a dozen `tupferl sync` runs against real git
#: -- about 0.4s measured, against 3ms. A profile budget that suited one would
#: make the other either useless or unbearable.
#:
#: Keyed by the same variable that picks the profile, so the two cannot disagree
#: about which run this is. `mutation` gets three: a sweep runs the suite once
#: per mutant, and a stateful machine that took thirty seconds would put a sweep
#: into hours for signal the example tests already carry.
#: Only the row that differs is written out: `dev` and `ci` are the fallback, and
#: spelling them as their own entries was three copies of one pair.
STATEFUL, STEPS = {"mutation": (3, 4)}.get(os.environ.get(ENV, "dev"), (20, 8))

#: Every profile above states every field it cares about, so that this line is
#: the only thing that decides which settings are in force. `tests/test_profiles.py`
#: asserts that by running this module in a subprocess with and without `CI`.
settings.load_profile(os.environ.get(ENV, "dev"))
