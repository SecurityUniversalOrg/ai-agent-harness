"""Static contracts for dual GitHub authentication (PAT or GitHub App
installation) support across the scan, fix, and verify workflows.

Every role-specific token (scan/fix/verify/reports) can come from either a
long-lived PAT secret (the existing, default behavior) or a short-lived,
auto-revoked GitHub App installation token minted at run time and scoped to
exactly the repository that role touches. This file locks down the wiring:
the choice input exists on every trigger, the App secrets are optional (not
required, since PAT remains the default), the validation step rejects a
run that supplies neither, and every token consumption site falls back
correctly between the two sources.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
ACTIONS = ROOT / ".github" / "actions"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _action(name: str) -> str:
    return (ACTIONS / name / "action.yml").read_text(encoding="utf-8")


FIX = "vulnhunter-agent-fix.yaml"
VERIFY = "vulnhunter-agent-verify.yaml"
SCAN = "org-ai-security-discovery.yaml"
SEC_SCAN = "claude-agent-sec-scan"


# ---- fix and verify (workflow_call reusable workflows) ----------------------


def test_fix_and_verify_expose_github_auth_method_on_both_triggers() -> None:
    for name in (FIX, VERIFY):
        text = _workflow(name)
        assert text.count("github_auth_method:") == 2  # workflow_dispatch + workflow_call
        assert "default: pat" in text
        assert "- pat" in text
        assert "- github_app" in text


def test_fix_and_verify_declare_app_secrets_as_optional() -> None:
    for name in (FIX, VERIFY):
        text = _workflow(name)
        assert "VULNHUNT_GITHUB_APP_ID:" in text
        assert "VULNHUNT_GITHUB_APP_PRIVATE_KEY:" in text
        # The PAT secrets must no longer be unconditionally required — exactly
        # one credential source is required, enforced at runtime instead (see
        # test_fix_and_verify_validate_auth_inputs_before_minting below).
        secrets_start = text.index("secrets:")
        secrets_block = text[secrets_start:]
        for line_start in ("VULNHUNT_GITHUB_APP_ID:", "VULNHUNT_GITHUB_APP_PRIVATE_KEY:"):
            idx = secrets_block.index(line_start)
            following = secrets_block[idx : idx + 120]
            assert "required: false" in following


def test_fix_and_verify_validate_auth_inputs_before_minting() -> None:
    for name in (FIX, VERIFY):
        text = _workflow(name)
        validate_index = text.index("Validate GitHub authentication inputs")
        first_mint_index = text.index("uses: actions/create-github-app-token@v2")
        assert validate_index < first_mint_index
        assert "github_auth_method=pat requires" in text
        assert "github_auth_method=github_app requires" in text
        assert "Unsupported github_auth_method" in text


def test_fix_and_verify_mint_role_scoped_app_tokens() -> None:
    fix_text = _workflow(FIX)
    assert fix_text.count("uses: actions/create-github-app-token@v2") == 2
    assert "id: mint-fix-token" in fix_text
    assert "id: mint-reports-token" in fix_text
    assert "owner: ${{ steps.app-scope.outputs.target-owner }}" in fix_text
    assert "repositories: ${{ steps.app-scope.outputs.target-repo }}" in fix_text
    assert "owner: ${{ steps.app-scope.outputs.reports-owner }}" in fix_text
    assert "repositories: ${{ steps.app-scope.outputs.reports-repo }}" in fix_text

    verify_text = _workflow(VERIFY)
    assert verify_text.count("uses: actions/create-github-app-token@v2") == 2
    assert "id: mint-verify-token" in verify_text
    assert "id: mint-reports-token" in verify_text
    assert "owner: ${{ steps.app-scope.outputs.verify-owner }}" in verify_text
    assert "repositories: ${{ steps.app-scope.outputs.verify-repo }}" in verify_text


def test_fix_token_consumers_fall_back_between_app_and_pat() -> None:
    text = _workflow(FIX)
    assert (
        "fix-token: ${{ inputs.github_auth_method == 'github_app' "
        "&& steps.mint-fix-token.outputs.token "
        "|| secrets.VULNHUNT_GITHUB_FIX_TOKEN }}" in text
    )
    assert (
        "reports-token: ${{ inputs.github_auth_method == 'github_app' "
        "&& steps.mint-reports-token.outputs.token "
        "|| secrets.VULNHUNT_GITHUB_REPORTS_TOKEN }}" in text
    )


def test_verify_token_consumers_fall_back_between_app_and_pat() -> None:
    text = _workflow(VERIFY)
    assert text.count(
        "verify-token: ${{ inputs.github_auth_method == 'github_app' "
        "&& steps.mint-verify-token.outputs.token "
        "|| secrets.VULNHUNT_GITHUB_VERIFY_TOKEN }}"
    ) == 2  # prepare step and run step both consume it
    assert (
        "reports-token: ${{ inputs.github_auth_method == 'github_app' "
        "&& steps.mint-reports-token.outputs.token "
        "|| secrets.VULNHUNT_GITHUB_REPORTS_TOKEN }}" in text
    )


def test_app_scope_resolution_splits_owner_and_repo_in_bash_not_expressions() -> None:
    """GitHub Actions expressions have no split() function — owner/repo must
    be parsed in a shell step, not attempted inline in a ${{ }} expression."""
    for name in (FIX, VERIFY):
        text = _workflow(name)
        assert "id: app-scope" in text
        assert "%%/*" in text and "#*/" in text


# ---- scan (top-level workflow_dispatch + schedule, matrix job) --------------


def test_scan_workflow_exposes_github_auth_method_input() -> None:
    text = _workflow(SCAN)
    assert "github_auth_method:" in text
    assert "default: pat" in text
    assert "- pat" in text
    assert "- github_app" in text


def test_scan_workflow_resolves_auth_method_with_schedule_fallback() -> None:
    """Matches the existing publish_results/submit_repo_issues shape: read the
    dispatch input directly, or fall back to a protected repository variable
    for scheduled runs (never inputs.* directly, which is empty on schedule)."""
    text = _workflow(SCAN)
    assert "RESOLVED_AUTH_METHOD:" in text
    assert "github.event_name == 'workflow_dispatch' && inputs.github_auth_method" in text
    assert "vars.VULNHUNT_GITHUB_AUTH_METHOD || 'pat'" in text


def test_scan_workflow_mints_scan_and_reports_scoped_tokens_per_matrix_row() -> None:
    text = _workflow(SCAN)
    assert text.count("uses: actions/create-github-app-token@v2") == 2
    assert "id: mint-scan-token" in text
    assert "id: mint-reports-token" in text
    # Scan scope comes straight from the matrix row — no bash parsing needed.
    assert "owner: ${{ matrix.owner }}" in text
    assert "repositories: ${{ matrix.repo }}" in text
    # Reports scope is parsed out of a full URL (scheme://host/owner/repo).
    assert 'without_scheme="${REPORTS_REPOSITORY_URL#*://}"' in text


def test_scan_token_consumers_fall_back_between_app_and_pat() -> None:
    text = _workflow(SCAN)
    assert text.count(
        "scan-token: ${{ env.RESOLVED_AUTH_METHOD == 'github_app' "
        "&& steps.mint-scan-token.outputs.token "
        "|| secrets.VULNHUNT_GITHUB_SCAN_TOKEN }}"
    ) == 2  # Opus branch and Mythos branch both consume it
    assert text.count(
        "reports-token: ${{ env.RESOLVED_AUTH_METHOD == 'github_app' "
        "&& steps.mint-reports-token.outputs.token "
        "|| secrets.VULNHUNT_GITHUB_REPORTS_TOKEN }}"
    ) == 2


def test_scan_workflow_validates_auth_inputs_before_minting() -> None:
    text = _workflow(SCAN)
    validate_index = text.index("Validate GitHub authentication inputs")
    first_mint_index = text.index("uses: actions/create-github-app-token@v2")
    assert validate_index < first_mint_index
    assert "github_auth_method=pat requires" in text
    assert "github_auth_method=github_app requires" in text


# ---- Mythos delivery token refresh (claude-agent-sec-scan) -----------------
#
# GitHub App installation tokens are hard-capped at 1 hour by GitHub's API —
# there is no way to mint a longer-lived one. The Mythos gVisor scan step can
# run for hours, so the scan-token/reports-token minted once at job start (in
# org-ai-security-discovery.yaml, before this composite action even starts)
# can expire long before "Deliver Mythos results" runs. These tests lock down
# that the composite action re-mints fresh, identically-scoped tokens
# immediately before delivery when (and only when) github-auth-method is
# github_app — 'pat' tokens don't expire on a fixed short window and must be
# left untouched.


def test_sec_scan_accepts_github_app_inputs() -> None:
    text = _action(SEC_SCAN)
    assert "github-auth-method:" in text
    assert "default: pat" in text
    assert "app-id:" in text
    assert "app-private-key:" in text


def test_sec_scan_validates_github_app_inputs() -> None:
    text = _action(SEC_SCAN)
    assert "github-auth-method=github_app requires the app-id and app-private-key inputs" in text
    assert "Unsupported github-auth-method" in text


def test_sec_scan_refreshes_tokens_only_for_mythos_github_app_delivery() -> None:
    text = _action(SEC_SCAN)
    assert text.count("uses: actions/create-github-app-token@v2") == 2
    assert "id: refresh-scan-token" in text
    assert "id: refresh-reports-token" in text

    refresh_scan_if = (
        "inputs.model == 'claude-mythos-5' && inputs.github-auth-method == 'github_app' "
        "&& inputs.submit-repo-issues == 'true'"
    )
    refresh_reports_if = (
        "inputs.model == 'claude-mythos-5' && inputs.github-auth-method == 'github_app' "
        "&& inputs.publish-results == 'true'"
    )
    assert refresh_scan_if in text
    assert refresh_reports_if in text

    # Opus never reaches "Deliver Mythos results" at all (that step, and the
    # refresh steps, are gated on model == claude-mythos-5), so a 'pat' run
    # and an Opus run are both unaffected by this refresh logic entirely.


def test_sec_scan_refresh_steps_run_before_delivery_and_reuse_original_scope() -> None:
    text = _action(SEC_SCAN)
    scope_index = text.index("id: delivery-app-scope")
    refresh_scan_index = text.index("id: refresh-scan-token")
    refresh_reports_index = text.index("id: refresh-reports-token")
    deliver_index = text.index('name: Deliver Mythos results')
    assert scope_index < refresh_scan_index < deliver_index
    assert scope_index < refresh_reports_index < deliver_index
    # Scoped from the same repository-url / reports-repository-url inputs the
    # original (now possibly stale) tokens were scoped from — never wider.
    assert "owner: ${{ steps.delivery-app-scope.outputs.target-owner }}" in text
    assert "repositories: ${{ steps.delivery-app-scope.outputs.target-repo }}" in text
    assert "owner: ${{ steps.delivery-app-scope.outputs.reports-owner }}" in text
    assert "repositories: ${{ steps.delivery-app-scope.outputs.reports-repo }}" in text


def test_sec_scan_deliver_step_prefers_refreshed_token_over_stale_input() -> None:
    text = _action(SEC_SCAN)
    assert (
        "VULNHUNT_GITHUB_SCAN_TOKEN: ${{ (inputs.github-auth-method == 'github_app' "
        "&& steps.refresh-scan-token.outputs.token) || inputs.scan-token }}" in text
    )
    assert (
        "VULNHUNT_GITHUB_REPORTS_TOKEN: ${{ (inputs.github-auth-method == 'github_app' "
        "&& steps.refresh-reports-token.outputs.token) || inputs.reports-token }}" in text
    )


def test_scan_workflow_passes_app_credentials_to_mythos_branch_only() -> None:
    text = _workflow(SCAN)
    mythos_step = text[text.index("Run isolated Mythos security scan") :]
    assert "github-auth-method: ${{ env.RESOLVED_AUTH_METHOD }}" in mythos_step
    assert "app-id: ${{ secrets.VULNHUNT_GITHUB_APP_ID }}" in mythos_step
    assert "app-private-key: ${{ secrets.VULNHUNT_GITHUB_APP_PRIVATE_KEY }}" in mythos_step
