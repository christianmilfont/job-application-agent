from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from sqlalchemy.orm import Session

from .config import get_settings, load_config
from .database import JobRepository, JobStatus, init_db, make_engine, make_session_factory
from .job_collector import ManualImportCollector, PoliteClient, build_collectors
from .logging_setup import setup_logging
from .pipeline import DiscoveryPipeline, DiscoveryResult, rescore_all

# Windows: garante acentos corretos mesmo com saída redirecionada (cp1252 por padrão).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(help="Job Application Agent — descoberta e análise de vagas (uso pessoal).",
                  no_args_is_help=True, add_completion=False)
console = Console()

_BAND_COLOR = {"EXCELENTE": "bold green", "ALTA": "green", "POSSÍVEL": "yellow", "NÃO PRIORIZAR": "dim"}


@contextmanager
def _session() -> Iterator[Session]:
    engine = make_engine()
    init_db(engine)
    session = make_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()


def _banner() -> None:
    setup_logging()
    console.print(f"[dim]modo:[/] [bold]{get_settings().mode.value}[/]  [dim]· nenhum envio de candidatura nesta etapa[/]")


def _print_result(result: DiscoveryResult, persisted: bool, min_show: int) -> None:
    s = result.stats
    console.print(f"\nColetadas: {s['collected']} · novas: [bold]{s['new']}[/] · duplicadas: {s['duplicate']}")
    for k, v in sorted(s.items()):
        if k.startswith("filtered"):
            console.print(f"  [dim]{k}: {v}[/]")
    for e in result.errors:
        console.print(f"[red]![/] {e}")
    jobs = sorted(result.new_jobs, key=lambda j: j.score or 0, reverse=True)
    shown = [j for j in jobs if (j.score or 0) >= min_show]
    if shown:
        _jobs_table(shown, title=f"Novas vagas com score ≥ {min_show}")
    if not persisted:
        console.print("[yellow]--preview: nada foi salvo no banco.[/]")


def _jobs_table(jobs, title: str = "Vagas") -> None:  # noqa: ANN001
    t = Table(title=title, show_lines=False)
    for col in ("ID", "Match", "Cargo", "Empresa", "Local", "Mod.", "Nível", "Status", "Publicada"):
        t.add_column(col, overflow="fold")
    for j in jobs:
        color = _BAND_COLOR.get(j.score_band or "", "")
        t.add_row(str(j.id or "-"), f"[{color}]{j.score}[/]", j.title, j.company, j.location or "-",
                  j.modality or "-", j.seniority or "-", j.status,
                  j.posted_at.strftime("%Y-%m-%d") if j.posted_at else "-")
    console.print(t)


@app.command()
def init() -> None:
    """Cria o banco de dados e as pastas locais."""
    s = get_settings()
    for d in (s.log_dir, s.applications_dir):
        d.mkdir(parents=True, exist_ok=True)
    init_db(make_engine())
    console.print(f"[green]OK[/] banco: {s.database_url}")


@app.command()
def discover(
    source: Optional[str] = typer.Option(None, help="greenhouse | lever"),
    company: Optional[str] = typer.Option(None, help="filtra boards pelo token/nome"),
    preview: bool = typer.Option(False, help="não salva nada no banco"),
    show: int = typer.Option(60, help="mostra novas vagas com score ≥ N"),
) -> None:
    """Coleta vagas nas fontes habilitadas, analisa e pontua."""
    _banner()
    cfg = load_config()
    with PoliteClient() as client, _session() as session:
        collectors = build_collectors(cfg.sources, client, only=source, company=company)
        if not collectors:
            console.print("[red]Nenhuma fonte habilitada/encontrada em config/sources.yaml[/]")
            raise typer.Exit(1)
        boards = sum(len(c.boards) for c in collectors)  # type: ignore[attr-defined]
        console.print(f"Consultando {boards} board(s) com intervalo ≥ {get_settings().http_min_interval_s}s por host...")
        result = DiscoveryPipeline(cfg, session, get_settings().mode).run(collectors, persist=not preview)
        console.print(f"[dim]requisições HTTP: {client.requests_made}[/]")
    _print_result(result, not preview, show)


