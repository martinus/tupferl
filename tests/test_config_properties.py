"""Properties of the config reader, stated over generated input.

Plan §7.2 puts property tests in from the start. The sync engine's are the ones
that matter and they arrive with the sync engine (milestone 3); these exist now
so that the machinery they need -- profiles, the `mutation` profile, the mypy
exemption, the CI leg that installs hypothesis -- is proven by something real
rather than by a placeholder that would be believed and never run.

Two properties, and they are chosen to be the ones an example test is worst at:

1. **Every key outside `KNOWN` is refused.** An example test can only name the
   typos its author thought of, and the one that matters is the one nobody
   thought of.
2. **A file this parser accepts round-trips.** Whatever `ignore` patterns and
   size limit go in come back out, unchanged and in order.
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests import profiles  # noqa: F401  -- registers and loads the profile
from tupferl.config import KNOWN, parse
from tupferl.errors import TupferlError

WHERE = "generated.toml"

#: Bare TOML keys: what a user actually types, and what does not need quoting.
#: Restricted on purpose -- a generated key containing a quote or a newline would
#: test the TOML parser rather than this module's rule about unknown keys.
KEYS = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_-", min_size=1, max_size=20)

#: Values for `ignore`. Printable ASCII without backslash or quote, so
#: `json.dumps` produces a string TOML reads back byte for byte -- the round-trip
#: property is about this module, not about escaping rules.
PATTERNS = st.text(
    alphabet=st.characters(min_codepoint=32, max_codepoint=126, exclude_characters='"\\'),
    max_size=40,
)


#: A valid TOML literal per accepted type, and what it should parse back to.
#: Used for the *other* half of the property below -- see there for why.
VALID: dict[type, tuple[str, object]] = {str: ('"x"', "x"), int: ("1", 1), list: ("[]", [])}


class TestUnknownKeysAreAlwaysRefused:
    @given(key=KEYS)
    def test_any_key_outside_the_table_raises(self, key: str) -> None:
        """And any key *inside* it is accepted, in the same property.

        Not `assume(key not in KNOWN)`. A property that only ever asserts "this
        raises" is satisfied by a parser that raises for everything, and the
        generator would almost never produce one of the four known keys to
        notice -- the assertion that passes against its own mutation, from
        CLAUDE.md §2.
        """
        if key in KNOWN:
            literal, expected = VALID[KNOWN[key]]
            assert getattr(parse(f"{key} = {literal}\n", WHERE), key) == expected
            return
        with pytest.raises(TupferlError) as caught:
            parse(f"{key} = 1\n", WHERE)
        assert key in str(caught.value)


class TestAcceptedFilesRoundTrip:
    @given(patterns=st.lists(PATTERNS, max_size=8), limit=st.integers(min_value=1, max_value=2**40))
    def test_what_goes_in_comes_back_out(self, patterns: list[str], limit: int) -> None:
        text = f"ignore = {json.dumps(patterns)}\nmax_file_size = {limit}\n"
        found = parse(text, WHERE)
        assert found.ignore == patterns
        assert found.max_file_size == limit

    @given(limit=st.integers(max_value=0))
    def test_no_non_positive_limit_is_ever_accepted(self, limit: int) -> None:
        """The boundary from the other side. `max_file_size = 0` has an example
        test; this says there is no negative value that slips through either."""
        with pytest.raises(TupferlError):
            parse(f"max_file_size = {limit}\n", WHERE)
