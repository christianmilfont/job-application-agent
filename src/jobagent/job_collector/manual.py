"""Importação manual (YAML/JSON) — caminho para vagas de LinkedIn, Gupy, indicações etc.
Você copia a vaga; o sistema não acessa essas plataformas."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import yaml

from .base import RawJob

REQUIRED = ("company", "title", "url", "description")


class ManualImportCollector:
    name = "manual"

    def __init__(self, path: Path) -> None:
        self.path = path

    def collect(self) -> Iterator[RawJob]:
        text = self.path.read_text(encoding="utf-8")
        data = json.loads(text) if self.path.suffix.lower() == ".json" else yaml.safe_load(text)
        items = data.get("jobs", data) if isinstance(data, dict) else data
        for i, item in enumerate(items or []):
            missing = [k for k in REQUIRED if not item.get(k)]
            if missing:
                raise ValueError(f"vaga #{i + 1} em {self.path.name}: campos ausentes {missing}")
            yield self._to_raw(item)

    @staticmethod
    def _to_raw(item: dict[str, Any]) -> RawJob:
        posted = item.get("posted_at")
        if isinstance(posted, str):
            posted = datetime.fromisoformat(posted)
        elif isinstance(posted, date) and not isinstance(posted, datetime):
            posted = datetime(posted.year, posted.month, posted.day)
        modality = (item.get("modality") or "").upper() or None
        return RawJob(
            source=item.get("source", "manual"),
            external_id=str(item["id"]) if item.get("id") else None,
            url=item["url"],
            apply_url=item.get("apply_url") or item["url"],
            apply_type=item.get("apply_type", "external"),
            company=item["company"],
            title=item["title"],
            location=item.get("location"),
            modality_hint=modality if modality in {"REMOTE", "HYBRID", "ONSITE"} else None,
            posted_at=posted,
            description_text=item["description"],
            recruiter=item.get("recruiter"),
            raw={"imported_from": item.get("source", "manual")},
        )
