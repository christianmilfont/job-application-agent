"""Etapa 7: currículo adaptado por vaga + análise ATS, limitado ao profile.yaml."""
from .generator import PackageResult, ResumeGenerator
from .llm import LLMError

__all__ = ["LLMError", "PackageResult", "ResumeGenerator"]
