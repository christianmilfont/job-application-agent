from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Protocol


@dataclass
class RawJob:
    """Vaga como veio da fonte, antes do parser."""
    source: str
    external_id: str | None
    url: str
    company: str
    title: str
    description_html: str = ""
    description_text: str = ""
    location: str | None = None
    modality_hint: str | None = None     # REMOTE | HYBRID | ONSITE, se a fonte informar
    posted_at: datetime | None = None
    apply_url: str | None = None
    apply_type: str | None = None
    recruiter: str | None = None
    department: str | None = None
    language: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class Collector(Protocol):
    name: str

    def collect(self) -> Iterator[RawJob]: ...
