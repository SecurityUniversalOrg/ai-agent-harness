"""Unit tests for the unattended fix-mode orchestration boundary."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import fix
from agent.config import FixConfig
from agent.fix_runner import FixOutputKind, FixSessionResult


def _checkpoints() -> list[dict]:
    return [
        {"phase": phase, "status": "validated", "artifacts": [f"{phase}.json"], "detail": "ok"}
        for phase in ("parse", "plan", "implement", "verify", "sweep", "deliver")
    ]


def _disposition(report: Path, *, delivery_enabled: bool = False) -> dict:
    return {
        "schema_version": "1.0",
        "run_status": "COMPLETED" if delivery_enabled else "DRY_RUN",
        "target_repo": "https://github.com/org/repo",
        "report_path": str(report),
        "delivery_enabled": delivery_enabled,
        "fork_repository": "org/vulnhunter-fix-repo" if delivery_enabled else None,
        "tracking_issue_url": None,
        "phase_checkpoints": _checkpoints(),
        "findings": [
            {
                "vuln_id": "VULN-001",
                "status": "VERIFIED_FULL",
                "detail": "RED then GREEN; all gates passed",
                "cwe": "CWE-89",
                "completeness_tier": "FULL",
                "branch": "fix/code-quality-input-validation-deadbeef",
                "commit_sha": "abc1234",
                "issue_url": None,
                "pr_url": None,
                "gate_status": "PASS",
                "residual_vectors": [],
            }
        ],
        "summary": "One finding remediated.",
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://github.com/org/repo", "https://github.com/org/repo"),
        ("https://github.com/org/repo.git", "https://github.com/org/repo"),
    ],
)
def test_normalize_target_repo(value: str, expected: str) -> None:
    assert fix.normalize_target_repo(value, "github.com") == expected


@pytest.mark.parametrize(
    "value",
    [
        "git@github.com:org/repo.git",
        "https://token@github.com/org/repo",
        "https://evil.example/org/repo",
        "https://github.com:8443/org/repo",
        "https://github.com/org/repo/tree/main",
        "https://github.com/org/repo?ref=main",
    ],
)
def test_normalize_target_repo_rejects_unsafe_shapes(value: str) -> None:
    with pytest.raises(fix.FixInputError):
        fix.normalize_target_repo(value, "github.com")


@pytest.mark.parametrize("host", ["", "github.com/path", "github.com;evil", "github..com", "github.com:99999"])
def test_normalize_target_repo_rejects_unsafe_configured_host(host: str) -> None:
    with pytest.raises(fix.FixInputError):
        fix.normalize_target_repo("https://github.com/org/repo", host)


def test_stage_local_results_copies_bounded_report(
    tmp_path: Path, populated_agent_config
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("# report", encoding="utf-8")
    (source / "poc").mkdir()
    (source / "poc" / "VULN-001.md").write_text("proof", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    staged = fix.stage_results(str(source), run_dir=run_dir, config=populated_agent_config)

    assert staged == (run_dir / "report").resolve()
    assert (staged / "README.md").read_text(encoding="utf-8") == "# report"
    assert (staged / "poc" / "VULN-001.md").is_file()


def test_stage_results_enforces_size_limit(tmp_path: Path, populated_agent_config) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("too large", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = dataclasses.replace(
        populated_agent_config,
        fix=dataclasses.replace(populated_agent_config.fix, max_report_bytes=2),
    )

    with pytest.raises(fix.FixInputError, match="limit is 2"):
        fix.stage_results(str(source), run_dir=run_dir, config=config)


def test_select_report_root_refuses_ambiguous_repo(tmp_path: Path) -> None:
    for name in ("A_VULNHUNT_RESULTS_opus_1", "B_VULNHUNT_RESULTS_opus_2"):
        report = tmp_path / name
        report.mkdir()
        (report / "README.md").write_text("# report", encoding="utf-8")
    with pytest.raises(fix.FixInputError, match="ambiguous"):
        fix._select_report_root(tmp_path)


def test_locate_skill_rejects_stale_install(tmp_path: Path, populated_agent_config) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# old skill", encoding="utf-8")
    config = dataclasses.replace(
        populated_agent_config,
        fix=dataclasses.replace(populated_agent_config.fix, skill_dir=str(skill)),
    )
    with pytest.raises(FileNotFoundError, match="predates automated fix mode"):
        fix._locate_skill(config)


def test_runtime_config_is_private_fork_only(populated_agent_config) -> None:
    config = dataclasses.replace(
        populated_agent_config,
        fix=FixConfig(
            fork_org="fixes",
            collaborators=("alice:write", "bob:read"),
        ),
    )
    payload = fix._runtime_config(
        config,
        delivery_enabled=True,
        test_policy="must-pass",
    )
    assert payload["execution"] == {
        "automated": True,
        "delivery_enabled": True,
        "test_policy": "must-pass",
    }
    assert payload["behavior"]["fork_visibility"] == "private"
    assert payload["behavior"]["deliver_to_fork_only"] is True
    assert payload["behavior"]["confirm_before_push"] is False
    assert payload["collaborators"] == [
        {"username": "alice", "role": "write"},
        {"username": "bob", "role": "read"},
    ]


def test_validate_semantics_rejects_success_without_all_checkpoints(tmp_path: Path) -> None:
    report = tmp_path / "report"
    report.mkdir()
    payload = _disposition(report)
    payload["phase_checkpoints"] = payload["phase_checkpoints"][:-1]
    error = fix._validate_semantics(
        payload,
        target_repo="https://github.com/org/repo",
        report=report,
        delivery_enabled=False,
    )
    assert "deliver" in error


def test_validate_semantics_rejects_cross_host_delivery_url(tmp_path: Path) -> None:
    report = tmp_path / "report"
    report.mkdir()
    payload = _disposition(report, delivery_enabled=True)
    payload["findings"][0]["pr_url"] = "https://evil.example/org/repo/pull/1"
    error = fix._validate_semantics(
        payload,
        target_repo="https://github.com/org/repo",
        report=report,
        delivery_enabled=True,
    )
    assert "target GitHub host" in error


@pytest.mark.asyncio
async def test_run_fix_dry_run_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    populated_agent_config,
) -> None:
    report_source = tmp_path / "input-report"
    report_source.mkdir()
    (report_source / "README.md").write_text("# report", encoding="utf-8")
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "## Automated agent execution profile\nVULNFIX_CONFIG_PATH\n",
        encoding="utf-8",
    )
    config = dataclasses.replace(
        populated_agent_config,
        github=dataclasses.replace(
            populated_agent_config.github,
            scan_token="scan-token",
        ),
        fix=dataclasses.replace(
            populated_agent_config.fix,
            scratch_base_dir=str(tmp_path / "runs"),
            skill_dir=str(skill),
        ),
    )

    async def fake_session(**kwargs):
        report = kwargs["cwd"] / "report"
        output_path = kwargs["out_dir"] / "fix_disposition.json"
        payload = _disposition(report)
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return FixSessionResult(
            kind=FixOutputKind.DISPOSITION,
            output_path=output_path,
            parsed=payload,
        )

    monkeypatch.setattr(fix, "run_fix_session", fake_session)
    staged_checkout = tmp_path / "runs" / "staged-target"

    def fake_clone(*args, **kwargs):
        staged_checkout.mkdir(parents=True)
        return staged_checkout

    monkeypatch.setattr(fix, "shallow_clone", fake_clone)
    monkeypatch.setattr(
        fix,
        "make_token_manager",
        lambda *args, **kwargs: SimpleNamespace(get_valid_token=lambda: "llm-token"),
    )

    rc = await fix.run_fix(
        config=config,
        target_repo="https://github.com/org/repo",
        results_input=str(report_source),
        scratch_base_dir=None,
        no_post=True,
        test_policy_override=None,
        model_override=None,
    )
    assert rc == 0
    assert list((tmp_path / "runs").glob("repo-*"))
