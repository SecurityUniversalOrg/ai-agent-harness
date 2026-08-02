from __future__ import annotations

import re
import subprocess
import tempfile
import textwrap

from . import docker_ops

CLAUDE_CODE_VERSION = "2.1.177"
BASE_TAG = f"scanning-agent-base:{CLAUDE_CODE_VERSION}"
_TAG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._/:-]*$")


def validate_tag(tag: str) -> None:
    if not _TAG_RE.match(tag):
        raise ValueError(f"invalid image tag: {tag!r}")


def build(dockerfile: str, tag: str, context: str | None = None) -> None:
    with tempfile.TemporaryDirectory() as ctx:
        with open(f"{ctx}/Dockerfile", "w") as f:
            f.write(dockerfile)
        if context is None:
            cmd = ["docker", "build", "-q", "-t", tag, ctx]
        else:
            cmd = [
                "docker",
                "build",
                "-q",
                "-f",
                f"{ctx}/Dockerfile",
                "-t",
                tag,
                context,
            ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)


def ensure_base() -> str:
    if docker_ops.image_exists(BASE_TAG):
        return BASE_TAG
    build(
        textwrap.dedent(f"""\
            FROM gcc:14
            RUN apt-get update && \\
                apt-get install -y --no-install-recommends nodejs npm ca-certificates && \\
                rm -rf /var/lib/apt/lists/* && \\
                npm install -g @anthropic-ai/claude-code@{CLAUDE_CODE_VERSION}
            WORKDIR /work
        """),
        BASE_TAG,
    )
    return BASE_TAG
