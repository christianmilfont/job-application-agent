"""Dashboard local (FastAPI + Jinja2 + HTMX). Escuta só em 127.0.0.1.

Proteções para um servidor local:
- Host permitido só 127.0.0.1/localhost (bloqueia DNS rebinding);
- todo POST exige o cabeçalho HX-Request e Origin local: um site malicioso aberto no seu
  navegador não consegue mudar status nem disparar geração de currículo (custo de API);
- arquivos servidos só de dentro de applications/ e só os 5 nomes conhecidos.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..application_assistant.approval import ApprovalError, approve, checklist, current_approval, revoke
from ..application_assistant.workflow import STATUS_LABELS, transition_error
from ..config import AppConfig, Mode, Settings, get_settings, load_config
from ..database import JobRepository, JobStatus, ResumeRecord, init_db, make_engine, make_session_factory

HERE = Path(__file__).parent
PAGE_SIZE = 50
SERVABLE = {"resume.pdf": "application/pdf", "resume.md": "md", "analysis.md": "md", "answers.md": "md",
            "job-description.md": "md", "application.json": "application/json"}
LOCAL_HOSTS = ["127.0.0.1", "localhost"]

_md = MarkdownIt("commonmark", {"html": False}).enable("table")


def _band_class(score: int | None) -> str:
    if score is None:
        return "band-none"
    return "band-x" if score >= 90 else "band-a" if score >= 75 else "band-p" if score >= 60 else "band-n"


def create_app(settings: Settings | None = None, config: AppConfig | None = None,
               extra_hosts: list[str] | None = None) -> FastAPI:
    s = settings or get_settings()
    engine = make_engine(s.database_url)
    init_db(engine)
    Session_ = make_session_factory(engine)
    hosts = LOCAL_HOSTS + (extra_hosts or [])

    app = FastAPI(title="Job Application Agent", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(STATUS_LABELS=STATUS_LABELS, STATUSES=[st.value for st in JobStatus],
                                 band_class=_band_class, mode=s.mode.value, model=s.llm_model)

    def db() -> Iterator[Session]:
        session = Session_()
        try:
            yield session
        finally:
            session.close()

    def local_post(request: Request) -> None:
        """Bloqueia POSTs que não vieram do próprio dashboard."""
        if request.headers.get("hx-request") != "true":
            raise HTTPException(403, "requisição recusada: use o dashboard")
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).hostname not in hosts:
            raise HTTPException(403, "origem não permitida")

    # ------------------------------------------------------------------ lista
    def _filters(request: Request) -> dict:
        q = request.query_params
        f = {k: (q.get(k) or "").strip() for k in ("min_score", "status", "company", "title", "location",
                                                   "days", "modality", "sort")}
        f["sort"] = f["sort"] or "score"
        return f

    def _query_args(f: dict) -> dict:
        args = {"status": f["status"] or None, "company": f["company"] or None, "title": f["title"] or None,
                "location": f["location"] or None, "modality": f["modality"] or None}
        if f["min_score"].isdigit():
            args["min_score"] = int(f["min_score"])
        if f["days"].isdigit():
            args["since"] = datetime.now(timezone.utc) - timedelta(days=int(f["days"]))
        return args

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, page: int = 1, session: Session = Depends(db)):
        repo = JobRepository(session)
        f = _filters(request)
        args = _query_args(f)
        total = repo.count(**args)
        page = max(1, page)
        jobs = repo.search(**args, sort=f["sort"], limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
        resumes = repo.latest_resumes([j.id for j in jobs])
        ctx = dict(request=request, jobs=jobs, resumes=resumes, f=f, total=total, page=page,
                   pages=max(1, -(-total // PAGE_SIZE)), counts=repo.status_counts(),
                   companies=repo.companies(), qs=lambda **kw: urlencode({**f, **kw}))
        name = "_table.html" if request.headers.get("hx-request") == "true" else "index.html"
        return templates.TemplateResponse(request, name, ctx)

    # ------------------------------------------------------------------ detalhe
    def _job_or_404(session: Session, job_id: int):
        job = JobRepository(session).get(job_id)
        if not job:
            raise HTTPException(404, "vaga não encontrada")
        return job

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int, session: Session = Depends(db)):
        job = _job_or_404(session, job_id)
        repo = JobRepository(session)
        return templates.TemplateResponse(request, "job.html", dict(
            request=request, job=job, a=job.analysis or {}, p=job.parsed or {}, resumes=repo.resumes(job.id),
            history=list(reversed(job.history)), error=None))

    @app.post("/jobs/{job_id}/status", response_class=HTMLResponse, dependencies=[Depends(local_post)])
    def change_status(request: Request, job_id: int, status: str = Form(...), note: str = Form(""),
                      session: Session = Depends(db)):
        job = _job_or_404(session, job_id)
        repo = JobRepository(session)
        try:
            target = JobStatus(status)
        except ValueError:
            raise HTTPException(400, "status inválido")
        error = transition_error(job, target, has_resume=bool(repo.resumes(job.id)),
                                 has_approval=current_approval(session, job) is not None)
        if not error:
            repo.set_status(job, target, note.strip() or "alterado no dashboard")
            session.commit()
        return templates.TemplateResponse(request, "_status.html", dict(
            request=request, job=job, error=error, history=list(reversed(job.history))))

    @app.post("/jobs/{job_id}/notes", response_class=HTMLResponse, dependencies=[Depends(local_post)])
    def save_notes(request: Request, job_id: int, notes: str = Form(""), session: Session = Depends(db)):
        job = _job_or_404(session, job_id)
        job.notes = notes.strip()[:5000] or None
        session.commit()
        return HTMLResponse('<span class="ok">Notas salvas.</span>')

    @app.post("/jobs/{job_id}/resume", response_class=HTMLResponse, dependencies=[Depends(local_post)])
    def generate_resume(request: Request, job_id: int, use_llm: str = Form("1"), session: Session = Depends(db)):
        from ..resume_generator import ResumeGenerator

        job = _job_or_404(session, job_id)
        repo = JobRepository(session)
        error = None
        if s.mode is Mode.DISCOVERY:
            error = "Modo DISCOVERY não gera currículo. Use DRY_RUN ou ASSISTED."
        else:
            try:
                ResumeGenerator(config or load_config(), s).generate(job, session, use_llm=use_llm == "1")
            except Exception as e:  # noqa: BLE001 — mostrado na tela; detalhes no log
                session.rollback()
                error = f"Falha ao gerar currículo: {e}"
        resp = templates.TemplateResponse(request, "_resumes.html", dict(
            request=request, job=job, resumes=repo.resumes(job.id), error=error))
        resp.headers["HX-Trigger"] = "statusChanged"
        return resp

    @app.get("/jobs/{job_id}/status-panel", response_class=HTMLResponse)
    def status_panel(request: Request, job_id: int, session: Session = Depends(db)):
        job = _job_or_404(session, job_id)
        return templates.TemplateResponse(request, "_status.html", dict(
            request=request, job=job, error=None, history=list(reversed(job.history))))

    # ------------------------------------------------------------------ candidatura (etapa 9)
    def _answer_service():
        from ..application_assistant.answers import AnswerService
        return AnswerService(config or load_config(), s)

    def _apply_ctx(request: Request, session: Session, job, **extra) -> dict:
        from ..application_assistant.answers import AnswerService
        ctx = dict(request=request, job=job, a=job.analysis or {},
                   answers=AnswerService._answers(session, job),
                   resume=(JobRepository(session).resumes(job.id) or [None])[0],
                   checks=checklist(session, job), approval=current_approval(session, job),
                   error=None, problems=[])
        ctx.update(extra)
        return ctx

    def _partial(request: Request, name: str, ctx: dict, trigger: str | None = "checklistChanged"):
        resp = templates.TemplateResponse(request, name, ctx)
        if trigger:
            resp.headers["HX-Trigger"] = trigger
        return resp

    @app.get("/jobs/{job_id}/apply", response_class=HTMLResponse)
    def apply_page(request: Request, job_id: int, session: Session = Depends(db)):
        job = _job_or_404(session, job_id)
        return templates.TemplateResponse(request, "apply.html", _apply_ctx(request, session, job))

    @app.post("/jobs/{job_id}/answers/suggest", response_class=HTMLResponse, dependencies=[Depends(local_post)])
    def suggest_answers(request: Request, job_id: int, use_llm: str = Form("1"), session: Session = Depends(db)):
        job = _job_or_404(session, job_id)
        error = None
        if s.mode is Mode.DISCOVERY:
            error = "Modo DISCOVERY não prepara candidaturas."
        else:
            _answer_service().suggest_all(job, session, use_llm=use_llm == "1")
        return _partial(request, "_answers.html", _apply_ctx(request, session, job, error=error))

    @app.post("/jobs/{job_id}/answers/custom", response_class=HTMLResponse, dependencies=[Depends(local_post)])
    def custom_answer(request: Request, job_id: int, question: str = Form(...), use_llm: str = Form("1"),
                      session: Session = Depends(db)):
        job = _job_or_404(session, job_id)
        if question.strip():
            _answer_service().add_custom(job, session, question[:1000], use_llm=use_llm == "1")
        return _partial(request, "_answers.html", _apply_ctx(request, session, job))

    def _answer_or_404(session: Session, answer_id: int):
        from ..database import Answer
        ans = session.get(Answer, answer_id)
        if not ans:
            raise HTTPException(404, "resposta não encontrada")
        return ans

    @app.post("/answers/{answer_id}", response_class=HTMLResponse, dependencies=[Depends(local_post)])
    def save_answer(request: Request, answer_id: int, text: str = Form(""), session: Session = Depends(db)):
        ans = _answer_or_404(session, answer_id)
        job = _job_or_404(session, ans.job_id)
        _answer_service().save(ans, text[:5000], job)
        session.commit()
        return _partial(request, "_answer.html", dict(request=request, ans=ans, saved=True))

    @app.post("/answers/{answer_id}/use-suggestion", response_class=HTMLResponse, dependencies=[Depends(local_post)])
    def use_suggestion(request: Request, answer_id: int, session: Session = Depends(db)):
        ans = _answer_or_404(session, answer_id)
        ans.final_text, ans.reviewed = ans.suggested, False   # volta à sugestão; precisa revisar de novo
        job = _job_or_404(session, ans.job_id)
        revoke(session, job, f"resposta trocada pela sugestão: {ans.question[:60]}")
        session.commit()
        return _partial(request, "_answer.html", dict(request=request, ans=ans, saved=False))

    @app.post("/answers/{answer_id}/delete", response_class=HTMLResponse, dependencies=[Depends(local_post)])
    def delete_answer(request: Request, answer_id: int, session: Session = Depends(db)):
        ans = _answer_or_404(session, answer_id)
        job = _job_or_404(session, ans.job_id)
        if not ans.key.startswith("custom:"):
            raise HTTPException(400, "só perguntas adicionadas por você podem ser removidas")
        session.delete(ans)
        revoke(session, job, "pergunta removida")
        session.commit()
        return _partial(request, "_answers.html", _apply_ctx(request, session, job))

    @app.get("/jobs/{job_id}/approval-panel", response_class=HTMLResponse)
    def approval_panel(request: Request, job_id: int, session: Session = Depends(db)):
        job = _job_or_404(session, job_id)
        return templates.TemplateResponse(request, "_approval.html", _apply_ctx(request, session, job))

    @app.post("/jobs/{job_id}/approve", response_class=HTMLResponse, dependencies=[Depends(local_post)])
    def approve_job(request: Request, job_id: int, resume_reviewed: str = Form(""), risks_ack: str = Form(""),
                    note: str = Form(""), session: Session = Depends(db)):
        job = _job_or_404(session, job_id)
        problems: list[str] = []
        try:
            approve(session, job, resume_reviewed=resume_reviewed == "1", risks_acknowledged=risks_ack == "1",
                    note=note[:1000])
        except ApprovalError as e:
            session.rollback()
            problems = e.problems
        return templates.TemplateResponse(request, "_approval.html",
                                          _apply_ctx(request, session, job, problems=problems))

    @app.post("/jobs/{job_id}/revoke", response_class=HTMLResponse, dependencies=[Depends(local_post)])
    def revoke_job(request: Request, job_id: int, session: Session = Depends(db)):
        job = _job_or_404(session, job_id)
        revoke(session, job, "revogada por você")
        session.commit()
        return templates.TemplateResponse(request, "_approval.html", _apply_ctx(request, session, job))

    @app.post("/jobs/{job_id}/applied", response_class=HTMLResponse, dependencies=[Depends(local_post)])
    def mark_applied(request: Request, job_id: int, confirm: str = Form(""), session: Session = Depends(db)):
        job = _job_or_404(session, job_id)
        problems = []
        if current_approval(session, job) is None:
            problems.append("Não há aprovação vigente para o currículo e as respostas atuais.")
        if confirm != "1":
            problems.append("Confirme que você mesmo enviou a candidatura no site da empresa.")
        if not problems:
            JobRepository(session).set_status(job, JobStatus.APPLIED, note="enviada por você (registro manual)")
            session.commit()
        return templates.TemplateResponse(request, "_approval.html",
                                          _apply_ctx(request, session, job, problems=problems))

    # ------------------------------------------------------------------ arquivos
    @app.get("/resumes/{resume_id}/{name}")
    def resume_file(request: Request, resume_id: int, name: str, session: Session = Depends(db)):
        if name not in SERVABLE:
            raise HTTPException(404)
        rec = session.get(ResumeRecord, resume_id)
        if not rec:
            raise HTTPException(404)
        base = s.applications_dir.resolve()
        path = (Path(rec.directory) / name).resolve()
        if not path.is_relative_to(base) or not path.is_file():
            raise HTTPException(404, "arquivo não encontrado (foi movido ou apagado?)")
        if SERVABLE[name] == "md":
            html = _md.render(path.read_text(encoding="utf-8"))
            return templates.TemplateResponse(request, "markdown.html", dict(
                request=request, title=name, html=html, rec=rec, job=JobRepository(session).get(rec.job_id)))
        return FileResponse(path, media_type=SERVABLE[name],
                            headers={"Content-Disposition": f'inline; filename="{name}"'})

    @app.get("/healthz")
    def health():
        return {"ok": True, "mode": s.mode.value}

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        detail = getattr(exc, "detail", "Não encontrado")
        return templates.TemplateResponse(request, "error.html", dict(request=request, msg=detail), status_code=404)

    return app


def run(port: int = 8765, open_browser: bool = False) -> None:
    import threading
    import webbrowser

    import uvicorn

    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
