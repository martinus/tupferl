# Branch protection

CLAUDE.md §1 says every change reaches `main` through a green pull request, and
that this should be **enforced rather than agreed to**, "or it decays the first
time someone is in a hurry". [`main.json`](main.json) is that enforcement,
written down so it can be reviewed, reverted and re-applied like anything else
in here.

GitHub does not read this file. It has to be applied once, by hand, by someone
with admin rights on the repository.

## Applying it

Either import it in the web UI — **Settings → Rules → Rulesets → New ruleset →
Import a ruleset** — and pick `main.json`, or:

```sh
gh api -X POST repos/martinus/tupferl/rulesets --input .github/rulesets/main.json
```

To update it later, find its id with `gh api repos/martinus/tupferl/rulesets`
and `PUT` to `repos/martinus/tupferl/rulesets/<id>` with the same file.

## What it does, and the one line that matters

```json
"bypass_actors": []
```

Nobody is exempt — **repository administrators included**. That is the whole
point: an admin who can push straight to `main` is an admin who eventually will,
at the end of a long day, and the branch protection that allowed it was
protecting nothing. With the list empty, an admin in a hurry opens a pull request
like everyone else. They may still merge their own, immediately, once CI is
green; what they cannot do is skip the record and the checks.

The rest:

| rule | effect |
|---|---|
| `pull_request` | direct pushes to `main` are refused; changes arrive as PRs |
| `required_status_checks` → `gate` | the one job that `needs:` every other one. See the comment on it in [`ci.yml`](../workflows/ci.yml) for why a *single* required check, and why it needs both `if: always()` and an explicit test of every dependency's result |
| `non_fast_forward` | no force-push, so a review cannot be of a history that has since been rewritten |
| `deletion` | `main` cannot be deleted |

`required_approving_review_count` is **0**, deliberately. A solo maintainer
cannot approve their own pull request, so any number above zero makes `main`
unwriteable rather than protected — the setting that gets switched off in an
emergency and never switched back. Raise it when there is a second person.

## Verifying it took

```sh
gh api repos/martinus/tupferl/rulesets --jq '.[] | "\(.name): \(.enforcement)"'
gh api repos/martinus/tupferl/rulesets/<id> --jq '.bypass_actors'   # must be []
```

An empty `bypass_actors` and `"enforcement": "active"` are the two facts worth
reading back. A ruleset in `evaluate` mode reports what it *would* have blocked
and blocks nothing, which looks identical to a protected branch from the outside
— CLAUDE.md §8's "never trust a green run you cannot explain", one layer up.
