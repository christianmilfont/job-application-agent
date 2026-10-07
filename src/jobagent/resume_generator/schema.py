"""Estrutura do currículo. O LLM preenche ResumeDraft (structured output); o gerador
determinístico também. Repare que competências, certificações e projetos são referenciados
por ID — o nome exibido vem sempre do profile.yaml, então o LLM não consegue inventá-los.
Texto livre só existe em headline, summary, bullets de projeto e rationale — e é validado."""
from __future__ import annotations

from pydantic import BaseModel, Field


class SkillGroup(BaseModel):
    name: str = Field(description="Nome do grupo de competências, ex.: 'Cloud', 'DevOps, CI/CD e IaC'")
    skill_ids: list[str] = Field(description="IDs de skills do perfil (somente IDs da lista fornecida)")


class ProjectEntry(BaseModel):
    project_id: str = Field(description="ID de um projeto do perfil")
    bullets: list[str] = Field(description="2 a 4 bullets reescritos SOMENTE a partir de summary/stack/highlights do projeto")


class ResumeDraft(BaseModel):
    headline: str = Field(description="Título curto alinhado à vaga, sem senioridade nem cargo que o perfil não tenha")
    summary: str = Field(description="Resumo profissional, 3 a 5 frases, só com fatos do perfil")
    skill_groups: list[SkillGroup]
    projects: list[ProjectEntry] = Field(description="Projetos na ordem de relevância para a vaga")
    certification_ids: list[str] = Field(description="IDs das certificações na ordem de relevância")
    keywords_used: list[str] = Field(description="Palavras-chave da vaga que foram usadas no currículo")
    rationale: list[str] = Field(description="3 a 6 frases curtas explicando por que o currículo foi adaptado desta forma")
