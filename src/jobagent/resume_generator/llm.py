"""Integração com LLM. Provider e modelo vêm do .env (JOBAGENT_LLM_PROVIDER / JOBAGENT_LLM_MODEL).

Por que structured output: o modelo devolve um ResumeDraft validado pelo SDK (Pydantic),
com skills/projetos/certificações por ID. O texto livre ainda passa pelo validador.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from ..config import Settings, get_settings
from .schema import ResumeDraft

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    draft: Any            # instância do output_format pedido (ResumeDraft, AnswerDraft...)
    meta: dict[str, Any] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str
    model: str

    def generate(self, system: list[str], user: str, output_format: type[BaseModel] = ResumeDraft) -> LLMResult: ...


class AnthropicProvider:
    """Claude via Messages API. Blocos de system (regras + perfil) são cacheados: são iguais
    para todas as vagas, então a partir da 2ª geração saem ~90% mais baratos."""
    name = "anthropic"

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        import anthropic

        self.cfg = settings
        self.model = settings.llm_model
        self._anthropic = anthropic
        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else ""
        self.client = client or (anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic())

    def generate(self, system: list[str], user: str, output_format: type[BaseModel] = ResumeDraft) -> LLMResult:
        blocks = [{"type": "text", "text": t} for t in system]
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
        kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.cfg.llm_max_tokens,
            system=blocks,
            messages=[{"role": "user", "content": user}],
            output_format=output_format,
            output_config={"effort": self.cfg.llm_effort},
        )
        if self.cfg.llm_fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        a = self._anthropic
        try:
            resp = self.client.beta.messages.parse(**kwargs)
        except a.AuthenticationError as e:
            raise LLMError("credencial Anthropic inválida (ANTHROPIC_API_KEY)") from e
        except a.NotFoundError as e:
            raise LLMError(f"modelo '{self.model}' não encontrado (JOBAGENT_LLM_MODEL)") from e
        except a.BadRequestError as e:
            raise LLMError(f"requisição rejeitada: {e.message}") from e
        except a.RateLimitError as e:
            raise LLMError("limite de requisições da API atingido; tente mais tarde") from e
        except a.APIStatusError as e:
            raise LLMError(f"erro da API ({e.status_code})") from e
        except a.APIConnectionError as e:
            raise LLMError("sem conexão com a API Anthropic") from e

        if resp.stop_reason == "refusal":
            cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
            raise LLMError(f"o modelo recusou a requisição (categoria: {cat})")
        if resp.stop_reason == "max_tokens":
            raise LLMError("resposta cortada por max_tokens; aumente JOBAGENT_LLM_MAX_TOKENS")
        draft = resp.parsed_output
        if draft is None:
            raise LLMError("resposta sem JSON estruturado válido")
        usage = resp.usage
        meta = {
            "provider": self.name,
            "model_requested": self.model,
            "model_served": resp.model,
            "request_id": getattr(resp, "_request_id", None),
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
        }
        log.info("LLM ok: modelo=%s req=%s in=%s out=%s cache_read=%s", resp.model, meta["request_id"],
                 meta["input_tokens"], meta["output_tokens"], meta["cache_read_input_tokens"])
        return LLMResult(draft, meta)


_PROVIDERS = {"anthropic": AnthropicProvider}


def credentials_available(settings: Settings) -> bool:
    if settings.llm_provider != "anthropic":
        return True
    has_key = bool(settings.anthropic_api_key and settings.anthropic_api_key.get_secret_value())
    return has_key or any(os.environ.get(v) for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"))


def get_provider(settings: Settings | None = None) -> LLMProvider:
    s = settings or get_settings()
    factory = _PROVIDERS.get(s.llm_provider.lower())
    if factory is None:
        raise LLMError(f"provider '{s.llm_provider}' não suportado. Disponíveis: {', '.join(_PROVIDERS)}")
    return factory(s)
