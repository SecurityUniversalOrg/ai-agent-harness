"""Container-side entrypoint for one Mythos-isolated verify session.

Invoked as ``python -m agent._mythos_verify_entry`` *inside* the gVisor
container by ``scripts/run_mythos_verify_sandbox.sh``. Everything this needs
— the rendered kickoff prompt, the pre-cloned target/additional repos, the
staged report, and ``comments.md`` — was produced by the trusted host
process (``agent/verify_mythos.py``) *before* the container started. This
process reads only Claude Platform on AWS credentials from its environment
and never touches GitHub: no GitHub token, no git remote, no GitHub host is
configured here, and the container's network egress is confined to the
Claude Platform inference endpoint by the launcher's Squid proxy regardless.

Deliberately a separate, minimal module rather than a new ``--mode`` on the
main CLI: it skips every trusted-host-only step (fetch, clone, homogeneity,
pre-flight, post) entirely rather than trying to make the full parser aware
of a "some steps already happened outside this process" state.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .auth import make_token_manager
from .config import load_config
from .model_policy import enforce_mythos_base_policy
from .verify_runner import OutputKind, run_verify_session


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m agent._mythos_verify_entry")
    parser.add_argument("--config", required=True, help="Path to the container's TOML config")
    parser.add_argument("--model", default=None, help="Model override (expected: claude-mythos-5)")
    parser.add_argument("--cwd", required=True, help="Run-scoped working directory")
    parser.add_argument("--out-dir", required=True, help="Directory the skill writes verify_disposition.json into")
    parser.add_argument("--prompt-file", required=True, help="Pre-rendered kickoff prompt")
    parser.add_argument("--log-path", required=True, help="Append-mode SDK event log path")
    return parser.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    model = args.model or config.anthropic.model
    # Defense-in-depth: the launcher's own docker-run/exec flags already
    # enforce runsc, read-only rootfs, dropped capabilities, and a single
    # allowed egress host; this repeats the *config-visible* half of that
    # contract (retention ack, sandbox posture, telemetry off, pinned proxy,
    # hardened-runtime marker) from inside the same process that is about to
    # start a model session, exactly as scan and fix do.
    enforce_mythos_base_policy(config, model)
    token_manager = make_token_manager(config, name="verify")
    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    result = await run_verify_session(
        config=config,
        auth_token=token_manager.get_valid_token(),
        cwd=Path(args.cwd),
        out_dir=Path(args.out_dir),
        prompt=prompt,
        log_path=Path(args.log_path),
        model_override=args.model,
    )
    if result.kind is not OutputKind.DISPOSITION:
        logging.error(
            "Mythos verify session produced no valid disposition: %s",
            result.error_detail,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    args = _parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except Exception:  # noqa: BLE001
        logging.exception("Mythos verify entry failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
