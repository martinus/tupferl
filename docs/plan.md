# Project plan: "tupferl" — a simple dotfiles manager

> The name is "tupferl" — Austrian dialect for "little dot", as in
> *das i-Tüpferl*, the finishing touch. The PyPI name is free
> (verified 2026-08-23). Use the meaning in the README's first line.
> This plan is the input for an AI coding agent. It defines the goal,
> the design decisions, and the build order.

## 1. Goal

Build a simple tool that stores dotfiles in a git repository and
shares them between computers.

The tool must:

- Be written completely in Python (3.10+).
- Keep all data in one git repository.
- Be very simple to install and to use.
- Make conflicts between computers easy to see and easy to resolve.
- Work well when several computers change the same files.

The tool is a simpler alternative to chezmoi. It does not copy the
chezmoi feature set. It removes complexity instead.

## 2. What we deliberately do NOT build

Keep the scope small. Do not build:

- A template language (chezmoi's biggest source of complexity).
- Encryption or secret management (users can add git-crypt later).
- Windows support in version 1. Target Linux and macOS.
- Hooks or scripts that run on apply.
- A daemon or background service.

If a feature is not in this plan, do not add it.

## 3. Core design decisions

### 3.1 Storage model: copy, not symlink

The repository holds a copy of each managed file. The tool copies
files between the repository and `$HOME`.

Reason: symlinks break with some programs, complicate backups, and
make the repository state unclear when a program rewrites its config
file. A copy model keeps every state explicit, and it is the model
that makes 3-way conflict resolution possible (see 3.4).

### 3.2 Repository layout

The repository mirrors `$HOME`. No name mangling (no `dot_` prefix
like chezmoi uses). Hidden files stay hidden.

```
~/.local/share/tupferl/repo/
├── .tupferl/
│   ├── config.toml        # tool settings
│   └── state/             # last-synced snapshots (see 3.4)
├── .bashrc
├── .gitconfig
└── .config/
    └── nvim/
        └── init.lua
```

The path of a file inside the repository is its path relative to
`$HOME`. This makes the mapping obvious. `tupferl add ~/.bashrc`
stores the file as `.bashrc` in the repository.

### 3.3 Per-machine differences without templates

Instead of templates, use host overlays:

```
~/.local/share/tupferl/repo/
├── .gitconfig                  # shared version
└── .tupferl/hosts/
    ├── work-laptop/
    │   └── .gitconfig          # replaces the shared version on this host
    └── home-desktop/
        └── .config/foo.conf
```

Rules:

- A file in `hosts/<hostname>/` replaces the shared file on that host.
- Whole-file replacement only. No merging, no variables.
- `tupferl add --host <file>` stores a file in the current host overlay.

This covers the common cases (different git email at work, different
monitor config) with zero template syntax.

### 3.4 Conflict resolution: the key feature

This is the main improvement over chezmoi. Design it first, build it
first.

**The problem.** Three versions of a file can differ:

1. The file in `$HOME` (local edits).
2. The file in the local repository.
3. The file in the remote repository (edits from another computer).

chezmoi makes the user run `chezmoi diff`, `chezmoi merge`, and git
commands by hand. That is the pain point we remove.

**The solution.** One command, `tupferl sync`, does everything and asks
the user only when it must:

1. Snapshot: the tool keeps a snapshot of every file as it was after
   the last successful sync (`.tupferl/state/`). This gives a merge
   base for a real 3-way merge.
2. Detect: compare `$HOME` file, repository file, and snapshot.
3. Auto-resolve when safe:
   - Only `$HOME` changed → copy into the repository, commit.
   - Only the repository changed (after `git pull`) → copy to `$HOME`.
   - Both changed, changes do not overlap → 3-way merge
     (`difflib`-based or `git merge-file`), apply silently, commit.
4. Ask only on a true conflict (both sides changed the same lines).
   Show an interactive prompt per file:

```
Conflict in .bashrc (lines 12-15)

  local (this computer)          remote (from repo)
  export EDITOR=nvim             export EDITOR=vim

  [l] keep local   [r] keep remote   [b] keep both
  [e] edit merged file   [d] show full diff   [s] skip
```

5. Push at the end. If the push fails because the remote moved,
   pull, repeat the merge step, push again.

Requirements for the prompt:

- Show the conflicting lines directly in the terminal, side by side
  or unified, with color.
- Every choice is one keypress.
- `[e]` opens the user's editor on a file with standard conflict markers.
  Which editor is git's answer, in git's order, with tupferl's own setting
  ahead of it: `editor` in `.tupferl/config.toml`, then `GIT_EDITOR`, then
  `core.editor`, then `$VISUAL`, then `$EDITOR`. Someone who configured an
  editor for git configured how they edit text.
- `[s] skip` leaves the file untouched and reports it at the end.
- A `--theirs` / `--ours` / `--no-input` flag set allows scripted use.

### 3.5 Multi-computer flow

The daily use is one command:

```
tupferl sync
```

It pulls, merges in both directions, resolves, commits, and pushes.
A user on two computers runs `tupferl sync` on each machine and never
touches git directly. Commit messages are generated
(`sync from <hostname>: update .bashrc, .gitconfig`).

`git` stays fully accessible: the repository is a normal git
repository. Power users can inspect or repair it with git commands.

## 4. Command set

Keep the command set this small:

| Command | Purpose |
|---|---|
| `tupferl init <git-url>` | Clone an existing repo, or create a new one and set the remote. Then run a first sync. |
| `tupferl add <path>...` | Start managing files. `--host` puts them in the host overlay. |
| `tupferl remove <path>` | Stop managing a file. Keeps the file in `$HOME`. |
| `tupferl sync` | Pull, merge both directions, resolve, commit, push. The main command. |
| `tupferl status [path] [--all] [--diff]` | What the next sync would do. Never modifies anything. `--all` shows every managed file rather than only the changed ones, marking host-overlay ones; `--diff` shows the lines that differ. Was three verbs — `status`, `list` and `diff` — until they were folded: all three read the same walk. |
| `tupferl doctor` | Check git presence, remote access, permissions, dangling state. |

No other commands in version 1.

## 5. Implementation notes

- **Language / runtime:** Python 3.10+, single package.
- **Dependencies:** keep them minimal.
  - `click` or stdlib `argparse` for the CLI (agent's choice; prefer
    fewer dependencies).
  - `rich` is allowed for the conflict UI. It is worth one dependency,
    and nothing imports it yet.
  - **The "colored diffs" half of that sanction is spent, and not on
    `rich`.** `status --diff` writes a plain unified diff and hands it to
    the pager git is already configured with -- `GIT_PAGER`, then
    `pager.diff`, then `core.pager`, then `$PAGER` -- so a machine set up
    for `delta` is already set up for this, and tupferl colours nothing
    itself. The value is a shell command line and is run through a shell,
    which is what git does and what the common `pager.diff = "if [ -t 1 ];
    then delta; else cat; fi"` needs. Only when stdout is a terminal, so a
    redirected diff stays plain and pipeable; and no fallback to `less`,
    because paging output that never paged before is a change to
    everyone's day for the sake of the few who asked. A pager that is
    missing or exits early is caught and the diff printed plainly: the
    diff is the point and the pager is only how.
  - Dev extra (`pip install -e '.[dev]'`): `hypothesis`, `ruff`,
    `mypy`, `coverage`. Never runtime dependencies.
  - No `GitPython`. Call the `git` binary through `subprocess`. This
    keeps behavior identical to the user's git and avoids a heavy
    dependency.
- **Config:** `~/.config/tupferl/config.toml`, read with stdlib `tomllib`.
  Two settings: files to ignore, and the size limit.

  **This section said `.tupferl/config.toml`, inside the repository, and
  that was wrong.** A file in the repository is the repository speaking to
  every machine that clones it, so the two per-machine settings it also
  held -- a hostname and an editor -- each needed an environment override
  above them and a paragraph explaining why. Both are gone: the hostname
  comes from `$TUPFERL_HOSTNAME` or the system, the editor from git's own
  chain (`$GIT_EDITOR`, `core.editor`, `$VISUAL`, `$EDITOR`), and the file
  itself is now a dotfile in `$HOME` that `tupferl add` shares if you want
  it shared. tupferl manages its own config the way it manages yours, so
  "is this shared?" has one mechanism instead of a special rule.

  It also means `.tupferl/` in the repository holds only machinery --
  `hosts/` and `state/` -- and `manifest.mergeable` lost the exception it
  needed to let the settings file through.
- **State snapshots:** store snapshots as content-addressed blobs or
  plain copies under `.tupferl/state/<hostname>/`. Snapshots are
  per-host and are committed to the repository, so every host knows
  its own merge base. Evaluate size impact; plain copies are fine
  for version 1.
- **File attributes:** preserve the executable bit. Ignore other
  permissions in version 1. Refuse to manage symlinks, sockets, and
  files larger than a configurable limit (default 1 MB) with a clear
  message.
- **Safety:** before the tool overwrites a file in `$HOME`, write a
  backup to `~/.local/state/tupferl/backup/<timestamp>/`. Keep the
  last 5 sync backups.
- **Errors:** every error message states what happened and what the
  user can do next. One sentence each.

## 6. Installation and distribution

- Publish to PyPI. Install with `pipx install tupferl` or
  `uv tool install tupferl`.
- Also support `pip install --user tupferl`.
- One entry point: the `tupferl` command.
- README shows a complete setup in under 10 lines:

```
pipx install tupferl
tupferl init git@github.com:me/dotfiles.git
tupferl add ~/.bashrc ~/.gitconfig ~/.config/nvim
tupferl sync
# on the second computer:
pipx install tupferl
tupferl init git@github.com:me/dotfiles.git   # pulls everything
```

## 7. Testing

Testing follows the working agreements in
`martinus/ai/templates/CLAUDE.md.template` and the practice in
`martinus/woswoar`. Two rules from there govern everything below:

- The bar is not "a test exists". The bar is "the test fails when
  the change is reverted", and the only way to know is to try it.
- Coverage is not the bar. Its only role here is to partition
  mutation survivors (7.3).

Seed the repository with the template as `CLAUDE.md` in milestone 1
and fill in the project-specific sections as facts are learned.

### 7.1 Ground rules

- Framework: stdlib `unittest`, not pytest. Reason: the mutation
  tooling ported from woswoar classifies unittest result objects,
  and the test stack stays standard library.
- Runner: port `tools/run_tests.py` from woswoar (Apache-2.0) for
  sharded parallel runs. It fails when a discovered test never
  reports back — a failure mode only a parallel run has.
- No mocks for git. Drive the real `git` binary. The "remote" is a
  local bare repository in a temporary directory. Fake `$HOME` the
  same way. No network access in any test.
- Prefer driving the real thing (a full `tupferl sync` subprocess)
  over asserting on internals, where speed allows it.

### 7.2 Property-based tests — from the start

- `hypothesis` is a dev-extra dependency (never a runtime one).
- Write the sync engine's property tests **before** its example
  tests, in milestone 3. Properties to encode:
  1. One-sided change wins: `merge(base, a, base) == a` and
     `merge(base, base, b) == b`.
  2. A non-overlapping merge contains every changed line from both
     sides and no other new line.
  3. Sync is idempotent: a second `sync` with no new edits changes
     nothing — working tree, repository, and snapshots stay
     byte-identical.
  4. Convergence: two simulated machines that each run `sync` until
     quiescent end with byte-identical managed files.
  5. No silent loss: every line chosen at a conflict prompt is
     present in the result on both machines after the next sync.
- Use Hypothesis stateful testing (`RuleBasedStateMachine`) for
  properties 3–5: model two machines and one remote; the rules are
  "edit a random managed file on A or B", "sync A", "sync B",
  "resolve a conflict with a random choice"; the invariants are
  convergence and no-loss.
- Define Hypothesis profiles in one place: `dev` (default),
  `ci` (derandomized, prints the failing seed), and `mutation`
  (few examples, short deadline). Without the `mutation` profile,
  every mutant pays the full example budget and a sweep takes hours
  for no extra signal.

### 7.3 Mutation tests — from the start

- Port `tools/mutate.py` and `tools/reached.py` from woswoar rather
  than adopting mutmut. Keep their load-bearing properties:
  - Every mutant runs in a throwaway copy of the tree. The tool
    never edits the real working tree.
  - Three verdicts: `caught` (a test method noticed), `SURVIVED`
    (none did), `BROKE`/`TIMEOUT` (the run never got to ask).
    `BROKE` is never counted as `caught`.
  - Survivors from a narrow selection are re-run against the whole
    suite before they are reported.
- Workflow per change: `python -m tools.mutate --base main`, and
  paste the output into the PR verbatim — never retyped.
- Cross the survivor list with coverage
  (`python -m tools.reached results.json coverage.json --list`).
  The two halves mean opposite things: a survivor on a line no test
  executes is a **missing test**; a survivor on a line the suite
  does execute is a **weak fixture or an equivalent mutant**.
  Never read a raw survivor list as a bug count.
- When a mutation survives, suspect the fixture first. The known
  shapes: symmetric inputs, a second way to produce the same
  observable, hostile input that never reaches the code under test,
  and mutants that are equivalent by construction. An equivalent
  mutant is named as such in the PR, not "fixed" with an invented
  fixture.
- Acceptance bar for the sync engine (milestones 3–4): zero
  unexplained survivors. Every survivor is either killed by a new
  test or named equivalent with a one-line reason.

### 7.4 What to test hardest, in order

1. The 3-way sync logic: every combination of
   (local changed / repo changed / both / neither) ×
   (overlapping / non-overlapping edits). Properties 1–4 cover the
   bulk; add example tests for the exact boundary cases.
2. Conflict prompt choices (`l`, `r`, `b`, `e`, `s`) via injected
   input, plus `--theirs/--ours/--no-input`.
3. Host overlays: replacement wins, add/remove with `--host`.
4. Failure paths: push rejected, dirty repo, missing git, no
   remote, interrupted sync (kill mid-run, then sync again — the
   snapshot state must stay consistent).

### 7.5 CI

- Preflight is one line, and it is exactly what CI runs:

```sh
ruff check . && ruff format --check . && mypy tupferl tests tools \
  && python -m tools.run_tests
```

- One gate job that `needs:` every other job, with `if: always()`
  and an explicit check of every dependency's result — a skipped
  required check counts as satisfied otherwise.
- Jobs: lint + types; tests on Python 3.10, 3.12, and latest; a
  two-machine end-to-end sync against real git; a `pipx` install
  smoke test.
- Mutation sweeps run per-PR on the diff (`--base main`). The full
  sweep (`--all --json`) runs on a weekly schedule, not per PR — it
  takes hours by design.

## 8. Build order (milestones)

Build in this order. Each milestone must work end to end before the
next starts.

1. **Skeleton and test infrastructure:** package layout, CLI entry
   point, `config.toml` loading, `doctor`. In the same milestone:
   `CLAUDE.md` from the template, `tools/run_tests.py` and
   `tools/mutate.py` ported from woswoar and proven against a
   deliberate one-line bug, Hypothesis profiles, and the CI gate
   job. The test infrastructure exists before the first feature.
2. **Repo management:** `init`, `add`, `remove`, and listing what is
   managed; git calls through subprocess. (Listing was its own `list`
   verb until §4's table folded it into `status --all`.)
3. **Sync engine, no conflicts:** snapshots, change detection,
   auto-resolution for one-sided changes, commit + push + pull.
   Property tests 1–4 land in this milestone, before the example
   tests.
4. **3-way merge and the conflict UI:** the interactive prompt,
   editor handoff, `--theirs/--ours/--no-input`.
5. **Host overlays.**
6. **Safety and polish:** backups, error messages, and the quality of
   what `status` prints in each of its three shapes.
7. **Packaging:** PyPI metadata, README, `pipx` verification. Done in
   1.0.0. `.github/workflows/release.yml` publishes on a `v*` tag over
   PyPI Trusted Publishing, and checks the tag against
   `tupferl/__init__.py`, that the commit is on `main`, and the whole
   preflight before it builds -- an upload is the one thing here that
   cannot be taken back.

## 9. Open questions for the agent

Decide these during milestone 1 and record the decision in the README:

- `argparse` or `click`.
- Snapshot storage format (plain copies vs. content-addressed).
- Exact merge implementation: `git merge-file` (needs git anyway,
  battle-tested) vs. pure-Python 3-way merge. Recommendation:
  `git merge-file`.
