"""Pipeline DISCOVERY: coletar -> deduplicar -> parsear -> filtrar -> pontuar -> salvar.
Nenhuma etapa aqui interage com formulários de candidatura."""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable

from sqlalchemy.orm import Session

from ..config import AppConfig, Mode
from ..database import CollectorRun, Job, JobRepository, JobStatus
from ..database.dedup import content_key, source_key, url_key
from ..database.models import utcnow
from ..job_collector import AccessDenied, BudgetExceeded, Collector, RawJob
from ..job_parser import JobParser, Taxonomy
from ..job_scorer import SCORER_VERSION, JobScorer

log = logging.getLogger(__name__)


@dataclass
class JobSummary:
    """Retrato da vaga desacoplado da sessão (válido mesmo após rollback em --preview)."""
    id: int | None
    score: int | None
    score_band: str | None
    title: str
    company: str
    location: str | None
    modality: str | None
    seniority: str | None
    status: str
    posted_at: datetime | None

    @classmethod
    def of(cls, job: Job, persisted: bool) -> JobSummary:
        return cls(job.id if persisted else None, job.score, job.score_band, job.title, job.company,
                   job.location, job.modality, job.seniority, job.status, job.posted_at)


@dataclass
class DiscoveryResult:
    stats: Counter = field(default_factory=Counter)
    new_jobs: list[JobSummary] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class DiscoveryPipeline:
    def __init__(self, config: AppConfig, session: Session, mode: Mode) -> None:
        self.session = session
        self.mode = mode
        self.repo = JobRepository(session)
        taxonomy = Taxonomy(config.taxonomy)
        self.parser = JobParser(taxonomy)
        self.scorer = JobScorer(config.profile, config.scoring, taxonomy)

    def run(self, collectors: Iterable[Collector], persist: bool = True) -> DiscoveryResult:
        result = DiscoveryResult()
        run = CollectorRun(mode=self.mode.value)
        for collector in collectors:
            try:
                for raw in collector.collect():
                    self._process(raw, result, persist)
            except AccessDenied as e:
                msg = f"{collector.name}: acesso negado, coleta interrompida ({e})"
                log.warning(msg)
                result.errors.append(msg)
            except BudgetExceeded as e:
                result.errors.append(str(e))
                log.warning(str(e))
                break
        run.finished_at = utcnow()
        run.stats = dict(result.stats) | {"errors": result.errors}
        if persist:
            self.session.add(run)
            self.session.commit()
        else:
            self.session.rollback()
        return result

    def _process(self, raw: RawJob, result: DiscoveryResult, persist: bool) -> None:
        result.stats["collected"] += 1
        ukey, skey = url_key(raw.url), source_key(raw.source, raw.external_id)
        ckey = content_key(raw.company, raw.title, raw.location)
        if self.repo.find_duplicate(ukey, skey, ckey):
            result.stats["duplicate"] += 1
            return

        parsed = self.parser.parse(raw)
        relevant, reason = self.scorer.is_relevant(raw.title, parsed, raw.company)
        if not relevant:
            result.stats[f"filtered: {reason}"] += 1
            return

        analysis = self.scorer.score(raw.title, raw.location, parsed, raw.company)
        job = Job(
            url_key=ukey, source_key=skey, content_key=ckey,
            source=raw.source, external_id=raw.external_id, url=raw.url,
            apply_url=raw.apply_url, apply_type=raw.apply_type or "unknown",
            company=raw.company, title=raw.title, location=raw.location,
            modality=parsed.modality, seniority=parsed.seniority, recruiter=raw.recruiter,
            department=raw.department, posted_at=raw.posted_at,
            description=parsed.description, parsed=parsed.to_dict(),
            raw=raw.raw | {"modality_hint": raw.modality_hint},
            score=analysis.score, score_band=analysis.band, analysis=analysis.to_dict(),
            scorer_version=SCORER_VERSION, status=JobStatus.FOUND.value,
        )
        # Salvamos mesmo em DRY_RUN: o banco é local e nada é enviado a terceiros.
        # Use --preview para não persistir.
        self.repo.add(job)
        self.repo.set_status(job, JobStatus.ANALYZED, note=f"score {analysis.score} ({analysis.band})")
        result.stats["new"] += 1
        result.new_jobs.append(JobSummary.of(job, persist))


def rescore_all(config: AppConfig, session: Session) -> int:
    """Recalcula score de todas as vagas (após mudar profile/scoring/taxonomia)."""
    taxonomy = Taxonomy(config.taxonomy)
    parser = JobParser(taxonomy)
    scorer = JobScorer(config.profile, config.scoring, taxonomy)
    repo = JobRepository(session)
    count = 0
    for job in repo.all():
        raw = RawJob(source=job.source, external_id=job.external_id, url=job.url, company=job.company,
                     title=job.title, location=job.location, description_text=job.description,
                     modality_hint=(job.raw or {}).get("modality_hint"))
        parsed = parser.parse(raw)
        analysis = scorer.score(job.title, job.location, parsed, job.company)
        job.parsed, job.analysis = parsed.to_dict(), analysis.to_dict()
        job.score, job.score_band, job.scorer_version = analysis.score, analysis.band, SCORER_VERSION
        job.modality, job.seniority = parsed.modality, parsed.seniority
        count += 1
    session.commit()
    return count
