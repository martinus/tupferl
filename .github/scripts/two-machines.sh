#!/usr/bin/env bash
# Plan §7.5's two-machine end-to-end sync, run against the *installed* tool.
#
# This is not the suite again. `tests/test_sync.py` drives two machines too, but
# through `python -m tupferl` with `PYTHONPATH` pointing at the source tree and
# an environment built by `tests/support.py`. This runs the console script a
# `pipx install` put on PATH, with nothing but `HOME` and `TUPFERL_HOSTNAME` set
# -- so it is the one check that would notice the entry point being broken, a
# module missing from the wheel, or a test fixture having quietly become
# load-bearing.
#
# Run it locally with the source tree instead of an install:
#
#     TUPFERL="python -m tupferl" .github/scripts/two-machines.sh
#
# `set -u` as well as `-e`: a typo in a variable name here would otherwise make
# a machine's `HOME` the empty string, and the run would write into whatever
# directory it was started from.
set -euo pipefail

TUPFERL="${TUPFERL:-tupferl}"
BOX="$(mktemp -d)"
trap 'rm -rf "$BOX"' EXIT

REMOTE="$BOX/remote.git"
A="$BOX/machine-a"
B="$BOX/machine-b"

git init --quiet --bare --initial-branch=main "$REMOTE"

for home in "$A" "$B"; do
  mkdir -p "$home"
  name="$(basename "$home")"
  printf '[user]\n\tname = %s\n\temail = %s@example.invalid\n[init]\n\tdefaultBranch = main\n' \
    "$name" "$name" > "$home/.gitconfig"
done

# `env -i` keeps this honest: only PATH and the two variables tupferl is being
# asked to read. Anything else it needs and does not get would fail here, which
# is the point of running it outside the test harness.
run() {
  local home="$1"; shift
  # shellcheck disable=SC2086  # $TUPFERL is a command line, not a file name:
  # unquoted so that TUPFERL="python -m tupferl" splits into three words.
  env -i PATH="$PATH" HOME="$home" TUPFERL_HOSTNAME="$(basename "$home")" $TUPFERL "$@"
}

expect() {
  local want="$1" got="$2" what="$3"
  if [ "$want" != "$got" ]; then
    echo "::error::$what: wanted '$want', got '$got'"
    exit 1
  fi
}

printf 'export EDITOR=nvim\nexport PAGER=less\nalias ll=ls\nexport TZ=UTC\n' > "$A/.bashrc"

echo "--- machine A sets up and shares .bashrc"
run "$A" init "$REMOTE"
run "$A" add "$A/.bashrc"
run "$A" sync

echo "--- machine B gets everything from one command"
run "$B" init "$REMOTE"
expect "$(cat "$A/.bashrc")" "$(cat "$B/.bashrc")" "B did not receive .bashrc"

echo "--- both edit different lines, and neither is asked anything"
sed -i 's/EDITOR=nvim/EDITOR=emacs/' "$A/.bashrc"
sed -i 's/TZ=UTC/TZ=Europe\/Vienna/' "$B/.bashrc"
run "$A" sync
run "$B" sync
run "$A" sync
expect "$(cat "$A/.bashrc")" "$(cat "$B/.bashrc")" "the two machines did not converge"
grep -q 'EDITOR=emacs' "$B/.bashrc" || { echo "::error::B lost A's edit"; exit 1; }
grep -q 'TZ=Europe/Vienna' "$A/.bashrc" || { echo "::error::A lost B's edit"; exit 1; }

echo "--- a second sync changes nothing"
before="$(cd "$A/.local/share/tupferl/repo" && git rev-parse HEAD)"
run "$A" sync
after="$(cd "$A/.local/share/tupferl/repo" && git rev-parse HEAD)"
expect "$before" "$after" "a quiet sync wrote a commit"

echo "--- both edit the same line: reported, and nothing is written"
sed -i 's/PAGER=less/PAGER=most/' "$A/.bashrc"
sed -i 's/PAGER=less/PAGER=bat/' "$B/.bashrc"
run "$A" sync
mine="$(cat "$B/.bashrc")"
status=0
run "$B" sync || status=$?
expect "1" "$status" "a conflict must exit 1"
expect "$mine" "$(cat "$B/.bashrc")" "a conflict must leave the local copy alone"
if grep -q '<<<<<<<' "$B/.bashrc"; then
  echo "::error::conflict markers were written into a \$HOME file"
  exit 1
fi

echo "--- a backup of the file that was overwritten exists"
test -n "$(find "$B/.local/state/tupferl/backup" -name .bashrc -print -quit)" \
  || { echo "::error::no backup was taken"; exit 1; }

echo "two machines synced, converged, and reported the one conflict they had"
