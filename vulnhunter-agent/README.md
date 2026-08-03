# VulnHunter Agent

A config-driven runtime that automates the [`/vulnhunt`](https://github.com/capitalone/vulnhunter)
scanner **headlessly** — no interactive Claude Code session required. Point it at a
repository and it will clone the target, run the scanner, publish the results, and file
each confirmed finding as a GitHub issue. It also has a `fix` mode that executes the
full TDD remediation lifecycle unattended in a private fork, and a `verify` mode that
drives the independent read-only fix-verification flow.

It is the automation layer around the skills: the skills define *how* to hunt and fix;
this agent makes a scan runnable unattended (CI, a scheduled job, a fleet worker, or a
container) and wires the results into GitHub.

## Purpose

- **Scan** — clone a target repo and run `/vulnhunt` against it via the
  [Claude Agent SDK](https://docs.claude.com/en/docs/claude-code), producing the standard
  `*_VULNHUNT_RESULTS_*` output directory.
- **Publish** *(optional)* — copy that results directory into a separate git repository
  and push a commit, so reports live outside the scanned repo.
- **Issues** *(optional)* — post one deduplicated GitHub issue per confirmed finding on
  the target repo, linking back to the published report; emit a "clean scan" receipt when
  there are no findings.
- **Verify** *(`--mode=verify`)* — orchestrate the `/vulnhunt-fix-verify` skill over a
  checkout and post a per-finding verdict.
- **Fix** *(`--mode=fix`)* — stage a VulnHunter report into an isolated run, drive the
  `/vulnhunter-fix` fork workflow through RED→GREEN, bounded repair, sweep, seven
  delivery gates, and private-fork issue/PR delivery.

The agent hardcodes nothing sensitive: every host, credential, and path comes from a
TOML config file and/or `VULNHUNT_*` environment variables, so the same image runs across
environments without rebuilding.

## Requirements

- Python 3.12+.
- The [Claude Agent SDK](https://docs.claude.com/en/docs/claude-code) (installed as a
  dependency) and the bundled Claude Code CLI it drives.
- `git`; fix mode also requires the `gh` CLI. GitHub operations authenticate with the
  configured role tokens.
- The repository skills installed with the root `install.sh`. Fix mode also needs the
  remediation skill's bundled Python environment created by that installer.
- Access to Claude — by default a **Claude Platform on AWS** workspace and API key.

```bash
cd vulnhunter-agent
python -m pip install -e ".[dev]"
cp agent/config.example.toml agent/config.toml   # then edit, or use env vars
```

## Quick start

Configure the default Claude Platform on AWS mode in `agent/config.toml`:

```toml
[anthropic]
auth_mode = "anthropic_aws"
model = "claude-opus-4-8"
aws_workspace_id = "wrkspc_..."
aws_region = "us-east-1"
aws_api_key = "..."
```

Then run a scan:

```bash
python -m agent --mode=scan https://github.com/your-org/your-service

# Scan only, no publish/issues:
python -m agent --mode=scan https://github.com/your-org/your-service --no-publish --no-issues
```

Run automated remediation from a local report directory:

```bash
python -m agent --mode=fix \
  https://github.com/your-org/your-service \
  /reports/YourService_VULNHUNT_RESULTS_opus48_2026-08-02

# Exercise the complete local TDD/evidence/gate path with no GitHub mutation:
python -m agent --mode=fix \
  https://github.com/your-org/your-service \
  /reports/YourService_VULNHUNT_RESULTS_opus48_2026-08-02 \
  --no-post
```

The second positional may instead be a GitHub repository URL containing exactly one
`*_VULNHUNT_RESULTS_*/README.md` tree. Remote reports use `reports_token`; target,
fork, issue, and PR operations use `scan_token`.

## Configuration

Settings load from a TOML file (`--config`, then `$VULNHUNT_AGENT_CONFIG`, then
`agent/config.toml`) and are overlaid by environment variables named
`VULNHUNT_<SECTION>_<KEY>` (env wins). See
[`agent/config.example.toml`](agent/config.example.toml) for every option.

### Authenticating to Claude — `[anthropic] auth_mode`

| `auth_mode` | How it authenticates | What to set |
|-------------|----------------------|-------------|
| `anthropic_aws` *(default)* | Claude Platform on AWS with a workspace-scoped API key | `[anthropic].aws_workspace_id`, `aws_region`, and `aws_api_key` |
| `bedrock_oauth` | Routes through an AWS Bedrock proxy fronted by an OAuth2 client-credentials token endpoint | `[anthropic].bedrock_base_url` + the `[oauth]` block (`token_endpoint`, `client_id`, `client_secret`) |
| `bedrock_sigv4` | Calls Amazon Bedrock directly with SigV4 request signing via the standard AWS credential chain — no proxy, no bearer token | `[anthropic].aws_region`; optionally `aws_profile` (named profile) and `bedrock_base_url` (VPC/custom endpoint). No `[oauth]` block. |

`anthropic_aws` passes `CLAUDE_CODE_USE_ANTHROPIC_AWS=1`,
`ANTHROPIC_AWS_WORKSPACE_ID`, `AWS_REGION`, and `ANTHROPIC_AWS_API_KEY` to the
bundled Claude Code process. `bedrock_oauth` exists for environments that front Claude with a Bedrock proxy and mint
bundled Claude Code process. `bedrock_oauth` exists for environments that front Claude
with a Bedrock proxy and mint short-lived bearer tokens. `bedrock_sigv4` is for AWS-native
setups that call Bedrock directly (use a cross-region inference-profile model ID, e.g.
`us.anthropic.claude-...`); credentials resolve from the usual AWS chain — env vars,
shared config/credentials file, SSO, or an instance/task role. Most users want the
default `anthropic_aws` mode.

### Other sections (abridged)

- `[github]` — `scan_token` (clone + issues) and `reports_token` (publish), injected into
  URLs only when the parsed host matches `host`. Set `broker_token_dir` to read tokens
  from `{dir}/{role}.json` written by an external broker instead (see below).
- `[publish]` — `destination_repo` + `branch` for pushing results.
- `[issues]` — labels, dedup, clean-scan receipts, extraction/dedup models.
- `[sandbox]` — OS-level filesystem/network sandbox for the CLI's tools.
- `[telemetry]` — optional OTLP export; `otel_exporter_otlp_endpoint` +
  `resource_attributes` (neutral default; set your own owner/org tags).
- `[scan]` — cloned-repo dir, allowed tools (`Bash` is stripped unless `--enable-bash`),
  `no_proxy`, autocompact threshold, stall timeout.
- `[verify]` — scratch dir and a `repo_aliases` table for cross-repo hint resolution.
- `[fix]` — scratch retention, private-fork owner/prefix, collaborators, repair/test
  policy, report copy limits, and extra sandbox egress for repository package mirrors.

### Automated fix-mode safety contract

Selecting `--mode=fix` is an explicit code-execution authorization: unlike default
scan mode, remediation necessarily runs exploit/security tests and repository test
tooling through Bash. The runner constrains that authority as follows:

- Execution starts in a fresh non-git scratch directory, so the skill can only select
  its fork/headless workflow; the interactive in-place workflow is not automated.
- The target URL must be a credential-free HTTPS `owner/repo` URL on `github.host`.
- A report is copied into the run after rejecting symlinks, special files, excessive
  file counts, and excessive byte counts. A remote results repository must identify
  exactly one report rather than silently choosing “latest.”
- Source and report content are declared untrusted. They cannot alter the runtime
  configuration or the checkpoint/delivery policy.
- Every phase checkpoint auto-advances only after its required artifacts validate.
  The automated profile does not skip TDD, graph analysis, completeness, sweep, schema
  validation, or any delivery gate.
- Missing external values, public-interface decisions, cross-repository coordination,
  and exhausted repair become explicit human-required outcomes; the model must not
  guess.
- Delivery is private-fork-only. For `--no-post`, Python pre-clones the target, then
  removes GitHub credentials and default GitHub egress from the model session. The run
  retains local branches and validation evidence without fork, push, issue, or PR
  mutations.
- The final `fix_disposition.json` is checked against
  `fix_disposition.schema.json`, then checked again for target/report/policy identity,
  unique finding/checkpoint IDs, complete successful checkpoints, and VERIFIED→gate
  consistency.

Fix mode uses an Opus model and rejects a non-Opus override before starting. Its GitHub
token is session-scoped and made available to `gh` and an in-memory Git credential
helper; it is never written into clone remotes or command arguments. Because the model
must operate GitHub and execute target tests, run fix jobs in a dedicated ephemeral
worker with a least-privilege token and narrowly configured `[fix].allowed_domains`.
Fix mode also forces the Claude command sandbox on, fails when it is unavailable, and
disallows unsandboxed command fallback regardless of the general `[sandbox]` defaults.

## Architecture

```
CLI (python -m agent)
  └─ config.load_config()            TOML + VULNHUNT_* env  → AgentConfig
  └─ make_token_manager(config)      anthropic_aws → AnthropicAwsApiKeyTokenManager
                                     bedrock_oauth → OAuthTokenManager
                                     bedrock_sigv4 → SigV4TokenManager
  └─ runner.run_vulnhunt()
        └─ build_claude_settings()   env (auth + proxy + telemetry) + sandbox JSON
        └─ Claude Agent SDK          runs /vulnhunt, streams events, retries on 429
  └─ fix.run_fix()
        └─ contained report staging + runtime policy
        └─ fix_runner                runs /vulnhunter-fix with Bash + task tools
        └─ fix_disposition.json      schema + semantic validation
  └─ manifest.write_manifest()       scan_manifest.json (validated against schema)
  └─ publish.publish_results()       optional: push results to destination_repo
  └─ issues stage                    optional: extract → dedup → render → post issues
  └─ audit                           optional JSONL lifecycle + finding events
```

- **Auth is a single chokepoint.** `build_claude_settings` renders the Claude Code
  settings JSON (environment + sandbox) and is the only place that knows whether to set
  the four Claude Platform on AWS variables (`anthropic_aws` mode), the Bedrock env +
  `ANTHROPIC_AUTH_TOKEN` (bedrock_oauth mode), or the Bedrock env *without* any token
  (bedrock_sigv4 mode — omitting `CLAUDE_CODE_SKIP_BEDROCK_AUTH` /
  `ANTHROPIC_AUTH_TOKEN` is what makes the bundled CLI sign requests itself). Both the
  scan loop and the issues-LLM calls go through it.
- **Token providers share one interface.** `AnthropicAwsApiKeyTokenManager`,
  `OAuthTokenManager`, and `SigV4TokenManager` all expose `get_valid_token()`;
  `make_token_manager(config)` returns the right one, so the rest of the code is
  auth-mode agnostic.
- **Contracts are schema-validated.** `scan_manifest.schema.json` (agent → scan-worker),
  `fix_disposition.schema.json` (automated remediation result), and
  `verify_disposition.schema.json` (independent verification output) are validated at
  their process boundaries.
- **The `vulnhunter` package** is the thin CLI entry point around the `agent` package.

## Customizing via a base-agent / container pattern

The agent is designed to be used as a **base** that you extend for your own environment,
rather than forked. Because all environment-specific inputs are config/env-driven, you can
build a derived agent without touching the code:

1. **Publish (or use) a base image** that installs this package and sets a neutral default
   entrypoint (`python -m agent`).
2. **Derive your own image `FROM` that base** and layer in only your environment:
   - a baked or mounted `config.toml` (or the corresponding `VULNHUNT_*` env vars);
   - `auth_mode` + credentials for how *you* reach Claude;
   - `[github]` tokens, or a `broker_token_dir` if a sidecar/parent process mints and
     refreshes tokens onto disk (the agent is then a pure token *consumer*);
   - a custom CA bundle via `[tls].ssl_cert_path`;
   - telemetry endpoint + `[telemetry].resource_attributes` tagged for your org.
3. **Wrap, don't fork.** Put org-specific orchestration (job discovery, queueing,
   result routing) in a thin parent process that shells out to `python -m agent ...` and
   reads its exit code + `scan_manifest.json`. The manifest is the stable integration
   contract; build your automation against it instead of the agent's internals.

This keeps your customizations (credentials, hosts, policy, telemetry identity) entirely
in your derived layer, so you can track upstream releases of the base agent cleanly.

## Tests

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
```

## License

Part of the VulnHunter project; licensed under the Apache License, Version 2.0. See the
repository-root `LICENSE`.
