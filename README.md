# tupferl

*Tupferl* is Austrian for "little dot" — as in *das i-Tüpferl*, the finishing
touch. It keeps your dotfiles in one git repository and shares them between
computers.

It is a simpler alternative to [chezmoi](https://www.chezmoi.io/): no template
language, no name mangling, no hooks. One command does the daily work, and the
one thing it does better is what happens when two computers changed the same
file.

Every *command* in those lines works today. The install does not yet —
publishing to PyPI is milestone 7, so for now it is
`pipx install git+https://github.com/martinus/tupferl`. See **Status**.

```sh
pipx install tupferl
tupferl init git@github.com:me/dotfiles.git
tupferl add ~/.bashrc ~/.gitconfig ~/.config/nvim
tupferl sync
# on the second computer:
pipx install tupferl
tupferl init git@github.com:me/dotfiles.git   # pulls everything
```

## Status: milestone 5 of 7

**`init`, `add`, `remove`, `list`, `sync` and `doctor` work.** Two computers can
share dotfiles today: `tupferl sync` pulls, merges in both directions, commits
and pushes, resolves everything it can without asking — and asks about the rest.

**When both computers changed the same lines, it asks.** One question per file,
one keypress per answer:

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

`[b]` keeps both versions in turn, with no markers left behind. `[e]` opens the
merged file — conflict markers and all — in whatever `editor` in
`.tupferl/config.toml` names, else `$VISUAL`, else `$EDITOR`, and refuses a save
that still has the markers in it. (The config first, because that file is the
one you can commit; a setting that loses to an environment variable is one you
cannot make stick.) `[s]` leaves both copies exactly as they were and reports the
file at the end, which is also what a `sync` with nobody at the keyboard does:

```
$ tupferl sync < /dev/null
conflict in .bashrc (1 to settle); both copies left as they are

1 file managed, 0 changed, 1 in conflict
```

That exit status is 1, so a script notices. For scripts that want an answer
rather than a report, `--ours` keeps this computer's version and `--theirs` the
repository's, for every conflict, and both exit 0; `--no-input` is the skipping
behaviour above, asked for explicitly. A stdin that is not a terminal is
`--no-input` whether or not you said so — a sync on a timer must not block on a
question nobody will see.

**One conflict it cannot settle yet** is two *commits* that git could not merge,
which happens when a computer has committed without pushing and the other pushes
to the same lines meanwhile. It says so, and `git -C <repo> pull` is the way out.

The two remaining unbuilt commands say so themselves:

```
$ tupferl status
tupferl: `tupferl status` is not built yet; it is milestone 6 of docs/plan.md.
```

The design, the scope boundary and the build order are in
[`docs/plan.md`](docs/plan.md). The working agreements for changing any of it
are in [`CLAUDE.md`](CLAUDE.md).

| milestone | what it adds | state |
|---|---|---|
| 1 | package skeleton, `doctor`, config loading, test infrastructure | **done** |
| 2 | `init`, `add`, `remove`, `list` | **done** |
| 3 | sync engine: snapshots, change detection, automatic merges | **done** |
| 4 | 3-way merge and the interactive conflict prompt | **done** |
| 5 | host overlays | **done** |
| 6 | backups, error messages, `status` and `diff` | |
| 7 | PyPI packaging | |

## How it works

**Copies, not symlinks.** The repository holds a copy of each managed file and
tupferl copies between it and `$HOME`. Symlinks break with programs that rewrite
their config file, and a copy is what makes a real 3-way merge possible.

**The repository mirrors `$HOME`.** `~/.bashrc` is stored as `.bashrc`. No
`dot_` prefix, no name mangling — the mapping is the path.

**Per-machine differences without templates.** A file in
`.tupferl/hosts/<hostname>/` replaces the shared one on that host. Whole files
only; no variables, no merging.

```sh
tupferl add --host ~/.gitconfig      # this machine gets its own version
tupferl remove --host ~/.gitconfig   # …and stops having one
```

The shared file stays managed either way, and other computers never see the
override — they keep using the shared version, and `tupferl list` marks the
files this machine overrides with `host`. After `remove --host`, the next sync
copies the shared version back into `$HOME` (backing up what the override put
there first). Plain `tupferl remove` is a different request: it stops managing
the file *everywhere*, taking the shared copy every other computer is using.

**Some files are refused, on purpose.** Symlinks, anything reached *through* a
symlink, anything outside `$HOME`, sockets and devices, files over
`max_file_size`, and tupferl's own repository. The first two matter most: a copy
cannot represent a link, so following one would store what it points at — which
is how a credentials file ends up committed under a name nobody would search
for.

**Conflicts are the point.** tupferl keeps a snapshot of every file as it was
after the last successful sync, under `.tupferl/state/<hostname>/`, so it has a
real merge base. `tupferl sync` compares three versions — your `$HOME`, the
repository, and that snapshot — and resolves everything it safely can:

| what changed | what happens |
|---|---|
| nothing | nothing |
| only `$HOME` | the copy in the repository is updated and committed |
| only the repository | your file is updated, after a backup |
| both, in different places | a 3-way merge, applied without asking |
| both, in the same place | one question, one keypress |

Only the last row needs a person, and it is the only one that asks.

**Nothing is overwritten without a copy.** Before `tupferl sync` replaces a file
in `$HOME` it writes the old one to `~/.local/state/tupferl/backup/<timestamp>/`,
and keeps the last five syncs' worth.

**A managed file you deleted comes back.** `tupferl remove` is how you stop
managing something (it leaves the file in `$HOME`); a file that is simply *gone*
is far more likely to be an `rm`, a reinstall or a new machine, so sync restores
it. Reading a missing file as "delete it everywhere" would let one mistake take
a dotfile off every computer you own.

## Decisions

The plan left three questions open ([`docs/plan.md`](docs/plan.md) §9). They were
decided in milestone 1:

| question | answer | why |
|---|---|---|
| `argparse` or `click` | **`argparse`** | eight verbs and a handful of flags; the plan says prefer fewer dependencies |
| snapshot storage | **plain copies** under `.tupferl/state/<hostname>/` | the plan sanctions it for v1, and content-addressing buys deduplication nobody has measured a need for |
| merge implementation | **`git merge-file`** | git is a hard requirement already, and its 3-way merge is battle-tested where a hand-written one would be the most defect-dense file in the project |

One thing diverges from the plan, and it is worth knowing before you write a
config file. Plan §5 puts a `hostname` override in `.tupferl/config.toml` — but
that file lives in the repository and is therefore *shared with every machine
that clones it*, which is the opposite of what "this host is called work-laptop"
means. So the key is honoured, and `TUPFERL_HOSTNAME` in the environment
overrides it. A single-machine installation can use either; a second machine
needs the environment variable.

## Requirements

- Python 3.10 or newer. On 3.10 the `tomli` backport is installed for you;
  3.11+ uses the standard library's `tomllib`.
- `git` on `PATH`.
- Linux or macOS. Windows is explicitly out of scope for version 1.

## Development

```sh
pip install -e '.[dev]'
python -m tools.run_tests                 # the suite, sharded across cores
ruff check . && ruff format --check . && mypy tupferl tests tools \
  && python -m tools.run_tests            # the preflight, exactly what CI runs
```

`tools/` holds the test infrastructure — a parallel runner that refuses to call
a partial run green, and a mutation harness that checks whether a test would
notice its subject breaking. They were ported from
[woswoar](https://github.com/martinus/woswoar) (Apache-2.0); see
[`tools/README.md`](tools/README.md) for what changed and why each one exists.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
