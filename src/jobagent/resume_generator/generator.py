"""Orquestra a etapa 7: plano -> rascunho (LLM ou determinístico) -> validação -> render -> ATS -> arquivos.

Fluxo com LLM:
  1. gera com o modelo configurado;
  2. valida; se houver violações, gera de novo enviando a lista de violações;
  3. se ainda falhar (ou a API falhar), usa o rascunho determinístico — que só reorganiza o
     perfil — e registra o motivo. Nunca grava um currículo reprovado no validador.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from ..config import AppConfig, Settings, get_settings
from ..database import Job, JobRepository, JobStatus, ResumeRecord
from ..database.dedup import normalize_text
from ..job_collector import RawJob
from ..job_parser import JobParser, ParsedJob, Taxonomy
from ..profile import Profile, ProfileIndex
from .ats import ATSReport, analyze_ats
from .llm import LLMError, LLMProvider, credentials_available, get_provider
from .prompts import SYSTEM_RULES, job_block, profile_block
from .render import build_document, render_markdown, render_pdf
from .schema import ProjectEntry, ResumeDraft, SkillGroup
from .selector import ResumePlan, build_plan
from .validator import ResumeValidator, ValidationReport

log = logging.getLogger(__name__)


@dataclass
class PackageResult:
    directory: Path
    generator: str                    # llm | deterministic
    ats: ATSReport
    validation: ValidationReport
    attempts: int = 0
    llm_meta: dict[str, Any] = field(default_factory=dict)
    fallback_reason: str | None = None
    rejected_violations: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------- rascunho determinístico
def deterministic_draft(idx: ProfileIndex, plan: ResumePlan) -> ResumeDraft:
    p, tax = idx.profile, idx.tax
    sentences = []
    grad = next((e for e in p.education if e.level == "graduacao"), None)
    post = next((e for e in p.education if e.level == "pos_graduacao"), None)
    if grad:
        s = f"{'Formado' if grad.status == 'completed' else 'Cursando'} em {grad.degree} ({grad.institution})"
        if post:
            s += (", cursando a " if post.status == "in_progress" else ", com ") + f"{post.degree} ({post.institution})"
        sentences.append(s + ".")
    certs = [idx.certification(c) for c in plan.certification_ids]
    exam_certs = [c.name for c in certs if c and c.kind == "certification"][:2]
    if exam_certs:
        sentences.append("Certificações " + " e ".join(exam_certs) + ".")
    owned = [s for s in plan.matched_skills if s in idx.owned_skills]
    in_projects = [idx.display_name(s) for s in owned if "project" in idx.evidence[s].kinds][:6]
    declared = [idx.display_name(s) for s in owned if "project" not in idx.evidence[s].kinds][:4]
    if in_projects:
        sentences.append("Projetos práticos com " + _join(in_projects) + ".")
    if declared:
        sentences.append("Conhecimentos em " + _join(declared) + ".")
    if p.achievements:
        sentences.append(p.achievements[0].text)

    projects = []
    weights = plan.job_weights
    for pick in plan.projects:
        pr = idx.project(pick.project_id)
        if pr is None:
            continue

        def relevance(h: str) -> int:
            return sum(weights.get(s, 0) for s in tax.find_skills(h))
        useful = [h for h in pr.highlights if len(h.split()) >= 3] or pr.highlights  # "Expo." sozinho não ajuda
        ranked = sorted(useful, key=relevance, reverse=True)[:4]
        projects.append(ProjectEntry(project_id=pr.id, bullets=ranked))

    return ResumeDraft(
        headline=p.personal.headline,
        summary=" ".join(sentences),
        skill_groups=[SkillGroup(name=g.name, skill_ids=g.skill_ids) for g in plan.skill_groups],
        projects=projects,
        certification_ids=plan.certification_ids,
        keywords_used=[idx.display_name(s) for s in plan.matched_skills],
        rationale=selection_rationale(idx, plan),
    )


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " e " + items[-1]


def selection_rationale(idx: ProfileIndex, plan: ResumePlan) -> list[str]:
    tax = idx.tax
    out = []
    for pk in plan.projects:
        name = idx.project(pk.project_id).name
        if pk.matched:
            out.append(f"{name} em destaque: cobre {', '.join(idx.display_name(s) for s in pk.matched[:6])}, citados na vaga.")
        else:
            out.append(f"{name} incluído para completar o portfólio (sem relação direta com a vaga).")
    if plan.skill_groups:
        out.append(f"Competências reordenadas: '{plan.skill_groups[0].name}' primeiro, por ser o grupo mais pedido.")
    if plan.certification_ids:
        out.append(f"Certificação em primeiro: {idx.certification(plan.certification_ids[0]).name}.")
    if plan.missing_skills:
        out.append("Não incluídos por não constarem no perfil: "
                   + ", ".join(tax.name(s) for s in plan.missing_skills[:8]) + ".")
    return out


# ----------------------------------------------------------------------------- utilidades
def parse_job(job: Job, parser: JobParser) -> ParsedJob:
    raw = RawJob(source=job.source, external_id=job.external_id, url=job.url, company=job.company,
                 title=job.title, location=job.location, description_text=job.description,
                 modality_hint=(job.raw or {}).get("modality_hint"))
    return parser.parse(raw)


def _slug(text: str, limit: int = 60) -> str:
    return re.sub(r"\s+", "-", normalize_text(text))[:limit].strip("-") or "vaga"


def application_dir(base: Path, job: Job) -> Path:
    d = base / f"{_slug(job.company, 30)}-{_slug(job.title)}"
    meta = d / "application.json"
    if meta.exists():
        try:
            if json.loads(meta.read_text(encoding="utf-8")).get("job", {}).get("id") != job.id:
                d = d.with_name(f"{d.name}-{job.id}")
        except (json.JSONDecodeError, OSError):
            d = d.with_name(f"{d.name}-{job.id}")
    return d


# ----------------------------------------------------------------------------- orquestração
class ResumeGenerator:
    def __init__(self, config: AppConfig, settings: Settings | None = None,
                 provider: LLMProvider | None = None) -> None:
        self.cfg = config
        self.settings = settings or get_settings()
        self.tax = Taxonomy(config.taxonomy)
        self.parser = JobParser(self.tax)
        self.idx = ProfileIndex(Profile.from_dict(config.profile), self.tax)
        self.validator = ResumeValidator(self.idx)
        self._provider = provider

    def llm_available(self) -> bool:
        return self._provider is not None or credentials_available(self.settings)

    def generate(self, job: Job, session: Session, use_llm: bool = True) -> PackageResult:
        parsed = parse_job(job, self.parser)
        plan = build_plan(self.idx, parsed)

        draft, lang, report = None, plan.language, ValidationReport()
        attempts, meta, fallback_reason, rejected = 0, {}, None, []
        if use_llm and self.llm_available():
            draft, report, attempts, meta, fallback_reason, rejected = self._llm_draft(job, parsed, plan)
        elif use_llm:
            fallback_reason = "sem credencial Anthropic (ANTHROPIC_API_KEY)"
        else:
            fallback_reason = "--no-llm"

        generator = "llm"
        if draft is None:
            generator, lang = "deterministic", "pt"
            draft = deterministic_draft(self.idx, plan)
            report = self.validator.validate(draft)
            if not report.ok:  # o determinístico só usa o perfil; falhar aqui é bug ou perfil inconsistente
                raise RuntimeError("rascunho determinístico reprovado: " + "; ".join(report.violations))

        doc = build_document(draft, self.idx, lang)
        md = render_markdown(doc)
        ats = analyze_ats(md, job.title, parsed, self.idx, self.idx.profile.target_seniority)

        out = application_dir(self.settings.applications_dir, job)
        out.mkdir(parents=True, exist_ok=True)
        (out / "job-description.md").write_text(_job_md(job, parsed), encoding="utf-8")
        (out / "resume.md").write_text(md, encoding="utf-8")
        render_pdf(doc, out / "resume.pdf")
        result = PackageResult(out, generator, ats, report, attempts, meta, fallback_reason, rejected)
        (out / "analysis.md").write_text(_analysis_md(job, draft, plan, result, self.idx), encoding="utf-8")
        (out / "application.json").write_text(
            json.dumps(_application_json(job, draft, result, self.settings), ensure_ascii=False, indent=2,
                       default=str), encoding="utf-8")

        session.add(ResumeRecord(job_id=job.id, directory=str(out), generator=generator,
                                 model=meta.get("model_served"), ats_score=ats.score,
                                 validation_ok=report.ok, attempts=attempts,
                                 meta={"fallback_reason": fallback_reason, **meta}))
        repo = JobRepository(session)
        from ..application_assistant.approval import revoke
        revoke(session, job, "currículo regenerado")   # aprovação vale só para o currículo aprovado
        if job.status in (JobStatus.FOUND.value, JobStatus.ANALYZED.value, JobStatus.APPROVED.value):
            repo.set_status(job, JobStatus.RESUME_GENERATED,
                            note=f"{generator}; ATS {ats.score}%" + (f"; modelo {meta.get('model_served')}" if meta else ""))
        session.commit()
        return result

    def _llm_draft(self, job: Job, parsed: ParsedJob, plan: ResumePlan):
        provider = self._provider or get_provider(self.settings)
        system = [SYSTEM_RULES, profile_block(self.idx)]
        violations: list[str] | None = None
        rejected: list[str] = []
        meta: dict[str, Any] = {}
        attempts = 0
        for attempts in range(1, self.settings.llm_max_attempts + 1):
            try:
                res = provider.generate(system, job_block(job, parsed, plan, self.idx, violations))
            except LLMError as e:
                log.warning("LLM falhou: %s", e)
                return None, ValidationReport(), attempts, meta, f"erro do LLM: {e}", rejected
            meta = res.meta
            report = self.validator.validate(res.draft)
            if report.ok:
                return res.draft, report, attempts, meta, None, rejected
            violations = report.violations
            rejected.extend(violations)
            log.info("rascunho do LLM reprovado (%d violações), tentativa %d", len(violations), attempts)
        return (None, ValidationReport(), attempts, meta,
                f"LLM reprovado no validador após {attempts} tentativa(s)", rejected)


# ----------------------------------------------------------------------------- arquivos
def _job_md(job: Job, parsed: ParsedJob) -> str:
    return "\n".join([
        f"# {job.title} — {job.company}", "",
        f"- **Local:** {job.location or '-'}",
        f"- **Modalidade:** {job.modality}",
        f"- **Senioridade:** {parsed.seniority}",
        f"- **Publicada:** {job.posted_at.date() if job.posted_at else '-'}",
        f"- **Link:** {job.url}",
        f"- **Candidatura:** {job.apply_type} — {job.apply_url or job.url}",
        f"- **Recrutador:** {job.recruiter or '-'}", "",
        "## Descrição", "", job.description, "",
    ])


def _analysis_md(job: Job, draft: ResumeDraft, plan: ResumePlan, r: PackageResult, idx: ProfileIndex) -> str:
    a = job.analysis or {}
    lines = [f"# Análise — {job.title} ({job.company})", "",
             f"**Match:** {job.score} ({job.score_band})  ", f"**ATS Match: {r.ats.score}%**  ",
             f"**Currículo gerado por:** {r.generator}"
             + (f" ({r.llm_meta.get('model_served')}, {r.attempts} tentativa(s))" if r.generator == "llm" else "")]
    if r.fallback_reason:
        lines.append(f"**Motivo do modo determinístico:** {r.fallback_reason}")
    for title, key in (("MATCH", "match"), ("GAPS", "gaps"), ("RISKS", "risks")):
        lines += ["", f"## {title}"] + [f"- {x}" for x in a.get(key) or ["—"]]

    lines += ["", "## Por que este currículo foi adaptado desta forma?"] + [f"- {x}" for x in draft.rationale]
    if r.generator == "llm":
        lines += ["", "### Seleção determinística (referência)"] + [f"- {x}" for x in selection_rationale(idx, plan)]

    lines += ["", f"## ATS — {r.ats.score}%", "", "| Palavra-chave | Tipo | Peso | No currículo | Você possui |",
              "|---|---|---|---|---|"]
    for c in r.ats.checks:
        lines.append(f"| {c.keyword} | {c.kind} | {c.weight} | {'✅' if c.present else '❌'} | {'sim' if c.owned else 'não'} |")
    lines += ["", f"**Senioridade:** {r.ats.seniority_note}", "", "### Como melhorar"] + [f"- {t}" for t in r.ats.tips]

    lines += ["", "## Validação de veracidade", "✅ Aprovado: tudo que o currículo afirma existe no profile.yaml."
              if r.validation.ok else "❌ Reprovado"]
    lines += [f"- ⚠️ {w}" for w in r.validation.warnings]
    if r.rejected_violations:
        lines += ["", "### Violações encontradas em versões descartadas do LLM"] + [f"- {v}" for v in r.rejected_violations]
    lines += ["", "> Revise o currículo antes de usar. O sistema não envia nada sem a sua aprovação."]
    return "\n".join(lines) + "\n"


def _application_json(job: Job, draft: ResumeDraft, r: PackageResult, s: Settings) -> dict[str, Any]:
    return {
        "job": {"id": job.id, "company": job.company, "title": job.title, "location": job.location,
                "modality": job.modality, "seniority": job.seniority, "url": job.url, "apply_url": job.apply_url,
                "apply_type": job.apply_type, "posted_at": job.posted_at},
        "status": JobStatus.RESUME_GENERATED.value,
        "score": {k: (job.analysis or {}).get(k) for k in ("score", "band", "match", "gaps", "risks")},
        "ats": r.ats.to_dict(),
        "resume": {
            "generator": r.generator, "attempts": r.attempts, "fallback_reason": r.fallback_reason,
            "llm": r.llm_meta,
            "validation": {"ok": r.validation.ok, "violations": r.validation.violations,
                           "warnings": r.validation.warnings, "rejected_llm_violations": r.rejected_violations},
            "files": ["job-description.md", "analysis.md", "resume.md", "resume.pdf", "application.json"],
        },
        "draft": draft.model_dump(),
        "answers": [],   # etapa 9: respostas sugeridas, editáveis por você
        "mode": s.mode.value,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
