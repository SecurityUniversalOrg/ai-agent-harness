from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent.build_settings import build_claude_settings, resolve_autocompact_pct
from agent.config import MythosConfig
from agent.model_policy import (
    MYTHOS_INFERENCE_HOST,
    MYTHOS_RUNTIME_MARKER,
    SUPPORTED_REMEDIATION_MODELS,
    enforce_mythos_mode_policy,
    is_mythos_model,
    is_supported_remediation_model,
    permission_mode_for_model,
    setting_sources_for_model,
)


def _mythos_config(base):
    return replace(
        base,
        anthropic=replace(
            base.anthropic,
            model="claude-mythos-5",
            auth_mode="anthropic_aws",
            aws_region="us-east-1",
        ),
        sandbox=replace(
            base.sandbox,
            enabled=True,
            fail_if_unavailable=True,
            allow_unsandboxed_commands=False,
        ),
        telemetry=replace(base.telemetry, enabled=False),
        mythos=MythosConfig(
            data_retention_acknowledged=True,
            https_proxy="http://mythos-egress:3128",
        ),
    )


def test_supported_remediation_models_are_exact() -> None:
    assert SUPPORTED_REMEDIATION_MODELS == {
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-mythos-5",
    }
    assert is_supported_remediation_model("CLAUDE-MYTHOS-5")
    assert not is_supported_remediation_model("not-really-opus")
    assert is_mythos_model(" claude-mythos-5 ")


def test_mythos_uses_one_million_context_threshold() -> None:
    assert resolve_autocompact_pct("claude-mythos-5", None) == 90


def test_mythos_ignores_untrusted_project_setting_sources() -> None:
    assert setting_sources_for_model("claude-mythos-5") == ["user"]
    assert setting_sources_for_model("claude-opus-4-8") == [
        "user",
        "project",
        "local",
    ]
    assert permission_mode_for_model("claude-mythos-5", "acceptEdits") == "dontAsk"
    assert permission_mode_for_model("claude-opus-4-8", "acceptEdits") == "acceptEdits"


def test_mythos_settings_are_proxy_only_and_strict(
    populated_agent_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _mythos_config(populated_agent_config)
    monkeypatch.setenv(MYTHOS_RUNTIME_MARKER, "1")

    settings = json.loads(
        build_claude_settings(
            cfg,
            "workspace-key",
            model="claude-mythos-5",
        )
    )

    assert settings["env"]["HTTPS_PROXY"] == "http://mythos-egress:3128"
    assert settings["env"]["NO_PROXY"] == "localhost,127.0.0.1"
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["failIfUnavailable"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["network"]["allowedDomains"] == [
        MYTHOS_INFERENCE_HOST
    ]
    assert "Read(//proc/**)" in settings["permissions"]["deny"]
    assert "Bash" in settings["permissions"]["deny"]


def test_mythos_rejects_extra_domain_and_github_token(
    populated_agent_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _mythos_config(populated_agent_config)
    monkeypatch.setenv(MYTHOS_RUNTIME_MARKER, "1")

    with pytest.raises(ValueError, match="additional sandbox domains"):
        build_claude_settings(
            cfg,
            "workspace-key",
            model="claude-mythos-5",
            sandbox_allowed_domains=["github.com"],
        )
    with pytest.raises(ValueError, match="GitHub credentials"):
        build_claude_settings(
            cfg,
            "workspace-key",
            model="claude-mythos-5",
            extra_env={"GH_TOKEN": "secret"},
        )


def test_mythos_scan_rejects_mutating_or_delivery_profiles(
    populated_agent_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _mythos_config(populated_agent_config)
    monkeypatch.setenv(MYTHOS_RUNTIME_MARKER, "1")

    with pytest.raises(ValueError, match="read-only"):
        enforce_mythos_mode_policy(
            cfg,
            "claude-mythos-5",
            mode="scan",
            read_only=False,
            enable_bash=True,
        )
    with pytest.raises(ValueError, match="Publish and issue delivery"):
        enforce_mythos_mode_policy(
            cfg,
            "claude-mythos-5",
            mode="scan",
            publish=True,
        )


def test_mythos_requires_runtime_attestation(
    populated_agent_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _mythos_config(populated_agent_config)
    monkeypatch.delenv(MYTHOS_RUNTIME_MARKER, raising=False)
    with pytest.raises(ValueError, match=MYTHOS_RUNTIME_MARKER):
        enforce_mythos_mode_policy(
            cfg,
            "claude-mythos-5",
            mode="scan",
        )


def test_mythos_proxy_cannot_be_redirected(
    populated_agent_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _mythos_config(populated_agent_config)
    cfg = replace(cfg, mythos=replace(cfg.mythos, https_proxy="http://evil:3128"))
    monkeypatch.setenv(MYTHOS_RUNTIME_MARKER, "1")

    with pytest.raises(ValueError, match="pinned"):
        enforce_mythos_mode_policy(cfg, "claude-mythos-5", mode="scan")
