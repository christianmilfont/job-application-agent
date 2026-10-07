import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jobagent.application_assistant.answers import AnswerDraft, AnswerService
from jobagent.application_assistant.approval import ApprovalError, approve, checklist, current_approval
from jobagent.config import AppConfig, Settings
from jobagent.dashboard import create_app
from jobagent.database import Answer, Job, JobRepository, JobStatus, init_db, make_engine, make_session_factory
from jobagent.resume_generator import ResumeGenerator
from jobagent.resume_generator.llm import LLMResult

DEVOPS = ("Sobre a vaga\nModelo híbrido em São Paulo.\nRequisitos:\n- AWS\n- Kubernetes\n- Terraform\n"
          "- Inglês avançado\nDiferenciais:\n- Datadog")
HX = {"HX-Request": "true"}


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(v, raising=False)
    return Settings(_env_file=None, applications_dir=tmp_path / "applications")


_seq = iter(range(10_000))


def _job(session, risks=("Exige 5+ anos",)):
    n = next(_seq)
    job = Job(url_key=f"a{n}", content_key=f"a{n}", source="manual", url="https://x/vaga", apply_url="https://x/apply",
              company="Acme", title="Engenheiro DevOps Júnior", location="São Paulo, SP", modality="HYBRID",
              seniority="junior", description=DEVOPS, parsed={}, raw={}, score=85, score_band="ALTA",
              analysis={"match": [], "gaps": [], "risks": list(risks)}, status=JobStatus.APPROVED.value)
    JobRepository(session).add(job)
    session.commit()
    return job


def _ready(config, session, settings, **kw):
    job = _job(session, **kw)
    ResumeGenerator(config, settings).generate(job, session, use_llm=False)
    svc = AnswerService(config, settings)
    answers = svc.suggest_all(job, session, use_llm=False)
    return job, svc, answers


def _fill_all(svc, session, job, answers):
    for a in answers:
        svc.save(a, a.final_text or "Resposta escrita por mim.", job)
    session.commit()


# ------------------------------------------------------------------ sugestões
def test_standard_answers_from_profile(config, session, settings):
    job, svc, answers = _ready(config, session, settings)
    by = {a.key: a for a in answers}
    assert by["motivation"].source == "template" and "Engenheiro DevOps Júnior" in by["motivation"].final_text
    assert by["salary"].needs_input and not by["salary"].final_text           # nunca sugere salário
    assert by["availability"].needs_input
    assert by["work_model"].final_text.startswith("Sim")                       # híbrido em SP = sua preferência
    assert by["english"].needs_input                                           # idiomas vazios no perfil
    k8s = by["exp:kubernetes"].final_text
    assert "ToggleMaster" in k8s and "Ainda não tenho experiência profissional" in k8s
    dd = by["exp:datadog"].final_text
    assert dd.startswith("Ainda não tenho experiência com Datadog") and "OpenTelemetry" in dd
    assert "github.com/christianmilfont" in by["links"].final_text


def test_experience_answer_does_not_overclaim_specific_term(config, settings):
    svc = AnswerService(config, settings)
    rhel = svc.experience_text("linux", "RHEL")
    assert rhel.startswith("Não especificamente com RHEL") and "Linux" in rhel
    tcp = svc.experience_text("networking", "TCP/IP")
    assert tcp.startswith("Não especificamente com TCP/IP") and "VPC" in tcp
    assert svc.experience_text("linux", "Linux").startswith("Sim")
    assert svc.experience_text("messaging", "SQS").startswith("Sim")
    assert svc.experience_text("messaging", "Kafka").startswith("Não especificamente com Kafka")


def test_motivation_template_passes_validator(config, session, settings):
    job, svc, answers = _ready(config, session, settings)
    text = next(a for a in answers if a.key == "motivation").final_text
    assert svc.validator.validate_text(text, ignore=[job.title, job.company]) == []


def test_salary_comes_only_from_profile(config, session, settings):
    data = copy.deepcopy(config.profile)
    data["application_preferences"]["salary"] = {"clt_monthly": 6000, "pj_monthly": None, "note": "negociável"}
    cfg = AppConfig(profile=data, sources=config.sources, taxonomy=config.taxonomy, scoring=config.scoring)
    job = _job(session)
    by = {a.key: a for a in AnswerService(cfg, settings).suggest_all(job, session, use_llm=False)}
    assert by["salary"].final_text == "Minha pretensão é de R$ 6.000 mensais (CLT). negociável."
    assert not by["salary"].needs_input


class FakeProvider:
    name, model = "fake", "fake"

    def __init__(self, answers):
        self.answers = list(answers)

    def generate(self, system, user, output_format=None):
        assert output_format is AnswerDraft
        return LLMResult(AnswerDraft(answer=self.answers.pop(0)), {})


def test_llm_motivation_validated_with_retry(config, session, settings):
    job = _job(session)
    good = "Tenho interesse pela vaga porque utilizei Kubernetes e Terraform no projeto ToggleMaster."
    svc = AnswerService(config, settings, provider=FakeProvider(["Tenho 3 anos de experiência com Kafka.", good]))
    by = {a.key: a for a in svc.suggest_all(job, session)}
    assert by["motivation"].source == "llm" and by["motivation"].final_text == good


def test_llm_motivation_falls_back_to_template(config, session, settings):
    job = _job(session)
    bad = "Atuei como SRE sênior em produção."
    svc = AnswerService(config, settings, provider=FakeProvider([bad, bad]))
    by = {a.key: a for a in svc.suggest_all(job, session)}
    assert by["motivation"].source == "template"


