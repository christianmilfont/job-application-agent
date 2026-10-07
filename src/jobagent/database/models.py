from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, Enum):
    FOUND = "FOUND"
    ANALYZED = "ANALYZED"
    APPROVED = "APPROVED"
    RESUME_GENERATED = "RESUME_GENERATED"
    READY_TO_APPLY = "READY_TO_APPLY"
    APPLIED = "APPLIED"
    REJECTED = "REJECTED"
    INTERVIEW = "INTERVIEW"
    OFFER = "OFFER"


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- Chaves de deduplicação (todas únicas)
    url_key: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    source_key: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    content_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)

    source: Mapped[str] = mapped_column(String(50))          # greenhouse | lever | manual
    external_id: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(1024))
    apply_url: Mapped[str | None] = mapped_column(String(1024))
    apply_type: Mapped[str | None] = mapped_column(String(50))  # greenhouse_form | lever_form | external | unknown

    company: Mapped[str] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(255), index=True)
    location: Mapped[str | None] = mapped_column(String(255), index=True)
    modality: Mapped[str | None] = mapped_column(String(20))    # REMOTE | HYBRID | ONSITE | UNKNOWN
    seniority: Mapped[str | None] = mapped_column(String(20))
    recruiter: Mapped[str | None] = mapped_column(String(255))
    department: Mapped[str | None] = mapped_column(String(255))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    description: Mapped[str] = mapped_column(Text)              # texto limpo
    parsed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # saída do parser
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)     # payload original (auditoria)

    score: Mapped[int | None] = mapped_column(Integer, index=True)
    score_band: Mapped[str | None] = mapped_column(String(30))
    analysis: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # match/gaps/risks/breakdown
    scorer_version: Mapped[str | None] = mapped_column(String(20))

    status: Mapped[str] = mapped_column(String(30), default=JobStatus.FOUND.value, index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    history: Mapped[list[StatusHistory]] = relationship(
        back_populates="job", cascade="all, delete-orphan", order_by="StatusHistory.at")


class StatusHistory(Base):
    __tablename__ = "status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    note: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="history")


class ResumeRecord(Base):
    """Cada currículo gerado (histórico; a vaga pode ter várias versões)."""
    __tablename__ = "resumes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    directory: Mapped[str] = mapped_column(String(1024))
    generator: Mapped[str] = mapped_column(String(20))          # llm | deterministic
    model: Mapped[str | None] = mapped_column(String(100))
    ats_score: Mapped[int] = mapped_column(Integer)
    validation_ok: Mapped[bool] = mapped_column(default=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Answer(Base):
    """Resposta para pergunta de formulário. `suggested` vem do sistema; `final_text` é a sua versão."""
    __tablename__ = "answers"
    __table_args__ = (UniqueConstraint("job_id", "key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(100))            # motivation, salary, exp:kubernetes, custom:1...
    question: Mapped[str] = mapped_column(Text)
    suggested: Mapped[str] = mapped_column(Text, default="")
    final_text: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20))           # profile | llm | template | manual
    needs_input: Mapped[bool] = mapped_column(default=False)  # depende de dado que só você tem
    reviewed: Mapped[bool] = mapped_column(default=False)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    position: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Approval(Base):
    """Sua aprovação explícita de uma candidatura. Vale só para o snapshot aprovado
    (hash do PDF + respostas); qualquer mudança posterior a revoga."""
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    resume_id: Mapped[int] = mapped_column(ForeignKey("resumes.id"))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_reason: Mapped[str | None] = mapped_column(Text)


class CollectorRun(Base):
    """Registro de cada execução de coleta (auditoria e controle de volume)."""
    __tablename__ = "collector_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
