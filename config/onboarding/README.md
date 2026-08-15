# Onboarding templates

Consumed by [`.github/workflows/onboard-repo-branching.yaml`](../../.github/workflows/onboard-repo-branching.yaml)
(rollout plan [Wave 1](../../docs/enterprise-program/03-rollout-plan.md#33-branching-strategy-onboarding-wave-1)).

| File | Purpose |
|---|---|
| `ruleset-template.json` | GitHub Ruleset body ([API reference](https://docs.github.com/en/rest/repos/rules)) applied to every onboarded repository's default branch. |
| `CODEOWNERS.template` | Seeded only if the repository has no `CODEOWNERS` file at any of GitHub's recognized locations. |
| `PULL_REQUEST_TEMPLATE.md.template` | Seeded only if the repository has no PR template. |
| `ci-stub.yml.template` | Seeded only if the repository has no workflow under `.github/workflows/`. A deliberately minimal placeholder — see the file itself for what it does and does not do. |
| `replay-finding.yml.template` | **Not** deployed by `onboard-repo-branching.yaml` (it depends on a later wave — see the file's own header comment). Deploy manually or via a future onboarding step once a repository can sandbox test execution. Implements the developer finding-replay mechanism ([reference architecture §2.5](../../docs/enterprise-program/02-reference-architecture.md#25-developer-finding-replay-mechanism)): commenting `/replay` on a VulnHunter finding issue re-runs its exploit test and posts the result. |

## Why `ruleset-template.json` has no `required_status_checks`

Wave 1 (branching) runs *before* Wave 2 (Dockerfile) and Wave 3 (unit tests) —
most repositories reaching Wave 1 have no CI check to require yet. Shipping a
`required_status_checks` rule pointing at a check name that will never report
would lock every PR on that repository, which is the opposite of this program's
goal. Add `required_status_checks` to a specific repository's ruleset once it
has a real, passing check (typically once it reaches Wave 3 or later) — the
`onboard-repo-branching.yaml` workflow's `update` mode (re-run with
`mode: update`) reapplies whatever the current template contains, so widening
the template's rules later reaches every already-onboarded repository on its
next run, not just newly onboarded ones.

## Ruleset conflicts

If a repository already has branch protection or an existing ruleset covering
the same branch, `onboard-repo-branching.yaml` does not overwrite it — it
looks for a ruleset it previously created (by name,
`vulnhunter-program-baseline`) and only creates or updates *that* one. A
repository with pre-existing, differently-named protection is left alone and
recorded on the dashboard as already at Wave 1 by inspection, not by this
workflow's own action.
