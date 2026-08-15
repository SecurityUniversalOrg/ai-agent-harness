# VulnHunter Enterprise Program

Status: **proposed target-state architecture** — a program design that extends the
implemented system described in [`docs/architecture/`](../architecture/README.md).
Nothing in this directory describes what exists today; every diagram and claim here
is a proposal to be reviewed, sequenced, and built. Where this program depends on a
capability that already exists, it is cited directly against the current-state docs
so reviewers can see the actual delta.

Primary audience: security engineering leadership, platform/DevOps engineering,
enterprise architecture, and the governance bodies (Agile/SAFe release trains, ITIL
change advisory board) that will fund and sequence this work.

## Why this program exists

The current system ([`docs/architecture/README.md`](../architecture/README.md))
implements a complete single-repository security lifecycle — scan, publish, track,
remediate, verify — driven by three GitHub Actions workflows
([`docs/architecture/github-actions-remediation.md`](../architecture/github-actions-remediation.md)).
It does not yet answer:

- How does one operator run this consistently across **30,000 repositories**
  spanning nearly every language in use, including 20–30 year old legacy
  applications?
- How do we know, at any point in time, the security posture of the whole estate —
  not just the result of the last scan someone happened to run?
- How do we keep AI inference cost proportional to risk, rather than paying
  frontier-model prices for every routine remediation?
- How do we make a security fix trustworthy to a developer who has never seen an
  AI-authored PR, without requiring them to re-derive the finding by hand?
- How do we bring repositories with no unit tests, no Dockerfile, and no branch
  protection to a baseline where automated remediation is *safe* to run
  unattended?
- How do we absorb findings from tools we don't control (GitHub Advanced Security,
  Wiz) into the same automated remediation pipeline, instead of running two
  disconnected remediation processes?

This program answers those questions. It does not replace the existing VulnHunter
architecture — it wraps it with fleet-scale onboarding, governance, cost control,
and a reporting layer, and it extends the three workflows with new capabilities
(unit-test-aware remediation, external-findings intake, developer-triggered replay).

## Document set

| # | Document | Answers |
|---|---|---|
| 1 | [Program charter & framework alignment](01-program-charter.md) | What are we building, for whom, and how does it satisfy the governance frameworks (Agile/Scrum, SAFe, DevSecOps, NIST SSDF, ITIL) the organization already runs under? |
| 2 | [Reference architecture](02-reference-architecture.md) | What does the target system look like end to end — model tiering, the expanded workflow set, unit-test subsystem, external findings intake, developer replay, and the dashboard? |
| 3 | [Rollout plan](03-rollout-plan.md) | In what order, and how, do we bring 30,000 heterogeneous repositories to a state where this is safe and effective — branching strategy, Dockerfile baseline, unit-test baseline, then the VulnHunter workflows themselves? |
| 4 | [Unit test coverage & remediation architecture](04-unit-test-coverage-architecture.md) | How do we discover, execute, and score unit test coverage across arbitrary languages, and use that to gate and generate security-fix PRs without breaking functionality? |
| 5 | [Central reporting dashboard](05-central-reporting-dashboard.md) | What do we measure, at what grain, and how does it roll up from one repo to a 30,000-repo portfolio view? |

## Relationship to the current-state architecture

Everything in `docs/architecture/` remains authoritative for *how the existing
scan/fix/verify pipeline works today*. This program:

- **Reuses it unmodified** as the per-repository execution engine for scan, fix,
  and verify (see [reference architecture § 2](02-reference-architecture.md#2-what-stays-unchanged)).
- **Extends it** with model tiering, unit-test gating, external-findings intake,
  and developer replay (see [reference architecture § 3–7](02-reference-architecture.md)).
- **Wraps it** with fleet onboarding, a dashboard, and governance-framework
  alignment that have no equivalent in the current single-repository design.

Each extension point in this program is written against the actual current-state
implementation (file paths, config keys, schemas) so that engineering can start
from a real diff, not a green-field guess.
