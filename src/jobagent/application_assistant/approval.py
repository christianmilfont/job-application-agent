"""Fluxo de aprovação (etapa 9).

A vaga só chega a READY_TO_APPLY por aqui, com você confirmando explicitamente:
- currículo gerado e aprovado no validador de veracidade;
- você revisou o currículo;
- todas as respostas revisadas por você (nenhuma pendente de dado seu);
- você leu os RISKS.
A aprovação guarda um snapshot (hash do PDF + respostas). Se o currículo for regenerado ou uma
resposta editada depois, a aprovação é revogada e a vaga volta para RESUME_GENERATED.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import Answer, Approval, Job, JobRepository, JobStatus, ResumeRecord
from ..database.models import utcnow


class ApprovalError(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass
class CheckItem:
    label: str
    ok: bool
    detail: str = ""


def _answers(session: Session, job: Job) -> list[Answer]:
    return list(session.scalars(select(Answer).where(Answer.job_id == job.id).order_by(Answer.position, Answer.id)))


def latest_resume(session: Session, job: Job) -> ResumeRecord | None:
    rs = JobRepository(session).resumes(job.id)
    return rs[0] if rs else None


def checklist(session: Session, job: Job) -> list[CheckItem]:
    rec = latest_resume(session, job)
    answers = _answers(session, job)
    pending = [a.question for a in answers if not a.reviewed or not (a.final_text or "").strip()]
    pdf_ok = bool(rec and (Path(rec.directory) / "resume.pdf").is_file())
    return [
        CheckItem("Currículo gerado", bool(rec) and pdf_ok,
                  "" if rec else "gere o currículo primeiro"),
        CheckItem("Currículo aprovado no validador de veracidade", bool(rec and rec.validation_ok),
                  "" if rec and rec.validation_ok else "regenere o currículo"),
        CheckItem("Respostas preparadas", bool(answers), "" if answers else "gere as sugestões de resposta"),
        CheckItem("Todas as respostas revisadas por você", bool(answers) and not pending,
                  f"{len(pending)} pendente(s): " + "; ".join(pending[:3]) if pending else ""),
    ]


def snapshot(session: Session, job: Job, rec: ResumeRecord) -> tuple[str, dict[str, Any]]:
    pdf = Path(rec.directory) / "resume.pdf"
    pdf_hash = hashlib.sha256(pdf.read_bytes()).hexdigest() if pdf.is_file() else ""
    answers = [{"key": a.key, "question": a.question, "answer": (a.final_text or "").strip()}
               for a in _answers(session, job)]
    snap = {"resume_id": rec.id, "resume_dir": rec.directory, "resume_pdf_sha256": pdf_hash, "answers": answers}
    digest = hashlib.sha256(json.dumps(snap, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return digest, snap


def current_approval(session: Session, job: Job) -> Approval | None:
    """Aprovação vigente E ainda idêntica ao estado atual (currículo + respostas)."""
    appr = session.scalars(select(Approval).where(Approval.job_id == job.id, Approval.revoked_at.is_(None))
                           .order_by(Approval.id.desc())).first()
    if not appr:
        return None
    rec = session.get(ResumeRecord, appr.resume_id)
    latest = latest_resume(session, job)
    if not rec or not latest or latest.id != rec.id:
        return None
    digest, _ = snapshot(session, job, rec)
    return appr if digest == appr.snapshot_hash else None


def approve(session: Session, job: Job, *, resume_reviewed: bool, risks_acknowledged: bool,
            note: str = "") -> Approval:
    problems = [f"{c.label}: {c.detail}" if c.detail else c.label for c in checklist(session, job) if not c.ok]
    if not resume_reviewed:
        problems.append("Confirme que revisou o currículo")
    if not risks_acknowledged and (job.analysis or {}).get("risks"):
        problems.append("Confirme que leu os riscos da vaga")
    if job.status in (JobStatus.APPLIED.value, JobStatus.INTERVIEW.value, JobStatus.OFFER.value):
        problems.append(f"Vaga já está em {job.status}")
    if problems:
        raise ApprovalError(problems)

    rec = latest_resume(session, job)
    revoke(session, job, "substituída por nova aprovação", change_status=False)
    digest, snap = snapshot(session, job, rec)
    appr = Approval(job_id=job.id, resume_id=rec.id, snapshot_hash=digest, snapshot=snap, note=note.strip() or None)
    session.add(appr)
    JobRepository(session).set_status(job, JobStatus.READY_TO_APPLY, note="aprovada por você" + (f": {note}" if note else ""))
    session.flush()
    _export(rec, snap, appr, job)
    session.commit()
    return appr


def revoke(session: Session, job: Job, reason: str, change_status: bool = True) -> bool:
    """Revoga aprovações vigentes. Volta a vaga para RESUME_GENERATED se estava pronta."""
    open_ = list(session.scalars(select(Approval).where(Approval.job_id == job.id, Approval.revoked_at.is_(None))))
    for appr in open_:
        appr.revoked_at = utcnow()
        appr.revoked_reason = reason
    if change_status and job.status == JobStatus.READY_TO_APPLY.value:
        JobRepository(session).set_status(job, JobStatus.RESUME_GENERATED, note=f"aprovação revogada: {reason}")
    return bool(open_)


def _export(rec: ResumeRecord, snap: dict[str, Any], appr: Approval, job: Job) -> None:
    """Grava answers.md (para copiar no formulário) e atualiza application.json."""
    d = Path(rec.directory)
    lines = [f"# Respostas aprovadas — {job.title} ({job.company})", "",
             f"Aprovado em {datetime.now(timezone.utc).astimezone().strftime('%d/%m/%Y %H:%M')}", ""]
    for a in snap["answers"]:
        lines += [f"## {a['question']}", "", a["answer"], ""]
    (d / "answers.md").write_text("\n".join(lines), encoding="utf-8")
    meta_path = d / "application.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else {}
    meta["answers"] = snap["answers"]
    meta["status"] = JobStatus.READY_TO_APPLY.value
    meta["approval"] = {"id": appr.id, "approved_at": utcnow().isoformat(), "snapshot_hash": appr.snapshot_hash,
                        "resume_pdf_sha256": snap["resume_pdf_sha256"], "note": appr.note}
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