@app.command("import")
def import_jobs(
    file: Path = typer.Argument(..., exists=True, readable=True, help="YAML/JSON com vagas copiadas manualmente"),
    preview: bool = typer.Option(False, help="não salva nada no banco"),
) -> None:
    """Importa vagas copiadas manualmente (LinkedIn, Gupy, indicação...)."""
    _banner()
    cfg = load_config()
    with _session() as session:
        result = DiscoveryPipeline(cfg, session, get_settings().mode).run(
            [ManualImportCollector(file)], persist=not preview)
    _print_result(result, not preview, 0)


@app.command("list")
def list_jobs(
    min_score: Optional[int] = typer.Option(None, "--min-score", "-m"),
    status: Optional[str] = typer.Option(None, "--status", "-s"),
    company: Optional[str] = typer.Option(None, "--company", "-c"),
    title: Optional[str] = typer.Option(None, "--title", "-t"),
    location: Optional[str] = typer.Option(None, "--location", "-l"),
    days: Optional[int] = typer.Option(None, "--days", "-d", help="publicadas nos últimos N dias"),
    limit: int = typer.Option(50),
) -> None:
    """Lista vagas com filtros (match, status, empresa, cargo, local, data)."""
    since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    with _session() as session:
        jobs = JobRepository(session).search(min_score=min_score, status=status, company=company,
                                             title=title, location=location, since=since, limit=limit)
        if not jobs:
            console.print("Nenhuma vaga encontrada com esses filtros.")
            return
        _jobs_table(jobs)


@app.command()
def show(job_id: int, description: bool = typer.Option(False, "--description", help="mostra a descrição completa")) -> None:
    """Mostra a análise completa de uma vaga: MATCH / GAPS / RISKS e breakdown do score."""
    with _session() as session:
        job = JobRepository(session).get(job_id)
        if not job:
            console.print(f"[red]Vaga {job_id} não encontrada[/]")
            raise typer.Exit(1)
        a, p = job.analysis or {}, job.parsed or {}
        color = _BAND_COLOR.get(job.score_band or "", "")
        console.print(Panel(
            f"[bold]{job.title}[/] — {job.company}\n"
            f"{job.location or '-'} · {job.modality} · nível: {job.seniority} · status: {job.status}\n"
            f"Publicada: {job.posted_at.date() if job.posted_at else '-'} · candidatura: {job.apply_type}\n"
            f"{job.url}",
            title=f"[{color}]Match {job.score} — {job.score_band}[/]"))

        bd = Table(title="Composição do score", show_header=True)
        for col in ("Critério", "Pontos", "Detalhe"):
            bd.add_column(col)
        for k, c in (a.get("breakdown") or {}).items():
            bd.add_row(k, f"{c['points']:.1f}/{c['max']:g}", c["detail"])
        for pen in a.get("penalties") or []:
            bd.add_row("penalidade", pen.split(":")[0], pen.split(":", 1)[1].strip())
        console.print(bd)

        for label, key, style in (("MATCH", "match", "green"), ("GAPS", "gaps", "yellow"), ("RISKS", "risks", "red")):
            items = a.get(key) or []
            console.print(f"\n[bold {style}]{label}[/] ({len(items)})")
            for item in items or ["—"]:
                console.print(f"  • {item}")

        console.print(f"\n[dim]Tecnologias: {', '.join(p.get('technologies') or []) or '-'}[/]")
        if p.get("requirements_text"):
            console.print(Panel(p["requirements_text"][:2500], title="Requisitos (extraídos)"))
        if p.get("desired_text"):
            console.print(Panel(p["desired_text"][:1500], title="Desejáveis (extraídos)"))
        if description:
            console.print(Panel(job.description, title="Descrição"))


