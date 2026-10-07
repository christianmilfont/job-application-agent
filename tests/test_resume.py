import json
from types import SimpleNamespace

import pytest

from jobagent.config import Settings
from jobagent.database import Job, JobRepository, JobStatus, ResumeRecord
from jobagent.job_collector import RawJob
from jobagent.profile import Profile, ProfileIndex
from jobagent.resume_generator import LLMError, ResumeGenerator
from jobagent.resume_generator.ats import analyze_ats
from jobagent.resume_generator.generator import deterministic_draft
from jobagent.resume_generator.llm import AnthropicProvider, LLMResult
from jobagent.resume_generator.schema import ProjectEntry, ResumeDraft, SkillGroup
from jobagent.resume_generator.selector import build_plan
from jobagent.resume_generator.validator import ResumeValidator

DEVOPS = "Requisitos:\n- AWS\n- Kubernetes\n- Terraform\n- CI/CD com GitHub Actions\nDiferenciais:\n- Datadog"
JAVA = "Requisitos:\n- Java 17\n- Spring Boot\n- MySQL\nDiferenciais:\n- Spring AI"
GENAI = "Requisitos:\n- IA generativa e LLMs\n- RAG\n- MCP\nDiferenciais:\n- Google Cloud"


@pytest.fixture(scope="module")
def idx(config, taxonomy):
    return ProfileIndex(Profile.from_dict(config.profile), taxonomy)


def _parsed(parser, text, title="Engenheiro DevOps Júnior"):
    return parser.parse(RawJob(source="manual", external_id=None, url="https://x", company="X", title=title,
                               location="São Paulo, SP", description_text=text))


# ------------------------------------------------------------------ seletor
def test_selector_picks_project_by_job_stack(idx, parser):
    assert build_plan(idx, _parsed(parser, DEVOPS)).projects[0].project_id == "togglemaster"
    assert build_plan(idx, _parsed(parser, JAVA, "Desenvolvedor Java")).projects[0].project_id == "barberhub"
    genai = build_plan(idx, _parsed(parser, GENAI, "AI Engineer"))
    assert genai.projects[0].project_id == "genai_lab"
    assert genai.certification_ids[0] == "gcp_genai_leader"


def test_selector_separates_missing_skills(idx, parser):
    plan = build_plan(idx, _parsed(parser, DEVOPS))
    assert "datadog" in plan.missing_skills
    assert {"aws", "kubernetes", "terraform", "github_actions"} <= set(plan.matched_skills)


# ------------------------------------------------------------------ validador
def _clean(idx, parser):
    return deterministic_draft(idx, build_plan(idx, _parsed(parser, DEVOPS)))


def test_deterministic_draft_passes_validation(idx, parser):
    rep = ResumeValidator(idx).validate(_clean(idx, parser))
    assert rep.ok, rep.violations


@pytest.mark.parametrize("field,text,expected", [
    ("summary", "Projetos com Kafka e Kubernetes.", "'Kafka'"),                     # mesma família (SQS), termo inventado
    ("summary", "Infraestrutura com EC2 e Lambda.", "'EC2'"),                       # serviço AWS que não está no perfil
    ("summary", "Monitoramento com Datadog.", "Datadog"),                           # tecnologia fora do perfil
    ("summary", "Reduzi custos em 40% com Terraform.", "40%"),                      # métrica inventada
    ("summary", "Possuo AWS Certified Solutions Architect.", "Certificação AWS"),    # certificação que não tem
    ("summary", "Tenho 3 anos de experiência com cloud.", "anos de experi"),        # experiência inventada
    ("summary", "Atuei como engenheiro DevOps.", "atuação profissional"),
    ("summary", "Pós-graduado em DevOps pela FIAP.", "concluída"),                  # formação em andamento
    ("summary", "Assistente com integração ao WhatsApp.", "planejado"),             # funcionalidade planejada
    ("headline", "Engenheiro DevOps Sênior", "senioridade"),
])
def test_validator_blocks_invented_facts(idx, parser, field, text, expected):
    d = _clean(idx, parser).model_copy(update={field: text})
    rep = ResumeValidator(idx).validate(d)
    assert not rep.ok
    assert any(expected.lower() in v.lower() for v in rep.violations), rep.violations


