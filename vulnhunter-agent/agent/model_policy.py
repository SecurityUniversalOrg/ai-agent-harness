"""Model capability and execution-policy helpers.

Mythos is deliberately handled as an execution profile, not as a string alias.
The model has no safety classifiers, so every entrypoint must apply the same
fail-closed controls before it performs target-repository network I/O or starts
the Claude SDK.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import AgentConfig


OPUS_47_MODEL = "claude-opus-4-7"
OPUS_48_MODEL = "claude-opus-4-8"
MYTHOS_MODEL = "claude-mythos-5"
MYTHOS_REGION = "us-east-1"
MYTHOS_INFERENCE_HOST = "aws-external-anthropic.us-east-1.api.aws"
MYTHOS_RUNTIME_MARKER = "VULNHUNT_MYTHOS_HARDENED_RUNTIME"
MYTHOS_PROXY_URL = "http://mythos-egress:3128"

SUPPORTED_REMEDIATION_MODELS = frozenset(
    {OPUS_47_MODEL, OPUS_48_MODEL, MYTHOS_MODEL}
)


def canonical_model(model: str) -> str:
    """Return the normalized model identifier used for policy comparisons."""
    return (model or "").strip().lower()


def is_mythos_model(model: str) -> bool:
    """True only for the canonical Claude Platform Mythos 5 identifier."""
    return canonical_model(model) == MYTHOS_MODEL


def is_supported_remediation_model(model: str) -> bool:
    """Return whether fix mode has an explicitly reviewed model profile."""
    return canonical_model(model) in SUPPORTED_REMEDIATION_MODELS


def is_long_context_model(model: str) -> bool:
    """Return whether the configured model has a one-million-token window."""
    normalized = canonical_model(model)
    return is_mythos_model(normalized) or "[1m]" in normalized or "_1m" in normalized


def setting_sources_for_model(model: str) -> list[str]:
    """Prevent target-controlled Claude settings/hooks in the Mythos profile."""
    return ["user"] if is_mythos_model(model) else ["user", "project", "local"]


def permission_mode_for_model(model: str, configured_mode: str) -> str:
    """Use deny-by-default permission resolution for the Mythos profile."""
    return "dontAsk" if is_mythos_model(model) else configured_mode


def _validate_proxy_url(proxy_url: str) -> None:
    if proxy_url != MYTHOS_PROXY_URL:
        raise ValueError(
            f"mythos.https_proxy is pinned to {MYTHOS_PROXY_URL!r}; got "
            f"{proxy_url!r}"
        )


def enforce_mythos_base_policy(
    config: "AgentConfig",
    model: str,
) -> None:
    """Validate controls shared by every Mythos SDK session.
    """
    if not is_mythos_model(model):
        return

    if config.anthropic.auth_mode != "anthropic_aws":
        raise ValueError(
            "claude-mythos-5 must use anthropic.auth_mode='anthropic_aws'"
        )
    if config.anthropic.aws_region != MYTHOS_REGION:
        raise ValueError(
            f"claude-mythos-5 is restricted to AWS region {MYTHOS_REGION!r}; "
            f"got {config.anthropic.aws_region!r}"
        )
    if not config.mythos.data_retention_acknowledged:
        raise ValueError(
            "claude-mythos-5 requires mythos.data_retention_acknowledged=true "
            "because Mythos requests have mandatory 30-day retention and are "
            "not eligible for zero-data-retention"
        )
    if not config.sandbox.enabled or not config.sandbox.fail_if_unavailable:
        raise ValueError(
            "claude-mythos-5 requires sandbox.enabled=true and "
            "sandbox.fail_if_unavailable=true"
        )
    if config.sandbox.allow_unsandboxed_commands:
        raise ValueError(
            "claude-mythos-5 forbids sandbox.allow_unsandboxed_commands=true"
        )
    if config.telemetry.enabled:
        raise ValueError(
            "claude-mythos-5 forbids telemetry so the model container has only "
            f"{MYTHOS_INFERENCE_HOST}:443 egress"
        )
    if not config.mythos.https_proxy:
        raise ValueError(
            "claude-mythos-5 requires mythos.https_proxy; the hardened launcher "
            "supplies the inference-only egress proxy"
        )
    _validate_proxy_url(config.mythos.https_proxy)
    if os.environ.get(MYTHOS_RUNTIME_MARKER) != "1":
        raise ValueError(
            "claude-mythos-5 requires the hardened gVisor launcher. The runtime "
            f"marker {MYTHOS_RUNTIME_MARKER}=1 is set only after the launcher "
            "verifies Docker reports runtime=runsc."
        )


def enforce_mythos_mode_policy(
    config: "AgentConfig",
    model: str,
    *,
    mode: str,
    read_only: bool = True,
    enable_bash: bool = False,
    publish: bool = False,
    issues: bool = False,
    no_post: bool = False,
    no_reopen: bool = False,
    check_runtime_environment: bool = True,
) -> None:
    """Apply mode-specific least-privilege constraints for Mythos.

    ``check_runtime_environment`` gates ``enforce_mythos_base_policy`` — the
    hardened-runtime-marker, pinned-proxy, sandbox, and telemetry checks that
    only make sense for the process about to actually start a model session
    inside the Mythos gVisor container. Scan and fix run their *entire*
    ``python -m agent`` invocation inside that container, so the default
    (``True``) is correct for them. Verify is different: its GitHub fetch and
    clone must run in a trusted host process *outside* any container (see
    ``agent/verify_mythos.py``), and only the model turn itself moves inside
    a container via a separate entrypoint that performs its own
    ``enforce_mythos_base_policy`` call. Callers on that split path pass
    ``check_runtime_environment=False`` so the outer, non-containerized
    process isn't rejected for not being the hardened runtime it was never
    meant to be.
    """
    if not is_mythos_model(model):
        return
    if check_runtime_environment:
        enforce_mythos_base_policy(config, model)

    if mode == "scan":
        if not read_only or enable_bash:
            raise ValueError(
                "claude-mythos-5 scan mode is read-only and never exposes Bash"
            )
        if publish or issues:
            raise ValueError(
                "claude-mythos-5 runs in an inference-only container. Publish "
                "and issue delivery must run later in a separate control-plane "
                "process; no GitHub credential may enter the Mythos container."
            )
    elif mode == "fix":
        if not no_post:
            raise ValueError(
                "claude-mythos-5 fix mode requires --no-post. GitHub delivery "
                "must be performed by a separately authorized control plane."
            )
        if config.fix.allowed_domains:
            raise ValueError(
                "claude-mythos-5 fix mode forbids fix.allowed_domains; only the "
                "Claude Platform inference endpoint may be reachable"
            )
    elif mode == "verify":
        if not no_post or not no_reopen:
            raise ValueError(
                "claude-mythos-5 verify mode requires --no-post and --no-reopen; "
                "GitHub mutations belong in a separate control-plane process"
            )
    else:
        raise ValueError(f"Unknown Mythos execution mode: {mode!r}")
