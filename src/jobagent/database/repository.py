from __future__ import annotations

from datetime import datetime
from typing import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .models import Job, JobStatus, ResumeRecord, StatusHistory


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.s = session

    def find_duplicate(self, url_key: str, source_key: str | None, content_key: str) -> Job | None:
        conds = [Job.url_key == url_key, Job.content_key == content_key]
        if source_key:
            conds.append(Job.source_key == source_key)
        return self.s.scalars(select(Job).where(or_(*conds)).limit(1)).first()

    def add(self, job: Job) -> Job:
        self.s.add(job)
        self.s.flush()
        self.s.add(StatusHistory(job_id=job.id, from_status=None, to_status=job.status,
                                 note="criada pela descoberta"))
        return job

    def get(self, job_id: int) -> Job | None:
        return self.s.get(Job, job_id)

    def set_status(self, job: Job, status: JobStatus, note: str | None = None) -> None:
        if job.status == status.value:
            return
        self.s.add(StatusHistory(job_id=job.id, from_status=job.status, to_status=status.value, note=note))
        job.status = status.value

    def all(self) -> Iterable[Job]:
        return self.s.scalars(select(Job))

    _SORTS = {
        "score": (Job.score.desc().nullslast(), Job.posted_at.desc().nullslast()),
        "date": (Job.posted_at.desc().nullslast(), Job.score.desc().nullslast()),
        "company": (Job.company.asc(), Job.score.desc().nullslast()),
        "updated": (Job.updated_at.desc(),),
    }

    def _filtered(self, q, *, min_score=None, status=None, company=None, title=None, location=None,
                  since=None, modality=None):
        if min_score is not None:
            q = q.where(Job.score >= min_score)
        if status:
            q = q.where(Job.status == status.upper())
        if company:
            q = q.where(Job.company.ilike(f"%{company}%"))
        if title:
            q = q.where(Job.title.ilike(f"%{title}%"))
        if location:
            q = q.where(Job.location.ilike(f"%{location}%"))
        if since:
            q = q.where(Job.posted_at >= since)
        if modality:
            q = q.where(Job.modality == modality.upper())
        return q

    def search(self, *, min_score: int | None = None, status: str | None = None,
               company: str | None = None, title: str | None = None, location: str | None = None,
               since: datetime | None = None, modality: str | None = None, sort: str = "score",
               limit: int = 50, offset: int = 0) -> list[Job]:
        q = self._filtered(select(Job), min_score=min_score, status=status, company=company, title=title,
                           location=location, since=since, modality=modality)
        q = q.order_by(*self._SORTS.get(sort, self._SORTS["score"])).limit(limit).offset(offset)
        return list(self.s.scalars(q))

    def count(self, **filters) -> int:
        return self.s.scalar(self._filtered(select(func.count(Job.id)), **filters)) or 0

    def status_counts(self) -> dict[str, int]:
        return dict(self.s.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all())

    def companies(self) -> list[str]:
        return list(self.s.scalars(select(Job.company).distinct().order_by(Job.company)))

    def latest_resumes(self, job_ids: list[int]) -> dict[int, ResumeRecord]:
        if not job_ids:
            return {}
        latest = (select(func.max(ResumeRecord.id)).where(ResumeRecord.job_id.in_(job_ids))
                  .group_by(ResumeRecord.job_id))
        rows = self.s.scalars(select(ResumeRecord).where(ResumeRecord.id.in_(latest)))
        return {r.job_id: r for r in rows}

    def resumes(self, job_id: int) -> list[ResumeRecord]:
        return list(self.s.scalars(select(ResumeRecord).where(ResumeRecord.job_id == job_id)
                                   .order_by(ResumeRecord.id.desc())))