def test_validator_checks_skill_group_names(idx, parser):
    d = _clean(idx, parser)
    d.skill_groups.append(SkillGroup(name="Observabilidade e SRE", skill_ids=["opentelemetry"]))
    assert any("grupos de competências" in v and "SRE" in v for v in ResumeValidator(idx).validate(d).violations)


def test_deterministic_summary_separates_project_from_declared(idx, parser):
    s = _clean(idx, parser).summary
    projects_part = s.split("Projetos práticos com ")[1].split(".")[0]
    assert "Kubernetes" in projects_part and "Linux" not in projects_part


def test_ats_uses_job_literal_terms(idx, parser):
    parsed = _parsed(parser, "Requisitos:\n- Mensageria com Kafka\n- SQS")
    ats = analyze_ats("Projetos com SQS", "Engenheiro", parsed, idx, ["junior"])
    assert "Kafka" in ats.do_not_add and "Kafka" not in ats.add_these
    assert any(c.keyword == "SQS" and c.present for c in ats.checks)


def test_validator_blocks_tech_from_other_project(idx, parser):
    d = _clean(idx, parser)
    d.projects = [ProjectEntry(project_id="barberhub", bullets=["Deploy do BarberHub em Kubernetes com Terraform."])]
    rep = ResumeValidator(idx).validate(d)
    assert any("Kubernetes" in v and "para este projeto" in v for v in rep.violations)


def test_validator_allows_planned_when_marked(idx, parser):
    d = _clean(idx, parser)
    d.projects = [ProjectEntry(project_id="celeste", bullets=["Integração com WhatsApp planejada para as próximas versões."])]
    assert ResumeValidator(idx).validate(d).ok


def test_validator_blocks_unknown_ids(idx, parser):
    d = _clean(idx, parser)
    d.skill_groups.append(SkillGroup(name="X", skill_ids=["datadog"]))
    d.certification_ids.append("cka")
    d.projects.append(ProjectEntry(project_id="inventado", bullets=["x"]))
    v = " | ".join(ResumeValidator(idx).validate(d).violations)
    assert "datadog" in v and "cka" in v and "inventado" in v


# ------------------------------------------------------------------ ATS
def test_ats_splits_addable_and_not_addable(idx, parser):
    parsed = _parsed(parser, "Requisitos:\n- SQL\n- Docker\n- Datadog")
    ats = analyze_ats("Competências: Docker, PostgreSQL", "Engenheiro DevOps", parsed, idx, ["junior"])
    assert "SQL" in ats.add_these              # você cobre (via PostgreSQL) mas não está escrito
    assert "Datadog" in ats.do_not_add         # você não possui
    assert 0 < ats.score < 100


# ------------------------------------------------------------------ pacote completo
_seq = iter(range(10_000))


def _job(session, text=DEVOPS):
    n = next(_seq)
    job = Job(url_key=f"u{n}", source_key=None, content_key=f"c{n}", source="manual", url="https://x/vaga",
              company="Acme", title="Engenheiro DevOps Júnior", location="São Paulo, SP", modality="HYBRID",
              seniority="junior", description=text, parsed={}, raw={}, score=88, score_band="ALTA",
              analysis={"match": ["AWS"], "gaps": [], "risks": []}, status=JobStatus.APPROVED.value)
    JobRepository(session).add(job)
    session.commit()
    return job


@pytest.fixture
def gen_settings(tmp_path, monkeypatch):
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(v, raising=False)
    return Settings(_env_file=None, applications_dir=tmp_path / "applications")


