"""Cliente HTTP conservador: rate limit por host, robots.txt, orçamento de requisições,
backoff com Retry-After. Em 401/403 o coletor PARA — nunca tenta contornar bloqueios."""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from ..config import Settings, get_settings

log = logging.getLogger(__name__)


class AccessDenied(Exception):
    """A plataforma recusou o acesso (401/403/robots). Não insistir."""


class BudgetExceeded(Exception):
    """Limite de requisições por execução atingido."""


class PoliteClient:
    def __init__(self, settings: Settings | None = None, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = settings or get_settings()
        self._client = httpx.Client(
            timeout=self.cfg.http_timeout_s,
            headers={"User-Agent": self.cfg.user_agent, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )
        self._sleep = sleep
        self._last_call: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._crawl_delay: dict[str, float] = {}
        self.requests_made = 0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # --- robots.txt
    def _allowed(self, url: str) -> bool:
        if not self.cfg.respect_robots_txt:
            return True
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                resp = self._client.get(f"{host}/robots.txt")
                self.requests_made += 1
                if resp.status_code >= 400:
                    rp = None  # sem robots.txt = sem restrições declaradas
                else:
                    rp.parse(resp.text.splitlines())
                    delay = rp.crawl_delay(self.cfg.user_agent)
                    if delay:
                        self._crawl_delay[parts.netloc] = float(delay)
            except httpx.HTTPError:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return rp is None or rp.can_fetch(self.cfg.user_agent, url)

    # --- rate limit
    def _wait_turn(self, host: str) -> None:
        interval = max(self.cfg.http_min_interval_s, self._crawl_delay.get(host, 0.0))
        interval += random.uniform(0, self.cfg.http_jitter_s)
        last = self._last_call.get(host)
        if last is not None:
            remaining = interval - (time.monotonic() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_call[host] = time.monotonic()

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        if not self._allowed(url):
            raise AccessDenied(f"robots.txt não permite {url}")
        host = urlsplit(url).netloc
        for attempt in range(self.cfg.http_max_retries + 1):
            if self.requests_made >= self.cfg.http_max_requests_per_run:
                raise BudgetExceeded(f"limite de {self.cfg.http_max_requests_per_run} requisições atingido")
            self._wait_turn(host)
            self.requests_made += 1
            try:
                resp = self._client.get(url, params=params)
            except httpx.TransportError as e:
                log.warning("erro de rede em %s (tentativa %d): %s", url, attempt + 1, e)
                self._sleep(2 ** attempt * 5)
                continue
            if resp.status_code in (401, 403):
                raise AccessDenied(f"{resp.status_code} em {url} — coleta interrompida para este host")
            if resp.status_code == 404:
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = _retry_after(resp) or 2 ** attempt * 10
                log.warning("%s em %s; aguardando %.0fs", resp.status_code, url, wait)
                self._sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        log.error("desistindo de %s após %d tentativas", url, self.cfg.http_max_retries + 1)
        return None


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    try:
        return min(float(value), 300.0) if value else None
    except ValueError:
        return None
