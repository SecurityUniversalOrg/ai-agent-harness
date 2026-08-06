"""Mythos gVisor bridge for ``--mode=verify``.

Fetch, clone, homogeneity, pre-flight cross-repo resolution, and disposition
posting all stay in the trusted host process (``agent/verify.py``) exactly as
for every other model — they need a real GitHub credential, and the entire
point of the Mythos execution profile is that this credential must never
reach the process that hands the model a live turn. Unlike scan and fix,
verify's credential is used for more than one clone (it also drives the
issue/comment/timeline REST+GraphQL fetch that produces ``narratives``), so
there is no single "pre-stage everything, then run the whole CLI inside the
container" shortcut here.

This module is the one seam that gets swapped in: instead of running the SDK
session in-process (``verify_runner.run_verify_session``, invoked by
``verify._run_skill``), ``run_skill_mythos`` renders the same inputs
(comments file, kickoff prompt) from already-fetched/cloned, credential-free
data, then calls ``scripts/run_mythos_verify_sandbox.sh`` to stream those
inputs into a gVisor container and stream back the disposition it produces.
No GitHub token, GitHub host, or network route to GitHub ever enters that
container — only Claude Platform on AWS credentials do, the same as scan's
Mythos profile.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .config import AgentConfig
from .model_policy import canonical_model
from .verify_extract import IssueNarrative, write_comments_file
from .verify_runner import (
    OutputKind,
    VerifySessionResult,
    build_kickoff_prompt,
    classify_output,
)

if TYPE_CHECKING:
    from .verify import _RunState

logger = logging.getLogger(__name__)

_SCRIPT_REL_PATH = ("scripts", "run_mythos_verify_sandbox.sh")

# Fixed container-side paths. The kickoff prompt is rendered with these —
# not the real host paths — because the model reads it only after these
# exact locations have been populated inside the container by the sandbox
# launcher. ``build_kickoff_prompt`` requires absolute paths; PurePosixPath
# keeps them POSIX-correct regardless of the orchestrating host's OS.
_CONTAINER_WORKSPACE = PurePosixPath("/workspace")
_CONTAINER_REPO = _CONTAINER_WORKSPACE / "repo"
_CONTAINER_REPORT = _CONTAINER_WORKSPACE / "report"
_CONTAINER_COMMENTS = _CONTAINER_WORKSPACE / "comments.md"
_CONTAINER_OUT_DIR = _CONTAINER_WORKSPACE / "out" / "iter-1"
_CONTAINER_ADDITIONAL_REPOS_DIR = _CONTAINER_WORKSPACE / "additional_repos"


class MythosVerifySandboxError(RuntimeError):
    """The gVisor verify sandbox launcher failed before it could run."""


def _repo_root() -> Path:
    # agent/verify_mythos.py -> vulnhunter-agent/agent -> vulnhunter-agent -> repo root
    return Path(__file__).resolve().parents[2]


async def run_skill_mythos(
    *,
    config: AgentConfig,
    token_manager: object | None = None,
    run_dir: Path,
    log_path: Path,
    target_repo: Path,
    report: Path,
    comments_path: Path,
    narratives: list[IssueNarrative],
    fixed_ids: list[str],
    state: "_RunState",
    model_override: str | None,
) -> VerifySessionResult:
    """Drop-in replacement for ``verify._run_skill`` under Mythos.

    Accepts the exact same kwargs as ``verify._run_skill`` — including
    ``token_manager``, which it ignores — so ``verify.run_verify`` can call
    whichever runner it picked through one uniform call site. Mythos needs
    no Claude Platform token from the trusted host: the container entrypoint
    (``agent/_mythos_verify_entry.py``) mints its own from the credentials
    the sandbox launcher put in the container's environment.
    """
    write_comments_file(
        comments_path,
        narratives,
        ignored_hints=sorted(state.ignored_hints),
    )
    out_dir = run_dir / "out" / "iter-1"
    out_dir.mkdir(parents=True, exist_ok=True)

    additional_repos = list(state.additional_repos)
    container_additional_repos = [
        Path(_CONTAINER_ADDITIONAL_REPOS_DIR / str(index))
        for index in range(len(additional_repos))
    ]
    prompt = build_kickoff_prompt(
        repo=Path(_CONTAINER_REPO),
        report=Path(_CONTAINER_REPORT),
        fixed_ids=fixed_ids,
        out=Path(_CONTAINER_OUT_DIR),
        comments=Path(_CONTAINER_COMMENTS),
        additional_repos=container_additional_repos or None,
    )
    prompt_path = run_dir / "mythos-kickoff-prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")

    additional_manifest = run_dir / "mythos-additional-repos.txt"
    additional_manifest.write_text(
        "\n".join(str(path) for path in additional_repos) + ("\n" if additional_repos else ""),
        encoding="utf-8",
    )

    script = _repo_root().joinpath(*_SCRIPT_REL_PATH)
    if not script.is_file() or script.is_symlink():
        raise MythosVerifySandboxError(
            f"Mythos verify sandbox launcher is missing or symlinked: {script}"
        )

    model = canonical_model(model_override or config.anthropic.model)
    env = dict(os.environ)
    env.update(
        {
            "MYTHOS_REPO_ROOT": str(_repo_root()),
            "MYTHOS_TARGET_REPO_CHECKOUT": str(target_repo),
            "MYTHOS_REPORT_DIR": str(report),
            "MYTHOS_PROMPT_FILE": str(prompt_path),
            "MYTHOS_ADDITIONAL_REPOS_MANIFEST": str(additional_manifest),
            "MYTHOS_OUT_DIR": str(out_dir),
            "MYTHOS_LOG_PATH": str(log_path),
            "VULNHUNT_ANTHROPIC_MODEL": model,
            "MYTHOS_DOCKER_RUNTIME": env.get("MYTHOS_DOCKER_RUNTIME", "runsc"),
        }
    )
    if "VULNHUNT_ANTHROPIC_AWS_WORKSPACE_ID" not in env or "VULNHUNT_ANTHROPIC_AWS_API_KEY" not in env:
        # The trusted host process loaded these from config/env already, but the
        # sandbox script requires them explicitly rather than falling back to a
        # TOML file the container never sees.
        env.setdefault("VULNHUNT_ANTHROPIC_AWS_WORKSPACE_ID", config.anthropic.aws_workspace_id)
        env.setdefault("VULNHUNT_ANTHROPIC_AWS_API_KEY", config.anthropic.aws_api_key)

    logger.info("Launching Mythos gVisor verify sandbox: %s", script)
    process = await asyncio.create_subprocess_exec(
        "bash",
        str(script),
        env=env,
    )
    return_code = await process.wait()
    if return_code != 0:
        logger.error("Mythos verify sandbox exited %d", return_code)
        return VerifySessionResult(
            kind=OutputKind.EMPTY,
            output_path=None,
            parsed=None,
            error_detail=f"Mythos verify sandbox exited {return_code}",
        )
    return classify_output(out_dir)
