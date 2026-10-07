"""Lever Postings API (pública e documentada):
https://github.com/lever/postings-api — 1 requisição por empresa."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator

from .base import RawJob
from .http import PoliteClient

log = logging.getLogger(__name__)
API = "https://api.lever.co/v0/postings/{token}"
_WORKPLACE = {"remote": "REMOTE", "hybrid": "HYBRID", "onsite": "ONSITE", "on-site": "ONSITE"}


class LeverCollector:
    name = "lever"

    def __init__(self, client: PoliteClient, boards: list[dict[str, str]]) -> None:
        self.client = client
        self.boards = boards

    def collect(self) -> Iterator[RawJob]:
        for board in self.boards:
            data = self.client.get_json(API.format(token=board["token"]), params={"mode": "json"})
            if not isinstance(data, list):
                log.warning("lever: board %s vazio ou inexistente", board["token"])
                continue
            for item in data:
                yield self._to_raw(item, board)

    @staticmethod
    def _to_raw(item: dict[str, Any], board: dict[str, str]) -> RawJob:
        cats = item.get("categories") or {}
        # Monta o HTML completo: abertura + listas (Requisitos, Diferenciais...) + adicional
        parts = [item.get("description") or ""]
        for lst in item.get("lists") or []:
            parts.append(f"<h3>{lst.get('text', '')}</h3><ul>{lst.get('content', '')}</ul>")
        parts.append(item.get("additional") or "")
        created = item.get("createdAt")
        return RawJob(
            source="lever",
            external_id=item["id"],
            url=item["hostedUrl"],
            apply_url=item.get("applyUrl"),
            apply_type="lever_form",
            company=board.get("company") or board["token"],
            title=(item.get("text") or "").strip(),
            location=cats.get("location") or ", ".join(cats.get("allLocations") or []) or None,
            modality_hint=_WORKPLACE.get((item.get("workplaceType") or "").lower()),
            posted_at=datetime.fromtimestamp(created / 1000, tz=timezone.utc) if created else None,
            description_html="\n".join(parts),
            department=cats.get("department") or cats.get("team"),
            raw={"id": item["id"], "categories": cats, "country": item.get("country"),
                 "workplaceType": item.get("workplaceType")},
        )
