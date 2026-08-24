"""tupferl -- a simple dotfiles manager. See docs/plan.md for the design."""

#: The one declaration. `pyproject.toml` reads it from here rather than carrying
#: its own, so `tupferl --version` and the wheel metadata cannot disagree.
__version__ = "0.0.1"