def test_deterministic_package(config, session, gen_settings):
    job = _job(session)
    r = ResumeGenerator(config, gen_settings).generate(job, session, use_llm=True)   # sem credencial -> determinístico
    assert r.generator == "deterministic" and "credencial" in r.fallback_reason
    files = {p.name for p in r.directory.iterdir()}
    assert files == {"job-description.md", "analysis.md", "resume.md", "resume.pdf", "application.json"}
    assert (r.directory / "resume.pdf").read_bytes()[:4] == b"%PDF"
    resume = (r.directory / "resume.md").read_text(encoding="utf-8")
    assert "ToggleMaster" in resume and "Christian Milfont" in resume
    analysis = (r.directory / "analysis.md").read_text(encoding="utf-8")
    assert "ATS Match" in analysis and "Por que este currículo foi adaptado" in analysis
    meta = json.loads((r.directory / "application.json").read_text(encoding="utf-8"))
    assert meta["resume"]["validation"]["ok"] is True
    assert job.status == JobStatus.RESUME_GENERATED.value
    assert session.query(ResumeRecord).count() == 1


class FakeProvider:
    name, model = "fake", "fake-model"

    def __init__(self, drafts):
        self.drafts = list(drafts)
        self.prompts = []

    def generate(self, system, user):
        self.prompts.append(user)
        d = self.drafts.pop(0)
        if isinstance(d, Exception):
            raise d
        return LLMResult(d, {"model_served": "fake-model"})


def test_llm_retry_with_validator_feedback(config, session, gen_settings, idx, parser):
    good = _clean(idx, parser)
    bad = good.model_copy(update={"summary": "Experiência profissional com Kafka."})
    fake = FakeProvider([bad, good])
    r = ResumeGenerator(config, gen_settings, provider=fake).generate(_job(session), session)
    assert r.generator == "llm" and r.attempts == 2
    assert r.rejected_violations
    assert "REJEITADA" in fake.prompts[1]           # 2ª chamada recebe as violações


def test_llm_failure_falls_back_to_deterministic(config, session, gen_settings, idx, parser):
    bad = _clean(idx, parser).model_copy(update={"summary": "Liderei time de SRE."})
    r = ResumeGenerator(config, gen_settings, provider=FakeProvider([bad, bad])).generate(_job(session), session)
    assert r.generator == "deterministic" and "reprovado" in r.fallback_reason
    r = ResumeGenerator(config, gen_settings, provider=FakeProvider([LLMError("x")])).generate(_job(session), session)
    assert r.generator == "deterministic" and "erro do LLM" in r.fallback_reason


# ------------------------------------------------------------------ provider Anthropic (cliente falso)
class _FakeMessages:
    def __init__(self, resp):
        self.resp, self.kwargs = resp, None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return self.resp


def _fake_client(resp):
    msgs = _FakeMessages(resp)
    return SimpleNamespace(beta=SimpleNamespace(messages=msgs)), msgs


def test_anthropic_provider_request_shape(idx, parser):
    draft = _clean(idx, parser)
    usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=8)
    resp = SimpleNamespace(stop_reason="end_turn", stop_details=None, parsed_output=draft, model="claude-sonnet-5-5",
                           usage=usage, _request_id="req_1")
    client, msgs = _fake_client(resp)
    s = Settings(_env_file=None, llm_model="claude-sonnet-5-5", llm_effort="high")
    res = AnthropicProvider(s, client=client).generate(["regras", "perfil"], "vaga")
    k = msgs.kwargs
    assert k["model"] == "claude-sonnet-5-5" and k["output_format"] is ResumeDraft
    assert k["output_config"] == {"effort": "high"}
    assert k["fallbacks"] == "default" and k["betas"] == ["server-side-fallback-2026-07-01"]
    assert k["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert res.meta["model_served"] == "claude-sonnet-5-5"


def test_anthropic_provider_refusal(idx, parser):
    resp = SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category="general_harms"),
                           parsed_output=None, model="m", usage=None)
    client, _ = _fake_client(resp)
    with pytest.raises(LLMError, match="recusou"):
        AnthropicProvider(Settings(_env_file=None), client=client).generate(["a"], "b")
