# GitHub Actions authentication setup

This repository runs three GitHub Actions workflows that need their own GitHub
credentials, separate from the built-in `GITHUB_TOKEN`:

| Workflow | File | Purpose | Protected environment |
|---|---|---|---|
| **Scan** | [`org-ai-security-discovery.yaml`](org-ai-security-discovery.yaml) | Runs VulnHunter across every repo in `config/repos.csv` and files findings issues | *(none)* |
| **Fix** | [`vulnhunter-agent-fix.yaml`](vulnhunter-agent-fix.yaml) | Unattended remediation against one published report | `vulnhunter-fix` |
| **Verify** | [`vulnhunter-agent-verify.yaml`](vulnhunter-agent-verify.yaml) | Confirms whether closed findings are actually fixed | `vulnhunter-verify` |

Every workflow supports two ways to supply its GitHub tokens, selected per run
with the `github_auth_method` input:

- **`pat`** (the default) — a token you create once and store as a long-lived
  secret.
- **`github_app`** — a GitHub App installation token minted fresh at the start
  of every run, scoped to only the repository that run touches, and
  automatically revoked when the job ends.

This document is a step-by-step setup guide for both. For the design
rationale and exactly how each workflow consumes these tokens, see
[GitHub Actions remediation and verification § GitHub authentication: PAT or GitHub App](../../docs/architecture/github-actions-remediation.md#github-authentication-pat-or-github-app).

> Pick one method per workflow — you don't have to use the same method for
> scan, fix, and verify. A common pattern is `pat` while you're getting a
> workflow working, then `github_app` once you're ready to stop maintaining a
> long-lived credential.

Independently, every workflow *also* has an `anthropic_auth_method` input
selecting how the agent itself authenticates to Claude — `api_key` (the
default, a long-lived Claude Platform on AWS workspace API key) or
`aws_role` (no static credential at all: the job assumes an AWS IAM role via
OIDC and the agent signs Bedrock requests with those temporary credentials).
This is completely independent of `github_auth_method` above — pick whichever
combination you want per workflow.

## Table of contents

- [Quick reference](#quick-reference)
- [Which token does what](#which-token-does-what)
- [Anthropic credentials: API key or AWS role](#anthropic-credentials-api-key-or-aws-role)
- [Option A: Personal Access Token (PAT) setup](#option-a-personal-access-token-pat-setup)
- [Option B: GitHub App setup](#option-b-github-app-setup)
- [Where to put secrets: repository, environment, or organization](#where-to-put-secrets-repository-environment-or-organization)
- [Per-workflow dispatch checklist](#per-workflow-dispatch-checklist)
- [Repository/organization variables reference](#repositoryorganization-variables-reference)
- [Troubleshooting](#troubleshooting)
- [Security recommendations](#security-recommendations)

## Quick reference

| Workflow | `github_auth_method=pat` secrets | `github_auth_method=github_app` secrets |
|---|---|---|
| Scan | `VULNHUNT_GITHUB_SCAN_TOKEN`, `VULNHUNT_GITHUB_REPORTS_TOKEN` | `VULNHUNT_GITHUB_APP_ID`, `VULNHUNT_GITHUB_APP_PRIVATE_KEY` |
| Fix | `VULNHUNT_GITHUB_FIX_TOKEN`, `VULNHUNT_GITHUB_REPORTS_TOKEN` | `VULNHUNT_GITHUB_APP_ID`, `VULNHUNT_GITHUB_APP_PRIVATE_KEY` |
| Verify | `VULNHUNT_GITHUB_VERIFY_TOKEN`, `VULNHUNT_GITHUB_REPORTS_TOKEN` | `VULNHUNT_GITHUB_APP_ID`, `VULNHUNT_GITHUB_APP_PRIVATE_KEY` |

All three workflows additionally need Anthropic credentials — either
`ANTHROPIC_AWS_WORKSPACE_ID` + `ANTHROPIC_AWS_API_KEY`, or
`ANTHROPIC_AWS_ROLE_ARN`, selected per run by the `anthropic_auth_method`
input (see
[below](#anthropic-credentials-api-key-or-aws-role)) — this is completely
unrelated to `github_auth_method`/GitHub authentication.

Every workflow validates its inputs in a "Validate GitHub authentication
inputs" step that runs *before* any clone or checkout. If you pick
`github_auth_method=pat` but only set up the App secrets (or vice versa), the
run fails immediately with a clear `::error::` instead of a confusing
downstream 401/403.

## Which token does what

Each token is scoped to exactly what that role touches — this matters for
both PAT scope selection and GitHub App permissions/installation below.

| Role | Used by | What it does | Minimum REST operations | PAT scope guidance | GitHub App permissions |
|---|---|---|---|---|---|
| `scan-token` | Scan | Clones the scanned repo; lists/creates/closes findings issues and labels on it | `GET/PUT labels`, `POST issues`, `PATCH issues`, `POST issue comments`, clone (git) | Classic: `repo`. Fine-grained: **Contents: Read**, **Issues: Read and write** | **Contents: Read**, **Issues: Read and write** |
| `verify-token` | Verify | Clones the target repo (and any cross-referenced repos); reads issue/comment/timeline history via REST + GraphQL; posts verdict comments and reopens issues when enabled | `GET issues`, GraphQL `userContentEdits`, `GET comments/events`, `POST issue comments`, `PATCH issues`, clone (git) | Classic: `repo`. Fine-grained: **Contents: Read**, **Issues: Read and write**, **Metadata: Read** | **Contents: Read**, **Issues: Read and write** |
| `fix-token` | Fix | Clones the target repo; when `delivery_enabled=true`, forks it, pushes branches, and opens PRs/issues in that private fork | Clone (git); `POST /repos/{owner}/{repo}/forks`; push (git); `POST pulls`; `POST issues` | Classic: `repo`, plus membership/repo-creation rights in the fork destination org. Fine-grained: **Contents: Read** (dry run) or **Contents: Read and write**, **Pull requests: Read and write**, **Issues: Read and write**, **Administration: Read and write** (delivery) | See [Fork delivery: a two-sided requirement](#fork-delivery-a-two-sided-requirement) below — this one is not a single simple grant |
| `reports-token` | All three | Reads the exact published report (fix/verify: read-only checkout of one path) or pushes new results (scan: write) | Sparse checkout (fix/verify) or `git push` (scan) | Classic: `repo`. Fine-grained: **Contents: Read** (fix/verify) or **Contents: Read and write** (scan) | **Contents: Read** (fix/verify) or **Contents: Read and write** (scan) |

`fix-token` and `verify-token` are deliberately separate from `scan-token`
even though they often point at the same target repo — see
[`docs/architecture/github-actions-remediation.md`](../../docs/architecture/github-actions-remediation.md)
for why the codebase keeps per-role tokens rather than one shared credential.

### Fork delivery: a two-sided requirement

`delivery_enabled=true` fix runs create a **private fork** of the target
repo and push there — this is the one role that isn't a simple "grant access
to one repo" story, for either auth method:

- **PAT**: the token's owner needs permission to create repositories in the
  fork destination (`fork-org` — see [variables reference](#repositoryorganization-variables-reference)
  below). A classic PAT with the `repo` scope, from an account that's a
  member of `fork-org` with repo-creation rights (or the org allows any
  member to create repos), covers this in one step.
- **GitHub App**: per
  [GitHub's own documentation](https://docs.github.com/en/rest/repos/forks#create-a-fork),
  *"the GitHub App must be installed on the destination account with access
  to all repositories and on the source account with access to the source
  repository."* Concretely: install the App on the target org (scoped to the
  target repo is fine) **and separately** on `fork-org` with **all
  repositories** access (not scoped to one repo — the fork doesn't exist
  until the run creates it) and **Administration: Read and write** there.

If you're setting up `github_auth_method=github_app` for fix, **start with
`delivery_enabled=false`** (the default) and confirm the dry run succeeds
before enabling delivery and testing the fork path. This repository's own
sandbox scripts and workflow wiring for GitHub App auth have not been
exercised end-to-end against live fork creation — treat the first
delivery-enabled + `github_app` run as a real test, not an assumption.

## Anthropic credentials: API key or AWS role

Unrelated to GitHub auth (see [above](#which-token-does-what) for that), but
required either way. Every workflow's `anthropic_auth_method` input picks
between two ways to authenticate to Claude:

| `anthropic_auth_method` | How it authenticates | What to set |
|---|---|---|
| `api_key` *(default)* | Claude Platform on AWS with a workspace-scoped API key | `ANTHROPIC_AWS_WORKSPACE_ID` + `ANTHROPIC_AWS_API_KEY` secrets |
| `aws_role` | Amazon Bedrock, calling `aws-actions/configure-aws-credentials` to assume an IAM role via OIDC before the scan/fix/verify step runs — no long-lived credential in secret storage at all | `ANTHROPIC_AWS_ROLE_ARN` secret |

Both methods also read the `VULNHUNT_ANTHROPIC_AWS_REGION` repository/org
variable (default `us-east-1`) for the AWS region.

> **Mythos (`model: claude-mythos-5`) does not support `aws_role` yet.** The
> isolated gVisor container only forwards Claude Platform on AWS API-key
> credentials into its egress-restricted network — see
> [`vulnhunter-agent/README.md`](../../vulnhunter-agent/README.md#authenticating-to-claude--anthropic-auth_mode)
> for why. Every workflow validates this combination up front and fails fast
> with a clear `::error::` rather than a confusing runtime failure. Use
> `anthropic_auth_method=api_key` for any Mythos run.

### Option A: API key (`anthropic_auth_method=api_key`)

The simplest way to get a workflow running, and the default. Create a Claude
Platform on AWS workspace, generate a workspace API key, and store both
values as secrets:

| Secret | Purpose |
|---|---|
| `ANTHROPIC_AWS_WORKSPACE_ID` | Claude Platform on AWS workspace selection |
| `ANTHROPIC_AWS_API_KEY` | Claude Platform on AWS authentication |

This is a long-lived credential — rotate it periodically, same as a PAT.

### Option B: AWS role (`anthropic_auth_method=aws_role`)

No static Anthropic credential enters secret storage at all. The job assumes
an IAM role via GitHub's OIDC identity provider (the same mechanism
`aws-actions/configure-aws-credentials` uses everywhere else), and the
bundled Claude Code CLI signs each Bedrock request directly with the
resulting temporary credentials (SigV4) — see
[`vulnhunter-agent/README.md`](../../vulnhunter-agent/README.md#authenticating-to-claude--anthropic-auth_mode)'s
`bedrock_sigv4` auth mode for how the agent itself consumes this.

1. In AWS IAM, [add GitHub's OIDC provider](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services#adding-the-identity-provider-to-aws)
   (`token.actions.githubusercontent.com`) to your account, if it isn't
   already there.
2. Create an IAM role with a trust policy scoped to this repository (and,
   ideally, the specific workflow ref) — GitHub's docs above show the exact
   trust-policy condition keys. Grant it only `bedrock:InvokeModel` and
   `bedrock:InvokeModelWithResponseStream` on the Claude model ARNs you use,
   in the region(s) you scan from. No other AWS permissions are needed.
3. Store the role's ARN as the `ANTHROPIC_AWS_ROLE_ARN` secret (see
   [where to put secrets](#where-to-put-secrets-repository-environment-or-organization)
   below for exactly where).
4. Set `anthropic_auth_method: aws_role` on dispatch (or, for scan's
   scheduled runs, set the `VULNHUNT_ANTHROPIC_AUTH_METHOD` repository/org
   variable — scheduled runs can't read a dispatch input, mirroring how
   `VULNHUNT_GITHUB_AUTH_METHOD` works for GitHub auth).

Each workflow's job already declares `permissions: id-token: write` so the
OIDC assumption step can request a token; you don't need to add that
yourself.

## Option A: Personal Access Token (PAT) setup

PATs are the simplest way to get a workflow running and the default
(`github_auth_method=pat`). Fine-grained PATs are strongly preferred over
classic PATs — they let you grant exactly the permissions in the table above
instead of the blanket `repo` scope, and they're scoped to specific
repositories instead of every repo the account can see.

### Steps (repeat per token role)

1. GitHub → your profile photo → **Settings** → **Developer settings** →
   **Personal access tokens** → **Fine-grained tokens** → **Generate new
   token**.
2. **Resource owner**: the org/account that owns the repo(s) this token
   needs to touch (see the table above — `scan-token`/`verify-token`/
   `fix-token` target the *scanned/verified/fixed* repo's owner;
   `reports-token` targets the *reports repository*'s owner).
3. **Repository access**: "Only select repositories" → pick exactly the
   repo(s) this token needs. For `scan-token`, that's every repo listed in
   [`config/repos.csv`](../../config/repos.csv) — for more than a handful of
   repos, a classic PAT with `repo` scope (blanket access) or a GitHub App
   installed org-wide is more practical than hand-picking each one in the
   fine-grained token UI.
4. **Permissions** → **Repository permissions**: set exactly what the
   table above lists for this role (e.g. `scan-token` → Contents: Read,
   Issues: Read and write). Leave everything else "No access".
5. **Expiration**: set the shortest expiration your operational tolerance
   allows, and put a recurring calendar reminder to rotate it — a
   fine-grained PAT cannot auto-renew the way a GitHub App installation
   token does. This is the main practical advantage `github_app` has over
   `pat` for a workflow you intend to run indefinitely.
6. Generate, copy the token immediately (GitHub shows it once), and store it
   as the matching secret from the [quick reference table](#quick-reference)
   — see [where to put secrets](#where-to-put-secrets-repository-environment-or-organization)
   below for exactly where.

`reports-token` is shared across all three workflows (they all read from or
publish to the same central reports repository) — create it once and reuse
the same secret value in each workflow's secret store.

## Option B: GitHub App setup

A GitHub App gives you one reusable credential (an App ID + private key)
that mints short-lived, automatically-scoped, automatically-revoked tokens
for every run — no long-lived secret to rotate, and every minted token is
visible in the App's own installation audit trail.

> **Token lifetime**: installation tokens are valid for exactly 1 hour —
> GitHub's API has no option for longer. Scan's Mythos path handles this
> automatically: since its scan step can run for hours and its delivery step
> runs strictly afterward, `claude-agent-sec-scan` re-mints fresh
> `scan-token`/`reports-token` immediately before delivery instead of
> reusing the ones minted at job start. Fix and verify don't need this —
> under Mythos they use their tokens once, immediately after minting, and
> never again (delivery is disabled entirely under Mythos for both). If a
> non-Mythos (Opus) scan run's *scan phase alone* exceeds an hour, its
> token can still go stale — that path has no discrete "deliver" step to
> refresh at, since scan/publish/issues all happen inside one continuous
> process; prefer `pat` for Opus scans you expect to run long, or keep
> individual repo scans under an hour.

### 1. Create the App

1. GitHub → your profile photo (or org settings, if this is an
   org-owned App) → **Settings** → **Developer settings** → **GitHub Apps**
   → **New GitHub App**.
2. **GitHub App name**: anything identifying, e.g. `vulnhunter-agent-ci`.
3. **Homepage URL**: any valid URL — this repository's URL is fine.
4. **Webhook**: uncheck **Active**. These workflows never receive webhook
   events; leaving it active just adds an unused attack surface.
5. **Repository permissions** — grant the *union* of what every role you
   plan to use needs, from the [table above](#which-token-does-what). If
   you're only setting this App up for one workflow (say, verify only),
   grant only that workflow's permissions:

   | Permission | Access level | Needed by |
   |---|---|---|
   | Contents | Read and write | All roles (write needed by `reports-token` on scan, and `fix-token` when delivery is enabled) |
   | Issues | Read and write | `scan-token`, `verify-token`, `fix-token` (delivery) |
   | Pull requests | Read and write | `fix-token` (delivery) |
   | Administration | Read and write | `fix-token` (delivery — fork creation only; see [Fork delivery](#fork-delivery-a-two-sided-requirement)) |
   | Metadata | Read-only | Every role (GitHub grants this automatically) |

6. **Where can this GitHub App be installed?**: "Only on this account"
   unless you specifically need it installed across multiple
   organizations/accounts you don't control directly.
7. Create the App.

### 2. Generate the private key

1. On the App's settings page, scroll to **Private keys** → **Generate a
   private key**. This downloads a `.pem` file — GitHub does not keep a
   copy, so store it securely immediately (a secrets manager, not a laptop
   downloads folder).
2. Note the **App ID** shown near the top of the same page — you'll need
   both values for secrets in step 4.

### 3. Install the App

Install the App (**Install App** in the left sidebar) on every account
these tokens need to reach:

- The org/account owning the repo(s) `scan-token` clones and files issues
  on — select the specific repos from `config/repos.csv`, or "All
  repositories" if that's simpler to maintain as the CSV changes.
- The org/account owning the repo `verify-token`/`fix-token` targets for a
  given run (this can be "All repositories" on an org if you run fix/verify
  against many repos in that org, or a specific repo if you only ever
  target one).
- The org/account owning the **reports repository** (`reports-token`).
- If `fix-token` will ever run with `delivery_enabled=true`: **also**
  install on `fork-org` with **all repositories** access — see
  [Fork delivery](#fork-delivery-a-two-sided-requirement) above, this is a
  separate installation from the target repo's.

The same App can be (and typically is) installed multiple times across
different accounts/orgs; each installation is independent.

### 4. Store the App credentials as secrets

For each workflow you want to use `github_auth_method=github_app` with, add
two secrets (see [where to put secrets](#where-to-put-secrets-repository-environment-or-organization)
for exactly where):

| Secret | Value |
|---|---|
| `VULNHUNT_GITHUB_APP_ID` | The App ID from step 2 |
| `VULNHUNT_GITHUB_APP_PRIVATE_KEY` | The full contents of the `.pem` file from step 2, including the `-----BEGIN/END PRIVATE KEY-----` lines |

These same two secrets are reused across all three workflows if you use one
App everywhere — you don't need a separate App per workflow, only a
correctly-scoped installation per org/account each workflow's tokens need
to reach.

## Where to put secrets: repository, environment, or organization

- **Fix and verify** each run under a [protected GitHub Environment](https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment)
  (`vulnhunter-fix` and `vulnhunter-verify` respectively — see each
  workflow's `environment:` key). **Store their secrets as environment
  secrets on those environments**, not repository secrets, and configure
  required reviewers on both environments. This is what makes
  `delivery_enabled`/`post_comments` actually gated by human approval rather
  than a config default.
- **Scan** (`org-ai-security-discovery.yaml`) does not declare a protected
  environment today, so its secrets must be repository or organization
  secrets.
- Any secret needed by more than one workflow (in particular
  `VULNHUNT_GITHUB_APP_ID`/`VULNHUNT_GITHUB_APP_PRIVATE_KEY`, or
  `ANTHROPIC_AWS_ROLE_ARN`, if you use one App/role everywhere) can be set
  once as an **organization secret** scoped to this repository, instead of
  duplicating it into every environment.

## Per-workflow dispatch checklist

**Scan** (`workflow_dispatch` or the weekly `schedule`):
- [ ] `config/repos.csv` lists every repo to scan
- [ ] Either `VULNHUNT_GITHUB_SCAN_TOKEN` + `VULNHUNT_GITHUB_REPORTS_TOKEN`, or `VULNHUNT_GITHUB_APP_ID` + `VULNHUNT_GITHUB_APP_PRIVATE_KEY`, are set
- [ ] Either `ANTHROPIC_AWS_WORKSPACE_ID` + `ANTHROPIC_AWS_API_KEY`, or `ANTHROPIC_AWS_ROLE_ARN`, are set
- [ ] For scheduled (non-dispatch) runs: set the repository variable `VULNHUNT_GITHUB_AUTH_METHOD` if you want scheduled runs to use `github_app`, and/or `VULNHUNT_ANTHROPIC_AUTH_METHOD` if you want them to use `aws_role` — scheduled runs can't read a dispatch input, so they fall back to these variables (default `pat` / `api_key`)
- [ ] If using Mythos (`model: claude-mythos-5`), also set `mythos_retention_acknowledged: true`, provision a `gvisor`-labeled runner, and leave `anthropic_auth_method` at `api_key` (`aws_role` isn't supported for Mythos)

**Fix** (`workflow_dispatch` or `workflow_call`):
- [ ] A published report exists (`reports_repository` + `reports_ref` + `results_path` point at it)
- [ ] Either `VULNHUNT_GITHUB_FIX_TOKEN` + `VULNHUNT_GITHUB_REPORTS_TOKEN`, or `VULNHUNT_GITHUB_APP_ID` + `VULNHUNT_GITHUB_APP_PRIVATE_KEY`, are set as **environment secrets on `vulnhunter-fix`**
- [ ] Either `ANTHROPIC_AWS_WORKSPACE_ID` + `ANTHROPIC_AWS_API_KEY`, or `ANTHROPIC_AWS_ROLE_ARN`, are set as **environment secrets on `vulnhunter-fix`**
- [ ] `delivery_enabled` left `false` for a first run; see [Fork delivery](#fork-delivery-a-two-sided-requirement) before ever setting it `true`
- [ ] If using Mythos, also set `mythos_retention_acknowledged: true` and leave `anthropic_auth_method` at `api_key`

**Verify** (`workflow_dispatch` or `workflow_call`):
- [ ] `VULNHUNT_TRUSTED_ISSUE_AUTHORS` (repository/org variable) names the scanner bot or App login that created the findings you're verifying
- [ ] Either `VULNHUNT_GITHUB_VERIFY_TOKEN` + `VULNHUNT_GITHUB_REPORTS_TOKEN`, or `VULNHUNT_GITHUB_APP_ID` + `VULNHUNT_GITHUB_APP_PRIVATE_KEY`, are set as **environment secrets on `vulnhunter-verify`**
- [ ] Either `ANTHROPIC_AWS_WORKSPACE_ID` + `ANTHROPIC_AWS_API_KEY`, or `ANTHROPIC_AWS_ROLE_ARN`, are set as **environment secrets on `vulnhunter-verify`**
- [ ] `post_comments`/`reopen_nonfixed` left `false` for a first run
- [ ] If using Mythos, also set `mythos_retention_acknowledged: true` and leave `anthropic_auth_method` at `api_key`

## Repository/organization variables reference

These are `vars.*`, not secrets — configure under **Settings → Secrets and
variables → Actions → Variables** (repository or organization level):

| Variable | Used by | Purpose |
|---|---|---|
| `VULNHUNT_GITHUB_AUTH_METHOD` | Scan (scheduled runs only) | `pat` or `github_app`; `workflow_dispatch` runs use the dispatch input instead |
| `VULNHUNT_ANTHROPIC_AUTH_METHOD` | Scan (scheduled runs only) | `api_key` or `aws_role`; `workflow_dispatch` runs use the dispatch input instead |
| `VULNHUNT_ANTHROPIC_AWS_REGION` | All three | AWS region for Bedrock/Claude Platform on AWS (default `us-east-1`) |
| `VULNHUNT_REPORTS_REPOSITORY` | Scan | Central reports repo URL (defaults to this repo) |
| `VULNHUNT_REPORTS_BRANCH` | Scan | Central reports branch (default `main`) |
| `VULNHUNT_PUBLISH_RESULTS` | Scan (scheduled runs only) | `false` to disable report publishing on schedule |
| `VULNHUNT_SUBMIT_REPO_ISSUES` | Scan (scheduled runs only) | `false` to disable issue filing on schedule |
| `VULNHUNT_FIX_FORK_ORG` | Fix | Where private forks are created — see [Fork delivery](#fork-delivery-a-two-sided-requirement) |
| `VULNHUNT_FIX_ALLOWED_DOMAINS` | Fix | Extra dependency-install domains allowed in the sandbox (Opus only — forbidden under Mythos) |
| `VULNHUNT_FIX_RUNNER` / `VULNHUNT_FIX_GVISOR_RUNNER` | Fix | Runner label override for Opus / Mythos |
| `VULNHUNT_VERIFY_RUNNER` / `VULNHUNT_VERIFY_GVISOR_RUNNER` | Verify | Runner label override for Opus / Mythos |
| `VULNHUNT_TRUSTED_ISSUE_AUTHORS` | Verify | Comma-separated logins verify trusts as finding-issue authors |

## Troubleshooting

- **`::error::github_auth_method=pat requires the ... secrets.`** — the
  method is `pat` (or defaulted to it) but one or both of that workflow's
  PAT secrets is empty. Check you set them on the right scope (environment
  vs repository — see [above](#where-to-put-secrets-repository-environment-or-organization)).
- **`::error::github_auth_method=github_app requires the ... secrets.`** —
  same, for `VULNHUNT_GITHUB_APP_ID`/`VULNHUNT_GITHUB_APP_PRIVATE_KEY`.
- **`::error::anthropic_auth_method=api_key requires the ... secrets.`** /
  **`::error::anthropic_auth_method=aws_role requires the ANTHROPIC_AWS_ROLE_ARN
  secret.`** — same idea, for the Anthropic credential pair: check
  `ANTHROPIC_AWS_WORKSPACE_ID`/`ANTHROPIC_AWS_API_KEY` (api_key) or
  `ANTHROPIC_AWS_ROLE_ARN` (aws_role) are set at the right scope.
- **`::error::claude-mythos-5 does not yet support anthropic-auth-mode=aws_role...`**
  — Mythos's isolated gVisor container only forwards Claude Platform on AWS
  API-key credentials into its egress-restricted network; switch that
  dispatch's `anthropic_auth_method` back to `api_key`.
- **`aws-actions/configure-aws-credentials` fails with "Not authorized to
  perform sts:AssumeRoleWithWebIdentity"** — the IAM role's trust policy
  doesn't match this repository/workflow, or the job is missing
  `permissions: id-token: write` (every workflow here already declares it at
  the job level — check you didn't override `permissions:` in a fork or
  local copy). Recheck the trust-policy condition keys against
  [GitHub's OIDC docs](https://docs.github.com/en/actions/deployment/security-hardening-your-deployments/configuring-openid-connect-in-amazon-web-services).
- **Bedrock calls fail with `AccessDeniedException` under `aws_role`** — the
  assumed role is missing `bedrock:InvokeModel`/
  `bedrock:InvokeModelWithResponseStream` on the model ARN, or
  `VULNHUNT_ANTHROPIC_AWS_REGION` points at a region where that model isn't
  enabled for your account.
- **`actions/create-github-app-token` fails with a 404 or "resource not
  accessible"** — the App isn't installed on the account/repo the run is
  trying to scope a token to. Recheck [step 3](#3-install-the-app);
  remember `fix-token`'s destination org needs a *separate* installation
  from its source org when delivery is enabled.
- **A token minted successfully but the run still gets a 403 from
  GitHub** — the App's *permissions* (step 1) don't cover the operation
  being attempted, even though the *installation* (step 3) is correct.
  Recheck the [permissions table](#1-create-the-app) against what actually
  failed.
- **PAT preflight failure with a token fingerprint in the error** — the
  agent's own startup preflight (`agent/__main__.py`) hit an auth failure
  against `GET /installation/repositories` and prints a token fingerprint
  (`ghp_...abcd (len=40)`) so you can compare it against what you configured
  — a mismatch usually means a stale secret value or the wrong secret scope
  shadowing the one you meant to set.
- **`[github] reports_token failed preflight ... (status 401)` (or
  `scan_token`) inside "Deliver Mythos results" on a long-running Mythos
  scan** — GitHub App installation tokens are hard-capped at **1 hour** by
  GitHub's API; there is no way to mint a longer-lived one. If your scan
  target takes longer than that, the `scan-token`/`reports-token` minted at
  the start of the job (before the multi-hour gVisor scan step) will have
  expired by the time delivery runs. This is expected and already handled
  for `github_auth_method=github_app`: `claude-agent-sec-scan` re-mints
  fresh, identically-scoped tokens immediately before "Deliver Mythos
  results" runs (see `Refresh scan-scoped GitHub App installation token`
  and `Refresh reports-scoped GitHub App installation token` in that
  action's log). If you're still seeing this:
  - Confirm `github_auth_method` was actually `github_app` for the run (a
    `pat` run has no refresh step, since PATs don't expire on a fixed short
    window — check the resolved value in the "Validate GitHub
    authentication inputs" step's log).
  - Confirm the App is installed with access to whatever repo the *stale*
    token role needs (`scan-token` → the scanned repo; `reports-token` →
    the reports destination) — the refresh step mints against the same
    scope the original token used, so a missing installation fails the
    refresh too, just with a different error (see the
    `actions/create-github-app-token` 404 entry above).
  - `pat`-based tokens genuinely cannot be refreshed mid-job by this
    workflow — a PAT that's valid at job start stays valid for its whole
    configured lifetime, so this specific failure mode is `github_app`-only.
    If you're deliberately using `pat` and still hit a 401 partway through a
    long run, the PAT itself expired or was revoked — check its expiration
    date, not the workflow.

## Security recommendations

- Prefer `github_app` for any workflow you run routinely — it removes a
  long-lived credential from secret storage entirely.
- Prefer `anthropic_auth_method=aws_role` for the same reason on the
  Anthropic side — no long-lived `ANTHROPIC_AWS_API_KEY` sits in secret
  storage, and every credential is scoped to one job run via OIDC. Scope the
  IAM role's trust policy to this repository (and ideally the specific
  workflow ref) and grant it nothing beyond `bedrock:InvokeModel`/
  `bedrock:InvokeModelWithResponseStream`.
- If you do use `pat`, prefer fine-grained tokens scoped to exactly the
  repos in the [table above](#which-token-does-what), set the shortest
  expiration you can tolerate, and rotate before it lapses.
- Configure required reviewers on the `vulnhunter-fix` and
  `vulnhunter-verify` environments before ever setting `delivery_enabled`
  or `post_comments` to `true` by default for anyone other than yourself.
- Never grant a fine-grained PAT or a GitHub App installation more repos or
  more permissions than the [table above](#which-token-does-what) lists for
  the role you're configuring it for — the codebase's own design keeps
  these roles separate specifically so a compromised token has a small,
  known blast radius.
