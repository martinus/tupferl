# tupferl

*Tupferl* is Austrian for "little dot" — as in *das i-Tüpferl*, the finishing
touch.

**Your dotfiles in one git repository, shared between computers.** Edit them on
whichever machine you are sitting at, run `tupferl sync`, and it merges — asking
only about what it genuinely cannot decide.

```sh
pipx install tupferl
tupferl init git@github.com:me/dotfiles.git
tupferl add ~/.bashrc ~/.gitconfig ~/.config/nvim
tupferl sync
```

On the next computer, one line — `init` runs the first sync itself:

```sh
tupferl init git@github.com:me/dotfiles.git
```

> Every command above works today. The `pipx install` does not yet — PyPI is the
> last milestone. For now: `pipx install git+https://github.com/martinus/tupferl`.

## Simple on purpose

No template language. No `dot_` prefix or name mangling. No hooks, no scripts,
no secrets managers. `~/.bashrc` is stored as `.bashrc`, and that is the whole
mapping.

Six commands. Four act — `init`, `add`, `remove`, `sync`. `doctor` checks that
this machine is set up. And `status` answers every question that only looks,
because they are one walk of your files:

```sh
tupferl status              # what would change
tupferl status --all        # every managed file, with its state
tupferl status --diff       # the lines that differ
```

`--diff` goes through the pager git is already configured with, in git's own
order — `GIT_PAGER`, `core.pager`, `$PAGER` — so a machine set up for
[delta](https://github.com/dandavison/delta) needs nothing here. Only when
there is a terminal to page: redirected, it is a plain unified diff with no
colour of its own, so `tupferl status --diff | delta` works too.

The conflict prompt's `[e]` reads git's editor the same way: `GIT_EDITOR`, then
`core.editor`, then `$VISUAL` and `$EDITOR`. An `editor` in
`.tupferl/config.toml` still wins, because that is the one you set for tupferl
on purpose.

**Copies, not symlinks.** The repository holds a copy of each file. Symlinks
break with programs that rewrite their config, and a copy is what makes a real
3-way merge possible.

**tupferl's own settings are a dotfile like any other.**
`~/.config/tupferl/config.toml` holds two optional keys — `ignore` and
`max_file_size` — and it is this machine's until you say otherwise:

```sh
tupferl add ~/.config/tupferl/config.toml          # share it
tupferl add --host ~/.config/tupferl/config.toml   # …except on this machine
```

**Per-machine differences without templates.** A file under
`.tupferl/hosts/<hostname>/` replaces the shared one on that machine. Whole
files, no variables:

```sh
tupferl add --host ~/.gitconfig      # this machine gets its own version
tupferl remove --host ~/.gitconfig   # …and stops having one
```

The override is committed and pushed like everything else, so a reinstall gets
it back — but only the machine it is named for reads it.

## Two-way, not generated

Most dotfile managers build `$HOME` from a source directory: you edit the
source, then apply. tupferl goes both ways, because it remembers — per machine —
what that machine last agreed with the repository.

That merge base is why `sync` can tell *you changed it* from *the other computer
changed it* from *both did*, per file, without being told. Only the last case is
a question:

```
$ tupferl sync

.bashrc: 1 conflict to settle.

  1 of 1, lines 12-16 of the merged file
  this computer
  | export EDITOR=nvim
  the repository
  | export EDITOR=vim

  [l] keep local   [r] keep remote   [b] keep both
  [e] edit merged file   [d] show full diff   [s] skip
```

One question per file, one keypress per answer. `[b]` keeps both versions with
no markers left behind; `[e]` opens `$EDITOR` on the merged file. `--ours`,
`--theirs` and `--no-input` answer for you.

`tupferl status` says what the next sync would do, without doing it.

## Plaintext, by decision

**What tupferl stores, it stores in plaintext and pushes to your remote.** There
is no encryption and none is planned. So `add` refuses a short list of names
whose only job is to hold a credential — `.ssh/id_*`, `.aws/credentials`,
`.netrc`, `.pgpass`, `.gnupg/*`, `*.pem`, `*.key` — and says why. `--anyway`
overrides it. `tupferl add ~/.ssh` stores `config`, `known_hosts` and `id_*.pub`
and skips the private keys (`*.pub` is the half of a key pair meant to be
shared).

## No dependencies

On Python 3.11 and newer tupferl imports **nothing outside the standard
library**. On 3.10 it installs exactly one thing — `tomli`, the library
`tomllib` was taken from — so the single dependency is a backport of a stdlib
module, and it disappears on the interpreters that ship it.

That is a supply chain of one package that goes to zero, and it is asserted
rather than intended: `tests/test_packaging.py` reads every import in the
package and every requirement in `pyproject.toml` and refuses to let the two
disagree in either direction.

## Requirements

- Python 3.10+ (3.10 gets the `tomli` backport installed for you).
- git 2.25+ on `PATH`. `tupferl doctor` checks it and names what it found.
- Linux or macOS. Windows is out of scope for version 1.

## Development

```sh
pip install -e '.[dev]'
ruff check . && ruff format --check . && mypy tupferl tests tools \
  && python -m tools.run_tests            # the preflight, exactly what CI runs
```

[`docs/plan.md`](docs/plan.md) is what this was built from, including what it
deliberately does not do. `tools/` holds the test infrastructure — a runner that
refuses to call a partial run green, and a mutation harness that checks whether
a test would notice its subject breaking; see [`tools/README.md`](tools/README.md).

## Licence

Apache-2.0. See [LICENSE](LICENSE).
