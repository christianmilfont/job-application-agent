from jobagent.job_collector import RawJob
from jobagent.job_collector.greenhouse import GreenhouseCollector
from jobagent.job_parser import classify_location, detect_modality, detect_seniority, html_to_text

import json
from conftest import FIXTURES


def _gh(i):
    data = json.loads((FIXTURES / "greenhouse_jobs.json").read_text(encoding="utf-8"))
    return GreenhouseCollector._to_raw(data["jobs"][i], {"token": "acme"})


def test_html_to_text_keeps_headings_and_bullets():
    text = html_to_text("<h3>Requisitos</h3><ul><li>Docker</li><li>K8s</li></ul><p><strong>Diferenciais</strong></p>")
    assert text.splitlines() == ["## Requisitos", "• Docker", "• K8s", "## Diferenciais"]


def test_sections_required_vs_desired(parser):
    p = parser.parse(_gh(0))
    assert {"docker", "kubernetes", "terraform", "linux", "python"} <= set(p.required_skills)
    assert ["aws", "azure"] in p.required_any_of               # "AWS ou Azure" = basta um
    assert {"prometheus", "grafana", "go"} <= set(p.desired_skills)
    assert "github_actions" in p.mentioned_skills          # citado em "o que você vai fazer"
    assert "azure_cert" in p.desired_certs
    assert p.education_required
    assert p.seniority == "junior"
    assert p.modality == "HYBRID"
    assert p.location_class == "sp"


def test_english_years_and_language(parser):
    p = parser.parse(_gh(1))
    assert p.description_language == "en"
    assert p.years_required == 5
    assert p.english["required"] is True
    assert p.seniority == "senior"
    assert "rust" in p.desired_skills


def test_inline_desired_inside_required_section(parser):
    raw = RawJob(source="manual", external_id=None, url="https://x/1", company="X", title="DevOps",
                 description_text="Requisitos:\n- Docker\n- Kubernetes será um diferencial\n- Linux")
    p = parser.parse(raw)
    assert "docker" in p.required_skills and "linux" in p.required_skills
    assert "kubernetes" in p.desired_skills


def test_alternatives_and_weekly_office_days(parser):
    raw = RawJob(source="manual", external_id=None, url="https://x/2", company="X", title="Engenheiro(a) de Software",
                 location="São Paulo, SP",
                 description_text="Requisitos:\n- Uma ou mais linguagens (C#, Java, Go ou Rust)\n- Docker\n"
                                  "- Modelo presencial com flexibilidade (2x por semana no escritório)")
    p = parser.parse(raw)
    assert p.required_skills == ["docker"]
    assert p.required_any_of == [["csharp", "go", "java", "rust"]]
    assert p.modality == "HYBRID"


def test_seniority_detection():
    assert detect_seniority("Desenvolvedor Backend Jr") == "junior"
    assert detect_seniority("Engenheiro de Software II") == "pleno"
    assert detect_seniority("Software Engineer III") == "senior"
    assert detect_seniority("Desenvolvedor Pleno/Sênior") == "pleno"
    assert detect_seniority("Estágio em Cloud") == "intern"
    assert detect_seniority("Staff Platform Engineer") == "lead"
    assert detect_seniority("Cloud Engineer", "Nível: Júnior") == "junior"
    assert detect_seniority("AI Engineer") == "unspecified"   # "AI" não é o numeral I
    assert detect_seniority("SOFTWARE DEVELOPER SPECIALIST I") == "lead"   # palavra vence numeral
    assert detect_seniority("SOFTWARE ENGINEER PL .NET") == "pleno"
    assert detect_seniority("Software Engineering Team Leader") == "lead"
    assert detect_seniority("SOFTWARE DEVELOPER MASTER") == "senior"


def test_modality_and_location():
    assert detect_modality("REMOTE", "São Paulo", "", "presencial") == "REMOTE"
    assert detect_modality(None, "São Paulo - Híbrido", "", "") == "HYBRID"
    assert detect_modality(None, "São Paulo", "", "Trabalho 100% remoto") == "REMOTE"
    assert classify_location("São Paulo, Brazil") == "sp"
    assert classify_location("Curitiba, PR") == "brazil"
    assert classify_location("Brazil") == "brazil_any"
    assert classify_location("Remoto", "pt") == "brazil"
    assert classify_location("Mexico City") == "abroad"
    assert classify_location("LATAM") == "latam"
