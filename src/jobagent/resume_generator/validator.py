"""Validador de veracidade: tudo que o currículo afirma precisa existir no profile.yaml.

Checagens (texto livre = headline, summary e bullets de projeto):
- IDs de projeto/skill/certificação existem no perfil;
- tecnologias citadas no texto pertencem ao perfil (nos bullets: ao PRÓPRIO projeto);
- certificações citadas são suas;
- todo número citado aparece no perfil (bloqueia métricas e resultados inventados);
- sem experiência profissional no perfil, frases como "atuei como", "anos de experiência",
  "em produção", "liderei", "sênior" são proibidas;
- formação em andamento não pode ser descrita como concluída;
- funcionalidades planejadas só podem aparecer marcadas como planejadas/futuras.
A justificativa (rationale) não entra no currículo e pode citar requisitos que você não tem.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..profile import ProfileIndex
from .schema import ResumeDraft

_NUMBER = re.compile(r"(?<![\w-])\d+(?:[.,]\d+)?%?")
_SENTENCE = re.compile(r"[^.;\n]+")
_PLANNED_MARKER = re.compile(
    r"planejad|em desenvolvimento|futur|roadmap|planned|upcoming|em evolu|pr[oó]ximos passos|future|previst", re.I)

_NO_EXPERIENCE_FORBIDDEN = [
    (re.compile(r"experi[eê]ncia (profissional|de mercado|corporativa|s[oó]lida|comprovada|em produ)", re.I),
     "afirma experiência profissional"),
    (re.compile(r"\banos? de experi[eê]ncia|years? of (professional )?experience|\bprofessional experience", re.I),
     "afirma anos de experiência"),
    (re.compile(r"\b(atuei|trabalhei|liderei|gerenciei|coordenei|fui respons[aá]vel)\b", re.I),
     "descreve atuação profissional"),
    (re.compile(r"\b(led|managed|worked as|was responsible)\b", re.I), "descreve atuação profissional"),
    (re.compile(r"\bem produ[cç][aã]o\b|ambientes? (de|em) produ[cç][aã]o|\bin production\b|production environment", re.I),
     "afirma uso em produção"),
    (re.compile(r"\b(s[eê]nior|especialista|expert|specialist)\b", re.I), "atribui senioridade/especialidade"),
    (re.compile(r"\b(reduzi|aumentei|economizei|otimizei em|melhorei em|milhares de usu[aá]rios|clientes reais)\b", re.I),
     "afirma resultado não registrado"),
]


@dataclass
class ValidationReport:
    violations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


class ResumeValidator:
    def __init__(self, idx: ProfileIndex) -> None:
        self.idx = idx
        p = idx.profile
        # Fonte de números permitidos: o perfil inteiro, exceto contato pessoal
        dump = p.model_dump(exclude={"personal": {"email", "phone"}})
        self._profile_text = repr(dump)
        self._numbers = set(_NUMBER.findall(self._profile_text))
        self._project_numbers = {pr.id: set(_NUMBER.findall(repr(pr.model_dump()))) for pr in p.projects}
        self._planned = [(pr.name, kw.lower()) for pr in p.projects for item in pr.planned for kw in item.keywords]
        self._in_progress_postgrad = any(e.level == "pos_graduacao" and e.status == "in_progress" for e in p.education)

    def validate(self, d: ResumeDraft) -> ValidationReport:
        r = ValidationReport()
        idx, tax = self.idx, self.idx.tax

        # --- referências por ID
        allowed = idx.allowed_skills
        for g in d.skill_groups:
            for sid in g.skill_ids:
                if sid not in allowed:
                    r.violations.append(f"Competência '{sid}' não existe no perfil")
        owned_certs = idx.owned_cert_ids()
        for cid in d.certification_ids:
            if cid not in owned_certs:
                r.violations.append(f"Certificação '{cid}' não existe no perfil")
        seen = set()
        for pe in d.projects:
            if idx.project(pe.project_id) is None:
                r.violations.append(f"Projeto '{pe.project_id}' não existe no perfil")
            if pe.project_id in seen:
                r.violations.append(f"Projeto '{pe.project_id}' repetido")
            seen.add(pe.project_id)

        # --- texto livre geral
        all_planned = set().union(*idx.planned_skills.values()) if idx.planned_skills else set()
        group_names = "\n".join(g.name for g in d.skill_groups)
        for label, text in (("título", d.headline), ("resumo", d.summary), ("grupos de competências", group_names)):
            self._check_text(r, label, text, allowed, self._numbers, all_planned)

        # --- bullets: tecnologias e números do PRÓPRIO projeto
        for pe in d.projects:
            proj = idx.project(pe.project_id)
            if proj is None:
                continue
            proj_allowed = idx.allowed_for_project(proj.id)
            for b in pe.bullets:
                self._check_text(r, f"projeto {proj.name}", b, proj_allowed, self._project_numbers[proj.id],
                                 idx.planned_skills.get(proj.id, set()))
            if not 1 <= len(pe.bullets) <= 5:
                r.warnings.append(f"Projeto {proj.name}: {len(pe.bullets)} bullets (ideal 2-4)")

        if len(d.summary) > 900:
            r.warnings.append("Resumo longo (>900 caracteres)")
        r.violations = list(dict.fromkeys(r.violations))
        return r

    def validate_text(self, text: str, where: str = "resposta", extra_numbers: set[str] | None = None,
                      ignore: list[str] | None = None) -> list[str]:
        """Valida texto livre sobre você (respostas de formulário). extra_numbers: números da vaga,
        que podem ser citados ao falar da empresa. ignore: trechos citados literalmente (cargo, empresa)."""
        for chunk in ignore or []:
            if chunk:
                text = text.replace(chunk, " ")
        r = ValidationReport()
        all_planned = set().union(*self.idx.planned_skills.values()) if self.idx.planned_skills else set()
        self._check_text(r, where, text, self.idx.allowed_skills, self._numbers | (extra_numbers or set()), all_planned)
        return list(dict.fromkeys(r.violations))

    @staticmethod
    def numbers_in(text: str) -> set[str]:
        return set(_NUMBER.findall(text))

    def _check_text(self, r: ValidationReport, where: str, text: str, allowed_skills: set[str],
                    numbers: set[str], planned_skills: set[str]) -> None:
        tax = self.idx.tax
        for sentence in _SENTENCE.findall(text):
            marked_planned = bool(_PLANNED_MARKER.search(sentence))
            for sid, terms in tax.find_skill_literals(sentence).items():
                if marked_planned and sid in planned_skills:
                    continue  # ex.: "integração com WhatsApp planejada"
                if sid not in allowed_skills:
                    r.violations.append(f"[{where}] cita '{tax.name(sid)}', que não consta no perfil"
                                        + (" para este projeto" if where.startswith("projeto") else ""))
                    continue
                for t in terms:
                    if not self.idx.literal_allowed(sid, t):
                        r.violations.append(f"[{where}] cita '{t}', termo que não aparece no perfil")
        owned = self.idx.owned_cert_ids()
        for cid in tax.find_certs(text):
            if not set(tax.certs[cid].satisfied_by) & owned:
                r.violations.append(f"[{where}] cita certificação '{tax.certs[cid].name}', que você não possui")
        for n in _NUMBER.findall(text):
            if n not in numbers and n.rstrip("%") not in numbers:
                r.violations.append(f"[{where}] número '{n}' não aparece no perfil")
        if not self.idx.has_professional_experience:
            for pattern, why in _NO_EXPERIENCE_FORBIDDEN:
                m = pattern.search(text)
                if m:
                    r.violations.append(f"[{where}] \"{m.group(0)}\" — {why} (perfil sem experiência profissional)")
        if self._in_progress_postgrad and re.search(r"p[óo]s[- ]graduad[oa]\b|conclu[íi] a p[óo]s", text, re.I):
            r.violations.append(f"[{where}] descreve a pós-graduação como concluída (status: em andamento)")
        for sentence in _SENTENCE.findall(text):
            low = sentence.lower()
            for proj_name, kw in self._planned:
                if kw in low and not _PLANNED_MARKER.search(sentence):
                    r.violations.append(f"[{where}] '{kw}' ({proj_name}) é planejado; precisa ser descrito como planejado/futuro")
