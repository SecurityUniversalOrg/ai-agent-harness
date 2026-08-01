# Architecture decisions

These are **inferred ADRs**: decisions consistently embodied in source, schemas, and
skill instructions, but not previously stored as formal ADR files. Their status is
`Observed` rather than `Accepted` so the documentation does not invent governance
history. Maintainers can split them into numbered ADRs if they want an approval log.

## Decision index

| ID | Decision | Status |
|---|---|---|
| IADR-001 | Separate security methodology skills from the headless integration runtime | Observed |
| IADR-002 | Use filesystem artifacts and schemas as phase and process boundaries | Observed |
| IADR-003 | Default scans to a strict, Bash-free tool envelope | Observed |
| IADR-004 | Support three inference-authentication strategies behind one token-provider interface | Observed |
| IADR-005 | Separate GitHub scan and report identities, with optional brokered refresh | Observed |
| IADR-006 | Publish reports to a distinct private repository before opening findings | Observed |
| IADR-007 | Use deterministic keys first and bounded semantic comparison second for issue deduplication | Observed |
| IADR-008 | Prefer AST graph evidence and degrade explicitly to low-confidence grep | Observed |
| IADR-009 | Require RED-to-GREEN evidence and deterministic delivery gates for remediation | Observed |
| IADR-010 | Treat issue content as untrusted and verify against reconstructed original markers | Observed |
| IADR-011 | Retry inference only where restart cannot discard useful work | Observed |
| IADR-012 | Customize deployments through config/env and wrapper processes instead of source forks | Observed |

## IADR-001: Separate methodology skills from the headless runtime

**Context.** Security analysis and remediation need rich, evolving instructions, while
unattended operation needs deterministic credential, filesystem, retry, and API logic.

**Decision.** Keep audit/remediation methodology in versioned Markdown skills and
phase prompts. Put unattended execution and external integration in the Python agent.

**Consequences.** Methodology can evolve without redesigning the CLI, and interactive
and headless execution can share the same prompts. Conversely, prompt changes are code
changes: they can break contracts without a Python type checker and must be covered by
sync/contract tests.

**Evidence.** [`vulnhunt/SKILL.md`](../../vulnhunt/SKILL.md),
[`vulnhunter-fix/SKILL.md`](../../vulnhunter-fix/SKILL.md), and
[`agent/runner.py`](../../vulnhunter-agent/agent/runner.py).

## IADR-002: Use filesystem artifacts and schemas as boundaries

**Context.** LLM sessions and subagents are asynchronous, context-limited, and not a
reliable in-memory workflow bus.

**Decision.** Require each phase to write specifically named artifacts, verify their
existence before advancing, and schema-validate machine-consumed JSON at trust
boundaries. Commit the scan manifest atomically.

**Consequences.** Runs are inspectable and partially recoverable; external workers can
integrate without importing agent internals. The repository must actively manage
schema versions and prompt/schema drift. Presence-only checks on some Markdown
artifacts remain weaker than schema validation.

**Evidence.** [`vulnhunt/SKILL.md`](../../vulnhunt/SKILL.md),
[`agent/manifest.py`](../../vulnhunter-agent/agent/manifest.py),
[`agent/verify_runner.py`](../../vulnhunter-agent/agent/verify_runner.py), and
[`vulnhunter-fix/references/`](../../vulnhunter-fix/references/).

## IADR-003: Default scans to a strict, Bash-free tool envelope

**Context.** A scanned repository is untrusted. Giving a model shell access to it can
execute malicious build hooks, dependency scripts, or test code.

**Decision.** Default to a read-only prompt and strip Bash from the effective SDK tool
list. Require two paired CLI flags to enable code execution. Set both SDK tool
visibility and approval lists to the same minimal set, with an optional OS-level
filesystem/network sandbox.

**Consequences.** Static scans are safer and reproducible, but exploit tests are written
rather than executed unless an operator explicitly trusts the checkout. The sandbox
does not constrain the Python orchestrator itself, so deployment isolation is still
required.

