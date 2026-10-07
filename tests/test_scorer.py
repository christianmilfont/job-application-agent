import json

from conftest import FIXTURES
from jobagent.job_collector import RawJob
from jobagent.job_collector.greenhouse import GreenhouseCollector
from jobagent.job_collector.lever import LeverCollector


def _score(parser, scorer, raw):
    p = parser.parse(raw)
    return p, scorer.score(raw.title, raw.location, p)


def test_good_fit_junior_devops_sp(parser, scorer):
    data = json.loads((FIXTURES / "greenhouse_jobs.json").read_text(encoding="utf-8"))
    raw = GreenhouseCollector._to_raw(data["jobs"][0], {"token": "acme"})
    _, a = _score(parser, scorer, raw)
    assert a.score >= 75, a.breakdown
    assert a.band in ("ALTA", "EXCELENTE")
    assert any("Kubernetes (obrigatório)" in m for m in a.match)
    assert any("AZ-104" in m for m in a.match)
    # desejáveis ausentes viram GAP, não RISK
    assert any("Prometheus" in g for g in a.gaps)
    assert not any("Prometheus" in r for r in a.risks)


def test_senior_role_is_low_with_risks(parser, scorer):
    data = json.loads((FIXTURES / "greenhouse_jobs.json").read_text(encoding="utf-8"))
    raw = GreenhouseCollector._to_raw(data["jobs"][1], {"token": "acme"})
    _, a = _score(parser, scorer, raw)
    assert a.score < 60
    assert any("Sênior" in r for r in a.risks)
    assert any("5+ anos" in r for r in a.risks)
    assert any("Datadog" in r for r in a.risks)        # obrigatório ausente
    assert any("Inglês" in r for r in a.risks)         # idioma não informado no perfil


def test_missing_desired_never_eliminates(parser, scorer):
    base = "Requisitos:\n- Python\n- Docker\n"
    raw_a = RawJob(source="manual", external_id=None, url="https://x/a", company="X",
                   title="Desenvolvedor Backend Júnior", location="Remoto", modality_hint="REMOTE",
                   description_text=base)
    raw_b = RawJob(source="manual", external_id=None, url="https://x/b", company="X",
                   title="Desenvolvedor Backend Júnior", location="Remoto", modality_hint="REMOTE",
                   description_text=base + "Diferenciais:\n- Rust\n- Scala\n")
    _, a = _score(parser, scorer, raw_a)
    _, b = _score(parser, scorer, raw_b)
    assert b.score >= 60
    assert a.score - b.score <= 10
    assert not b.risks or not any("Rust" in r for r in b.risks)


def test_implied_skill_via(parser, scorer):
    raw = RawJob(source="manual", external_id=None, url="https://x/c", company="X", title="Backend Developer",
                 location="São Paulo", description_text="Requisitos:\n- SQL\n- Infraestrutura como código")
    _, a = _score(parser, scorer, raw)
    assert any(m.startswith("SQL") and "via" in m for m in a.match)
    # IaC agora tem evidência direta: stack do ToggleMaster cita "Infrastructure as Code"
    assert any(m.startswith("Infraestrutura como Código") and "projeto ToggleMaster" in m for m in a.match)


def test_alternative_group_satisfied_by_one(parser, scorer):
    raw = RawJob(source="manual", external_id=None, url="https://x/g", company="X", title="Engenheiro(a) de Software Júnior",
                 location="São Paulo, SP", description_text="Requisitos:\n- C#, Java, Go ou Rust")
    _, a = _score(parser, scorer, raw)
    assert a.breakdown["required_skills"].detail.startswith("1/1")
    assert not any("Rust" in r or "Go" in r for r in a.risks)


def test_affirmative_job_flagged_as_risk(parser, scorer):
    raw = RawJob(source="manual", external_id=None, url="https://x/p", company="X",
                 title="Engenharia de Software Back-end Júnior | Vaga exclusiva para PCD",
                 location="São Paulo, SP", description_text="Requisitos:\n- Java")
    _, a = _score(parser, scorer, raw)
    assert any("afirmativa" in r for r in a.risks)


def test_role_fit_uses_title_segments(scorer):
    assert scorer.role_fit("Engenheiro(a) de Software | Assistente e Júnior", "XP Inc.") == 1.0
    assert scorer.role_fit("Grupo QuintoAndar | Senior Site Reliability Engineer", "QuintoAndar") == 1.0
    assert scorer.role_fit("Analista Júnior em Infraestrutura | SRE", "XP") == 1.0
    assert scorer.role_fit("Analista de Qualidade Pleno | AI Engineering", "C6") == 0.5   # alvo só no time
    assert scorer.role_fit("Product Design Junior | Plataforma Cadastro", "XP") == 0.0    # negativo vence
    assert scorer.role_fit("TECH OPS SPECIALIST I (Salesforce Marketing Cloud)", "Inter") == 0.0
    assert scorer.role_fit("[Crédito] Software Engineer II - C#", "Stone") == 1.0


def test_senior_and_staff_fall_below_possible(parser, scorer):
    for title in ("Senior Platform Engineer", "Staff Platform Engineer"):
        raw = RawJob(source="manual", external_id=None, url="https://x/s", company="X", title=title,
                     location="Remoto", modality_hint="REMOTE",
                     description_text="Requisitos:\n- Kubernetes\n- Terraform\n- AWS\n- Python")
        _, a = _score(parser, scorer, raw)
        assert a.score < 60, (title, a.score)


def test_relevance_filter(parser, scorer):
    lv = json.loads((FIXTURES / "lever_jobs.json").read_text(encoding="utf-8"))
    growth = LeverCollector._to_raw(lv[1], {"token": "beta"})
    ok, reason = scorer.is_relevant(growth.title, parser.parse(growth))
    assert not ok and reason == "cargo fora do alvo"
    cloud = LeverCollector._to_raw(lv[0], {"token": "beta"})
    assert scorer.is_relevant(cloud.title, parser.parse(cloud))[0]
