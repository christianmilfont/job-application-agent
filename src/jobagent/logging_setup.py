"""Logging com redação de segredos: nada de senha, token, cookie ou header de auth nos logs."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler

from .config import get_settings

# (padrão, substituição) — o grupo 1 (nome do campo) é preservado, o valor vira ***
_PATTERNS = [
    (re.compile(r"(?i)\b(authorization|cookie|set-cookie)(\s*[:=]\s*).+"), r"\1\2***"),
    (re.compile(r"(?i)\b(password|passwd|senha|token|api[_-]?key|secret)(\s*[:=]\s*)[^\s,;&]+"), r"\1\2***"),
    (re.compile(r"(?i)([?&](?:token|key|api_key|access_token|sig)=)[^&\s]+"), r"\1***"),
]


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 4]

    def redact(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        for pattern, repl in _PATTERNS:
            text = pattern.sub(repl, text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        return True


def setup_logging() -> None:
    s = get_settings()
    s.log_dir.mkdir(parents=True, exist_ok=True)
    secrets = [s.anthropic_api_key.get_secret_value()] if s.anthropic_api_key else []
    redactor = RedactingFilter(secrets)

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(s.log_dir / "jobagent.log", maxBytes=2_000_000,
                                       backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redactor)

    root = logging.getLogger()
    root.setLevel(s.log_level.upper())
    root.handlers = [file_handler]
    # httpx loga URLs em INFO; mantemos em WARNING para não poluir.
    logging.getLogger("httpx").setLevel(logging.WARNING)
