import httpx
import pytest

from conftest import FIXTURES, fixture_transport
from jobagent.application_assistant import SubmissionBlocked, assert_submission_allowed
from jobagent.config import Mode
from jobagent.database import Approval, Job, JobRepository, JobStatus
from jobagent.database.dedup import content_key, url_key
from jobagent.job_collector import (AccessDenied, GreenhouseCollector, LeverCollector, ManualImportCollector,
                                    PoliteClient)
from jobagent.logging_setup import RedactingFilter
from jobagent.pipeline import DiscoveryPipeline

GH = [{"token": "acme", "company": "Acme"}]
LV = [{"token": "beta", "company": "Beta"}]


def test_url_key_strips_tracking():
    a = url_key("https://www.linkedin.com/jobs/view/123/?trk=public_jobs&utm_source=x")
    b = url_key("https://linkedin.com/jobs/view/123")
    assert a == b


def test_content_key_ignores_case_and_accents():
    assert content_key("Acme", "Engenheiro DevOps Júnior", "São Paulo") == \
        content_key("ACME", "engenheiro devops junior", "Sao Paulo")


def test_pipeline_end_to_end_and_no_reprocessing(config, session, client):
    pipe = DiscoveryPipeline(config, session, Mode.DRY_RUN)
    r1 = pipe.run([GreenhouseCollector(client, GH), LeverCollector(client, LV)])
    assert r1.stats["collected"] == 5
    assert r1.stats["new"] == 3                       # vendas e growth filtradas
    assert r1.stats["filtered: cargo fora do alvo"] == 2
    jobs = JobRepository(session).search()
    assert all(j.status == JobStatus.ANALYZED.value for j in jobs)
    assert all(len(j.history) == 2 for j in jobs)     # FOUND -> ANALYZED

    r2 = pipe.run([GreenhouseCollector(client, GH), LeverCollector(client, LV)])
    assert r2.stats["new"] == 0
    assert r2.stats["duplicate"] == 3


def test_manual_import_and_cross_source_duplicate(config, session):
    pipe = DiscoveryPipeline(config, session, Mode.DRY_RUN)
    r = pipe.run([ManualImportCollector(FIXTURES / "manual_jobs.yaml")])
    assert r.stats["new"] == 1 and r.new_jobs[0].score >= 75
    r = pipe.run([ManualImportCollector(FIXTURES / "manual_jobs.yaml")])
    assert r.stats["duplicate"] == 1


def test_preview_persists_nothing(config, session, client):
    DiscoveryPipeline(config, session, Mode.DRY_RUN).run([GreenhouseCollector(client, GH)], persist=False)
    assert session.query(Job).count() == 0


def test_403_stops_collector_without_retry(config, session, test_settings):
    calls: list[str] = []
    c = PoliteClient(test_settings, transport=fixture_transport(calls, status=403), sleep=lambda _: None)
    with pytest.raises(AccessDenied):
        list(GreenhouseCollector(c, GH).collect())
    api_calls = [u for u in calls if "robots" not in u]
    assert len(api_calls) == 1


def test_429_respects_retry_after(test_settings):
    waits: list[float] = []
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={"ok": 1})])

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return next(responses)

    c = PoliteClient(test_settings, transport=httpx.MockTransport(handler), sleep=waits.append)
    assert c.get_json("https://example.com/api") == {"ok": 1}
    assert 7.0 in waits


def test_robots_disallow_blocks(test_settings):
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        raise AssertionError("não deveria acessar a API")

    c = PoliteClient(test_settings, transport=httpx.MockTransport(handler), sleep=lambda _: None)
    with pytest.raises(AccessDenied):
        c.get_json("https://example.com/jobs")


def test_submission_guard_blocks_outside_assisted():
    job = Job(status=JobStatus.READY_TO_APPLY.value)
    appr = Approval(job_id=1, resume_id=1, snapshot_hash="x")
    with pytest.raises(SubmissionBlocked):
        assert_submission_allowed(Mode.DRY_RUN, job, user_confirmed=True, approval=appr)
    with pytest.raises(SubmissionBlocked):
        assert_submission_allowed(Mode.ASSISTED, job, user_confirmed=False, approval=appr)
    with pytest.raises(SubmissionBlocked):
        assert_submission_allowed(Mode.ASSISTED, Job(status="ANALYZED"), user_confirmed=True, approval=appr)
    with pytest.raises(SubmissionBlocked, match="aprovação"):
        assert_submission_allowed(Mode.ASSISTED, job, user_confirmed=True, approval=None)
    assert_submission_allowed(Mode.ASSISTED, job, user_confirmed=True, approval=appr)


def test_log_redaction():
    f = RedactingFilter(["sk-supersecret123"])
    assert "abc123" not in f.redact("Authorization: Bearer abc123")
    assert "sk-supersecret123" not in f.redact("chave carregada: sk-supersecret123")
    assert f.redact("login password=hunter2 ok") == "login password=*** ok"
    assert f.redact("GET /x?token=xyz&a=1") == "GET /x?token=***&a=1"
