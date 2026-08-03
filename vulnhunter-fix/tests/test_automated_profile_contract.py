"""Sync guards for the vulnhunter-agent automated remediation profile."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_root_skill_defines_explicit_automated_profile() -> None:
    skill = _read("SKILL.md")
    assert "## Automated agent execution profile" in skill
    assert "VULNFIX_AUTOMATED=1" in skill
    assert "VULNFIX_CONFIG_PATH" in skill
    assert "execution.delivery_enabled=false" in skill
    assert "CANNOT_AUTO_FIX" in skill
    assert "checkpoint entry" in skill


def test_plan_auto_advances_only_after_validated_checkpoint() -> None:
    plan = _read("prompts/plan_fork.md")
    assert "VULNFIX_AUTOMATED=1" in plan
    assert "validate and record the Plan checkpoint" in plan
    assert "structured human-required outcome" in plan


def test_dry_run_contract_spans_implementation_and_delivery() -> None:
    implement = _read("prompts/implement.md")
    deliver = _read("prompts/deliver.md")
    deliver_words = " ".join(deliver.split())
    assert "Automated local dry-run" in implement
    assert "execution.delivery_enabled=false" in implement
    assert "behavior.target_checkout" in implement
    assert "Do not clone/fetch" in implement and "run `gh`" in implement
    assert "### Automated local dry-run" in deliver
    assert "all seven applicable local delivery gates" in deliver_words
    assert "Do not execute any mutating `gh` command" in deliver_words


def test_automated_preflight_does_not_require_second_claude_binary() -> None:
    preflight = _read("scripts/preflight.py")
    assert 'os.environ.get("VULNFIX_AUTOMATED") == "1"' in preflight
    assert "Claude Agent SDK session (automated profile)" in preflight


def test_dry_run_skips_network_version_and_gh_auth_preflights() -> None:
    skill = _read("SKILL.md")
    assert "skip this network version check" in skill
    assert "skip Step 1b" in skill
    assert "execution.delivery_enabled=false" in skill
