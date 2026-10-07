from __future__ import annotations

from typing import Any

from .base import Collector, RawJob
from .greenhouse import GreenhouseCollector
from .http import AccessDenied, BudgetExceeded, PoliteClient
from .lever import LeverCollector
from .manual import ManualImportCollector


def build_collectors(sources: dict[str, Any], client: PoliteClient,
                     only: str | None = None, company: str | None = None) -> list[Collector]:
    """Monta os coletores habilitados em config/sources.yaml."""
    factories = {"greenhouse": GreenhouseCollector, "lever": LeverCollector}
    result: list[Collector] = []
    for name, factory in factories.items():
        cfg = sources.get(name) or {}
        if not cfg.get("enabled") or (only and only != name):
            continue
        boards = cfg.get("boards") or []
        if company:
            boards = [b for b in boards if company.lower() in (b["token"] + b.get("company", "")).lower()]
        if boards:
            result.append(factory(client, boards))
    return result


__all__ = ["AccessDenied", "BudgetExceeded", "Collector", "GreenhouseCollector", "LeverCollector",
           "ManualImportCollector", "PoliteClient", "RawJob", "build_collectors"]
