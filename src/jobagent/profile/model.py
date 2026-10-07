"""Modelo tipado do profile.yaml + índice derivado (evidência de cada skill).

A evidência é calculada, não declarada: uma skill citada no stack/highlights de um projeto
ganha evidência "project" com o nome do projeto; uma certificação dá "certification".
Isso permite ao currículo dizer "Kubernetes (projeto ToggleMaster)" e nunca
"experiência com Kubernetes" sem base.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..job_parser import Taxonomy


class Personal(BaseModel):
    name: str = ""
    headline: str = ""
    location: str = ""
    email: str = ""
    phone: str = ""
    links: dict[str, str] = Field(default_factory=dict)


class Education(BaseModel):
    id: str
    degree: str
    institution: str
    level: Literal["graduacao", "pos_graduacao", "tecnico", "curso"]
    status: Literal["completed", "in_progress"]


class Certification(BaseModel):
    id: str
    name: str
    issuer: str = ""
    kind: Literal["certification", "training"] = "certification"
    skills: list[str] = Field(default_factory=list)


class Achievement(BaseModel):
    id: str
    text: str
    skills: list[str] = Field(default_factory=list)


class PlannedItem(BaseModel):
    text: str
    keywords: list[str] = Field(default_factory=list)


class Project(BaseModel):
    id: str
    name: str
    context: str = ""
    status: Literal["completed", "in_progress", "unknown"] = "unknown"
    link: str = ""
    summary: str
    stack: list[str] = Field(default_factory=list)
    highlights: list[str] = Field(default_factory=list)
    planned: list[PlannedItem] = Field(default_factory=list)


class Experience(BaseModel):
    company: str
    role: str
    start: str = ""
    end: str = ""
    highlights: list[str] = Field(default_factory=list)
    stack: list[str] = Field(default_factory=list)


class SkillDecl(BaseModel):
    evidence: list[str] = Field(default_factory=lambda: ["declared"])


class Language(BaseModel):
    name: str
    level: str = ""


class Salary(BaseModel):
    clt_monthly: int | None = None
    pj_monthly: int | None = None
    note: str = ""


class ApplicationPreferences(BaseModel):
    salary: Salary = Field(default_factory=Salary)
    availability: str = ""
    work_models: list[Literal["remote", "hybrid", "onsite"]] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    relocation: bool | None = None


class Profile(BaseModel):
    personal: Personal = Field(default_factory=Personal)
    target_seniority: list[str] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)
    achievements: list[Achievement] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: dict[str, SkillDecl] = Field(default_factory=dict)
    languages: list[Language] = Field(default_factory=list)
    application_preferences: ApplicationPreferences = Field(default_factory=ApplicationPreferences)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Profile:
        return cls.model_validate(data or {})


@dataclass
class Evidence:
    kinds: set[str] = field(default_factory=set)        # declared, project, certification, training, ...
    sources: list[str] = field(default_factory=list)    # "projeto ToggleMaster", "certificação AZ-104"


_KIND_LABEL = {"certification": "certificação", "training": "treinamento", "academic": "formação acadêmica",
               "professional": "experiência profissional", "declared": "declarado no perfil", "project": "projeto"}


class ProfileIndex:
    """Visão derivada do perfil usada por scorer, seletor, validador e ATS."""

    def __init__(self, profile: Profile, taxonomy: Taxonomy) -> None:
        self.profile = profile
        self.tax = taxonomy
        self.evidence: dict[str, Evidence] = {}
        self.project_skills: dict[str, set[str]] = {}
        self.unmapped_stack: dict[str, list[str]] = {}
        self.unknown_skill_ids: list[str] = []

        for sid, decl in profile.skills.items():
            if sid not in taxonomy.skills:
                self.unknown_skill_ids.append(sid)
            self._add(sid, decl.evidence, None)

        for cert in profile.certifications:
            for sid in cert.skills:
                self._add(sid, [cert.kind], f"{_KIND_LABEL[cert.kind]} {cert.name}")

        for exp in profile.experience:
            for sid in taxonomy.find_skills(" ; ".join(exp.stack + exp.highlights)):
                self._add(sid, ["professional"], f"{exp.role} — {exp.company}")

        for proj in profile.projects:
            found: set[str] = set()
            unmapped = []
            for item in proj.stack:
                hits = taxonomy.find_skills(item)
                if not hits:
                    unmapped.append(item)
                found |= hits
            found |= taxonomy.find_skills(" ; ".join(proj.highlights))
            # Itens planejados NÃO viram evidência
            self.project_skills[proj.id] = found
            if unmapped:
                self.unmapped_stack[proj.id] = unmapped
            for sid in found:
                self._add(sid, ["project"], f"projeto {proj.name}")

        for ach in profile.achievements:
            for sid in ach.skills:
                self._add(sid, ["academic"], None)

        # Texto-fonte para rótulos de exibição (somente o que já foi implementado)
        self._source_text = " ; ".join(
            [s for pr in profile.projects for s in pr.stack + pr.highlights]
            + [c.name for c in profile.certifications] + [a.text for a in profile.achievements])
        self._display_cache: dict[str, str] = {}
        evidence_text = self._source_text + " ; " + " ; ".join(
            [pr.summary for pr in profile.projects] + [e.degree for e in profile.education])
        self._evidence_text_low = evidence_text.lower()
        self.planned_skills: dict[str, set[str]] = {
            pr.id: taxonomy.find_skills(" ; ".join(i.text for i in pr.planned)) for pr in profile.projects}

    def literal_allowed(self, skill_id: str, term: str) -> bool:
        """Termo exato pode ser usado? Só se aparece no perfil ou é a grafia canônica da skill.
        Ex.: "SQS" ok (está no ToggleMaster); "Kafka" não, mesmo sendo da mesma família (mensageria)."""
        low = term.lower()
        return low in self._evidence_text_low or low in self.tax.canonical_terms(skill_id)

    def _add(self, sid: str, kinds: list[str], source: str | None) -> None:
        ev = self.evidence.setdefault(sid, Evidence())
        ev.kinds.update(kinds)
        if source and source not in ev.sources:
            ev.sources.append(source)

    # ------------------------------------------------------------ consultas
    @property
    def owned_skills(self) -> set[str]:
        return set(self.evidence)

    @property
    def has_professional_experience(self) -> bool:
        return bool(self.profile.experience)

    def covers(self, skill_id: str, within: set[str] | None = None) -> list[str]:
        """IDs do perfil que cobrem a skill pedida (ela mesma ou via implied_by)."""
        pool = self.owned_skills if within is None else within
        if skill_id in pool:
            return [skill_id]
        entry = self.tax.skills.get(skill_id)
        return [s for s in (entry.implied_by if entry else []) if s in pool]

    @property
    def allowed_skills(self) -> set[str]:
        """Skills que podem aparecer no texto: as do perfil + as implicadas por elas."""
        allowed = set(self.owned_skills)
        for sid, entry in self.tax.skills.items():
            if any(s in self.owned_skills for s in entry.implied_by):
                allowed.add(sid)
        return allowed

    def allowed_for_project(self, project_id: str) -> set[str]:
        own = self.project_skills.get(project_id, set())
        allowed = set(own)
        for sid, entry in self.tax.skills.items():
            if any(s in own for s in entry.implied_by):
                allowed.add(sid)
        return allowed

    def owned_cert_ids(self) -> set[str]:
        return {c.id for c in self.profile.certifications}

    def describe(self, skill_id: str) -> str | None:
        """Texto de evidência para MATCH: 'projeto ToggleMaster; certificação AZ-104'."""
        covering = self.covers(skill_id)
        if not covering:
            return None
        if covering[0] != skill_id:
            return "via " + ", ".join(self.tax.name(s) for s in covering)
        ev = self.evidence[skill_id]
        parts = list(ev.sources)
        for k in ("declared", "academic", "professional"):
            if k in ev.kinds and not any(_KIND_LABEL[k] in p for p in parts):
                parts.append(_KIND_LABEL[k])
        return "; ".join(parts)

    def display_name(self, skill_id: str) -> str:
        """Rótulo para o currículo usando só termos que aparecem no perfil.
        A taxonomia diz "Mensageria (Kafka/RabbitMQ/SQS)"; o currículo deve dizer "Mensageria (SQS)"."""
        if skill_id in self._display_cache:
            return self._display_cache[skill_id]
        entry = self.tax.skills.get(skill_id)
        if entry is None:
            return skill_id
        if "(" not in entry.name and " / " not in entry.name:
            self._display_cache[skill_id] = entry.name     # nome simples: usa como está
            return entry.name
        base = re.split(r"\s*\(|\s+/\s+", entry.name)[0].strip()
        literals: list[str] = []
        for pattern in entry.patterns:
            for m in pattern.finditer(self._source_text):
                lit = m.group(0)
                low = lit.lower()
                if low in base.lower() or base.lower() in low or any(low == x.lower() for x in literals):
                    continue
                literals.append(lit)
        label = f"{base} ({', '.join(literals[:3])})" if literals else base
        self._display_cache[skill_id] = label
        return label

    def project(self, project_id: str) -> Project | None:
        return next((p for p in self.profile.projects if p.id == project_id), None)

    def certification(self, cert_id: str) -> Certification | None:
        return next((c for c in self.profile.certifications if c.id == cert_id), None)
