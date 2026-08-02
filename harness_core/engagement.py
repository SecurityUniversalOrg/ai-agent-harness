from __future__ import annotations

from pathlib import Path


def load_engagement_context(path: str | Path | None, default: str) -> str:
    if not path:
        return default
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"--engagement-context file not found: {p}")
    text = p.read_text().strip()
    return text or default
