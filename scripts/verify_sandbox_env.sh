[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "vuln-hunter-agent"
dynamic = ["version"]
description = "VulnHunter runtime agent: drives the /vulnhunt Claude Code skill, publishes results, and files findings as GitHub issues."
requires-python = ">=3.12"
readme = "README.md"
dependencies = [
    "claude-agent-sdk",
    "httpx",
    "jsonschema>=4.18",
    "tenacity",
    "certifi",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-cov", "pytest-asyncio", "syrupy", "respx", "hypothesis", "anyio"]

[project.urls]
homepage = "https://github.com/SecurityUniversalOrg/ai-agent-harness"

[tool.setuptools.packages.find]
where = ["."]
include = ["agent*", "vulnhunter*"]

[tool.setuptools.dynamic]
version = { attr = "vulnhunter.__version__" }

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
asyncio_mode = "auto"
filterwarnings = [
    "error",
    "ignore::DeprecationWarning:claude_agent_sdk.*",
    "ignore::DeprecationWarning:hypothesis.*",
    "ignore::RuntimeWarning:runpy",
    "ignore::pytest.PytestUnraisableExceptionWarning",
]

[tool.coverage.run]
branch = true
source = ["agent", "vulnhunter"]
relative_files = true

[tool.coverage.report]
show_missing = true
skip_covered = false
exclude_lines = [
    "pragma: no cover",
    "raise NotImplementedError",
    "if __name__ == .__main__.:",
]