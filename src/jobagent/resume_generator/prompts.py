from __future__ import annotations

import json

from ..database import Job
from ..job_parser import ParsedJob
from ..profile import ProfileIndex
from .selector import ResumePlan

SYSTEM_RULES = """\
Você monta currículos para uma única pessoa, adaptados a cada vaga de tecnologia.

Seu trabalho é SELECIONAR, ORDENAR e REDIGIR informações que já existem no perfil fornecido —
nunca criar informação nova. O currículo será enviado a empresas reais em nome dessa pessoa;
qualquer fato inventado (tecnologia, número, cargo, responsabilidade, resultado) a prejudica
em entrevistas e é inaceitável. Um validador automático rejeita a resposta se encontrar algo
fora do perfil.

Regras:
- Use apenas fatos do perfil. Se a vaga pede algo que o perfil não tem, simplesmente não mencione
  no currículo (você pode citar a ausência na "rationale", que é só para a pessoa).
- Nos bullets de um projeto, cite apenas tecnologias do stack/highlights DAQUELE projeto
  (a lista allowed_skill_ids de cada projeto). Reescreva para destacar o que a vaga valoriza,
  sem acrescentar funcionalidades, escala, usuários, métricas ou resultados.
- Não use números que não estejam no perfil (nada de porcentagens, quantidades ou anos).
- O perfil não tem experiência profissional registrada. Não escreva "experiência profissional",
  "anos de experiência", "atuei como", "trabalhei", "liderei", "em produção", nem se descreva como
  sênior/especialista/expert. Prefira "projetos práticos", "conhecimento em", "laboratório", "estudos".
- Formação em andamento deve aparecer como em andamento (ex.: "cursando").
- Itens em "planned" de um projeto ainda não existem: só mencione como planejado/futuro, ou omita.
- skill_groups, certification_ids e projects referenciam IDs do perfil. Em skill_groups use somente
  IDs de allowed_skill_ids (gerais). Coloque primeiro os grupos e as skills que a vaga mais valoriza.
- Use as palavras-chave da vaga quando forem verdadeiras para o perfil (ajuda em ATS), com a grafia
  usada na vaga.
- Escreva no idioma indicado em "language" (pt = português do Brasil, en = inglês). Tom objetivo,
  sem adjetivos vazios ("apaixonado", "proativo").
- rationale: 3 a 6 frases curtas para a pessoa, explicando as escolhas (o que foi destacado e por quê,
  o que ficou de fora porque não consta no perfil).
"""


def profile_block(idx: ProfileIndex) -> str:
    """Perfil sem dados de contato. Determinístico (chaves ordenadas) para o cache funcionar."""
    p = idx.profile
    tax = idx.tax
    data = {
        "headline": p.personal.headline,
        "location": p.personal.location,
        "target_seniority": p.target_seniority,
        "professional_experience": [e.model_dump() for e in p.experience],
        "education": [e.model_dump() for e in p.education],
        "certifications": [c.model_dump() for c in p.certifications],
        "achievements": [a.model_dump() for a in p.achievements],
        "projects": [
            {**pr.model_dump(exclude={"link"}),
             "allowed_skill_ids": sorted(idx.allowed_for_project(pr.id))}
            for pr in p.projects
        ],
        "languages": [lang.model_dump() for lang in p.languages],
        "allowed_skill_ids": {sid: idx.display_name(sid) for sid in sorted(idx.allowed_skills)},
        "skill_evidence": {sid: idx.describe(sid) for sid in sorted(idx.owned_skills)},
    }
    return "PERFIL (fonte única da verdade):\n" + json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)


def job_block(job: Job, parsed: ParsedJob, plan: ResumePlan, idx: ProfileIndex,
              previous_violations: list[str] | None = None) -> str:
    tax = idx.tax
    sugestao = {
        "language": plan.language,
        "projetos_sugeridos_em_ordem": [
            {"project_id": pk.project_id, "cobre_da_vaga": [idx.display_name(s) for s in pk.matched]} for pk in plan.projects
        ],
        "skills_da_vaga_que_o_perfil_cobre": [idx.display_name(s) for s in plan.matched_skills],
        "skills_da_vaga_que_o_perfil_NAO_tem (não mencionar no currículo)": [tax.name(s) for s in plan.missing_skills],
        "certificacoes_sugeridas_em_ordem": plan.certification_ids,
    }
    parts = [
        f"VAGA: {job.title} — {job.company}",
        f"Local: {job.location or '-'} · Modalidade: {job.modality} · Senioridade: {parsed.seniority}",
        "",
        "DESCRIÇÃO COMPLETA DA VAGA:",
        job.description,
        "",
        "SUGESTÃO DO SELETOR DETERMINÍSTICO (pode ajustar a ordem se fizer sentido):",
        json.dumps(sugestao, ensure_ascii=False, indent=1),
    ]
    if previous_violations:
        parts += ["", "A versão anterior foi REJEITADA pelo validador pelos motivos abaixo. "
                      "Gere uma nova versão corrigindo todos eles:"]
        parts += [f"- {v}" for v in previous_violations]
    parts += ["", "Gere o currículo adaptado para esta vaga."]
    return "\n".join(parts)