@app.command()
def status(job_id: int, new_status: str, note: Optional[str] = typer.Option(None)) -> None:
    """Atualiza o status de uma vaga (ex.: REJECTED, APPLIED após você aplicar manualmente)."""
    try:
        st = JobStatus(new_status.upper())
    except ValueError:
        console.print(f"[red]Status inválido.[/] Use: {', '.join(s.value for s in JobStatus)}")
        raise typer.Exit(1)
    with _session() as session:
        repo = JobRepository(session)
        job = repo.get(job_id)
        if not job:
            console.print(f"[red]Vaga {job_id} não encontrada[/]")
            raise typer.Exit(1)
        from .application_assistant.approval import current_approval
        from .application_assistant.workflow import transition_error

        error = transition_error(job, st, has_resume=bool(repo.resumes(job.id)),
                                 has_approval=current_approval(session, job) is not None)
        if error:
            console.print(f"[red]Bloqueado:[/] {error}")
            raise typer.Exit(1)
        old = job.status
        repo.set_status(job, st, note)
        session.commit()
        console.print(f"Vaga {job_id}: {old} → [bold]{st.value}[/]")


@app.command()
def rescore() -> None:
    """Recalcula o score de todas as vagas (após editar profile/scoring/taxonomia)."""
    with _session() as session:
        n = rescore_all(load_config(), session)
    console.print(f"[green]{n} vaga(s) recalculada(s)[/]")


@app.command()
def profile() -> None:
    """Valida o profile.yaml e mostra a evidência derivada de cada skill e projeto."""
    from .job_parser import Taxonomy
    from .profile import Profile, ProfileIndex

    cfg = load_config()
    try:
        prof = Profile.from_dict(cfg.profile)
    except Exception as e:  # noqa: BLE001 — erro de schema do pydantic, mostrado ao usuário
        console.print(f"[red]profile.yaml inválido:[/]\n{e}")
        raise typer.Exit(1)
    tax = Taxonomy(cfg.taxonomy)
    idx = ProfileIndex(prof, tax)
    t = Table(title="Projetos → skills derivadas")
    for col in ("Projeto", "Status", "Skills (IDs da taxonomia)", "Planejado (não conta)"):
        t.add_column(col, overflow="fold")
    for pr in prof.projects:
        t.add_row(pr.name, pr.status, ", ".join(sorted(idx.project_skills.get(pr.id, []))),
                  "; ".join(i.text for i in pr.planned) or "-")
    console.print(t)
    ev = Table(title="Evidência por skill")
    ev.add_column("Skill")
    ev.add_column("Evidência", overflow="fold")
    for sid in sorted(idx.owned_skills, key=tax.name):
        ev.add_row(tax.name(sid), idx.describe(sid) or "-")
    console.print(ev)
    problems = []
    if idx.unknown_skill_ids:
        problems.append(f"skills fora da taxonomia: {', '.join(idx.unknown_skill_ids)}")
    for pid, items in idx.unmapped_stack.items():
        problems.append(f"{pid}: itens do stack sem tecnologia reconhecida: {', '.join(items)}")
    todos = [k for k, v in (("personal.email", prof.personal.email), ("languages", prof.languages)) if not v]
    todos += [f"projects.{p.id}.context" for p in prof.projects if not p.context]
    for p_ in problems:
        console.print(f"[red]![/] {p_}")
    if todos:
        console.print(f"[yellow]Campos a completar:[/] {', '.join(todos)}")
    if not problems:
        console.print("[green]OK[/] perfil válido.")


@app.command()
def resume(
    job_id: int,
    no_llm: bool = typer.Option(False, "--no-llm", help="não chama o LLM; só reorganiza o profile.yaml"),
) -> None:
    """Gera currículo adaptado + análise ATS em applications/<empresa-cargo>/."""
    from .config import Mode
    from .resume_generator import ResumeGenerator

    _banner()
    s = get_settings()
    if s.mode is Mode.DISCOVERY:
        console.print("[red]Modo DISCOVERY só encontra e analisa vagas.[/] Use DRY_RUN ou ASSISTED para gerar currículo.")
        raise typer.Exit(1)
    with _session() as session:
        job = JobRepository(session).get(job_id)
        if not job:
            console.print(f"[red]Vaga {job_id} não encontrada[/]")
            raise typer.Exit(1)
        if (job.score or 0) < 60:
            console.print(f"[yellow]Atenção:[/] score {job.score} (< 60). Gerando mesmo assim.")
        gen = ResumeGenerator(load_config(), s)
        if not no_llm and gen.llm_available():
            console.print(f"Gerando com [bold]{s.llm_provider}/{s.llm_model}[/] (effort {s.llm_effort})...")
        r = gen.generate(job, session, use_llm=not no_llm)

    label = f"LLM ({r.llm_meta.get('model_served')}, {r.attempts} tentativa(s))" if r.generator == "llm" else "determinístico"
    console.print(f"\n[green]OK[/] currículo gerado — {label}")
    if r.fallback_reason:
        console.print(f"[yellow]Modo determinístico:[/] {r.fallback_reason}")
    if r.rejected_violations:
        console.print(f"[dim]{len(r.rejected_violations)} violação(ões) em versões descartadas — ver analysis.md[/]")
    console.print(f"[bold]ATS Match: {r.ats.score}%[/]")
    for tip in r.ats.tips:
        console.print(f"  • {tip}")
    console.print(f"\nArquivos em: {r.directory}")


