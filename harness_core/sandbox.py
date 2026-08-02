from __future__ import annotations

import contextlib
import os
import subprocess
from typing import Iterator

from . import docker_ops


RUNTIME_ENV = "VULN_HARNESS_AGENT_RUNTIME"
PROXY_ENV = "VULN_HARNESS_EGRESS_PROXY"
NETWORK_ENV = "VULN_HARNESS_AGENT_NETWORK"
NETWORK_DEFAULT = "vsa-sandbox"

PROFILE_DIR_KEY = "_VH_PROFILE_DIR"
PROFILE_CONTAINER_DIR = "/root/.config/anthropic"
WIF_TOKEN_FILE_KEY = "ANTHROPIC_IDENTITY_TOKEN_FILE"
WIF_TOKEN_CONTAINER_PATH = "/root/.anthropic_identity_token"


def runtime() -> str | None:
    return os.environ.get(RUNTIME_ENV) or None


def proxy() -> str | None:
    return os.environ.get(PROXY_ENV) or None


def network() -> str:
    if not runtime():
        return "bridge"
    return os.environ.get(NETWORK_ENV) or NETWORK_DEFAULT


def permission_mode() -> str:
    return "bypassPermissions" if runtime() else "auto"


@contextlib.contextmanager
def agent_container(
    image: str,
    name: str,
    auth: dict[str, str] | None = None,
    *,
    memory: str = "4g",
    shm_size: str | None = None,
    mounts: list[tuple[str, str]] | None = None,
) -> Iterator[str]:
    all_mounts = list(mounts or [])
    profile_dir = (auth or {}).get(PROFILE_DIR_KEY)
    if auth and (tf := auth.get(WIF_TOKEN_FILE_KEY)):
        all_mounts.append((tf, WIF_TOKEN_CONTAINER_PATH))
    container = docker_ops.run(
        image,
        name=name,
        runtime=runtime(),
        network=network(),
        memory=memory,
        shm_size=shm_size,
        env=container_env(auth),
        mounts=all_mounts,
    )
    try:
        if profile_dir:
            docker_ops.copy_dir_in(container, profile_dir, PROFILE_CONTAINER_DIR)
        yield container
    finally:
        docker_ops.rm(container)


def container_env(auth: dict[str, str] | None) -> dict[str, str]:
    e = dict(auth or {})
    if WIF_TOKEN_FILE_KEY in e:
        e[WIF_TOKEN_FILE_KEY] = WIF_TOKEN_CONTAINER_PATH
    if e.pop(PROFILE_DIR_KEY, None):
        e["ANTHROPIC_CONFIG_DIR"] = PROFILE_CONTAINER_DIR
    if p := proxy():
        e["HTTPS_PROXY"] = p
    return e


def require(override: bool) -> str | None:
    """Return an error message if the sandbox isn't configured"""
    if override:
        return None
    rt = runtime()
    if not rt:
        return(
            "error: refusing to spawn agents outside of the sandbox.\n"
            "  Run via `bin/vuln-scan-harness ...`"
        )
    runtimes = subprocess.run(
        ["docker", "info", "--format", "{{range $k,$v := .Runtimes}}{{$k}} {{end}}"],
        capture_output=True,
        text=True,
    ).stdout.split()
    if rt not in runtimes:
        return (
            f"error: {RUNTIME_ENV}={rt!r} but docker has no such runtime ({runtimes})"
        )
    return None
