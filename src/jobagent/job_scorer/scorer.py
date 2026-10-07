"""Scoring de aderência vaga x perfil. Determinístico e explicável: cada ponto tem motivo.

Princípios:
- Falta de tecnologia DESEJÁVEL nunca elimina a vaga (vira GAP).
- Falta de requisito OBRIGATÓRIO vira RISK, reduz o score, mas não elimina.
- Só conta como MATCH o que está no profile.yaml.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..job_parser import ParsedJob, Taxonomy
from ..profile import Profile, ProfileIndex

SCORER_VERSION = "1.1"

_SENIORITY_LABEL = {"intern": "Estágio", "trainee": "Trainee", "junior": "Júnior", "pleno": "Pleno",
                    "senior": "Sênior", "lead": "Liderança/Especialista", "unspecified": "Não informada"}


@dataclass
class Component:
    points: float
    max: float
    detail: str


@dataclass
class Analysis:
    score: int
    band: str
    match: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    breakdown: dict[str, Component] = field(default_factory=dict)
    penalties: list[str] = field(default_factory=list)
    version: str = SCORER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobScorer:
    def __init__(self, profile: dict[str, Any] | ProfileIndex, scoring: dict[str, Any], taxonomy: Taxonomy) -> None:
        self.idx = profile if isinstance(profile, ProfileIndex) else ProfileIndex(Profile.from_dict(profile), taxonomy)
        p = self.idx.profile
        self.cfg = scoring
        self.tax = taxonomy
        self.cert_ids = {c.id: c for c in p.certifications}
        self.has_english = any(re.search(r"ingl|english", lang.name, re.I) for lang in p.languages)
        self.has_degree = any(e.level == "graduacao" and e.status == "completed" for e in p.education)
        self.degree_label = next((f"{e.degree} ({e.institution})" for e in p.education
                                  if e.level == "graduacao" and e.status == "completed"), "")
        role = scoring.get("role") or {}
        self._strong = [re.compile(p, re.I) for p in role.get("strong", [])]
        self._partial = [re.compile(p, re.I) for p in role.get("partial", [])]
        self._negative = [re.compile(p, re.I) for p in role.get("negative", [])]

    # ------------------------------------------------------------------ helpers
    def _skill_match(self, skill_id: str) -> str | None:
        """Descrição da evidência (projeto/certificação/declarado) se o perfil cobre a skill."""
        return self.idx.describe(skill_id)

    def role_fit(self, title: str, company: str = "") -> float:
        """1.0 = cargo-alvo no segmento principal; 0.5 = relacionado ou alvo só no nome do time."""
        if any(p.search(title) for p in self._negative):
            return 0.0
        segments = _title_segments(title, company)
        main, rest = segments[0], " ".join(segments[1:])
        if any(p.search(main) for p in self._strong):
            return 1.0
        if any(p.search(main) for p in self._partial) or any(p.search(rest) for p in self._strong):
            return 0.5
        return 0.0

    def is_relevant(self, title: str, parsed: ParsedJob, company: str = "") -> tuple[bool, str]:
        """Filtro da descoberta: decide se a vaga merece ser salva/analisada."""
        disc = self.cfg.get("discovery") or {}
        if disc.get("keep_if_role_match", True) and self.role_fit(title, company) == 0:
            return False, "cargo fora do alvo"
        if disc.get("drop_locations_abroad_onsite", True) and self._location_key(parsed) == "abroad_onsite":
            return False, "presencial/híbrido fora do Brasil"
        return True, "ok"

    @staticmethod
    def _location_key(p: ParsedJob) -> str:
        loc, mod = p.location_class, p.modality
        if mod == "REMOTE":
            if loc in ("sp", "brazil", "brazil_any"):
                return "remote_brazil"
            if loc == "latam":
                return "remote_latam"
            if loc == "abroad":
                return "remote_abroad"
            return "remote_brazil" if p.description_language == "pt" else "remote_abroad"
        if loc in ("latam", "abroad"):
            return "abroad_onsite"
        if mod == "HYBRID":
            return {"sp": "hybrid_sp", "brazil": "hybrid_or_onsite_other_br"}.get(loc, "unknown")
        if mod == "ONSITE":
            return {"sp": "onsite_sp", "brazil": "hybrid_or_onsite_other_br"}.get(loc, "unknown")
        return {"sp": "unknown_modality_sp", "brazil": "hybrid_or_onsite_other_br"}.get(loc, "unknown")

    # ------------------------------------------------------------------- score
    def score(self, title: str, location: str | None, p: ParsedJob, company: str = "") -> Analysis:
        w = self.cfg["weights"]
        a = Analysis(score=0, band="")
        n = self.tax.name

        # 1) Obrigatórias. Sem seção de requisitos, as tecnologias citadas fazem o papel.
        explicit_required = bool(p.required_skills or p.required_any_of)
        pool = p.required_skills if explicit_required else p.mentioned_skills
        hits = 0
        # Grupos de alternativas contam como 1 requisito: basta ter um deles.
        for group in p.required_any_of:
            owned = [(s, ev) for s in group if (ev := self._skill_match(s))]
            names = " ou ".join(n(s) for s in group)
            if owned:
                hits += 1
                a.match.append(f"{names} (obrigatório, basta um) — você tem: "
                               + "; ".join(f"{n(s)} ({ev})" for s, ev in owned))
            else:
                a.risks.append(f"Requisito obrigatório ausente no perfil: um de {names}")
        units = len(pool) + len(p.required_any_of)
        for s in pool:
            ev = self._skill_match(s)
            label = "obrigatório" if explicit_required else "citado na vaga"
            if ev:
                hits += 1
                a.match.append(f"{n(s)} ({label}) — {ev}")
            elif explicit_required:
                a.risks.append(f"Requisito obrigatório ausente no perfil: {n(s)}")
            else:
                a.gaps.append(f"{n(s)} (citado na vaga, obrigatoriedade não identificada)")
        frac = hits / units if units else self.cfg.get("neutral_required_fraction", 0.5)
        a.breakdown["required_skills"] = Component(
            w["required_skills"] * frac, w["required_skills"],
            f"{hits}/{units} requisitos {'obrigatórios' if explicit_required else 'citados'}"
            if units else "nenhuma tecnologia identificada — valor neutro")

        # Tecnologias citadas fora dos requisitos (ex.: responsabilidades) quando há seção de requisitos
        if explicit_required:
            for s in p.mentioned_skills:
                ev = self._skill_match(s)
                if ev:
                    a.match.append(f"{n(s)} (citado nas atividades) — {ev}")
                else:
                    a.gaps.append(f"{n(s)} (citado nas atividades)")

        # 2) Desejáveis — nunca eliminam
        d_hits = 0
        for s in p.desired_skills:
            ev = self._skill_match(s)
            if ev:
                d_hits += 1
                a.match.append(f"{n(s)} (desejável) — {ev}")
            else:
                a.gaps.append(f"{n(s)} (desejável)")
        d_frac = d_hits / len(p.desired_skills) if p.desired_skills else self.cfg.get("neutral_desired_fraction", 0.6)
        a.breakdown["desired_skills"] = Component(
            w["desired_skills"] * d_frac, w["desired_skills"],
            f"{d_hits}/{len(p.desired_skills)} desejáveis" if p.desired_skills else "sem desejáveis listados — valor neutro")

        # 3) Cargo
        rf = self.role_fit(title, company)
        a.breakdown["role"] = Component(w["role"] * rf, w["role"],
                                        {1.0: "cargo-alvo", 0.5: "cargo relacionado", 0.0: "cargo fora do alvo"}[rf])

        # 4) Senioridade
        sp = self.cfg["seniority_points"].get(p.seniority, 0.7)
        a.breakdown["seniority"] = Component(w["seniority"] * sp, w["seniority"], _SENIORITY_LABEL[p.seniority])
        if p.seniority in ("senior", "lead"):
            a.risks.append(f"Senioridade {_SENIORITY_LABEL[p.seniority]} — perfil sem experiência profissional registrada")
        elif p.seniority == "intern":
            a.risks.append("Estágio: normalmente exige matrícula ativa em graduação (confirme elegibilidade)")
        elif p.seniority in ("junior", "trainee", "pleno"):
            a.match.append(f"Senioridade compatível: {_SENIORITY_LABEL[p.seniority]}")

        # 5) Localização + modalidade
        lk = self._location_key(p)
        lp = self.cfg["location_points"].get(lk, 0.5)
        loc_desc = f"{location or 'local não informado'} · {p.modality}"
        a.breakdown["location"] = Component(w["location"] * lp, w["location"], f"{loc_desc} ({lk})")
        if lp >= 0.75:
            a.match.append(f"Localização/modalidade: {loc_desc}")
        elif lp <= 0.25:
            a.risks.append(f"Localização/modalidade pouco compatível: {loc_desc}")
        else:
            a.gaps.append(f"Localização/modalidade a confirmar: {loc_desc}")

        # 6) Formação + certificações
        cred, cred_notes = 0.0, []
        if self.has_degree:
            cred += 4
            cred_notes.append("graduação concluída")
            if p.education_required:
                a.match.append(f"Formação superior exigida — {self.degree_label}")
        elif p.education_required:
            a.risks.append("Vaga exige formação superior")
        cert_pts = 0.0
        for cid in p.required_certs + p.desired_certs:
            entry = self.tax.certs[cid]
            owned = [self.cert_ids[c].name for c in entry.satisfied_by if c in self.cert_ids]
            if owned:
                cert_pts += 3
                a.match.append(f"{entry.name} — você possui: {', '.join(owned)}")
            elif cid in p.required_certs:
                a.risks.append(f"Certificação exigida não possuída: {entry.name}")
            else:
                a.gaps.append(f"Certificação desejável não possuída: {entry.name}")
        techs = set(p.technologies)
        for cert in self.cert_ids.values():
            if techs & set(cert.skills):
                pts = 1.5 if cert.kind == "training" else 3
                cert_pts += pts
                a.match.append(f"Certificação relacionada à stack da vaga: {cert.name}")
        cred += min(cert_pts, w["credentials"] - 4)
        a.breakdown["credentials"] = Component(min(cred, w["credentials"]), w["credentials"],
                                               ", ".join(cred_notes) + f"; certificações +{min(cert_pts, w['credentials'] - 4):g}")

        total = sum(c.points for c in a.breakdown.values())

        # Penalidades
        pen = self.cfg.get("penalties") or {}
        sen_pen = {"senior": pen.get("seniority_senior", 0), "lead": pen.get("seniority_lead", 0)}.get(p.seniority, 0)
        if sen_pen:
            total -= sen_pen
            a.penalties.append(f"-{sen_pen}: senioridade {_SENIORITY_LABEL[p.seniority]}")
        if p.years_required and p.years_required >= 3:
            total -= pen.get("years_required_high", 10)
            a.penalties.append(f"-{pen.get('years_required_high', 10)}: exige {p.years_required}+ anos")
            a.risks.append(f"Exige {p.years_required}+ anos de experiência: \"{p.years_evidence[0][:120]}\"")
        elif p.years_required:
            if p.years_required == 2:
                total -= pen.get("years_required_mid", 4)
                a.penalties.append(f"-{pen.get('years_required_mid', 4)}: exige 2 anos")
            a.gaps.append(f"Pede {p.years_required} ano(s) de experiência — avalie se projetos/formação cobrem")

        if p.english:
            if self.has_english:
                a.match.append("Inglês — informado no perfil")
            elif p.english.get("required"):
                total -= pen.get("english_required_unknown", 3)
                a.penalties.append(f"-{pen.get('english_required_unknown', 3)}: inglês exigido, não informado no perfil")
                level = f" ({p.english['level']})" if p.english.get("level") else ""
                a.risks.append(f"Inglês exigido{level} — nível não informado no profile.yaml")
            else:
                a.gaps.append("Inglês desejável — nível não informado no profile.yaml")

        if p.affirmative:
            a.risks.append(f"Vaga afirmativa (\"{p.affirmative}\") — confirme se você está no público elegível")

        a.score = max(0, min(100, round(total)))
        a.band = next(b["label"] for b in self.cfg["bands"] if a.score >= b["min"])
        a.match, a.gaps, a.risks = _dedupe(a.match), _dedupe(a.gaps), _dedupe(a.risks)
        return a


def _title_segments(title: str, company: str) -> list[str]:
    """'Grupo X | [Time] Cargo Sênior - Squad' -> ['Cargo Sênior', 'Squad'] (remove empresa e [tags])."""
    tags = " ".join(re.findall(r"\[([^\]]*)\]", title))
    clean = re.sub(r"\[[^\]]*\]", " ", title)
    parts = [s.strip() for s in re.split(r"\s+[|–—]\s+|\s+-\s+|\s*\|\s*", clean) if s.strip()]
    company_norm = company.lower().strip()
    parts = [s for s in parts if not company_norm or s.lower() not in company_norm and company_norm not in s.lower()]
    parts = parts or [clean.strip()]
    return parts + ([tags] if tags else [])


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))
