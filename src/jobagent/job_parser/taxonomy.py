from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


def _literal(alias: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w]){re.escape(alias)}(?![\w])", re.IGNORECASE)


@dataclass
class Entry:
    id: str
    name: str
    patterns: list[re.Pattern[str]]
    implied_by: list[str] = field(default_factory=list)
    satisfied_by: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    synonyms: bool = False


class Taxonomy:
    def __init__(self, data: dict[str, Any]) -> None:
        self.skills = {k: self._entry(k, v) for k, v in (data.get("skills") or {}).items()}
        self.certs = {k: self._entry(k, v) for k, v in (data.get("certifications") or {}).items()}

    @staticmethod
    def _entry(key: str, v: dict[str, Any]) -> Entry:
        patterns = [_literal(a) for a in v.get("aliases", [])]
        patterns += [re.compile(r) for r in v.get("regex", [])]
        return Entry(key, v.get("name", key), patterns, v.get("implied_by", []), v.get("satisfied_by", []),
                     [str(a) for a in v.get("aliases", [])], bool(v.get("synonyms", False)))

    @staticmethod
    def _find(entries: dict[str, Entry], text: str) -> set[str]:
        return {e.id for e in entries.values() if any(p.search(text) for p in e.patterns)}

    def find_skills(self, text: str) -> set[str]:
        return self._find(self.skills, text)

    def find_skill_literals(self, text: str) -> dict[str, set[str]]:
        """skill_id -> termos exatos encontrados (ex.: messaging -> {"Kafka"})."""
        out: dict[str, set[str]] = {}
        for e in self.skills.values():
            for p in e.patterns:
                for m in p.finditer(text):
                    out.setdefault(e.id, set()).add(m.group(0))
        return out

    def canonical_terms(self, skill_id: str) -> set[str]:
        """Grafias aceitas sem evidência literal no perfil (nome base; sinônimos se synonyms: true)."""
        e = self.skills.get(skill_id)
        if not e:
            return set()
        base = re.split(r"\s*\(|\s+/\s+", e.name)[0].strip().lower()
        if e.synonyms:
            return {base, e.name.lower()} | {a.lower() for a in e.aliases}
        if "(" in e.name or " / " in e.name:
            return {base}   # nome lista exemplos (ex.: Mensageria (Kafka/SQS)): só a categoria é canônica
        return {base} | ({e.aliases[0].lower()} if e.aliases else set())

    def find_certs(self, text: str) -> set[str]:
        return self._find(self.certs, text)

    def name(self, skill_id: str) -> str:
        e = self.skills.get(skill_id) or self.certs.get(skill_id)
        return e.name if e else skill_id