**Evidence.** [`agent/__main__.py`](../../vulnhunter-agent/agent/__main__.py),
[`agent/runner.py`](../../vulnhunter-agent/agent/runner.py), and
[`agent/build_settings.py`](../../vulnhunter-agent/agent/build_settings.py).

## IADR-004: Unify three inference-authentication modes

**Context.** Deployments may call Anthropic directly, use an OAuth-fronted Bedrock
proxy, or call Bedrock with the AWS credential chain and SigV4.

**Decision.** Expose `get_valid_token()` through API-key, OAuth, and SigV4 providers.
Centralize provider-specific environment construction in `build_claude_settings`.

**Consequences.** Scan, verify, extraction, and dedup code stay authentication-agnostic.
The shared interface returns an empty string for SigV4, so correct omission of bearer
environment variables is a critical invariant covered in the settings builder.

**Evidence.** [`agent/auth.py`](../../vulnhunter-agent/agent/auth.py) and
[`agent/build_settings.py`](../../vulnhunter-agent/agent/build_settings.py).

## IADR-005: Separate GitHub identities by role

**Context.** Target-repository access and report-repository access have different
least-privilege scopes and rotation lifecycles.

**Decision.** Use a `scan` identity for target clone/issues/verify and a `reports`
identity for report publication/download. Optionally read role tokens from files
maintained by an external broker, resolving the file on every HTTP request.

**Consequences.** A report token does not need target issue privileges and a scan token
does not need report write privileges. Broker mode supports short-lived credentials
without a restart, at the cost of a local file-integrity and availability dependency.

**Evidence.** [`agent/token_client.py`](../../vulnhunter-agent/agent/token_client.py),
[`agent/clone.py`](../../vulnhunter-agent/agent/clone.py), and
[`agent/publish.py`](../../vulnhunter-agent/agent/publish.py).

## IADR-006: Publish reports before creating findings

**Context.** Finding issues need durable links to detailed evidence, while the scanned
repository should not be filled with bulky reports and exploit artifacts.

**Decision.** Store results under a source/repository/timestamp/commit/results path in
a separate private Git repository. Reject issue delivery when a fresh scan is not also
published.

**Consequences.** Reports have independent access control and history, and issue bodies
remain concise. Publication becomes a critical dependency for issue delivery and adds
a partial-failure boundary. Re-running identical publication content fails loudly.

**Evidence.** [`agent/__main__.py`](../../vulnhunter-agent/agent/__main__.py),
[`agent/publish.py`](../../vulnhunter-agent/agent/publish.py), and
[`agent/issues_render.py`](../../vulnhunter-agent/agent/issues_render.py).

## IADR-007: Deduplicate by stable key, then semantics

**Context.** Scan-local `VULN-NNN` identifiers are not stable across scans, and text can
change while the defect remains the same.

**Decision.** Compute a 16-character SHA-256 prefix over location, CWE, and root cause;
embed it in issue bodies; match it deterministically first. Optionally run a bounded
LLM comparison for unmatched findings against open issues.

**Consequences.** Most repeat scans deduplicate without inference. Semantic comparison
catches equivalent prose but adds cost and probabilistic behavior. Chunking, body
truncation, issue-count caps, nonces, and strict issue-number checking constrain that
behavior.

**Evidence.** [`agent/issues_extract.py`](../../vulnhunter-agent/agent/issues_extract.py),
[`agent/issues_dedup.py`](../../vulnhunter-agent/agent/issues_dedup.py), and
[`agent/issues_render.py`](../../vulnhunter-agent/agent/issues_render.py).

## IADR-008: Prefer AST graph evidence with explicit fallback

**Context.** Completeness claims require knowing which callers can reach a security
sink, but the optional graph backend may be unavailable or incompatible with a
sandboxed environment.

**Decision.** Normalize `graphifyy` output into a stable internal graph schema, cache it
by content hash, and use only its AST extraction path. On isolation, import, detection,
or extraction failure, build a grep graph marked `backend=grep` and `confidence=low`.

**Consequences.** Downstream code is insulated from upstream graph format changes and
can lower its completeness claim when evidence degrades. Catch-all fallback preserves
workflow availability but can hide backend regressions unless warning telemetry is
monitored.

