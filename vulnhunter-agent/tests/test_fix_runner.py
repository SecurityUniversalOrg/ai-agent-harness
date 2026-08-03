"""Contract tests for the automated fix SDK runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import fix_runner
from agent.fix_runner import FixOutputKind, build_kickoff_prompt, classify_output


def _payload(report: Path) -> dict:
    return {
        "schema_version": "1.0",
        "run_status": "NO_FINDINGS",
        "target_repo": "https://github.com/org/repo",
        "report_path": str(report),
        "delivery_enabled": True,
        "fork_repository": None,
        "tracking_issue_url": None,
        "phase_checkpoints": [
            {"phase": "parse", "status": "validated", "artifacts": ["findings.json"], "detail": "none"}
        ],
        "findings": [],
        "summary": "No confirmed findings.",
    }


def test_kickoff_prompt_preserves_all_rigor_and_disables_mutations() -> None:
    prompt = build_kickoff_prompt(
        target_repo="https://github.com/org/repo",
        report=Path("/run/report"),
        runtime_config=Path("/run/config.json"),
        out_dir=Path("/run/out"),
        schema_path=Path("/run/schema.json"),
        delivery_enabled=False,
        test_policy="must-pass",
        target_checkout=Path("/run/work/repo"),
    )
    assert prompt.startswith(
        f"/vulnhunter-fix https://github.com/org/repo {Path('/run/report')}"
    )
    assert "Do not skip RED evidence" in prompt
    assert "Do not call AskUserQuestion" in prompt
    assert "GitHub credentials and default" in prompt
    assert str(Path("/run/work/repo")) in prompt
    assert "do not clone/fetch" in prompt
    assert "test policy: must-pass" in prompt
    assert str(Path("/run/out") / "fix_disposition.json") in prompt


def test_classify_output_accepts_schema_valid_document(tmp_path: Path) -> None:
    path = tmp_path / "fix_disposition.json"
    path.write_text(json.dumps(_payload(tmp_path / "report")), encoding="utf-8")
    result = classify_output(tmp_path)
    assert result.kind is FixOutputKind.DISPOSITION
    assert result.parsed is not None
    assert result.parsed["run_status"] == "NO_FINDINGS"


def test_classify_output_rejects_missing_or_invalid_document(tmp_path: Path) -> None:
    assert classify_output(tmp_path).kind is FixOutputKind.EMPTY
    (tmp_path / "fix_disposition.json").write_text(
        json.dumps({"schema_version": "1.0"}), encoding="utf-8"
    )
    result = classify_output(tmp_path)
    assert result.kind is FixOutputKind.SCHEMA_INVALID
    assert "validation" in result.error_detail


def test_classify_output_enforces_uri_format(tmp_path: Path) -> None:
    payload = _payload(tmp_path / "report")
    payload["tracking_issue_url"] = "not a URL"
    (tmp_path / "fix_disposition.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    result = classify_output(tmp_path)
    assert result.kind is FixOutputKind.SCHEMA_INVALID
    assert "tracking_issue_url" in result.error_detail


def test_classify_output_rejects_oversized_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fix_runner, "_MAX_DISPOSITION_BYTES", 2)
    (tmp_path / "fix_disposition.json").write_text("{}\n", encoding="utf-8")
    result = classify_output(tmp_path)
    assert result.kind is FixOutputKind.SCHEMA_INVALID
    assert "limit is 2" in result.error_detail


def test_fix_environment_uses_process_local_git_credential_helper(
    tmp_path: Path, populated_agent_config
) -> None:
    runtime = tmp_path / "runtime.json"
    env = fix_runner._fix_environment(
        config=populated_agent_config,
        github_token="secret-token",
        cwd=tmp_path,
        runtime_config=runtime,
        delivery_enabled=True,
    )
    assert env["GH_TOKEN"] == "secret-token"
    assert env["VULNFIX_AUTOMATED"] == "1"
    assert env["VULNFIX_CONFIG_PATH"] == str(runtime)
    assert "secret-token" not in env["GIT_CONFIG_VALUE_0"]
    assert "${GH_TOKEN}" in env["GIT_CONFIG_VALUE_0"]
    assert env["TMPDIR"].startswith(str(tmp_path))


def test_fix_environment_removes_github_credentials_for_dry_run(
    tmp_path: Path, populated_agent_config
) -> None:
    env = fix_runner._fix_environment(
        config=populated_agent_config,
        github_token="must-not-be-exposed",
        cwd=tmp_path,
        runtime_config=tmp_path / "runtime.json",
        delivery_enabled=False,
    )
    assert env["GH_TOKEN"] == ""
    assert env["GH_ENTERPRISE_TOKEN"] == ""
    assert env["GIT_CONFIG_KEY_0"] == "credential.helper"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert "must-not-be-exposed" not in json.dumps(env)


@pytest.mark.asyncio
async def test_run_fix_session_uses_locked_tools_and_scoped_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    populated_agent_config,
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    runtime = tmp_path / "runtime.json"
    runtime.write_text("{}", encoding="utf-8")
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# skill", encoding="utf-8")
    captured: dict = {}

    def fake_settings(*args, **kwargs):
        captured["settings_kwargs"] = kwargs
        return "{}"

    class FakeClient:
        def __init__(self, options):
            captured["options"] = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def query(self, prompt):
            captured["prompt"] = prompt
            (out_dir / "fix_disposition.json").write_text(
                json.dumps(_payload(tmp_path / "report")), encoding="utf-8"
            )

        async def receive_response(self):
            if False:
                yield None

    monkeypatch.setattr(fix_runner, "build_claude_settings", fake_settings)
    monkeypatch.setattr(fix_runner, "ClaudeSDKClient", FakeClient)

    result = await fix_runner.run_fix_session(
        config=populated_agent_config,
        auth_token="llm-token",
        github_token="github-token",
        cwd=tmp_path,
        out_dir=out_dir,
        prompt="/vulnhunter-fix ...",
        log_path=tmp_path / "agent.log",
        runtime_config=runtime,
        skill_dir=skill,
    )

    assert result.kind is FixOutputKind.DISPOSITION
    options = captured["options"]
    assert options.tools == options.allowed_tools
    assert "Bash" in options.tools
    assert "TaskCreate" in options.tools
    kwargs = captured["settings_kwargs"]
    assert kwargs["extra_env"]["GH_TOKEN"] == "github-token"
    assert kwargs["extra_env"]["VULNFIX_AUTOMATED"] == "1"
    assert "github.com" in kwargs["sandbox_allowed_domains"]
    assert str(skill) in kwargs["sandbox_allow_read_paths"]
    assert kwargs["strict_sandbox"] is True


@pytest.mark.asyncio
async def test_run_fix_session_omits_default_github_egress_in_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    populated_agent_config,
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    captured: dict = {}

    def fake_settings(*args, **kwargs):
        captured.update(kwargs)
        return "{}"

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def query(self, prompt):
            (out_dir / "fix_disposition.json").write_text(
                json.dumps(_payload(tmp_path / "report")), encoding="utf-8"
            )

        async def receive_response(self):
            if False:
                yield None

    monkeypatch.setattr(fix_runner, "build_claude_settings", fake_settings)
    monkeypatch.setattr(fix_runner, "ClaudeSDKClient", FakeClient)
    await fix_runner.run_fix_session(
        config=populated_agent_config,
        auth_token="llm-token",
        github_token="github-token",
        cwd=tmp_path,
        out_dir=out_dir,
        prompt="/vulnhunter-fix ...",
        log_path=tmp_path / "agent.log",
        runtime_config=tmp_path / "runtime.json",
        skill_dir=tmp_path,
        delivery_enabled=False,
    )
    assert "github.com" not in captured["sandbox_allowed_domains"]
    assert captured["extra_env"]["GH_TOKEN"] == ""
