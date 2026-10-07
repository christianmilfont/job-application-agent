"""Regras de transição de status usadas pelo dashboard e pela CLI."""
from __future__ import annotations

from ..database import Job, JobStatus

STATUS_LABELS = {
    "FOUND": "Encontrada", "ANALYZED": "Analisada", "APPROVED": "Aprovada p/ currículo",
    "RESUME_GENERATED": "Currículo gerado", "READY_TO_APPLY": "Pronta p/ candidatura",
    "APPLIED": "Candidatura enviada", "REJECTED": "Rejeitada", "INTERVIEW": "Entrevista", "OFFER": "Proposta",
}


def transition_error(job: Job, target: JobStatus, has_resume: bool, has_approval: bool = False) -> str | None:
    """Motivo do bloqueio da troca manual de status, ou None se permitida."""
    if job.status == target.value:
        return None
    if target is JobStatus.READY_TO_APPLY:
        return "Use a página de candidatura: revise currículo e respostas e aprove por lá."
    if target is JobStatus.APPLIED and not (job.status == JobStatus.READY_TO_APPLY.value and has_approval):
        return "Só marque como enviada uma candidatura aprovada (página de candidatura)."
    if target in (JobStatus.INTERVIEW, JobStatus.OFFER) and job.status not in (
            JobStatus.APPLIED.value, JobStatus.INTERVIEW.value, JobStatus.OFFER.value):
        return "Entrevista/proposta só depois de marcar a candidatura como enviada."
    return None