**Evidence.** [`vulnhunter_fix/graph/build.py`](../../vulnhunter-fix/vulnhunter_fix/graph/build.py),
[`vulnhunter_fix/graph/schema.py`](../../vulnhunter-fix/vulnhunter_fix/graph/schema.py),
and [`references/triage-schema.json`](../../vulnhunter-fix/references/triage-schema.json).

## IADR-009: Require TDD evidence and deterministic delivery gates

**Context.** A plausible code patch is not sufficient evidence that a vulnerability is
fixed, does not regress behavior, or covers every reachable instance.

**Decision.** Do not edit source before RED evidence exists. Require a security test to
fail before the fix and pass after it, run regressions, record discrimination evidence,
sweep the root cause, classify completeness/residual risk, and run seven mechanical
gates before PR creation.

**Consequences.** Delivery is slower and sometimes requires operator collaboration, but
PRs carry an auditable evidence chain. Repositories without testability or clear fix
boundaries fall into draft/manual-review paths instead of receiving an overstated
`FULL` claim.

**Evidence.** [`vulnhunter-fix/SKILL.md`](../../vulnhunter-fix/SKILL.md),
[`prompts/implement.md`](../../vulnhunter-fix/prompts/implement.md),
[`scripts/run-gates.py`](../../vulnhunter-fix/scripts/run-gates.py), and
[`references/result-schema.json`](../../vulnhunter-fix/references/result-schema.json).

## IADR-010: Reconstruct and isolate issue content before verification

**Context.** GitHub issue bodies and developer comments may be edited, may contain
prompt injection, and may point at unauthorized repositories.

**Decision.** Fetch edit history, reconstruct the original issue body, take markers
only from that body, delimit developer prose as untrusted, neutralize marker collisions,
and resolve additional repositories only through allowed hosts or exact config aliases.

**Consequences.** A developer cannot redirect verification to an arbitrary report or
clone target merely by editing an issue. The design depends on GitHub edit-history
availability and on the missing verification skill honoring the documented untrusted
content convention.

**Evidence.** [`agent/verify.py`](../../vulnhunter-agent/agent/verify.py),
[`agent/verify_extract.py`](../../vulnhunter-agent/agent/verify_extract.py), and
[`agent/verify_resolve.py`](../../vulnhunter-agent/agent/verify_resolve.py).

## IADR-011: Retry only at safe restart boundaries

**Context.** Transient inference failures are common, but restarting a 30-minute scan
after meaningful work discards expensive state.

**Decision.** Retry scan sessions only when transient failures occur before any
assistant output. Preserve mid-stream sessions and let an outer worker decide whether
to repeat the process. For short JSON-only calls, retry the same model once and then
fall back to a secondary model. For verify, rely on a fresh outer-process retry.

**Consequences.** Cold starts recover automatically without duplicating work. Operators
must configure wrapper-level retry/idempotency for mid-stream and verify failures.

**Evidence.** [`agent/runner.py`](../../vulnhunter-agent/agent/runner.py),
[`agent/_llm.py`](../../vulnhunter-agent/agent/_llm.py), and
[`agent/verify_runner.py`](../../vulnhunter-agent/agent/verify_runner.py).

## IADR-012: Customize by composition, not source fork

**Context.** Organizations need different credentials, hosts, CAs, telemetry tags,
queueing, and result routing, but changes to core security methodology should remain
upgradeable.

**Decision.** Make environment-specific values configurable by TOML/environment and
recommend a derived container plus a thin parent wrapper. Treat the scan manifest and
exit code as the outer integration contract.

**Consequences.** Deployments can track upstream code with smaller merge burdens. The
base image must ship all required skills and schemas together, and contract-version
governance becomes more important than internal Python API stability.

**Evidence.** [`vulnhunter-agent/README.md`](../../vulnhunter-agent/README.md),
[`agent/config.py`](../../vulnhunter-agent/agent/config.py), and
[`scan_manifest.schema.json`](../../vulnhunter-agent/scan_manifest.schema.json).