def test_regenerating_never_overwrites_reviewed(config, session, settings):
    job, svc, answers = _ready(config, session, settings)
    mot = next(a for a in answers if a.key == "motivation")
    svc.save(mot, "Minha versão.", job)
    session.commit()
    svc.suggest_all(job, session, use_llm=False)
    assert session.get(Answer, mot.id).final_text == "Minha versão."


def test_custom_questions(config, session, settings):
    job = _job(session)
    svc = AnswerService(config, settings)
    sal = svc.add_custom(job, session, "Qual a sua expectativa salarial?", use_llm=False)
    assert sal.needs_input and not sal.final_text
    exp = svc.add_custom(job, session, "Você tem experiência com Terraform?", use_llm=False)
    assert "ToggleMaster" in exp.final_text
    other = svc.add_custom(job, session, "Conte sobre um desafio que você superou.", use_llm=False)
    assert other.needs_input and other.source == "manual"


def test_edited_answer_gets_non_blocking_warnings(config, session, settings):
    job, svc, answers = _ready(config, session, settings)
    mot = next(a for a in answers if a.key == "motivation")
    warnings = svc.save(mot, "Tenho 5 anos de experiência com Kafka.", job)
    assert warnings and mot.reviewed


# ------------------------------------------------------------------ aprovação
def test_approval_requires_everything(config, session, settings):
    job, svc, answers = _ready(config, session, settings)
    with pytest.raises(ApprovalError) as e:
        approve(session, job, resume_reviewed=True, risks_acknowledged=True)
    assert any("pendente" in p for p in e.value.problems)
    _fill_all(svc, session, job, answers)
    with pytest.raises(ApprovalError) as e:
        approve(session, job, resume_reviewed=False, risks_acknowledged=False)
    assert len(e.value.problems) == 2
    assert all(c.ok for c in checklist(session, job))


def test_approve_exports_and_sets_ready(config, session, settings):
    job, svc, answers = _ready(config, session, settings)
    _fill_all(svc, session, job, answers)
    appr = approve(session, job, resume_reviewed=True, risks_acknowledged=True, note="ok")
    assert job.status == JobStatus.READY_TO_APPLY.value and current_approval(session, job).id == appr.id
    d = Path(JobRepository(session).resumes(job.id)[0].directory)
    assert "Por que você quer trabalhar conosco?" in (d / "answers.md").read_text(encoding="utf-8")
    meta = json.loads((d / "application.json").read_text(encoding="utf-8"))
    assert meta["approval"]["snapshot_hash"] == appr.snapshot_hash and meta["answers"]


def test_edit_after_approval_revokes(config, session, settings):
    job, svc, answers = _ready(config, session, settings)
    _fill_all(svc, session, job, answers)
    approve(session, job, resume_reviewed=True, risks_acknowledged=True)
    svc.save(answers[0], "Texto novo.", job)
    session.commit()
    assert current_approval(session, job) is None and job.status == JobStatus.RESUME_GENERATED.value


def test_regenerating_resume_revokes(config, session, settings):
    job, svc, answers = _ready(config, session, settings)
    _fill_all(svc, session, job, answers)
    approve(session, job, resume_reviewed=True, risks_acknowledged=True)
    ResumeGenerator(config, settings).generate(job, session, use_llm=False)
    assert current_approval(session, job) is None and job.status == JobStatus.RESUME_GENERATED.value


def test_tampered_pdf_invalidates_approval(config, session, settings):
    job, svc, answers = _ready(config, session, settings)
    _fill_all(svc, session, job, answers)
    approve(session, job, resume_reviewed=True, risks_acknowledged=True)
    pdf = Path(JobRepository(session).resumes(job.id)[0].directory) / "resume.pdf"
    pdf.write_bytes(pdf.read_bytes() + b"\n% editado")
    assert current_approval(session, job) is None


# ------------------------------------------------------------------ dashboard
def test_dashboard_full_flow(tmp_path, config, settings):
    url = f"sqlite:///{(tmp_path / 'f.db').as_posix()}"
    st = settings.model_copy(update={"database_url": url})
    engine = make_engine(url)
    init_db(engine)
    Session_ = make_session_factory(engine)
    with Session_() as s:
        job_id = _job(s, risks=()).id
    client = TestClient(create_app(st, config, extra_hosts=["testserver"]))

    assert client.post(f"/jobs/{job_id}/resume", data={"use_llm": "0"}, headers=HX).status_code == 200
    r = client.post(f"/jobs/{job_id}/answers/suggest", data={"use_llm": "0"}, headers=HX)
    assert "Por que você quer trabalhar conosco?" in r.text and r.headers["HX-Trigger"] == "checklistChanged"
    r = client.post(f"/jobs/{job_id}/approve", data={"resume_reviewed": "1"}, headers=HX)
    assert "Ainda não dá para aprovar" in r.text

    with Session_() as s:
        ids = [(a.id, a.final_text) for a in s.query(Answer).filter_by(job_id=job_id)]
    for aid, text in ids:
        assert client.post(f"/answers/{aid}", data={"text": text or "Minha resposta."}, headers=HX).status_code == 200
    r = client.post(f"/jobs/{job_id}/approve", data={"resume_reviewed": "1"}, headers=HX)
    assert "Aprovada ✓" in r.text and "https://x/apply" in r.text

    assert "Confirme que você mesmo" in client.post(f"/jobs/{job_id}/applied", data={}, headers=HX).text
    r = client.post(f"/jobs/{job_id}/applied", data={"confirm": "1"}, headers=HX)
    assert "Candidatura registrada" in r.text
    with Session_() as s:
        assert s.get(Job, job_id).status == JobStatus.APPLIED.value
    assert client.get(f"/jobs/{job_id}/apply").status_code == 200
