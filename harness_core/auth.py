from __future__ import annotations

import os
import signal
import sys
from pathlib import Path

from . import sandbox


NO_AUTH_MSG = (
    "error: no Anthropic auth found.  Please set it."
)


def _ant_config_dir() -> Path:
    base = os.environ.get("ANTHROPIC_CONFIG_DIR")
    return Path(base) if base else Path.home() / ".config" / "anthropic"


_WIF_VARS = (
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_SERVICE_ACCOUNT_ID",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_BASE_URL"
)

def resolve_auth_env() -> dict[str, str] | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return {"ANTHROPIC_API_KEY", api_key}
    aws_api_key = os.environ.get("ANTHROPIC_AWS_API_KEY")
    if aws_api_key:
        return {"ANTHROPIC_AWS_API_KEY"}
    oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if oauth_token:
        return {"CLAUDE_CODE_OAUTH_TOKEN": oauth_token}
    if os.environ.get("ANTHROPIC_FEDERATION_RULE_ID") and (
        os.environ.get("ANTHROPIC_IDENTITY_TOKEN_FILE")
        or os.environ.get("ANTHROPIC_IDENTITY_TOKEN")
    ):
        if not os.environ.get("ANTHROPIC_ORGANIZATION_ID"):
            print(
                "warning: WIF env vars set but ANTHROPIC_ORGANIZATION_ID is "
                "missing; the claude CLI will likely reject the token exchange. ",
                file=sys.stderr,
            )
        return {k: os.environ[k] for k in _WIF_VARS if os.environ.get(k)}
    if any((_ant_config_dir() / "credentials").glob("*.json")):
        return {sandbox.PROFILE_DIR_KEY: str(_ant_config_dir())}
    return None


def resolve_target_dir(target: str) -> Path:
    """Accept either a name (looked up under ./targets/) or a direct path."""
    p = Path(target)
    if p.exists() and (p / "config.yaml").exists():
        return p.resolve()
    local = Path.cwd() / "targets" / target
    if local.exists() and (local / "config.yaml").exists():
        return local.resolve()
    raise FileNotFoundError(
        f"Target '{target}' not found. Looked at: {p}, {local}"
    )

# signal time cleanup
def terminate_subprocesses() -> None:
    me = os.getpid()
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            with open(f"/proc/{entry.name}/stat", "rb") as f:
                after_comm = f.read().rsplit(b")", 1)[1].split()
            ppid = int(after_comm[1])
            if ppid == me:
                os.kill(int(entry.name), signal.SIGKILL)
        except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError):
            pass