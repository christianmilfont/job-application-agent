"""Seleção determinística do que destacar para cada vaga.

Peso de cada tecnologia da vaga: obrigatória/alternativa 3, desejável 2, citada 1.
Um projeto pontua pelas tecnologias da vaga que ele cobre (direta ou via implied_by).
Ex.: vaga AWS + Kubernetes + Terraform + CI/CD -> ToggleMaster no topo.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..job_parser import ParsedJob
from ..profile import ProfileIndex

W_REQUIRED, W_DESIRED, W_MENTIONED = 3, 2, 1

# Agrupamento das competências no currículo (ordem dos grupos é reordenada por relevância)
CATEGORIES: dict[str, list[str]] = {
    "Cloud": ["aws", "azure", "gcp", "oci", "cloud", "cloud_architecture", "serverless", "networking", "finops"],
    "DevOps, CI/CD e IaC": ["devops", "ci_cd", "github_actions", "gitlab_ci", "jenkins", "azure_devops", "argocd",
                            "terraform", "iac", "pulumi", "cloudformation", "ansible", "git", "github",
                            "progressive_delivery", "feature_flags"],
    "Containers e Kubernetes": ["docker", "containers", "kubernetes", "helm", "istio"],
    "Observabilidade": ["observability", "opentelemetry", "jaeger", "prometheus", "grafana", "datadog",
                              "dynatrace", "new_relic", "azure_monitor", "sre"],
    "Segurança": ["devsecops", "checkov", "kyverno", "jwt"],
    "IA / GenAI": ["genai", "llm_platforms", "rag", "mcp", "fine_tuning", "ai_agents", "ai_assistants", "langchain",
                   "machine_learning", "mlops", "spring_ai", "voice_ai"],
    "Linguagens e Frameworks": ["python", "java", "csharp", "dotnet", "go", "javascript", "typescript", "nodejs",
                                "react", "expo", "kotlin", "spring", "flask", "fastapi", "django", "leaflet"],
    "Arquitetura e Integração": ["rest_api", "graphql", "grpc", "microservices", "messaging", "automation", "geolocation"],
    "Dados": ["sql", "postgresql", "mysql", "h2", "sql_server", "mongodb", "redis", "nosql", "elasticsearch"],
    "Sistemas": ["linux", "bash"],
}


@dataclass
class ProjectPick:
    project_id: str
    score: int
    matched: list[str]           # IDs de skills da vaga cobertas pelo projeto


@dataclass
class SkillGroupPlan:
    name: str
    skill_ids: list[str]


@dataclass
class ResumePlan:
    job_weights: dict[str, int]
    projects: list[ProjectPick]
    skill_groups: list[SkillGroupPlan]
    certification_ids: list[str]
    matched_skills: list[str]            # skills da vaga que o perfil cobre, por peso
    missing_skills: list[str]            # skills da vaga que o perfil NÃO cobre
    language: str = "pt"
    notes: list[str] = field(default_factory=list)


def job_skill_weights(p: ParsedJob) -> dict[str, int]:
    w: dict[str, int] = {}
    for s in p.mentioned_skills:
        w[s] = max(w.get(s, 0), W_MENTIONED)
    for s in p.desired_skills:
        w[s] = max(w.get(s, 0), W_DESIRED)
    for s in p.required_skills + [s for g in p.required_any_of for s in g]:
        w[s] = max(w.get(s, 0), W_REQUIRED)
    return w


def build_plan(idx: ProfileIndex, parsed: ParsedJob, max_projects: int = 3, min_projects: int = 2,
               max_groups: int = 8) -> ResumePlan:
    weights = job_skill_weights(parsed)

    picks: list[ProjectPick] = []
    for proj in idx.profile.projects:
        own = idx.project_skills.get(proj.id, set())
        matched = [s for s in weights if idx.covers(s, within=own)]
        picks.append(ProjectPick(proj.id, sum(weights[s] for s in matched), sorted(matched, key=lambda s: -weights[s])))
    picks.sort(key=lambda p: (-p.score, -len(idx.project_skills.get(p.project_id, ()))))
    chosen = [p for p in picks if p.score > 0][:max_projects]
    for p in picks:
        if len(chosen) >= min_projects:
            break
        if p not in chosen:
            chosen.append(p)

    matched_skills = sorted((s for s in weights if idx.covers(s)), key=lambda s: -weights[s])
    missing = sorted((s for s in weights if not idx.covers(s)), key=lambda s: -weights[s])

    # Competências: só as do perfil; as que a vaga pede primeiro. Grupos mais relevantes primeiro.
    # Inclui termos que a vaga usa e você cobre por implicação (ex.: "SQL" via PostgreSQL),
    # para o ATS encontrar a palavra literal sem afirmar nada novo.
    owned = idx.owned_skills | set(matched_skills)
    relevance = {s: weights.get(s, 0) for s in owned}
    for s in matched_skills:
        for c in idx.covers(s):
            relevance[c] = max(relevance.get(c, 0), weights[s])
    groups: list[SkillGroupPlan] = []
    placed: set[str] = set()
    for name, ids in CATEGORIES.items():
        members = [s for s in ids if s in owned]
        if members:
            members.sort(key=lambda s: -relevance.get(s, 0))
            groups.append(SkillGroupPlan(name, members))
            placed.update(members)
    rest = sorted(owned - placed)
    if rest:
        groups.append(SkillGroupPlan("Outros", rest))
    groups.sort(key=lambda g: -sum(relevance.get(s, 0) for s in g.skill_ids))
    # Uma página: grupos sem relação com a vaga ficam com as 4 primeiras skills; no máximo 8 grupos
    for g in groups:
        if not any(relevance.get(s, 0) for s in g.skill_ids):
            g.skill_ids = g.skill_ids[:4]
    groups = groups[:max_groups]

    techs = set(weights)
    certs = sorted(idx.profile.certifications,
                   key=lambda c: (-len(techs & set(c.skills)), c.kind != "certification"))

    return ResumePlan(
        job_weights=weights,
        projects=chosen,
        skill_groups=groups,
        certification_ids=[c.id for c in certs],
        matched_skills=matched_skills,
        missing_skills=missing,
        language=parsed.description_language,
    )
