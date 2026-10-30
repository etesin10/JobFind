"""Local REST API (/api/v1) and MCP server: auth, scopes, host check, fill-only writes, review/tailoring flow."""
import json
import sys

import pytest
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.stdio import StdioServerParameters

from jsh.api import create_api, ensure_tokens, regenerate_token
from jsh.mcp_server import build_server, config_snippets
from jsh.modes import CANADA, MEXICO
from jsh.services import JobInput

from .test_handoff import CV, JD

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def svc(app):
    c = app.candidate.create_candidate("Data")
    app.candidate.import_text(c.id, CV)
    app.candidate.set_skill_status([s.id for s in app.candidate.skills(c.id)], "approved")
    app.candidate.set_item_status([i.id for i in app.candidate.items(c.id)], "approved")
    for m in (CANADA, MEXICO):
        app.candidate.set_active_candidate(m, c.id)
    app.candidate.update_contact(c.id, {"email": "jane@example.com"})
    job, _ = app.jobs.upsert_job(JobInput(title="Data Engineer", company_name="Maple Data", job_country="CA",
                                          canonical_url="https://maple.example/jobs/1", description=JD))
    app.job_id = job.id
    return app


@pytest.fixture
def client(svc):
    tokens = ensure_tokens(svc)
    c = TestClient(create_api(svc), base_url="http://127.0.0.1:8765")
    c.write = {"Authorization": f"Bearer {tokens['write']}", "X-Agent-Name": "tester"}
    c.read = {"Authorization": f"Bearer {tokens['read']}"}
    return c


