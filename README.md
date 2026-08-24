# tupferl

*Tupferl* is Austrian for "little dot" — as in *das i-Tüpferl*, the finishing
touch. It keeps your dotfiles in one git repository and shares them between
computers.

It is a simpler alternative to [chezmoi](https://www.chezmoi.io/): no template
language, no name mangling, no hooks. One command does the daily work, and the
one thing it does better is what happens when two computers changed the same
file.

The first four lines work today; `sync` is the next milestone. See **Status**.

```sh
pipx install tupferl
tupferl init git@github.com:me/dotfiles.git
tupferl add ~/.bashrc ~/.gitconfig ~/.config/nvim
tupferl sync
# on the second computer:
pipx install tupferl
tupferl init git@github.com:me/dotfiles.git   # pulls everything
```

## Status: milestone 2 of 7

**`init`, `add`, `remove`, `list` and `doctor` work. Nothing syncs yet** — the
repository is a normal git repository, so until milestone 3 you can `git push`
it yourself. The three unbuilt commands say which milestone builds them:

```
$ tupferl sync
tupferl: `tupferl sync` is not built yet; it is milestone 3 of docs/plan.md.
```

The design, the scope boundary and the build order are in
[`docs/plan.md`](docs/plan.md). The working agreements for changing any of it
are in [`CLAUDE.md`](CLAUDE.md).

| milestone | what it adds | state |
|---|---|---|
| 1 | package skeleton, `doctor`, config loading, test infrastructure | **done** |
| 2 | `init`, `add`, `remove`, `list` | **done** |
| 3 | sync engine: snapshots, change detection, one-sided merges | |
| 4 | 3-way merge and the interactive conflict prompt | |
| 5 | host overlays | |
| 6 | backups, error messages, `status` and `diff` | |
| 7 | PyPI packaging | |

## How it will work

**Copies, not symlinks.** The repository holds a copy of each managed file and
tupferl copies between it and `$HOME`. Symlinks break with programs that rewrite
their config file, and a copy is what makes a real 3-way merge possible.

**The repository mirrors `$HOME`.** `~/.bashrc` is stored as `.bashrc`. No
`dot_` prefix, no name mangling — the mapping is the path.

**Per-machine differences without templates.** A file in
`.tupferl/hosts/<hostname>/` replaces the shared one on that host — `tupferl add
--host ~/.gitconfig` puts it there. Whole files only; no variables, no merging.

**Some files are refused, on purpose.** Symlinks, anything reached *through* a
symlink, anything outside `$HOME`, sockets and devices, files over
`max_file_size`, and tupferl's own repository. The first two matter most: a copy
cannot represent a link, so following one would store what it points at — which
is how a credentials file ends up committed under a name nobody would search
for.

**Conflicts are the point.** tupferl keeps a snapshot of every file as it was
after the last successful sync, so it has a merge base. `tupferl sync` resolves
everything it safely can and asks only when both sides changed the same lines —
one keypress per file, with `--ours` / `--theirs` / `--no-input` for scripts.

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
