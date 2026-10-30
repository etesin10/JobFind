"""UI (simulated browser) for session 9: JD keywords, AI review import/accept, tailoring, Empresas/Contactos,
Análisis IA and the API/MCP configuration section."""
import json
import logging

import pytest
from nicegui import ui
from nicegui.testing import user_simulation

from jsh.modes import CANADA
from jsh.ui.app import configure, index

from .test_handoff import CV, JD
from jsh.services import JobInput


@pytest.fixture(autouse=True)
def no_error_logs(caplog):
    caplog.set_level(logging.WARNING)
    yield
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]


@pytest.fixture
def ready(app):
    c = app.candidate.create_candidate("Data")
    app.candidate.import_text(c.id, CV)
    app.candidate.set_skill_status([s.id for s in app.candidate.skills(c.id)], "approved")
    app.candidate.set_item_status([i.id for i in app.candidate.items(c.id)], "approved")
    app.candidate.set_active_candidate(CANADA, c.id)
    job, _ = app.jobs.upsert_job(JobInput(title="Data Engineer", company_name="Maple Data", job_country="CA",
                                          canonical_url="https://maple.example/jobs/1", description=JD))
    app.job_id = job.id
    app.current_mode = CANADA
    configure(app)
    return app


def _close_dialogs(user):
    for dlg in user.find(ui.dialog).elements:
        dlg.close()


async def test_detail_keywords_review_and_tailoring(ready):
    app = ready
    async with user_simulation(root=index) as user:
        await user.open("/")
        user.find(marker=f"detail_{app.job_id}").click()
        await user.should_see(marker="jd_keywords")
        await user.should_see("Requeridas:")
        await user.should_see("✓ SQL")  # approved skill highlighted
        # import an AI review through the paste dialog
        pkg = app.reviews.build_package(CANADA, [app.job_id])
        result = {"protocol_version": "jsh-review/1", "package_id": pkg["package_id"], "mode": CANADA,
                  "provider": "claude", "model": "m", "reviews": [{
                      "job_id": app.job_id, "jd_hash": pkg["jobs"][0]["jd_hash"], "technical_fit_score": 81,
                      "location_fit": "yes", "work_authorization_fit": "unknown", "recommendation": "high_priority",
                      "summary": "Encaja muy bien"}]}
        user.find(marker="review_paste").click()
        user.find(marker="paste_json").type(json.dumps(result))
        user.find(marker="paste_submit").click()
        rid = app.reviews.list(CANADA)[0]["review_id"]
        await user.should_see(marker=f"accept_review_{rid}", retries=20)
        await user.should_see(marker=f"review_{rid}")
        user.find(marker=f"accept_review_{rid}").click()
        assert app.jobs.rows(CANADA)[0]["ai_fit_score"] == 81
        # deterministic tailoring → review dialog → approve
        user.find(marker="tailor_auto").click()
        await user.should_see(marker="tailor_approve")
        user.find(marker="tailor_approve").click()
        await user.should_see("CV adaptado aprobado", retries=10)
        ver = app.tailoring.tailored_versions(app.job_id)[0]
        await user.should_see(marker=f"docx_{ver.id}")
        user.find(marker=f"docx_{ver.id}").click()
        assert ver.id and list((app.paths.resumes / "tailored").glob("*.docx"))
        _close_dialogs(user)
        await user.should_see(marker=f"ai_badge_{app.job_id}")


async def test_companies_and_contacts_tabs(ready):
    app = ready
    async with user_simulation(root=index) as user:
        await user.open("/")
        await user.should_see(marker="companies_table")
        cid = app.crm.list_companies()[0]["company_id"]
        user.find(marker="companies_table").trigger("rowClick", [{}, {"company_id": cid}, 0])
        await user.should_see(marker="company_save")
        for el in user.find(marker="company_watch").elements:
            el.value = True
        user.find(marker="company_save").click()
        assert app.crm.get_company(cid)["watch"] is True
        _close_dialogs(user)
        user.find(marker="contact_new").click()
        user.find(marker="contact_name").type("Ana Recruiter")
        user.find(marker="contact_email").type("ana@maple.example")
        user.find(marker="contact_save").click()
        contacts = app.crm.list_contacts()
        assert contacts[0]["name"] == "Ana Recruiter"
        _close_dialogs(user)
        await user.should_see("Ana Recruiter")
        user.find(marker="contacts_table").trigger("rowClick", [{}, {"contact_id": contacts[0]["contact_id"]}, 0])
        user.find(marker="event_note").type("Primer contacto")
        user.find(marker="event_add").click()
        assert app.crm.get_contact(contacts[0]["contact_id"])["events"][0]["note"] == "Primer contacto"


async def test_ai_tab_package_and_config_section(ready):
    app = ready
    async with user_simulation(root=index) as user:
        await user.open("/")
        await user.should_see(marker="ai_jobs_table")
        for t in user.find(marker="ai_jobs_table").elements:
            t.selected = [r for r in t.rows]
        user.find(marker="ai_build").click()
        assert list(app.paths.exports.glob("review_CANADA_VISA_*.md"))
        await user.should_see(marker="ai_counts")
        await user.should_see(marker="api_enabled")
        await user.should_see(marker="copy_token_write")
        await user.should_see("Claude Desktop (claude_desktop_config.json)")
        await user.should_see(marker="contact_box")


async def test_delete_vacancy_from_detail_with_confirmation_word(ready):
    app = ready
    async with user_simulation(root=index) as user:
        await user.open("/")
        user.find(marker=f"detail_{app.job_id}").click()
        await user.should_see(marker="detail_delete")
        user.find(marker="detail_delete").click()
        await user.should_see(marker="delete_confirm")
        user.find(marker="delete_confirm").click()  # without the word: refused
        assert len(app.jobs.rows(CANADA, "all")) == 1
        user.find(marker="delete_confirm_word").type("eliminar")
        user.find(marker="delete_confirm").click()
        assert app.jobs.rows(CANADA, "all") == []
        await user.should_see(marker="deleted_jobs")


async def test_bulk_delete_and_contact_established_switch(ready):
    app = ready
    other, _ = app.jobs.upsert_job(JobInput(title="Other job", canonical_url="https://o.example/2"))
    async with user_simulation(root=index) as user:
        await user.open("/")
        for t in user.find(marker="vacantes_table").elements:
            t.selected = [r for r in t.rows if r["job_id"] == other.id]
        user.find(marker="bulk_delete").click()
        user.find(marker="delete_confirm_word").type("ELIMINAR")
        user.find(marker="delete_confirm").click()
        assert [r["job_id"] for r in app.jobs.rows(CANADA, "all")] == [app.job_id]
        _close_dialogs(user)
        user.find(marker="contact_new").click()
        user.find(marker="contact_name").type("Ana Established")
        for el in user.find(marker="contact_established").elements:
            el.value = True
        user.find(marker="contact_save").click()
        assert app.crm.list_contacts()[0]["established"] is True