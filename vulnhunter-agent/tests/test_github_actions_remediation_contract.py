"""Static security contracts for fix/verify GitHub Actions wiring.

These tests intentionally use only the standard library. YAML syntax is validated by
actionlint in CI/operations; this suite locks down security-sensitive strings and
cross-file mappings even in the agent's minimal development environment.
"""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ACTIONS = ROOT / ".github" / "actions"
WORKFLOWS = ROOT / ".github" / "workflows"


ACTION_NAMES = (
    "setup-vulnhunter-agent",
    "prepare-vulnhunter-fix",
    "run-vulnhunter-fix",
    "attest-vulnhunter-fix",
    "prepare-vulnhunter-verify",
    "run-vulnhunter-verify",
    "attest-vulnhunter-verify",
    "package-vulnhunter-run",
)


def _action(name: str) -> str:
    return (ACTIONS / name / "action.yml").read_text(encoding="utf-8")


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_all_remediation_composites_exist() -> None:
    for name in ACTION_NAMES:
        path = ACTIONS / name / "action.yml"
        assert path.is_file(), name
        assert "using: composite" in path.read_text(encoding="utf-8")


def test_workflows_are_explicit_and_reusable_without_pr_triggers() -> None:
    for name in ("vulnhunter-agent-fix.yaml", "vulnhunter-agent-verify.yaml"):
        text = _workflow(name)
        assert "workflow_dispatch:" in text
        assert "workflow_call:" in text
        assert "pull_request:" not in text
        assert "pull_request_target:" not in text
        assert "workflow_run:" not in text
        assert "contents: read" in text
        assert "actions: read" in text
        assert "cancel-in-progress: false" in text
        assert "persist-credentials: false" in text
        assert "continue-on-error: true" in text
        assert "Enforce " in text


def test_fix_workflow_defaults_to_dry_run_and_protected_environment() -> None:
    text = _workflow("vulnhunter-agent-fix.yaml")
    assert "delivery_enabled:" in text
    assert text.count("default: false") >= 2  # dispatch and workflow_call
    assert "environment: vulnhunter-fix" in text
    assert "VULNHUNT_GITHUB_FIX_TOKEN" in text
    assert "VULNHUNT_GITHUB_REPORTS_TOKEN" in text
    assert "results-path: ${{ inputs.results_path }}" in text
    assert "claude-mythos-5" not in text


def test_fix_report_intake_is_exact_and_link_free() -> None:
    text = _action("prepare-vulnhunter-fix")
    assert "sparse-checkout: ${{ inputs.results-path }}" in text
    assert "persist-credentials: false" in text
    assert "README.md" in text
    assert "find \"${report}\"" in text
    assert "-type l" in text
    assert "Refusing" not in text or "symlink" in text
    assert "dedicated mode-aware gVisor launcher" in text


def test_fix_run_forces_strict_sandbox_and_uses_argv() -> None:
    text = _action("run-vulnhunter-fix")
    assert "VULNHUNT_SANDBOX_ENABLED: \"true\"" in text
    assert "VULNHUNT_SANDBOX_FAIL_IF_UNAVAILABLE: \"true\"" in text
    assert "VULNHUNT_SANDBOX_ALLOW_UNSANDBOXED_COMMANDS: \"false\"" in text
    assert "VULNHUNT_TELEMETRY_ENABLED: \"false\"" in text
    assert "post_args=(--no-post)" in text
    assert '"${post_args[@]}"' in text
    assert "eval " not in text
    assert "agent-exit-code=${agent_rc}" in text


def test_fix_attestation_revalidates_schema_semantics_and_exit_mapping() -> None:
    text = _action("attest-vulnhunter-fix")
    assert "classify_output" in text
    assert "_validate_semantics" in text
    assert "workflow_attestation.json" in text
    assert "expected_rc" in text
    assert "GH_TOKEN" not in text


def test_verify_intake_requires_json_host_scope_state_and_trusted_author() -> None:
    text = _action("prepare-vulnhunter-verify")
    assert 'json.loads(os.environ["ISSUE_URLS_JSON"])' in text
    assert "between 1 and 100 URLs" in text
    assert "All verify issue URLs must belong to one repository" in text
    assert "authored by a trusted scanner identity" in text
    assert "is not closed" in text
    assert "configured HTTPS GitHub host" in text
    assert "eval " not in text
    assert "dedicated staged-input gVisor launcher" in text


def test_verify_run_maps_mutation_controls_and_builds_argv_array() -> None:
    text = _action("run-vulnhunter-verify")
    assert "mapfile -t issue_urls" in text
    assert "mutation_args=(--no-post --no-reopen)" in text
    assert "mutation_args=(--no-reopen)" in text
    assert '"${issue_urls[@]}"' in text
    assert "eval " not in text
    assert "VULNHUNT_PUBLISH_DESTINATION_REPO" in text
    assert "agent-exit-code=${agent_rc}" in text


def test_verify_workflow_defaults_to_no_mutation_and_attests_count() -> None:
    text = _workflow("vulnhunter-agent-verify.yaml")
    assert text.count("default: false") >= 4
    assert "environment: vulnhunter-verify" in text
    assert "trusted-issue-authors: ${{ vars.VULNHUNT_TRUSTED_ISSUE_AUTHORS }}" in text
    assert "expected-count: ${{ steps.prepare.outputs.issue-count }}" in text
    assert "claude-mythos-5" not in text


def test_artifact_packaging_excludes_source_credentials_and_special_files() -> None:
    text = _action("package-vulnhunter-run")
    for excluded in ('.git', '.tmp', 'work', 'repo', 'report', 'node_modules', '.venv'):
        assert f'"{excluded}"' in text
    assert "S_ISLNK" in text
    assert "S_ISREG" in text
    assert "SHA256_MANIFEST.json" in text
    assert "GH_TOKEN" not in text
    assert "ANTHROPIC" not in text


def test_setup_uses_runtime_dependencies_and_never_writes_config_toml() -> None:
    text = _action("setup-vulnhunter-agent")
    assert "pip install --disable-pip-version-check -e" in text
    assert '.[dev]' not in text
    assert "config.toml" not in text
    assert "RUNNER_TEMP" in text
