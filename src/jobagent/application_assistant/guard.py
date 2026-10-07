"""Trava de segurança central. Toda ação irreversível (envio de candidatura) DEVE passar aqui."""
from __future__ import annotations

from ..config import Mode
from ..database import Approval, Job, JobStatus


class SubmissionBlocked(Exception):
    pass


def assert_submission_allowed(mode: Mode, job: Job, user_confirmed: bool, approval: Approval | None) -> None:
    """approval deve vir de approval.current_approval(): vigente e idêntica ao estado atual."""
    if mode is not Mode.ASSISTED:
        raise SubmissionBlocked(f"modo {mode.value}: nenhum envio é permitido (use ASSISTED)")
    if job.status != JobStatus.READY_TO_APPLY.value:
        raise SubmissionBlocked(f"vaga em status {job.status}; precisa estar READY_TO_APPLY (aprovada por você)")
    if approval is None:
        raise SubmissionBlocked("sem aprovação vigente para o currículo e as respostas atuais")
    if not user_confirmed:
        raise SubmissionBlocked("confirmação explícita do usuário ausente")
