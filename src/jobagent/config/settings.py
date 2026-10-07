from __future__ import annotations

from enum import Enum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/jobagent/config/settings.py -> raiz do projeto
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Mode(str, Enum):
    DRY_RUN = "DRY_RUN"        # simula tudo; nunca envia nada
    DISCOVERY = "DISCOVERY"    # encontra e analisa vagas
    ASSISTED = "ASSISTED"      # prepara candidaturas; cada envio exige aprovação


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="JOBAGENT_",
        extra="ignore",
    )

    mode: Mode = Mode.DRY_RUN
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'jobagent.db').as_posix()}"
    config_dir: Path = PROJECT_ROOT / "config"
    log_dir: Path = PROJECT_ROOT / "logs"
    applications_dir: Path = PROJECT_ROOT / "applications"
    log_level: str = "INFO"

    http_min_interval_s: float = 3.0
    http_jitter_s: float = 1.5
    http_timeout_s: float = 20.0
    http_max_requests_per_run: int = 100
    http_max_retries: int = 3
    respect_robots_txt: bool = True
    user_agent: str = "jobagent-personal/0.1 (uso pessoal; baixo volume)"

    # --- LLM (etapa 7+). Provider/modelo trocáveis por variável de ambiente.
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-5-5"
    llm_effort: str = "high"            # low | medium | high | xhigh | max
    llm_max_tokens: int = 16000
    llm_fallbacks: bool = True          # fallback server-side em caso de recusa (Claude API)
    llm_max_attempts: int = 2           # 1 geração + 1 correção guiada pelo validador
    # Lida do .env sem prefixo (nome padrão do SDK). Nunca logada.
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias="ANTHROPIC_API_KEY")


@lru_cache
def get_settings() -> Settings:
    return Settings()
