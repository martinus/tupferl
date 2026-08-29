"""`.tupferl/config.toml`: the four settings, and why a typo in one is an error.

Plan §5 names the file, the parser and the settings. What it does not decide is
what happens to a key nobody recognises, and the two answers differ in the way
that matters for this program:

- Ignore it, and `ignroe = ["*.pem"]` is a config file that reads as if a private
  key is excluded while `tupferl add` happily copies it into a repository that is
  pushed to a remote. The user sees their own intent in the file and no sign it
  did nothing.
- Refuse it, and they get one sentence naming the key.

So this is strict: unknown key, wrong type, and unparseable file are all errors
that say what to do next. That is a deliberate departure from the "degrade to a
safe default" rule used for *progress* state -- losing progress costs a re-run,
where mis-reading intent costs a secret.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tupferl.errors import TupferlError


def toml() -> Any:
    """The TOML parser, imported when a file is actually read.

    Deferred, not decorative. Every `tupferl` command pays its imports before it
    does anything, the test suite runs the CLI as a subprocess several times per
    test, and the mutation sweep runs the suite once per mutant -- so an import
    on the module path is multiplied by tens of thousands. `tomllib` is 4.0ms of
    a 63.7ms run and only `parse` needs it. Measured; see CLAUDE.md.

    3.10 has no `tomllib`; `tomli` is the library it was taken from, and the
    3.10 CI leg is what proves this branch is reachable.
    """
    # survivor: boundary -- tupferl/config.py:41 in toml() -- `>=` becomes `>` -- equivalent,
    #   measured: `sys.version_info` on a real 3.11.0 is `(3, 11, 0, 'final', 0)`, which is already
    #   `> (3, 11)`. The two spellings can only differ for a version tuple of exactly `(3, 11)`,
    #   which no interpreter reports.
    if sys.version_info >= (3, 11):
        import tomllib

        return tomllib
    import tomli

    return tomli


#: One megabyte, as plan §5 asks. A limit rather than no limit because the
#: repository is copied to every machine and pushed on every sync, and a dotfile
#: is a config file: something this size is a database, a cache or a mistake, and
#: all three are better refused with a message than silently synced forever.
DEFAULT_MAX_FILE_SIZE = 1024 * 1024

#: Every key the file may contain, and the type it must have. A table rather than
#: a series of `get` calls, because it is also what the error message lists --
#: two lists would let the message drift from what is accepted.
#: Two, and both are facts about the *repository* rather than about a machine.
#: There were four. `hostname` and `editor` went together and for one reason:
#: this file is committed and shared, so a per-machine answer set here reaches
#: every machine that clones it -- which is the opposite of what "this host is
#: called work-laptop" or "I edit with kakoune" means. Both now come from
#: somewhere per-machine: `TUPFERL_HOSTNAME` or the system's own name, and git's
#: editor chain.
KNOWN: dict[str, type] = {
    "ignore": list,
    "max_file_size": int,
}


@dataclass(frozen=True)
class Config:
    """The settings, with the defaults that apply when the file is absent.

    Frozen because it is read from several places during one sync and nothing
    should be able to change what "ignored" means halfway through.

    A `NamedTuple` would drop the `dataclasses` import, which pulls in `inspect`
    and costs 8.5ms of a 63.7ms `tupferl --version`. Measured and *not* taken:
    that is about 4% of a mutation sweep, and it costs `ignore` becoming a tuple
    -- a change to what a setting *is* -- plus the premise of
    `TestTheTableAndTheRecordAgree`, which holds because `KNOWN` describes what
    TOML delivers and the field describes what is stored. A small share of what
    is left is where an optimisation sequence stops.
    """

    ignore: list[str] = field(default_factory=list)
    max_file_size: int = DEFAULT_MAX_FILE_SIZE


def parse(text: str, where: Path | str) -> Config:
    """Read the settings out of TOML text, or say what is wrong with it.

    Split from `load` so the error paths can be tested against a string rather
    than against a file that has to be written first -- and `where` is still a
    parameter, because a message that cannot name the file it is complaining
    about sends the reader looking for it.
    """
    tomllib = toml()
    try:
        raw: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as broken:
        raise TupferlError(f"{where} is not valid TOML ({broken}); fix that line.") from broken

    for key, value in raw.items():
        if key not in KNOWN:
            known = ", ".join(sorted(KNOWN))
            raise TupferlError(
                f"{where} sets an unknown key {key!r}, which tupferl would otherwise "
                f"ignore silently; the settings are: {known}."
            )
        # `bool` before the type table, and not as an entry in it: `True` is an
        # `int` in Python, so `max_file_size = true` would pass an `isinstance`
        # check and then be used as the number 1 -- every file "too large".
        if isinstance(value, bool) or not isinstance(value, KNOWN[key]):
            raise TupferlError(
                f"{where} sets {key} to {value!r}, but it must be a "
                f"{KNOWN[key].__name__}; change the value."
            )

    if any(not isinstance(pattern, str) for pattern in raw.get("ignore", [])):
        raise TupferlError(
            f"{where} has an `ignore` entry that is not a string; every entry must be "
            f"a path pattern in quotes."
        )
    if "max_file_size" in raw and raw["max_file_size"] <= 0:
        raise TupferlError(
            f"{where} sets max_file_size to {raw['max_file_size']}, which would refuse "
            f"every file; set a positive number of bytes."
        )
    return Config(**raw)


def load(path: Path) -> Config:
    """The settings at `path`, or the defaults if there is no file there.

    A missing file is not an error: `tupferl init` does not have to write one,
    and a user who wants no settings should not have to keep an empty file to
    say so. A file that exists and cannot be *read* is an error -- that is a
    permission problem, and reporting defaults for it would hide it.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    except OSError as unreadable:
        raise TupferlError(
            f"cannot read {path} ({unreadable.strerror}); check its permissions."
        ) from unreadable
    return parse(text, path)
