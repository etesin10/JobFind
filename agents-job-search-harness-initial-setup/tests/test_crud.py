"""Session 10: permanent vacancy deletion (+ tombstones), contact 'established' flag, résumé as an AI source,
agent skill proposals and optional auto-approval of high-confidence skills."""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from mcp import Client

from jsh.api import create_api, ensure_tokens
from jsh.connectors.base import FetchResult
from jsh.mcp_server import build_server
from jsh.modes import CANADA, MEXICO
from jsh.services import JobInput
from jsh.services.candidate import AUTO_APPROVE_KEY
from jsh.services.jobs import DeletedJobError

from .test_handoff import CV, JD


@pytest.fixture
def ready(app):
    c = app.candidate.create_candidate("Data")
    app.candidate.import_text(c.id, CV)
    app.candidate.set_skill_status([s.id for s in app.candidate.skills(c.id)], "approved")
    app.candidate.set_item_status([i.id for i in app.candidate.items(c.id)], "approved")
    for m in (CANADA, MEXICO):
        app.candidate.set_active_candidate(m, c.id)
    app.candidate.update_contact(c.id, {"name": "Jane Example", "email": "jane@example.com"})
    job, _ = app.jobs.upsert_job(JobInput(title="Data Engineer", company_name="Maple Data", source="amazon_jobs",
                                          source_job_id="A1", canonical_url="https://maple.example/jobs/1",
                                          description=JD))
    app.cid, app.job_id = c.id, job.id
    return app


# --------------------------------------------------------------------------- vacancy deletion
def test_delete_vacancy_removes_everything_and_keeps_tailored_cv(ready):
    app, jid = ready, ready.job_id
    app.jobs.set_decision(jid, CANADA, "saved")
    app.jobs.set_application_status(jid, CANADA, "applied")
    contact_id, _ = app.crm.upsert_contact({"name": "Ana", "company_name": "Maple Data"})
    app.crm.add_event(contact_id, "email", "CV enviado", job_id=jid, mode_id=CANADA)
    pkg = app.reviews.build_package(CANADA, [jid])
    app.reviews.import_result({"protocol_version": "jsh-review/1", "package_id": pkg["package_id"], "mode": CANADA,
                               "reviews": [{"job_id": jid, "jd_hash": pkg["jobs"][0]["jd_hash"],
                                            "technical_fit_score": 70, "location_fit": "yes",
                                            "work_authorization_fit": "unknown", "recommendation": "review"}]})
    run = app.tailoring.deterministic_proposal(jid, CANADA)
    vid = app.tailoring.approve(run["run_id"], [c["change_id"] for c in app.tailoring.get_run(run["run_id"])["changes"]])

    res = app.jobs.delete_jobs([jid])
    assert res["deleted"] == 1 and res["backup"]
    assert app.jobs.rows(CANADA, "all") == []
    con = sqlite3.connect(app.paths.db_file)
    for table in ("jobs", "job_sources", "job_snapshots", "job_mode_evaluations", "user_job_decisions",
                  "applications", "application_events", "ai_reviews", "tailoring_runs"):
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table
    assert con.execute("SELECT job_id FROM resume_versions WHERE id = ?", (vid,)).fetchone() == (None,)
    assert con.execute("SELECT job_id, note FROM contact_events").fetchone() == (None, "CV enviado")
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []
    con.close()
    assert any(h["action"] == "job_deleted" for h in app.jobs.history())
    assert app.jobs.deleted_jobs()[0]["title"] == "Data Engineer"
    assert list(app.paths.backups.glob("*pre_delete_jobs.db"))
    with pytest.raises(ValueError):
        app.jobs.delete_jobs([])


def test_deleted_vacancy_is_never_recreated_by_agents_connectors_or_imports(ready, monkeypatch):
    app = ready
    app.jobs.delete_jobs([app.job_id])
    same_url = JobInput(title="Data Engineer", canonical_url="https://maple.example/jobs/1")
    same_src = JobInput(title="Data Engineer (re-post)", source="amazon_jobs", source_job_id="A1")
    for inp in (same_url, same_src):
        for actor in ("connector:amazon_jobs", "csv_import", "excel_import", "agent:x"):
            with pytest.raises(DeletedJobError):
                app.jobs.upsert_job(inp, actor=actor)
    # inbox / API / MCP: not an error, just skipped
    con = sqlite3.connect(app.paths.db_file, timeout=30)
    con.execute("INSERT INTO job_inbox(payload) VALUES (?)",
                (json.dumps({"title": "Data Engineer", "canonical_url": "https://maple.example/jobs/1"}),))
    con.commit()
    app.sync.tick()
    status, result, message = con.execute("SELECT status, result, message FROM job_inbox").fetchone()
    con.close()
    assert (status, result) == ("imported", "unchanged") and "eliminada" in message
    # connector run: counted as a warning, run stays OK
    from jsh.services import discovery as disc

    class FakeConn:
        hosts, min_interval = ("example.com",), 0

        def fetch(self, q, http):
            return FetchResult(jobs=[same_src, JobInput(title="New one", canonical_url="https://n.example/1")])
    src = app.discovery.create_source({"name": "Fake", "provider": "amazon_jobs", "keywords": ["data"]})
    monkeypatch.setitem(disc.CONNECTORS, "amazon_jobs", FakeConn())
    run = app.discovery.run_source(src.id)
    assert run.status == "ok" and run.created == 1 and any("eliminó" in w for w in run.warnings)
    assert [r["title"] for r in app.jobs.rows(CANADA, "all")] == ["New one"]
    # the user adding it again by hand is an explicit decision
    job, st = app.jobs.upsert_job(same_url, actor="user")
    assert st == "created" and app.jobs.deleted_jobs() == []