# --------------------------------------------------------------------------- REST API
def test_api_auth_scopes_and_host_check(svc, client):
    assert client.get("/status").status_code == 401
    assert client.get("/status", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/status", headers=client.read).json()["scope"] == "read"
    assert client.post("/jobs", json={"title": "X"}, headers=client.read).status_code == 403
    evil = TestClient(create_api(svc), base_url="http://evil.example")
    assert evil.get("/status", headers=client.write).status_code == 403  # DNS-rebinding guard
    svc.db.set_setting("api_enabled", False)
    assert client.get("/status", headers=client.write).status_code == 503
    svc.db.set_setting("api_enabled", True)
    old = client.read["Authorization"]
    regenerate_token(svc, "read")
    assert client.get("/status", headers={"Authorization": old}).status_code == 401
    assert client.get("/openapi.json").json()["info"]["title"].startswith("Job Search Harness")


def test_api_jobs_read_write_and_errors(svc, client):
    r = client.get("/jobs", params={"mode": CANADA}, headers=client.read).json()
    assert r["total"] == 1 and r["items"][0]["job_id"] == svc.job_id
    assert client.get("/jobs", params={"mode": "XX"}, headers=client.read).status_code == 422
    doc = client.get(f"/jobs/{svc.job_id}", params={"mode": CANADA}, headers=client.read).json()
    assert doc["jd"]["required_keywords"] and doc["match"]["matched"]
    md = client.get(f"/jobs/{svc.job_id}/jd", params={"mode": CANADA}, headers=client.read)
    assert md.headers["content-type"].startswith("text/markdown") and "Palabras clave" in md.text
    assert "BASIC QUALIFICATIONS" in client.get(f"/jobs/{svc.job_id}/jd", params={"format": "text"},
                                                headers=client.read).text
    assert client.get("/jobs/nope", headers=client.read).status_code == 404
    new = client.post("/jobs", json={"title": "BI Dev", "company_name": "Globant", "work_mode": "remote",
                                     "remote_eligible_countries": ["MX"], "canonical_url": "https://g.example/1",
                                     "posted_date": "October 29, 2026", "modes": [MEXICO]}, headers=client.write)
    assert new.status_code == 200 and new.json()["result"] == "created"
    created = [h for h in svc.jobs.history() if h["action"] == "job_created" and h["actor"] == "api:tester"]
    assert created
    assert client.post("/jobs", json={"company_name": "no title"}, headers=client.write).status_code == 422
    r = client.post(f"/jobs/{new.json()['job_id']}/ad-status", json={"status": "expired"}, headers=client.write)
    assert r.json()["changed"] is True
    svc.jobs.set_decision(svc.job_id, CANADA, "discarded", "salary")
    dec = client.get("/decisions", params={"mode": CANADA}, headers=client.read).json()
    assert dec[0]["discard_reason"] == "salary"
    cand = client.get("/candidate", params={"mode": CANADA}, headers=client.read).json()
    assert cand["approved_skills"] and "jane@example.com" not in json.dumps(cand)
    assert client.get("/rules", params={"mode": MEXICO}, headers=client.read).json()["rules"]


def test_api_companies_contacts_fill_only(svc, client):
    comp = next(c for c in svc.crm.list_companies() if c["name"] == "Maple Data")
    svc.crm.upsert_company({"company_id": comp["company_id"], "notes": "mía"})
    r = client.post("/companies", json={"name": "Maple Data", "notes": "del agente", "website": "https://m.example"},
                    headers=client.write).json()
    assert r["result"] == "updated"
    got = client.get(f"/companies/{comp['company_id']}", headers=client.read).json()
    assert got["notes"] == "mía" and got["website"] == "https://m.example"
    c = client.post("/contacts", json={"name": "Ana", "company_name": "Maple Data", "email": "ana@m.example"},
                    headers=client.write).json()
    ev = client.post(f"/contacts/{c['contact_id']}/events", json={"kind": "message", "note": "Hola"},
                     headers=client.write)
    assert ev.status_code == 200
    assert client.get("/contacts", params={"q": "ana"}, headers=client.read).json()[0]["last_interaction"]
    assert client.post(f"/contacts/{c['contact_id']}/events", json={"kind": "fax"},
                       headers=client.write).status_code == 422


def test_api_review_and_tailoring_roundtrip(svc, client):
    pkg = client.post("/packages/review", json={"mode": CANADA, "job_ids": [svc.job_id]}, headers=client.write).json()
    assert "contact" not in pkg["candidate"]
    result = {"protocol_version": "jsh-review/1", "package_id": pkg["package_id"], "mode": CANADA,
              "provider": "claude", "model": "m", "reviews": [{
                  "job_id": svc.job_id, "jd_hash": pkg["jobs"][0]["jd_hash"], "technical_fit_score": 77,
                  "location_fit": "yes", "work_authorization_fit": "unknown", "recommendation": "review"}]}
    rep = client.post("/reviews", json=result, headers=client.write).json()
    assert len(rep["received"]) == 1
    assert client.get("/reviews", params={"status": "received"}, headers=client.read).json()[0]["source"] == "api"
    assert client.post("/reviews", json={**result, "protocol_version": "x"}, headers=client.write).status_code == 422
    tp = client.post("/packages/tailoring", json={"job_id": svc.job_id, "mode": CANADA}, headers=client.write).json()
    prop = {"protocol_version": "jsh-tailoring/1", "package_id": tp["package_id"], "job_id": svc.job_id,
            "jd_hash": tp["jd_hash"], "master_version_id": tp["master_version_id"],
            "changes": [{"change_id": "c1", "action": "rewrite", "target_id": "P1.H1",
                         "proposed": "Built ETL pipelines in Python and SQL processing 2M rows daily for finance",
                         "evidence_ids": ["P1.H1"]}]}
    rep = client.post("/tailoring", json=prop, headers=client.write).json()
    assert rep["changes"] == 1 and rep["flagged"] == 0
    assert client.get("/tailoring", params={"job_id": svc.job_id}, headers=client.read).json()[0]["source"] == "api"


# --------------------------------------------------------------------------- MCP
async def test_mcp_tools_in_process(svc):
    async with Client(build_server(svc, "claude-test")) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert {"search_jobs", "get_job", "submit_job_review", "submit_resume_tailoring"} <= names
        r = await c.call_tool("search_jobs", {"mode": CANADA, "view": "all", "query": "snowflake"})
        assert r.structured_content["total"] == 1
        md = await c.call_tool("get_job_markdown", {"job_id": svc.job_id, "mode": CANADA})
        assert "Palabras clave" in md.content[0].text
        bad = await c.call_tool("search_jobs", {"mode": "XX"})
        assert bad.is_error and "mode must be one of" in bad.content[0].text
        missing = await c.call_tool("get_job", {"job_id": "nope"})
        assert missing.is_error and "Not found" in missing.content[0].text
        added = await c.call_tool("add_or_update_job", {"job": {"title": "Remote BI", "canonical_url":
                                                                 "https://r.example/1", "modes": [MEXICO]}})
        assert added.structured_content["result"] == "created"
        pkg = (await c.call_tool("get_review_package", {"mode": CANADA, "job_ids": [svc.job_id]})).structured_content
        rv = await c.call_tool("submit_job_review", {"review": {
            "protocol_version": "jsh-review/1", "package_id": pkg["package_id"], "mode": CANADA, "reviews": [{
                "job_id": svc.job_id, "jd_hash": pkg["jobs"][0]["jd_hash"], "technical_fit_score": 70,
                "location_fit": "yes", "work_authorization_fit": "unknown", "recommendation": "review"}]}})
        assert len(rv.structured_content["received"]) == 1
    assert any(h["actor"] == "mcp:claude-test" for h in svc.jobs.history())
    assert svc.reviews.list(CANADA)[0]["source"] == "mcp"


async def test_mcp_read_only_server(svc):
    async with Client(build_server(svc, read_only=True)) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert "search_jobs" in names and not names & {"add_or_update_job", "submit_job_review", "upsert_company",
                                                    "get_review_package"}


async def test_mcp_stdio_subprocess(tmp_path, svc):
    """End-to-end over a real stdio pipe, exactly how Claude Desktop / VS Code launch it."""
    params = StdioServerParameters(command=sys.executable,
                                   args=["-m", "jsh", "--mcp", "--home", str(svc.paths.home), "--agent-name", "e2e"])
    async with Client(params) as c:
        r = await c.call_tool("get_candidate_profile", {"mode": CANADA})
        assert not r.is_error and r.structured_content["approved_skills"]
        r = await c.call_tool("search_jobs", {"mode": CANADA, "view": "all"})
        assert r.structured_content["items"][0]["job_id"] == svc.job_id


def test_mcp_config_snippets():
    s = config_snippets(r"C:\Apps\JobSearchManager\JobSearchManager.exe", r"C:\Apps\JobSearchManager")
    assert json.loads(s["claude_desktop"])["mcpServers"]["job-search-harness"]["args"][0] == "--mcp"
    assert json.loads(s["vscode"])["servers"]["job-search-harness"]["type"] == "stdio"
    assert s["claude_code"].startswith("claude mcp add job-search-harness -- ")
