"""The one exception the CLI turns into a message rather than a traceback.

Plan §5: "every error message states what happened and what the user can do
next. One sentence each." That is a property of the *message*, so it is worth
saying where the type is defined: a `TupferlError` whose text does not say what
to do next is a bug in the same way a wrong return value is.

Everything else propagates. A `KeyError` from tupferl's own code is a defect and
a traceback is the right report for it; catching broadly here would turn every
such defect into a polite sentence that says nothing.
"""

from __future__ import annotations


class TupferlError(Exception):
    """Something the user can act on, phrased so they can.

    Raised with the whole message, not a code plus a lookup: the message is
    written where the failure is understood, and a table of strings somewhere
    else is a table that rots away from its call sites.
    """
