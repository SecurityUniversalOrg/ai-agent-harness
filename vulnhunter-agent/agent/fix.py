"""Top-level orchestration for ``python -m agent --mode=fix``.

The Python layer creates a contained, non-git run root; stages a bounded immutable
copy of the VulnHunter report; supplies a run-scoped remediation policy; invokes the
skill's automated fork profile; and validates the final machine disposition.  The
skill remains responsible for the TDD, graph, sweep, gate, and GitHub workflows.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .auth import make_token_manager
from .clone import shallow_clone
from .config import AgentConfig
from .fix_runner import (
    FixOutputKind,
    build_kickoff_prompt,
    fix_schema_text,
    run_fix_session,
)
from .token_client import get_github_token

logger = logging.getLogger(__name__)

_EXIT_OK = 0
_EXIT_FAILURE = 1
_EXIT_BAD_ARGS = 2
_EXIT_AUTH_FAILURE = 3
_EXIT_PARTIAL = 5

_REPO_PART_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SUCCESS_STATUSES = {"COMPLETED", "DRY_RUN", "NO_FINDINGS"}
_FULL_PHASES = {"parse", "plan", "implement", "verify", "sweep", "deliver"}


class FixInputError(ValueError):
    """An operator-supplied fix input violates the contained-run contract."""


def _is_link_like(path: Path) -> bool:
    """Detect symbolic links and Windows directory junctions."""
    return path.is_symlink() or (
        hasattr(path, "is_junction") and path.is_junction()
    )


def _normalize_github_authority(configured_host: str) -> str:
    """Return a safe ``hostname[:port]`` GitHub authority from configuration."""
    value = configured_host.strip().rstrip("/")
    if (
        not value
        or ".." in value
        or not re.fullmatch(r"[A-Za-z0-9.-]+(?::\d+)?", value)
    ):
        raise FixInputError(
            "github.host must be a hostname with an optional numeric port"
        )
    parsed = urlparse(f"https://{value}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise FixInputError("github.host contains an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise FixInputError("github.host port must be between 1 and 65535")
    return value


def normalize_target_repo(repo_url: str, configured_host: str) -> str:
    """Validate and canonicalize an HTTPS ``owner/repo`` GitHub URL."""
    configured_host = _normalize_github_authority(configured_host)
    parsed = urlparse(repo_url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise FixInputError("fix target must be a full HTTPS GitHub repository URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise FixInputError("fix target URL must not contain credentials, query, or fragment")
    expected = urlparse(f"https://{configured_host}")
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        raise FixInputError("fix target URL contains an invalid port") from exc
    if (
        parsed.hostname.lower() != (expected.hostname or "").lower()
        or parsed_port != expected.port
    ):
        raise FixInputError(
            f"fix target authority {parsed.netloc!r} does not match "
            f"github.host {configured_host!r}"
        )
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise FixInputError("fix target URL must identify exactly one owner/repository")
    owner, repo = parts
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not owner or not repo or not all(_REPO_PART_RE.fullmatch(p) for p in (owner, repo)):
        raise FixInputError("fix target owner/repository contains unsupported characters")
    host = configured_host.rstrip("/")
    return f"https://{host}/{owner}/{repo}"


def _locate_skill(config: AgentConfig) -> Path:
    candidates: list[Path] = []
    if config.fix.skill_dir:
        candidates.append(Path(config.fix.skill_dir).expanduser().resolve())
    candidates.extend(
        [
            Path("/home/appuser/.claude/skills/vulnhunter-fix"),
            Path.home() / ".claude" / "skills" / "vulnhunter-fix",
        ]
    )
    for candidate in candidates:
        skill_file = candidate / "SKILL.md"
        if skill_file.is_file():
            text = skill_file.read_text(encoding="utf-8")
            required_markers = (
                "## Automated agent execution profile",
                "VULNFIX_CONFIG_PATH",
            )
            if not all(marker in text for marker in required_markers):
                raise FileNotFoundError(
                    f"Installed skill at {candidate} predates automated fix mode. "
                    "Re-run the repository's install.sh before using --mode=fix."
                )
            return candidate.resolve()
    searched = ", ".join(str(path) for path in candidates)
    raise FileNotFoundError(
        "Installed vulnhunter-fix skill not found. Run the repository's "
        f"install.sh or set fix.skill_dir. Searched: {searched}"
    )


def _contained_run_dir(base: Path, repo_name: str) -> Path:
    base = base.expanduser().resolve()
    safe_repo = re.sub(r"[^A-Za-z0-9_.-]", "_", repo_name)[:80] or "repo"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    candidate = (base / f"{safe_repo}-{timestamp}-{uuid.uuid4().hex[:8]}").resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:  # pragma: no cover - defense in depth
        raise FixInputError("computed fix run directory escaped scratch base") from exc
    return candidate


def _report_entries(source: Path) -> tuple[int, int]:
    """Return regular-file count/bytes while rejecting links and special files."""
    file_count = 0
    byte_count = 0
    for root, dirnames, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for dirname in dirnames:
            path = root_path / dirname
            if _is_link_like(path):
                raise FixInputError(f"results report contains a link: {path}")
        for filename in filenames:
            path = root_path / filename
            if _is_link_like(path):
                raise FixInputError(f"results report contains a link: {path}")
            if not path.is_file():
                raise FixInputError(f"results report contains a non-regular file: {path}")
            file_count += 1
            byte_count += path.stat().st_size
    return file_count, byte_count


def _copy_report(source: Path, destination: Path, config: AgentConfig) -> Path:
    source = source.expanduser()
    if _is_link_like(source):
        raise FixInputError(f"results path must not be a link: {source}")
    source = source.resolve()
    if not source.is_dir() or not (source / "README.md").is_file():
        raise FixInputError(
            f"invalid results path {source}: expected a directory containing README.md"
        )
    count, size = _report_entries(source)
    if count > config.fix.max_report_files:
        raise FixInputError(
            f"results report has {count} files; limit is {config.fix.max_report_files}"
        )
    if size > config.fix.max_report_bytes:
        raise FixInputError(
            f"results report has {size} bytes; limit is {config.fix.max_report_bytes}"
        )
    shutil.copytree(
        source,
        destination,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git"),
    )
    # Recheck the copied tree to close the scan/copy race: copytree preserves rather
    # than follows links, and this second pass rejects any link or oversized file that
    # appeared after the source preflight.
    copied_count, copied_size = _report_entries(destination)
    if (
        copied_count > config.fix.max_report_files
        or copied_size > config.fix.max_report_bytes
    ):
        shutil.rmtree(destination, ignore_errors=True)
        raise FixInputError("results report changed while it was being staged")
    return destination.resolve()


def _select_report_root(checkout: Path) -> Path:
    if (checkout / "README.md").is_file():
        return checkout
    candidates = sorted(
        {
            readme.parent
            for readme in checkout.rglob("README.md")
            if "_VULNHUNT_RESULTS_" in readme.parent.name
        }
    )
    if not candidates:
        raise FixInputError(
            "results repository contains no *_VULNHUNT_RESULTS_*/README.md directory"
        )
    if len(candidates) != 1:
        rendered = ", ".join(str(path.relative_to(checkout)) for path in candidates[:8])
        raise FixInputError(
            "results repository is ambiguous; expected exactly one report directory, "
            f"found {len(candidates)} ({rendered})"
        )
    return candidates[0]


def stage_results(
    results_input: str,
    *,
    run_dir: Path,
    config: AgentConfig,
) -> Path:
    """Stage a local report or a single-report GitHub repository."""
    parsed = urlparse(results_input)
    if parsed.scheme in ("http", "https"):
        results_repo = normalize_target_repo(results_input, config.github.host)
        reports_token = get_github_token("reports", config)
        if not reports_token:
            raise FixInputError(
                "a remote results URL requires [github] reports_token"
            )
        checkout = shallow_clone(
            results_repo,
            run_dir / "results-source",
            timeout_seconds=config.fix.clone_timeout_seconds,
            re_clone=True,
            github_token=reports_token,
            github_host=config.github.host,
            token_in_environment=True,
        )
        source = _select_report_root(checkout)
    elif not parsed.scheme or (len(parsed.scheme) == 1 and results_input[1:3] in (":\\", ":/")):
        source = Path(results_input)
    else:
        raise FixInputError(
            "results input must be a local directory or full HTTPS GitHub repo URL"
        )
    return _copy_report(source, run_dir / "report", config)


def _runtime_config(
    config: AgentConfig,
    *,
    delivery_enabled: bool,
    test_policy: str,
    target_checkout: Path | None = None,
) -> dict:
    collaborators = []
    for value in config.fix.collaborators:
        username, _, role = value.rpartition(":")
        collaborators.append({"username": username, "role": role})
    return {
        "schema_version": "1.0",
        "execution": {
            "automated": True,
            "delivery_enabled": delivery_enabled,
            "test_policy": test_policy,
        },
        "github": {
            "host": config.github.host,
            "default_base_branch": config.fix.default_base_branch,
            "fork_org": config.fix.fork_org,
            "fork_prefix": config.fix.fork_prefix,
            "pr_draft": config.fix.pr_draft,
            "pr_labels": list(config.fix.pr_labels),
        },
        "verification": {
            "max_repair_attempts": config.fix.max_repair_attempts,
            "test_timeout_seconds": config.fix.test_timeout_seconds,
        },
        "behavior": {
            "fork_visibility": "private",
            "deliver_to_fork_only": True,
            "fallback_to_issue": True,
            "confirm_before_push": False,
            "work_dir": "./work",
            "target_checkout": str(target_checkout) if target_checkout else None,
            "keep_workdir": config.fix.keep_workdir,
        },
        "collaborators": collaborators,
    }


def _validate_semantics(
    payload: dict,
    *,
    target_repo: str,
    report: Path,
    delivery_enabled: bool,
) -> str:
    """Validate relationships intentionally clearer in Python than JSON Schema."""
    if payload.get("target_repo") != target_repo:
        return "disposition target_repo does not match the requested repository"
    if Path(str(payload.get("report_path", ""))).resolve() != report.resolve():
        return "disposition report_path does not match the staged report"
    if payload.get("delivery_enabled") is not delivery_enabled:
        return "disposition delivery_enabled does not match the invocation policy"

    target = urlparse(target_repo)

    def artifact_url_error(value: object, kind: str) -> str:
        if value is None:
            return ""
        candidate = urlparse(str(value))
        try:
            candidate_port = candidate.port
        except ValueError:
            return f"disposition {kind} contains an invalid port"
        if (
            candidate.scheme != "https"
            or candidate.hostname != target.hostname
            or candidate_port != target.port
            or candidate.username
            or candidate.password
            or candidate.query
            or candidate.fragment
        ):
            return f"disposition {kind} is not a canonical URL on the target GitHub host"
        parts = [part for part in candidate.path.strip("/").split("/") if part]
        route = "pull" if kind == "pr_url" else "issues"
        if len(parts) != 4 or parts[2] != route or not parts[3].isdigit():
            return f"disposition {kind} is not a canonical GitHub {route} URL"
        return ""

    error = artifact_url_error(payload.get("tracking_issue_url"), "tracking_issue_url")
    if error:
        return error

    findings = payload.get("findings", [])
    ids = [item.get("vuln_id") for item in findings]
    if len(ids) != len(set(ids)):
        return "disposition contains duplicate finding IDs"

    checkpoints = payload.get("phase_checkpoints", [])
    checkpoint_phases = [item.get("phase") for item in checkpoints]
    if len(checkpoint_phases) != len(set(checkpoint_phases)):
        return "disposition contains duplicate phase checkpoints"
    validated = {
        item.get("phase")
        for item in checkpoints
        if item.get("status") == "validated"
    }
    run_status = payload.get("run_status")
    if run_status in ("COMPLETED", "DRY_RUN") and validated != _FULL_PHASES:
        missing = sorted(_FULL_PHASES - validated)
        return f"successful disposition lacks validated phase checkpoints: {missing}"
    if run_status == "DRY_RUN" and delivery_enabled:
        return "DRY_RUN disposition cannot have delivery enabled"
    if run_status == "COMPLETED" and not delivery_enabled:
        return "delivery-disabled invocation must use DRY_RUN, not COMPLETED"
    if run_status == "NO_FINDINGS" and findings:
        return "NO_FINDINGS disposition must have an empty findings array"
    if run_status == "NO_FINDINGS" and "parse" not in validated:
        return "NO_FINDINGS disposition requires a validated parse checkpoint"
    if run_status == "PARTIAL" and (not findings or "parse" not in validated):
        return "PARTIAL disposition requires parsed findings and a validated parse checkpoint"
    if run_status == "COMPLETED" and not findings:
        return "COMPLETED disposition must contain at least one finding"

    for finding in findings:
        for field in ("issue_url", "pr_url"):
            error = artifact_url_error(finding.get(field), field)
            if error:
                return f"{finding.get('vuln_id')}: {error}"
        if finding.get("status", "").startswith("VERIFIED") and finding.get(
            "gate_status"
        ) != "PASS":
            return (
                f"{finding.get('vuln_id')} is VERIFIED but its delivery gates "
                "did not pass"
            )
    return ""


async def run_fix(
    *,
    config: AgentConfig,
    target_repo: str,
    results_input: str,
    scratch_base_dir: Path | None,
    no_post: bool,
    test_policy_override: str | None,
    model_override: str | None,
    target_checkout_override: Path | None = None,
) -> int:
    """Run one unattended fork-mode remediation and return its process exit code.

    ``target_checkout_override``, when supplied, points at a target-repo
    checkout a trusted caller already cloned (with its own GitHub
    credential) *before* this process started — for example the host-side
    control plane in ``scripts/run_mythos_fix_sandbox.sh``, which clones
    the target outside the gVisor container and then streams only the
    checkout in. When set, this run performs no GitHub clone of its own and
    requires no ``[github] scan_token`` at all: the credential never has to
    exist in whatever process/container is about to hand Bash to the model.
    Only meaningful with ``no_post=True`` — a pre-staged checkout carries no
    push/fork/PR credential, so delivery is impossible either way.
    """
    try:
        target_repo = normalize_target_repo(target_repo, config.github.host)
    except FixInputError as exc:
        logger.error("Invalid fix target: %s", exc)
        return _EXIT_BAD_ARGS

    if target_checkout_override is not None and not no_post:
        logger.error(
            "target_checkout_override requires no_post=True: a pre-staged "
            "checkout carries no credential to deliver with"
        )
        return _EXIT_BAD_ARGS

    github_token = ""
    if target_checkout_override is None:
        github_token = get_github_token("scan", config)
        if not github_token:
            logger.error(
                "[github] scan_token is required for --mode=fix (target/fork/PR/issue "
                "access) unless a pre-staged target checkout is supplied"
            )
            return _EXIT_AUTH_FAILURE

    try:
        skill_dir = _locate_skill(config)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return _EXIT_FAILURE

    scratch_root = (
        scratch_base_dir or Path(config.fix.scratch_base_dir)
    ).expanduser().resolve()
    run_dir = _contained_run_dir(scratch_root, target_repo.rsplit("/", 1)[-1])
    run_dir.mkdir(parents=True, exist_ok=False)
    out_dir = run_dir / "out"
    out_dir.mkdir()
    logger.info("Fix run scratch: %s", run_dir)

    try:
        report = stage_results(results_input, run_dir=run_dir, config=config)
    except (FixInputError, RuntimeError, OSError) as exc:
        logger.error("Could not stage fix report: %s", exc)
        return _EXIT_FAILURE

    target_checkout: Path | None = None
    if target_checkout_override is not None:
        resolved_override = target_checkout_override.expanduser()
        if _is_link_like(resolved_override):
            logger.error(
                "--target-checkout must not be a link: %s", resolved_override
            )
            return _EXIT_BAD_ARGS
        resolved_override = resolved_override.resolve()
        if not resolved_override.is_dir() or not (resolved_override / ".git").exists():
            logger.error(
                "--target-checkout must be an existing git checkout directory: %s",
                resolved_override,
            )
            return _EXIT_BAD_ARGS
        target_checkout = resolved_override
        logger.info("Using pre-staged target checkout: %s", target_checkout)
    elif no_post:
        # A dry-run model receives no GitHub credential. Stage the only private
        # network input it needs before starting the SDK session, using a
        # process-local credential helper so the token never enters argv/remotes.
        try:
            target_checkout = shallow_clone(
                target_repo,
                run_dir / "work",
                timeout_seconds=config.fix.clone_timeout_seconds,
                re_clone=True,
                github_token=github_token,
                github_host=config.github.host,
                token_in_environment=True,
            ).resolve()
        except (RuntimeError, OSError) as exc:
            logger.error("Could not stage dry-run target checkout: %s", exc)
            return _EXIT_FAILURE

    test_policy = test_policy_override or config.fix.test_policy
    runtime_config_path = run_dir / "vulnfix-runtime-config.json"
    runtime_config_path.write_text(
        json.dumps(
            _runtime_config(
                config,
                delivery_enabled=not no_post,
                test_policy=test_policy,
                target_checkout=target_checkout,
            ),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    staged_schema = run_dir / "fix_disposition.schema.json"
    staged_schema.write_text(fix_schema_text(), encoding="utf-8")

    prompt = build_kickoff_prompt(
        target_repo=target_repo,
        report=report,
        runtime_config=runtime_config_path,
        out_dir=out_dir,
        schema_path=staged_schema,
        delivery_enabled=not no_post,
        test_policy=test_policy,
        target_checkout=target_checkout,
    )
    token_manager = make_token_manager(config, name="fix")
    result = await run_fix_session(
        config=config,
        auth_token=token_manager.get_valid_token(),
        github_token=github_token,
        cwd=run_dir,
        out_dir=out_dir,
        prompt=prompt,
        log_path=run_dir / "agent.log",
        runtime_config=runtime_config_path,
        skill_dir=skill_dir,
        model_override=model_override,
        delivery_enabled=not no_post,
    )
    if result.kind is not FixOutputKind.DISPOSITION or result.parsed is None:
        logger.error("Fix session produced no valid disposition: %s", result.error_detail)
        print(f"Fix run:  FAILED ({result.error_detail})")
        print(f"Scratch:  {run_dir}")
        return _EXIT_FAILURE

    semantic_error = _validate_semantics(
        result.parsed,
        target_repo=target_repo,
        report=report,
        delivery_enabled=not no_post,
    )
    if semantic_error:
        logger.error("Fix disposition semantic validation failed: %s", semantic_error)
        print(f"Fix run:  FAILED ({semantic_error})")
        print(f"Scratch:  {run_dir}")
        return _EXIT_FAILURE

    status = str(result.parsed["run_status"])
    print(f"Fix run:  {status}")
    print(f"Findings: {len(result.parsed['findings'])}")
    print(f"Result:   {result.output_path}")
    print(f"Scratch:  {run_dir}")
    if result.parsed.get("tracking_issue_url"):
        print(f"Tracking: {result.parsed['tracking_issue_url']}")

    if status in _SUCCESS_STATUSES:
        return _EXIT_OK
    if status == "PARTIAL":
        return _EXIT_PARTIAL
    return _EXIT_FAILURE
