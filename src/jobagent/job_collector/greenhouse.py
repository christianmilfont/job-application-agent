"""Greenhouse Job Board API (pública e documentada):
https://developers.greenhouse.io/job-board.html — 1 requisição por empresa."""
from __future__ import annotations

import html
import logging
from datetime import datetime
from typing import Any, Iterator

from .base import RawJob
from .http import PoliteClient

log = logging.getLogger(__name__)
API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


class GreenhouseCollector:
    name = "greenhouse"

    def __init__(self, client: PoliteClient, boards: list[dict[str, str]]) -> None:
        self.client = client
        self.boards = boards

    def collect(self) -> Iterator[RawJob]:
        for board in self.boards:
            data = self.client.get_json(API.format(token=board["token"]), params={"content": "true"})
            if not data:
                log.warning("greenhouse: board %s vazio ou inexistente", board["token"])
                continue
            for item in data.get("jobs", []):
                yield self._to_raw(item, board)

    @staticmethod
    def _to_raw(item: dict[str, Any], board: dict[str, str]) -> RawJob:
        departments = ", ".join(d.get("name", "").strip() for d in item.get("departments") or [])
        posted = item.get("first_published") or item.get("updated_at")
        return RawJob(
            source="greenhouse",
            external_id=str(item["id"]),
            url=item["absolute_url"],
            apply_url=item["absolute_url"],
            apply_type="greenhouse_form",
            company=board.get("company") or item.get("company_name") or board["token"],
            title=item.get("title", "").strip(),
            location=(item.get("location") or {}).get("name"),
            posted_at=datetime.fromisoformat(posted) if posted else None,
            description_html=html.unescape(item.get("content") or ""),
            department=departments or None,
            language=item.get("language"),
            raw={k: item.get(k) for k in ("id", "internal_job_id", "requisition_id", "updated_at",
                                          "first_published", "location", "metadata")},
        )