def test_forget_deleted_allows_reimport(ready):
    app = ready
    app.jobs.delete_jobs([app.job_id])
    app.jobs.forget_deleted(app.jobs.deleted_jobs()[0]["id"])
    _, st = app.jobs.upsert_job(JobInput(title="Data Engineer", canonical_url="https://maple.example/jobs/1"),
                                actor="agent:x")
    assert st == "created"


# --------------------------------------------------------------------------- contact established
def test_contact_established_flag_user_and_agents(ready):
    crm = ready.crm
    cid, _ = crm.upsert_contact({"name": "Ana", "email": "ana@m.example"})
    assert crm.get_contact(cid)["established"] is False
    crm.upsert_contact({"email": "ana@m.example", "established": "true"}, actor="api:x", fill_only=True)
    assert crm.get_contact(cid)["established"] is True  # user never set it: an agent may
    crm.upsert_contact({"contact_id": cid, "established": False})  # the user decides
    crm.upsert_contact({"email": "ana@m.example", "established": True}, actor="api:x", fill_only=True)
    assert crm.get_contact(cid)["established"] is False  # agent cannot override the user's choice
    assert [c["name"] for c in crm.list_contacts(established=False)] == ["Ana"]
    assert crm.list_contacts(established=True) == []
    with pytest.raises(ValueError):
        crm.upsert_contact({"contact_id": cid, "established": "maybe"})


# --------------------------------------------------------------------------- résumé as an AI source
def test_resume_for_agents_has_ids_skills_and_masked_source_text(ready):
    app = ready
    doc = app.tailoring.resume_for_agents(CANADA)
    assert doc["master_resume"]["experience"][0]["bullets"][0]["id"] == "P1.H1"
    assert "SQL" in doc["approved_skills"] and doc["source_texts"]
    blob = json.dumps(doc)
    assert "jane@example.com" not in blob and "Jane Example" not in blob
    run = app.tailoring.deterministic_proposal(app.job_id, CANADA)
    vid = app.tailoring.approve(run["run_id"], [c["change_id"] for c in app.tailoring.get_run(run["run_id"])["changes"]])
    doc = app.tailoring.resume_for_agents(CANADA, include_source_text=False)
    assert doc["tailored_versions"][0]["resume_version_id"] == vid and "source_texts" not in doc
    tv = app.tailoring.tailored_for_agents(vid)
    assert "EXPERIENCE" in tv["text"] and "jane@example.com" not in json.dumps(tv)
    with pytest.raises(KeyError):
        app.tailoring.tailored_for_agents("nope")


def test_source_text_drops_name_header_even_without_contact_header(app):
    from jsh.privacy import mask_document
    cv = "Juan Pérez\nSenior Data Engineer · Guadalajara\n\nSUMMARY\nBuilt pipelines.\n\nEXPERIENCE\nDev at X\n"
    out = mask_document(cv)
    assert "Juan Pérez" not in out and out.startswith("SUMMARY") and "Dev at X" in out
    assert mask_document("No section titles here\nJust text") == "No section titles here\nJust text"


def test_agent_skill_proposals_are_never_auto_approved(ready):
    app = ready
    rep = app.tailoring.propose_skills(CANADA, [{"name": "Snowflake", "evidence": "used Snowflake at Contoso"},
                                                {"name": "SQL"}, {"name": ""}], actor="mcp:claude")
    assert rep["proposed"] == 1 and rep["existing"] == ["SQL (approved)"] and rep["ignored"] == ["(sin nombre)"]
    sk = next(s for s in app.candidate.skills(app.cid) if s.name == "Snowflake")
    assert (sk.status, sk.origin) == ("proposed", "agent")
    assert app.candidate.evidence(sk.id)[0].excerpt == "used Snowflake at Contoso"
    with pytest.raises(ValueError):
        app.tailoring.propose_skills(CANADA, [], actor="mcp:x")