@app.command()
def answers(
    job_id: int,
    no_llm: bool = typer.Option(False, "--no-llm", help="sem LLM: só perfil e templates"),
) -> None:
    """Gera/atualiza sugestões de resposta para a vaga (edição e aprovação: no dashboard)."""
    from .application_assistant.answers import AnswerService
    from .config import Mode

    _banner()
    s = get_settings()
    if s.mode is Mode.DISCOVERY:
        console.print("[red]Modo DISCOVERY não prepara candidaturas.[/]")
        raise typer.Exit(1)
    with _session() as session:
        job = JobRepository(session).get(job_id)
        if not job:
            console.print(f"[red]Vaga {job_id} não encontrada[/]")
            raise typer.Exit(1)
        rows = AnswerService(load_config(), s).suggest_all(job, session, use_llm=not no_llm)
        for a in rows:
            tag = {"profile": "perfil", "template": "evidência", "llm": "Claude", "manual": "manual"}.get(a.source, a.source)
            state = "[green]revisada[/]" if a.reviewed else "[yellow]pendente[/]"
            console.print(Panel(a.final_text or "[red](precisa da sua resposta: o sistema não sugere este dado)[/]",
                                title=f"{a.question}", subtitle=f"{tag} · {state}", title_align="left"))
    console.print(f"Revise, edite e aprove em: [bold]jobagent dashboard[/] → /jobs/{job_id}/apply")


@app.command()
def review(job_id: int) -> None:
    """Mostra o checklist de aprovação da vaga."""
    from .application_assistant.approval import checklist, current_approval

    with _session() as session:
        job = JobRepository(session).get(job_id)
        if not job:
            console.print(f"[red]Vaga {job_id} não encontrada[/]")
            raise typer.Exit(1)
        console.print(f"[bold]{job.title}[/] — {job.company} · status {job.status}")
        for c in checklist(session, job):
            mark = "[green]✓[/]" if c.ok else "[red]✗[/]"
            console.print(f"  {mark} {c.label}" + (f" [dim]— {c.detail}[/]" if c.detail else ""))
        appr = current_approval(session, job)
        console.print(f"Aprovação vigente: {'[green]sim[/] (' + appr.approved_at.strftime('%d/%m/%Y %H:%M') + ')' if appr else '[yellow]não[/]'}")


@app.command()
def dashboard(
    port: int = typer.Option(8765, help="porta local"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="abre o navegador"),
) -> None:
    """Abre o dashboard local em http://127.0.0.1:<porta> (Ctrl+C para sair)."""
    from .dashboard import run

    setup_logging()
    console.print(f"Dashboard em [bold]http://127.0.0.1:{port}/[/] · modo {get_settings().mode.value} · Ctrl+C para sair")
    run(port=port, open_browser=open_browser)


@app.command()
def sources() -> None:
    """Mostra as fontes configuradas."""
    cfg = load_config()
    for name in ("greenhouse", "lever"):
        c = cfg.sources.get(name) or {}
        state = "[green]ativo[/]" if c.get("enabled") else "[dim]desativado[/]"
        tokens = ", ".join(b["token"] for b in c.get("boards") or [])
        console.print(f"{name}: {state} — {tokens}")
    console.print("manual: [green]sempre disponível[/] — jobagent import arquivo.yaml")


if __name__ == "__main__":
    app()
