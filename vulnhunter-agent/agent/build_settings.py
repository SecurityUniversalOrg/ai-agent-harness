"""Build the Claude Code settings JSON the SDK passes through to the CLI.

Routes Anthropic calls one of three ways: through Claude Platform on AWS with
a workspace API key (``anthropic_aws`` mode), through an AWS Bedrock proxy
fronted by an OAuth bearer token (``bedrock_oauth`` mode), or directly to
Amazon Bedrock with SigV4 request signing via the standard AWS credential
chain (``bedrock_sigv4`` mode). It also blanks out inherited HTTP proxies,
applies an OS-level sandbox, and (optionally) enables OTLP telemetry.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from urllib.parse import urlparse

from .config import AgentConfig
from .model_policy import (
    enforce_mythos_base_policy,
    is_long_context_model,
    is_mythos_model,
)

# Deny system paths, restrict reads/writes to the cwd (cloned repo).
# "." resolves to cwd inside Claude Code's sandbox.
_SANDBOX_DENY_READ_PATHS = ["/app", "/etc", "/proc", "/root", "/home", "/var", "/sys", "/run"]
_SANDBOX_DENY_WRITE_PATHS = ["/"]
_SANDBOX_ALLOW_READ_PATHS = ["."]
_SANDBOX_ALLOW_WRITE_PATHS = ["."]

_DEFAULT_OTEL_RESOURCE_ATTRIBUTES = "service.name=vulnhunter-agent,type=cli"


def resolve_autocompact_pct(model: str, override: int | None) -> int:
    """Pick the context-compaction threshold for a given model.

    - explicit ``override`` wins
    - 1M-context variants → 90% (~100K headroom)
    - standard 200K models → 85% (~30K headroom)
    """
    if override is not None:
        return override
    return 90 if is_long_context_model(model) else 85


def _anthropic_host(cfg: AgentConfig) -> str:
    """The network host the SDK talks to, for the sandbox allow-list."""
    if cfg.anthropic.auth_mode == "anthropic_aws":
        region = cfg.anthropic.aws_region
        return f"aws-external-anthropic.{region}.api.aws" if region else ""
    if cfg.anthropic.auth_mode == "bedrock_oauth":
        return urlparse(cfg.anthropic.bedrock_base_url).hostname or ""
    if cfg.anthropic.auth_mode == "bedrock_sigv4":
        # Explicit base URL (VPC/custom endpoint) wins; otherwise the
        # regional Bedrock runtime endpoint the CLI will call by default.
        if cfg.anthropic.bedrock_base_url:
            return urlparse(cfg.anthropic.bedrock_base_url).hostname or ""
        region = cfg.anthropic.aws_region
        return f"bedrock-runtime.{region}.amazonaws.com" if region else ""
    return ""


def _sandbox_allow_read_paths(cfg: AgentConfig) -> list[str]:
    """Paths the sandboxed CLI may read.

    Always the cwd (the cloned repo). In ``bedrock_sigv4`` mode also the
    shared AWS config directory (``~/.aws`` — config, credentials, and the
    SSO token cache): on Linux, home directories sit under the denied
    ``/home`` / ``/root`` prefixes, so without this carve-out the credential
    chain degrades to env-var credentials only and ``aws_profile`` / SSO
    silently stop working. The allow entry is more specific than the deny
    prefix, so it wins; it is read-only (``allowWrite`` is untouched).
    """
    paths = list(_SANDBOX_ALLOW_READ_PATHS)
    if cfg.anthropic.auth_mode == "bedrock_sigv4":
        paths.append(os.path.expanduser("~/.aws"))
    return paths


def _sandbox_allowed_domains(cfg: AgentConfig) -> list[str]:
    """Hosts the sandboxed CLI is allowed to reach.

    Always the Anthropic/Bedrock inference host. In ``bedrock_sigv4`` mode the
    CLI also signs with the AWS credential chain, which for assume-role / SSO
    credentials calls regional STS — so we add the regional STS endpoint too.
    (Static-key credentials need no extra host; IMDS/instance-role uses the
    link-local 169.254.169.254 address, and full SSO login flows may need
    additional endpoints — for those, widen this list or disable the sandbox.)
    """
    host = _anthropic_host(cfg)
    domains = [host] if host else []
    if cfg.anthropic.auth_mode == "bedrock_sigv4":
        region = cfg.anthropic.aws_region
        if region:
            sts = f"sts.{region}.amazonaws.com"
            if sts not in domains:
                domains.append(sts)
    return domains


def _dedupe(values: Sequence[str]) -> list[str]:
    """Return non-empty strings in first-seen order."""
    return list(dict.fromkeys(value for value in values if value))


def _build_sandbox(
    cfg: AgentConfig,
    *,
    extra_allowed_domains: Sequence[str] = (),
    extra_allow_read_paths: Sequence[str] = (),
    extra_allow_write_paths: Sequence[str] = (),
    strict: bool = False,
) -> dict:
    allowed_domains = _dedupe(
        [*_sandbox_allowed_domains(cfg), *extra_allowed_domains]
    )
    return {
        "enabled": True if strict else cfg.sandbox.enabled,
        "failIfUnavailable": True if strict else cfg.sandbox.fail_if_unavailable,
        "allowUnsandboxedCommands": (
            False if strict else cfg.sandbox.allow_unsandboxed_commands
        ),
        "filesystem": {
            "denyRead": list(_SANDBOX_DENY_READ_PATHS),
            "allowRead": _dedupe(
                [*_sandbox_allow_read_paths(cfg), *extra_allow_read_paths]
            ),
            "denyWrite": list(_SANDBOX_DENY_WRITE_PATHS),
            "allowWrite": _dedupe(
                [*_SANDBOX_ALLOW_WRITE_PATHS, *extra_allow_write_paths]
            ),
        },
        "network": {"allowedDomains": allowed_domains},
    }


def build_claude_settings(
    cfg: AgentConfig,
    auth_token: str,
    *,
    model: str,
    scan_id: str = "",
    extra_env: Mapping[str, str] | None = None,
    sandbox_allowed_domains: Sequence[str] = (),
    sandbox_allow_read_paths: Sequence[str] = (),
    sandbox_allow_write_paths: Sequence[str] = (),
    strict_sandbox: bool = False,
    execution_mode: str = "scan",
) -> str:
    """Return a JSON string matching Claude Code's settings file schema."""
    mythos = is_mythos_model(model)
    if mythos:
        # Defense in depth for programmatic callers that bypass the CLI's
        # mode preflight. Mythos may never inherit a wider network allow-list
        # or a GitHub credential through a mode-specific runner.
        enforce_mythos_base_policy(cfg, model)
        if sandbox_allowed_domains:
            raise ValueError(
                "claude-mythos-5 forbids additional sandbox domains; only the "
                "Claude Platform inference endpoint is allowed"
            )
        sensitive_extra_env = {
            key
            for key, value in (extra_env or {}).items()
            if value
            and key.upper()
            in {
                "GH_TOKEN",
                "GH_ENTERPRISE_TOKEN",
                "GITHUB_TOKEN",
                "VULNHUNT_GITHUB_SCAN_TOKEN",
                "VULNHUNT_GITHUB_REPORTS_TOKEN",
            }
        }
        if sensitive_extra_env:
            raise ValueError(
                "claude-mythos-5 forbids GitHub credentials in the model "
                "process: " + ", ".join(sorted(sensitive_extra_env))
            )
        protected_extra_env = {
            key
            for key in (extra_env or {})
            if key.upper()
            in {
                "ANTHROPIC_BASE_URL",
                "ANTHROPIC_BEDROCK_BASE_URL",
                "AWS_REGION",
                "CLAUDE_CODE_ENABLE_TELEMETRY",
                "CLAUDE_CODE_USE_ANTHROPIC_AWS",
                "CLAUDE_CODE_USE_BEDROCK",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "NO_PROXY",
                "NODE_TLS_REJECT_UNAUTHORIZED",
                "OTEL_EXPORTER_OTLP_ENDPOINT",
            }
        }
        if protected_extra_env:
            raise ValueError(
                "claude-mythos-5 forbids mode-specific overrides of protected "
                "provider/network settings: "
                + ", ".join(sorted(protected_extra_env))
            )

    autocompact_pct = resolve_autocompact_pct(
        model, cfg.scan.autocompact_pct_override
    )
    no_proxy = "localhost,127.0.0.1" if mythos else cfg.scan.no_proxy
    proxy = cfg.mythos.https_proxy if mythos else ""
    env: dict[str, str] = {
        "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": str(autocompact_pct),
        "http_proxy": proxy,
        "https_proxy": proxy,
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "no_proxy": no_proxy,
        "NO_PROXY": no_proxy,
        "NODE_TLS_REJECT_UNAUTHORIZED": "",
    }

    if cfg.anthropic.auth_mode == "anthropic_aws":
        env.update(
            {
                "CLAUDE_CODE_USE_ANTHROPIC_AWS": "1",
                "ANTHROPIC_AWS_WORKSPACE_ID": cfg.anthropic.aws_workspace_id,
                "AWS_REGION": cfg.anthropic.aws_region,
                "ANTHROPIC_AWS_API_KEY": auth_token or cfg.anthropic.aws_api_key,
            }
        )
    elif cfg.anthropic.auth_mode == "bedrock_oauth":
        env.update(
            {
                "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "1",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_REGION": cfg.anthropic.aws_region,
                "ANTHROPIC_BEDROCK_BASE_URL": cfg.anthropic.bedrock_base_url,
                "ANTHROPIC_AUTH_TOKEN": auth_token,
                # Extend the prompt-cache TTL from the 5-minute default to
                # ~1 hour on Bedrock. Same write/read pricing as the default
                # TTL, so for multi-turn scans (often 30-60 minutes total)
                # this is strictly cheaper: we pay the cache-creation premium
                # once instead of once per cache eviction.
                "ENABLE_PROMPT_CACHING_1H_BEDROCK": "1",
            }
        )
    elif cfg.anthropic.auth_mode == "bedrock_sigv4":
        # Direct Amazon Bedrock with SigV4 signing. Crucially we set
        # CLAUDE_CODE_USE_BEDROCK=1 but DELIBERATELY omit both
        # CLAUDE_CODE_SKIP_BEDROCK_AUTH and ANTHROPIC_AUTH_TOKEN — their
        # absence is what makes the bundled CLI sign requests with the AWS
        # credential chain (env vars, shared config/credentials, SSO,
        # container/instance role) instead of forwarding a bearer token to
        # a proxy. auth_token is empty in this mode (SigV4TokenManager).
        env.update(
            {
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "AWS_REGION": cfg.anthropic.aws_region,
                # Same 1-hour cache-TTL rationale as bedrock_oauth.
                "ENABLE_PROMPT_CACHING_1H_BEDROCK": "1",
            }
        )
        # Optional explicit endpoint (VPC / custom Bedrock endpoint). Blank →
        # the CLI derives the regional bedrock-runtime endpoint from AWS_REGION.
        if cfg.anthropic.bedrock_base_url:
            env["ANTHROPIC_BEDROCK_BASE_URL"] = cfg.anthropic.bedrock_base_url
        # Optional named profile from the shared AWS config/credentials file.
        # Blank → default credential chain.
        if cfg.anthropic.aws_profile:
            env["AWS_PROFILE"] = cfg.anthropic.aws_profile
    else:
        raise ValueError(
            f"Unsupported Anthropic auth mode: {cfg.anthropic.auth_mode}"
        )

    # Cap how long an async subagent (Task tool) can go without forward
    # progress before the CLI returns a "Request timed out" tool result
    # to the orchestrator. Set 0 in config to fall through to the CLI default.
    if cfg.scan.async_agent_stall_timeout_ms > 0:
        env["CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS"] = str(
            cfg.scan.async_agent_stall_timeout_ms
        )

    if cfg.telemetry.enabled and cfg.telemetry.otel_exporter_otlp_endpoint:
        resource_attributes = (
            cfg.telemetry.resource_attributes or _DEFAULT_OTEL_RESOURCE_ATTRIBUTES
        )
        if scan_id:
            resource_attributes = f"{resource_attributes},scan.id={scan_id}"
        env.update(
            {
                "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
                "OTEL_SERVICE_NAME": "vulnhunter-agent",
                "OTEL_METRICS_EXPORTER": "otlp",
                "OTEL_LOG_USER_PROMPTS": "1",
                "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
                "OTEL_EXPORTER_OTLP_ENDPOINT": cfg.telemetry.otel_exporter_otlp_endpoint,
                "OTEL_EXPORTER_OTLP_TIMEOUT": "60000",
                "OTEL_LOGS_EXPORT_INTERVAL": "15000",
                "OTEL_METRIC_EXPORT_INTERVAL": "60000",
                "OTEL_METRIC_EXPORT_TIMEOUT": "30000",
                "CLAUDE_CODE_OTEL_SHUTDOWN_TIMEOUT_MS": "30000",
                "OTEL_LOG_TOOL_DETAILS": "1",
                "OTEL_LOG_TOOL_CONTENT": "1",
                "OTEL_RESOURCE_ATTRIBUTES": resource_attributes,
            }
        )
    else:
        env["CLAUDE_CODE_ENABLE_TELEMETRY"] = "0"

    # Mode-specific runners may need narrowly-scoped process settings (for
    # example GH_TOKEN/GH_HOST and a cwd-local TMPDIR in remediation mode).
    # These are supplied by trusted Python orchestration, never from target
    # source or model output.
    if extra_env:
        env.update({str(key): str(value) for key, value in extra_env.items()})

    settings: dict[str, object] = {
        "env": env,
        "sandbox": _build_sandbox(
            cfg,
            extra_allowed_domains=sandbox_allowed_domains,
            extra_allow_read_paths=sandbox_allow_read_paths,
            extra_allow_write_paths=sandbox_allow_write_paths,
            strict=True if mythos else strict_sandbox,
        ),
    }
    if mythos:
        # Claude's documented permission rules apply to native Read/Edit/Glob/Grep
        # tools as well as the Bash sandbox. In particular, deny /proc so a model
        # cannot use Read to recover the workspace API key from process environ.
        # ``Edit`` covers all built-in mutation tools, including Write.
        sensitive_roots = (
            "app",
            "etc",
            "opt",
            "proc",
            "root",
            "run",
            "sys",
            "tmp",
            "var",
        )
        deny = [
            *(f"Read(//{root}/**)" for root in sensitive_roots),
            *(f"Edit(//{root}/**)" for root in sensitive_roots),
            "Read(/.claude/**)",
            "Read(/CLAUDE.md)",
            "Read(**/.claude/**)",
            "Read(**/CLAUDE.md)",
            "Edit(/.claude/**)",
            "Edit(/.git/**)",
            "Edit(/CLAUDE.md)",
            "Edit(**/.claude/**)",
            "Edit(**/CLAUDE.md)",
            "Bash",
            "WebFetch",
            "WebSearch",
            "NotebookEdit",
        ]
        settings["permissions"] = {
            "deny": [
                rule
                for rule in deny
                if rule != "Bash" or execution_mode != "fix"
            ],
            "disableBypassPermissionsMode": "disable",
            "disableAutoMode": "disable",
        }
    return json.dumps(settings)
