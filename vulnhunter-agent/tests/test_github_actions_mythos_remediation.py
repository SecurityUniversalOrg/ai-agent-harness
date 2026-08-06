"""Static contracts for Mythos gVisor support in the fix/verify GitHub Actions
wiring and its supporting sandbox launcher scripts.

Companion to ``test_github_actions_remediation_contract.py`` (the
model-agnostic contracts) and ``test_github_actions_trust_boundaries.py``
(origin propagation). This file is specifically about the properties that
must hold only when ``claude-mythos-5`` is selected: no GitHub credential
may reach the model container, the container must run under gVisor with a
locked-down profile, and delivery/mutation must be impossible.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACTIONS = ROOT / ".github" / "actions"
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPTS = ROOT / "scripts"


def _action(name: str) -> str:
    return (ACTIONS / name / "action.yml").read_text(encoding="utf-8")


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _script(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


# ---- launcher scripts exist and are executable-shaped -----------------------


def test_mythos_fix_and_verify_sandbox_scripts_exist() -> None:
    for name in ("run_mythos_fix_sandbox.sh", "run_mythos_verify_sandbox.sh"):
        path = SCRIPTS / name
        assert path.is_file(), name
        text = path.read_text(encoding="utf-8")
        assert text.startswith("#!/usr/bin/env bash")
        assert "set -euo pipefail" in text


# ---- fix sandbox: trusted clone stays outside the container -----------------


def test_mythos_fix_sandbox_clones_before_container_and_uses_target_checkout() -> None:
    text = _script("run_mythos_fix_sandbox.sh")
    # The clone happens in this trusted process, before any docker run.
    clone_index = text.index("git clone")
    first_docker_run = text.index("docker run --detach")
    assert clone_index < first_docker_run
    # The container-side invocation uses --target-checkout, which needs no
    # GitHub token at all (agent.fix.run_fix skips get_github_token entirely).
    assert "--target-checkout /workspace/repo" in text
    assert "--no-post" in text
    assert "--mode=fix" in text


def test_mythos_fix_sandbox_never_leaks_a_github_credential_into_the_container() -> None:
    text = _script("run_mythos_fix_sandbox.sh")
    assert (
        "grep -Eq '^(GH_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_TOKEN|"
        "VULNHUNT_GITHUB_SCAN_TOKEN|VULNHUNT_GITHUB_REPORTS_TOKEN|"
        "VULNHUNT_GITHUB_FIX_TOKEN|VULNHUNT_GITHUB_VERIFY_TOKEN)$'"
    ) in text
    # The env-file written into the container never assigns a GitHub token.
    env_file_start = text.index('cat >"${env_file}"')
    env_file_end = text.index("EOF", env_file_start)
    env_file_body = text[env_file_start:env_file_end]
    for forbidden in ("GH_TOKEN=", "GITHUB_TOKEN=", "VULNHUNT_GITHUB_"):
        assert forbidden not in env_file_body


def test_mythos_fix_sandbox_forbids_allowed_domains_and_pins_gvisor() -> None:
    text = _script("run_mythos_fix_sandbox.sh")
    assert "VULNHUNT_FIX_ALLOWED_DOMAINS" not in text
    assert 'runtime="${MYTHOS_DOCKER_RUNTIME:-runsc}"' in text
    assert '"${runtime}" == "runsc"' in text
    assert "--cap-drop ALL" in text
    assert "--read-only" in text
    assert "--ipc none" in text
    assert "VULNHUNT_MYTHOS_HARDENED_RUNTIME=1" in text


# ---- verify sandbox: only the model turn moves into the container -----------


def test_mythos_verify_sandbox_takes_pre_staged_inputs_only() -> None:
    text = _script("run_mythos_verify_sandbox.sh")
    for required in (
        "MYTHOS_TARGET_REPO_CHECKOUT",
        "MYTHOS_REPORT_DIR",
        "MYTHOS_PROMPT_FILE",
        "MYTHOS_OUT_DIR",
    ):
        assert required in text
    # No GitHub fetch/clone of its own — it only streams already-cloned,
    # already-fetched host paths into the container.
    assert "git clone" not in text
    assert "gh api" not in text
    assert "_mythos_verify_entry" in text


def test_mythos_verify_sandbox_never_leaks_a_github_credential_into_the_container() -> None:
    text = _script("run_mythos_verify_sandbox.sh")
    assert (
        "grep -Eq '^(GH_TOKEN|GH_ENTERPRISE_TOKEN|GITHUB_TOKEN|"
        "VULNHUNT_GITHUB_SCAN_TOKEN|VULNHUNT_GITHUB_REPORTS_TOKEN|"
        "VULNHUNT_GITHUB_FIX_TOKEN|VULNHUNT_GITHUB_VERIFY_TOKEN)$'"
    ) in text
    env_file_start = text.index('cat >"${env_file}"')
    env_file_end = text.index("EOF", env_file_start)
    env_file_body = text[env_file_start:env_file_end]
    for forbidden in ("GH_TOKEN=", "GITHUB_TOKEN=", "VULNHUNT_GITHUB_"):
        assert forbidden not in env_file_body


def test_mythos_verify_sandbox_pins_gvisor_and_drops_privileges() -> None:
    text = _script("run_mythos_verify_sandbox.sh")
    assert 'runtime="${MYTHOS_DOCKER_RUNTIME:-runsc}"' in text
    assert '"${runtime}" == "runsc"' in text
    assert "--cap-drop ALL" in text
    assert "--read-only" in text
    assert "--ipc none" in text
    assert "VULNHUNT_MYTHOS_HARDENED_RUNTIME=1" in text


# ---- prepare actions: Mythos accepted with a retention gate -----------------


def test_prepare_fix_accepts_mythos_with_retention_and_dry_run_gate() -> None:
    text = _action("prepare-vulnhunter-fix")
    assert "claude-opus-4-7|claude-opus-4-8|claude-mythos-5" in text
    assert "mythos-retention-acknowledged" in text
    assert "MYTHOS_RETENTION_ACKNOWLEDGED" in text
    assert 'MODEL}" == "claude-mythos-5"' in text
    assert 'DELIVERY_ENABLED}" == "false"' in text


def test_prepare_verify_accepts_mythos_with_retention_and_no_mutation_gate() -> None:
    text = _action("prepare-vulnhunter-verify")
    assert "claude-opus-4-7|claude-opus-4-8|claude-mythos-5" in text
    assert "mythos-retention-acknowledged" in text
    assert "MYTHOS_RETENTION_ACKNOWLEDGED" in text
    assert 'MODEL}" == "claude-mythos-5"' in text
    assert 'POST_COMMENTS}" == "false"' in text


# ---- run-vulnhunter-fix: model-conditional Mythos branch --------------------


def test_run_vulnhunter_fix_has_model_conditional_mythos_branch() -> None:
    text = _action("run-vulnhunter-fix")
    assert "if: inputs.model != 'claude-mythos-5'" in text
    assert "if: inputs.model == 'claude-mythos-5'" in text
    assert "run_mythos_fix_sandbox.sh" in text
    # Both branches feed the same three composite outputs.
    assert "steps.run-opus.outputs.agent-exit-code || steps.run-mythos.outputs.agent-exit-code" in text
    assert "steps.run-opus.outputs.run-directory || steps.run-mythos.outputs.run-directory" in text
    assert "steps.run-opus.outputs.disposition-path || steps.run-mythos.outputs.disposition-path" in text


# ---- workflows: gvisor runner, isolation proof, mythos input ----------------


def test_both_workflows_route_mythos_to_a_gvisor_runner() -> None:
    for name in ("vulnhunter-agent-fix.yaml", "vulnhunter-agent-verify.yaml"):
        text = _workflow(name)
        assert "inputs.model == 'claude-mythos-5'" in text
        assert "'gvisor'" in text
        assert "runs-on:" in text


def test_both_workflows_run_the_isolation_proof_before_the_mutable_stages() -> None:
    for name in ("vulnhunter-agent-fix.yaml", "vulnhunter-agent-verify.yaml"):
        text = _workflow(name)
        proof_index = text.index("mythos-proof")
        prepare_index = text.index("id: prepare")
        run_index = text.index("id: fix" if "fix" in name else "id: verify")
        assert prepare_index < proof_index < run_index
        assert "validate_mythos_isolation.sh" in text
        assert "ISOLATION_PROOF" in text
        assert 'MYTHOS_PROOF_OUTCOME}" == "success" || "${MYTHOS_PROOF_OUTCOME}" == "skipped"' in text


def test_both_workflows_expose_mythos_retention_input_on_both_triggers() -> None:
    for name in ("vulnhunter-agent-fix.yaml", "vulnhunter-agent-verify.yaml"):
        text = _workflow(name)
        assert text.count("mythos_retention_acknowledged:") == 2  # workflow_dispatch + workflow_call
