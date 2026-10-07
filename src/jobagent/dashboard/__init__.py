"""Etapa 8: dashboard local (FastAPI + Jinja2 + HTMX), só em 127.0.0.1."""
from .app import create_app, run

__all__ = ["create_app", "run"]