def test_auto_approve_high_confidence_on_import(app):
    c = app.candidate.create_candidate("Auto")
    app.db.set_setting(AUTO_APPROVE_KEY, True)
    res = app.candidate.import_text(c.id, CV)
    skills = app.candidate.skills(c.id)
    high = [s for s in skills if s.confidence == "high"]
    assert high and res.auto_approved == len(high)
    assert all(s.status == "approved" for s in high)
    assert all(s.status == "proposed" for s in skills if s.confidence != "high")
    # a skill the user rejected is never re-approved by a later import
    app.candidate.set_skill_status([high[0].id], "rejected")
    app.candidate.import_text(c.id, CV + "\nExtra line to change the hash.")
    assert app.candidate.get_skill(high[0].id).status == "rejected"


def test_auto_approve_off_by_default(app):
    c = app.candidate.create_candidate("Manual")
    res = app.candidate.import_text(c.id, CV)
    assert res.auto_approved == 0 and all(s.status == "proposed" for s in app.candidate.skills(c.id))


# --------------------------------------------------------------------------- API / MCP / inbox
def test_api_and_mcp_resume_skills_and_no_delete(ready):
    tokens = ensure_tokens(ready)
    c = TestClient(create_api(ready), base_url="http://127.0.0.1")
    W = {"Authorization": f"Bearer {tokens['write']}", "X-Agent-Name": "t"}
    R = {"Authorization": f"Bearer {tokens['read']}"}
    r = c.get("/resume", params={"mode": CANADA}, headers=R)
    assert r.status_code == 200 and r.json()["master_resume"]["summary"][0]["id"] == "S1"
    r = c.post("/skills/propose", json={"mode": CANADA, "skills": [{"name": "dbt"}]}, headers=W)
    assert r.json()["proposed"] == 1
    assert c.post("/skills/propose", json={"mode": CANADA, "skills": [{"name": "x"}]}, headers=R).status_code == 403
    assert c.delete(f"/jobs/{ready.job_id}", headers=W).status_code == 403
    ready.crm.upsert_contact({"name": "Luis", "established": True})
    assert [x["name"] for x in c.get("/contacts", params={"established": True}, headers=R).json()] == ["Luis"]

    async def go():
        async with Client(build_server(ready, "t")) as m:
            names = {t.name for t in (await m.list_tools()).tools}
            assert {"get_resume", "get_tailored_resume", "propose_skills"} <= names
            assert not any("delete" in n for n in names)
            got = (await m.call_tool("get_resume", {"mode": CANADA, "include_source_text": False})).structured_content
            assert got["approved_skills"]
            prop = (await m.call_tool("propose_skills", {"mode": CANADA, "skills": [{"name": "Kafka"}]}))
            assert prop.structured_content["proposed"] == 1
            return (await m.call_tool("list_contacts", {"established": "yes"})).structured_content
    import asyncio
    assert [x["name"] for x in asyncio.run(go())["result"]] == ["Luis"]


def test_inbox_type_skills(ready):
    con = sqlite3.connect(ready.paths.db_file, timeout=30)
    con.execute("INSERT INTO job_inbox(payload, source) VALUES (?, 'claude')",
                (json.dumps({"type": "skills", "mode": CANADA, "skills": [{"name": "Terraform"}, {"name": "SQL"}]}),))
    con.commit()
    seq = ready.sync.seq
    ready.sync.tick()
    status, result, message = con.execute("SELECT status, result, message FROM job_inbox").fetchone()
    con.close()
    assert (status, result) == ("imported", "created") and "SQL" in message
    assert any(s.name == "Terraform" and s.status == "proposed" for s in ready.candidate.skills(ready.cid))
    assert ready.sync.changes_since(seq)["skills"] == 1


def test_migration_6_from_v5(tmp_path):
    from sqlalchemy import text

    from jsh.db import SCHEMA_VERSION
    from jsh.paths import get_paths
    from jsh.services import AppServices
    home = tmp_path / "v5"
    svc = AppServices(get_paths(home))
    svc.crm.upsert_contact({"name": "Old"})
    svc.close()
    con = sqlite3.connect(get_paths(home).db_file)
    con.execute("DROP TABLE deleted_jobs")
    con.execute("ALTER TABLE contacts DROP COLUMN established")
    con.execute("PRAGMA user_version = 5")
    con.commit()
    con.close()
    svc = AppServices(get_paths(home))
    with svc.db.engine.connect() as c:
        assert c.execute(text("PRAGMA user_version")).scalar() == SCHEMA_VERSION
    assert svc.crm.list_contacts()[0]["established"] is False and svc.jobs.deleted_jobs() == []
    svc.close()
