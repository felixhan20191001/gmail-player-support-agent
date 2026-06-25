"""Project-root path helpers for the standalone gmail agent."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def project_root() -> Path:
    """Return the gmailAgent project root directory."""

    override = os.getenv("PLAYER_SUPPORT_AGENT_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists():
            return parent
    return here.parents[2]


def resolve_project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return project_root() / path


def default_config_path() -> Path:
    return project_root() / "config" / "config.local.toml"


def default_config_example_path() -> Path:
    return project_root() / "config" / "config.example.toml"


def default_knowledge_rules_path() -> Path:
    return project_root() / "knowledge" / "generic_support_rules.toml"


def default_templates_dir() -> Path:
    return project_root() / "templates" / "replies"


def default_eval_fixtures_dir() -> Path:
    return project_root() / "eval_fixtures"


def default_var_dir() -> Path:
    return project_root() / "var"