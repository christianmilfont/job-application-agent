from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from jobagent.config import Mode, Settings
from jobagent.dashboard import create_app
from jobagent.database import Job, JobRepository, JobStatus, ResumeRecord, init_db, make_engine, make_session_factory

HX = {"HX-Request": "true"}


@pytest.fixture
def env(tmp_path, monkeypatch, config):
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(v, raising=False)
    url = f"sqlite:///{(tmp_path / 'd.db').as_posix()}"
    settings = Settings(_env_file=None, database_url=url, applications_dir=tmp_path / "applications")
    engine = make_engine(url)
    init_db(engine)
    s = make_session_factory(engine)()
    repo = JobRepository(s)
    now = datetime.now(timezone.utc)
    rows = [("Engenheiro DevOps Júnior", "Acme", "São Paulo, SP", 88, now - timedelta(days=2)),
            ("Senior SRE", "Beta", "Remoto", 45, now - timedelta(days=40)),
            ("Backend Developer Pleno", "Acme", "Curitiba, PR", 66, now - timedelta(days=10))]
    for i, (title, company, loc, score, posted) in enumerate(rows):
        repo.add(Job(url_key=f"u{i}", content_key=f"c{i}", source="manual", url=f"https://example.com/{i}",
                     company=company, title=title, location=loc, modality="HYBRID", seniority="junior",
                     description="Requisitos:\n- AWS\n- Kubernetes\n- Terraform", parsed={}, raw={},
                     score=score, score_band="ALTA", posted_at=posted, status=JobStatus.ANALYZED.value,
                     analysis={"match": ["AWS (obrigatório)"], "gaps": ["Datadog (desejável)"],
                               "risks": ["Exige 5+ anos"], "breakdown": {}}))
    s.commit()
    s.close()
    app = create_app(settings, config, extra_hosts=["testserver"])
    return TestClient(app), settings, make_session_factory(engine)


def test_index_lists_and_filters(env):
    client, _, _ = env
    html = client.get("/").text
    assert "Engenheiro DevOps Júnior" in html and "Senior SRE" in html
    assert "Senior SRE" not in client.get("/?min_score=60").text
    assert "Backend Developer" not in client.get("/?days=7").text
    r = client.get("/?company=beta", headers=HX)
    assert "Senior SRE" in r.text and "<html" not in r.text      # HTMX recebe só a tabela
    assert "Curitiba" in client.get("/?location=curitiba").text
    assert "Nenhuma vaga" in client.get("/?status=OFFER").text


def test_detail_page(env):
    client, _, _ = env
    html = client.get("/jobs/1").text
    assert "MATCH" in html and "Datadog (desejável)" in html and "Exige 5+ anos" in html
    assert client.get("/jobs/999").status_code == 404


def test_post_requires_dashboard_origin(env):
    client, _, _ = env
    assert client.post("/jobs/1/status", data={"status": "APPROVED"}).status_code == 403
    r = client.post("/jobs/1/status", data={"status": "APPROVED"},
                    headers={**HX, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_rejects_foreign_host(env):
    client, _, _ = env
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400


def test_status_change_and_history(env):
    client, _, Session_ = env
    r = client.post("/jobs/1/status", data={"status": "APPROVED", "note": "gostei"}, headers=HX)
    assert r.status_code == 200 and "gostei" in r.text
    with Session_() as s:
        job = s.get(Job, 1)
        assert job.status == "APPROVED" and job.history[-1].note == "gostei"


def test_workflow_rules(env):
    client, _, Session_ = env
    r = client.post("/jobs/1/status", data={"status": "READY_TO_APPLY"}, headers=HX)
    assert "página de candidatura" in r.text
    r = client.post("/jobs/1/status", data={"status": "APPLIED"}, headers=HX)
    assert "aprovada" in r.text
    r = client.post("/jobs/1/status", data={"status": "INTERVIEW"}, headers=HX)
    assert "APPLIED" in r.text
    with Session_() as s:
        assert s.get(Job, 1).status == "ANALYZED"


def test_generate_resume_and_serve_files(env):
    client, settings, Session_ = env
    r = client.post("/jobs/1/resume", data={"use_llm": "0"}, headers=HX)
    assert r.status_code == 200 and "ATS" in r.text and r.headers.get("HX-Trigger") == "statusChanged"
    with Session_() as s:
        rec = s.query(ResumeRecord).one()
        assert s.get(Job, 1).status == "RESUME_GENERATED"
    pdf = client.get(f"/resumes/{rec.id}/resume.pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    analysis = client.get(f"/resumes/{rec.id}/analysis.md")
    assert "<table>" in analysis.text and "ATS" in analysis.text
    assert client.get(f"/resumes/{rec.id}/..%2F..%2Fsecret.txt").status_code == 404
    assert client.get(f"/resumes/{rec.id}/notes.txt").status_code == 404
    # mesmo com currículo, "pronta" só pelo fluxo de aprovação
    assert "página de candidatura" in client.post("/jobs/1/status", data={"status": "READY_TO_APPLY"}, headers=HX).text


def test_files_outside_applications_dir_are_not_served(env, tmp_path):
    client, _, Session_ = env
    outside = tmp_path / "fora"
    outside.mkdir()
    (outside / "resume.pdf").write_bytes(b"%PDF-1.4 segredo")
    with Session_() as s:
        rec = ResumeRecord(job_id=1, directory=str(outside), generator="deterministic", ats_score=1)
        s.add(rec)
        s.commit()
        rid = rec.id
    assert client.get(f"/resumes/{rid}/resume.pdf").status_code == 404


def test_discovery_mode_blocks_generation(env, config):
    _, settings, _ = env
    client = TestClient(create_app(settings.model_copy(update={"mode": Mode.DISCOVERY}), config,
                                   extra_hosts=["testserver"]))
    r = client.post("/jobs/1/resume", data={"use_llm": "0"}, headers=HX)
    assert "DISCOVERY" in r.text
