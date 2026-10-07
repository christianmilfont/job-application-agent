from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from jobagent.config import PROJECT_ROOT, Settings, load_config
from jobagent.database import init_db, make_engine, make_session_factory
from jobagent.job_collector import PoliteClient
from jobagent.job_parser import JobParser, Taxonomy
from jobagent.job_scorer import JobScorer

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def config():
    return load_config(PROJECT_ROOT / "config")


@pytest.fixture(scope="session")
def taxonomy(config):
    return Taxonomy(config.taxonomy)


@pytest.fixture(scope="session")
def parser(taxonomy):
    return JobParser(taxonomy)


@pytest.fixture(scope="session")
def scorer(config, taxonomy):
    return JobScorer(config.profile, config.scoring, taxonomy)


@pytest.fixture
def session():
    engine = make_engine("sqlite:///:memory:")
    init_db(engine)
    s = make_session_factory(engine)()
    yield s
    s.close()


@pytest.fixture
def test_settings():
    return Settings(_env_file=None, http_min_interval_s=0, http_jitter_s=0, http_max_requests_per_run=20,
                    respect_robots_txt=True)


def fixture_transport(calls: list[str] | None = None, status: int = 200) -> httpx.MockTransport:
    """Simula Greenhouse/Lever sem rede."""
    gh = json.loads((FIXTURES / "greenhouse_jobs.json").read_text(encoding="utf-8"))
    lv = json.loads((FIXTURES / "lever_jobs.json").read_text(encoding="utf-8"))

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /embed/\n")
        if status != 200:
            return httpx.Response(status)
        if "greenhouse" in request.url.host:
            return httpx.Response(200, json=gh)
        if "lever" in request.url.host:
            return httpx.Response(200, json=lv)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.fixture
def client(test_settings):
    c = PoliteClient(test_settings, transport=fixture_transport(), sleep=lambda _: None)
    yield c
    c.close()
