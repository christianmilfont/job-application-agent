"""Carrega os YAMLs de config/ (perfil, fontes, taxonomia, scoring)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .settings import get_settings


def _load(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass(frozen=True)
class AppConfig:
    profile: dict[str, Any]
    sources: dict[str, Any]
    taxonomy: dict[str, Any]
    scoring: dict[str, Any]


def load_config(config_dir: Path | None = None) -> AppConfig:
    d = config_dir or get_settings().config_dir
    return AppConfig(
        profile=_load(d / "profile.yaml"),
        sources=_load(d / "sources.yaml"),
        taxonomy=_load(d / "skills_taxonomy.yaml"),
        scoring=_load(d / "scoring.yaml"),
    )
