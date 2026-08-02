"""Build the per target agent image: includes the target binary and claude CLI"""

from __future__ import annotations

import functools
import subprocess

from harness_core import docker_ops
from harness_core.image import (
    BASE_TAG,
    CLAUDE_CODE_VERSION,
    build,
    ensure_base,
    validate_tag
)

def agent_tag(target_tag: str) -> str:
    return f"{target_tag.replace(':', '-')}-agent:{CLAUDE_CODE_VERSION}"


@functools.lru_cache(maxsize=None)
def ensure(target_tag: str) -> str:
    validate_tag(target_tag)
    tag = agent_tag(target_tag)
    if docker_ops.image_exists(tag):
        return tag
    ensure_base()
    build(
        f"FROM {BASE_TAG}\nCOPY --from={target_tag} /work /work\n",
        tag,
    )
    subprocess.run(
        ["docker", "tag", tag, f"{tag.rsplit(':', 1)[0]}:latest"],
        check=True,
    )
    return tag