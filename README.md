# tupferl

*Tupferl* is Austrian for "little dot" — as in *das i-Tüpferl*, the finishing
touch.

**Your dotfiles in one git repository, shared between computers.** Edit them on
whichever machine you are sitting at, run `tupferl sync`, and it shows you what
you changed before it stores anything. `--auto` if you would rather it did not
ask.

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

Or `uv tool install tupferl`, or `pip install --user tupferl`. To run the
unreleased main branch instead:
`pipx install git+https://github.com/martinus/tupferl`.

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
tupferl status --diff       # the lines the next sync will change
```

Each row says which way the file is about to travel, so the direction is
readable at a glance rather than inferred from a sentence:

```
$ tupferl status
what the next sync would do:

  ->  .bashrc     store the change you made here
  <-  .gitconfig  update it from the repository
  <>  .vimrc      merge both changes; they do not overlap
  !!  .tmux.conf  both changed and the edits overlap: 2 conflicts to settle

origin/main: 1 commit to pull; this status does not include it yet.
4 files managed, 4 to change, 1 in conflict
```

On a terminal the marker is coloured by whose change it is — the same cyan and
yellow the conflict prompt uses for "this computer" and "the repository". With
nothing pending the last line is `4 files managed, nothing to do`.

`--diff` goes through the pager git is already configured with, in git's own
order — `GIT_PAGER`, `pager.diff`, `core.pager`, `$PAGER` — so a machine set up
for [delta](https://github.com/dandavison/delta) needs nothing here, including
the usual per-command form:

```ini
[pager]
	diff = "if [ -t 1 ]; then delta; else cat; fi"
```

The value is a shell command line, exactly as git treats it. Only when there is
a terminal to page — and tupferl colours the diff itself when there is one, so a
machine with no pager configured still gets a readable one. Redirected, it is a
plain unified diff with no colour at all, so `tupferl status --diff | delta`
works too and `NO_COLOR` is honoured either way.

The conflict prompt's `[e]` reads git's editor the same way: `GIT_EDITOR`, then
`core.editor`, then `$VISUAL` and `$EDITOR`. For one run, say so on the command
line — `GIT_EDITOR=meld tupferl sync`.

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
changed it* from *both did*, per file, without being told.

A change **you** made is shown and put to you before it is stored:

```
$ tupferl sync

.bashrc: you changed this here; the repository has the older copy.

--- .bashrc (the repository)
+++ .bashrc (this computer)
@@ -1,3 +1,3 @@
-export EDITOR=vim
+export EDITOR=nvim

  [l] store your version   [r] discard it, take the repository's
  [d] show the whole diff   [s] skip
```

`[r]` is the undo: your edit goes, the repository's copy comes back. A change
the *other* computer made is applied without asking — `tupferl status --diff`
shows what is waiting before you sync. `--auto` skips the question entirely,
and so do `--ours`, `--theirs`, `--no-input`, and any run whose input is not a
terminal, so `init`, cron jobs and CI are unaffected.

When **both** sides changed the same file, that is the one case tupferl cannot
decide, and it asks differently:

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
