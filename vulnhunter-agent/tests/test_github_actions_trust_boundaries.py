"""Static trust-boundary contracts for remediation GitHub Actions resources."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_remediation_workflows_reject_cross_repository_local_action_calls() -> None:
    for workflow in (
        ".github/workflows/vulnhunter-agent-fix.yaml",
        ".github/workflows/vulnhunter-agent-verify.yaml",
    ):
        text = _read(workflow)
        assert "Enforce trusted same-repository workflow invocation" in text
        assert "startsWith(github.workflow_ref" in text
        assert "ref: ${{ github.workflow_sha }}" in text
        assert "persist-credentials: false" in text


def test_fix_github_origin_is_derived_then_passed_to_agent() -> None:
    prepare = _read(".github/actions/prepare-vulnhunter-fix/action.yml")
    run = _read(".github/actions/run-vulnhunter-fix/action.yml")
    workflow = _read(".github/workflows/vulnhunter-agent-fix.yaml")

    assert 'github_host="${server#https://}"' in prepare
    assert 'echo "github-host=${github_host}"' in prepare
    assert "VULNHUNT_GITHUB_HOST: ${{ inputs.github-host }}" in run
    assert "github-host: ${{ steps.prepare.outputs.github-host }}" in workflow


def test_verify_github_origin_preserves_and_enforces_custom_port() -> None:
    prepare = _read(".github/actions/prepare-vulnhunter-verify/action.yml")
    run = _read(".github/actions/run-vulnhunter-verify/action.yml")
    workflow = _read(".github/workflows/vulnhunter-agent-verify.yaml")

    assert "github_host = origin.netloc.lower()" in prepare
    assert "parsed.port != origin.port" in prepare
    assert 'output.write(f"github-host={github_host}\\n")' in prepare
    assert "VULNHUNT_GITHUB_HOST: ${{ inputs.github-host }}" in run
    assert "github-host: ${{ steps.prepare.outputs.github-host }}" in workflow
