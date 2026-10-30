"""UI (simulated browser) for session 11: tags, saved views, salary note, ad-liveness, Informes, preferences,
currency settings and 'Borrar todos los datos'."""
import asyncio
import logging

import httpx
import pytest
from nicegui import ui
from nicegui.testing import user_simulation

from jsh.modes import CANADA
from jsh.services import JobInput
from jsh.services.liveness import LivenessService
from jsh.ui.app import configure, index

from .test_handoff import CV, JD


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
    app.candidate.set_skill_status([s.id for s in app.candidate.skills(c.id) if s.name != "Power BI"], "approved")
    app.candidate.set_active_candidate(CANADA, c.id)
    a, _ = app.jobs.upsert_job(JobInput(title="Data Engineer", company_name="Maple Data", job_country="CA",
                                        location_text="Toronto, ON", canonical_url="https://maple.example/jobs/1",
                                        salary_min=100000, salary_max=120000, currency="USD", salary_period="year",
                                        description=JD))
    b, _ = app.jobs.upsert_job(JobInput(title="BI Analyst", company_name="Leaf", job_country="CA",
                                        location_text="Vancouver, BC", canonical_url="https://leaf.example/jobs/2",
                                        description=JD))
    app.salary.set_manual_rate("USD", 1.4)
    app.a, app.b = a.id, b.id
    app.current_mode = CANADA
    configure(app)
    return app


def _close_dialogs(user):
    for dlg in user.find(ui.dialog).elements:
        dlg.close()


async def test_tags_salary_and_liveness_on_cards_and_detail(ready):
    app = ready

    def handler(request):
        return httpx.Response(404) if "maple" in str(request.url) else httpx.Response(200, html="<p>Apply</p>")
    app.liveness = LivenessService(app, transport=httpx.MockTransport(handler), resolver=lambda h: ["93.184.216.34"],
                                   sleep=lambda s: None)
    async with user_simulation(root=index) as user:
        await user.open("/")
        await user.should_see(marker=f"salary_note_{app.a}")
        await user.should_see("≈ 140k–168k CAD/año")
        user.find(marker=f"detail_{app.a}").click()
        await user.should_see(marker="tags_input")
        user.find(marker="tags_input").elements.pop().value = ["Prioridad", "Toronto"]
        user.find(marker="tags_save").click()
        await user.should_see("Etiquetas guardadas: Prioridad, Toronto", retries=10)
        assert app.jobs.tags(app.a) == ["Prioridad", "Toronto"]
        user.find(marker="liveness_check").click()
        await user.should_see(marker="liveness_expire", retries=20)
        await user.should_see(marker=f"closed_badge_{app.a}")
        user.find(marker="liveness_expire").click()
        await user.should_see("Anuncio marcado como expirado", retries=10)
        assert app.jobs.detail(app.a, CANADA)["job"].ad_status == "expired"


async def test_saved_view_from_filters_dialog(ready):
    app = ready
    async with user_simulation(root=index) as user:
        await user.open("/")
        await user.should_see(marker=f"card_{app.b}")
        user.find(marker="filters_open").click()
        await user.should_see(marker="flt_location")
        user.find(marker="flt_location").type("toronto")
        user.find(marker="flt_view_name").type("Toronto")
        user.find(marker="flt_save").click()
        await user.should_see("Vista guardada", retries=10)
        assert app.views.list(CANADA)[0]["filters"] == {"location": "toronto"}
        await user.should_not_see(marker=f"card_{app.b}", retries=10)
        await user.should_see(marker=f"card_{app.a}")
        user.find(marker="filters_clear").click()
        await user.should_see(marker=f"card_{app.b}", retries=10)
        assert app.views.selected(CANADA) == ""


async def test_bulk_tags_in_vacantes(ready):
    app = ready
    async with user_simulation(root=index) as user:
        await user.open("/")
        table = user.find(marker="vacantes_table").elements.pop()
        table.selected = list(table.rows)
        user.find(marker="bulk_tags").click()
        await user.should_see(marker="bulk_tags_input")
        user.find(marker="bulk_tags_input").elements.pop().value = ["Q4"]
        user.find(marker="bulk_tags_add").click()
        await user.should_see("2 vacante(s) actualizada(s)", retries=10)
        assert app.jobs.all_tags() == [("Q4", 2)]


async def test_informes_tab_skill_gaps_and_approve(ready):
    app = ready
    async with user_simulation(root=index) as user:
        await user.open("/")
        await user.should_see(marker="gaps_table")
        await user.should_see("Snowflake")
        await user.should_see(marker="salary_table")
        user.find(marker="gap_approve_Power BI").click()
        await user.should_see("'Power BI' aprobado", retries=10)
        assert any(s.name == "Power BI" and s.status == "approved" for s in app.candidate.skills(
            app.candidate.active_candidate(CANADA).id))


async def test_currency_preferences_and_wipe(ready):
    app = ready
    async with user_simulation(root=index) as user:
        await user.open("/")
        user.find(marker="fx_manual_currency").type("EUR")
        user.find(marker="fx_manual_rate").elements.pop().value = 1.5
        user.find(marker="fx_manual_save").click()
        await user.should_see("Tipo manual guardado", retries=10)
        assert app.salary.rates()["EUR"]["rate"] == 1.5
        user.find(marker="pref_theme").elements.pop().value = "verde"
        user.find(marker="prefs_save").click()
        await user.should_see("Preferencias guardadas", retries=10)
        assert app.db.get_setting("ui_theme") == "verde"
        user.find(marker="wipe_open").click()
        await user.should_see(marker="wipe_word")
        user.find(marker="wipe_word").type("borrar")
        user.find(marker="wipe_confirm").click()
        await user.should_see("Escriba BORRAR TODO para confirmar.", retries=10)
        assert app.db.table_counts()["jobs"] == 2
        user.find(marker="wipe_word").clear().type("BORRAR TODO")
        user.find(marker="wipe_confirm").click()
        for _ in range(100):
            if app.db.table_counts()["jobs"] == 0 and app.db.list_backups():
                break
            await asyncio.sleep(0.2)
        assert app.db.table_counts()["jobs"] == 0
        assert app.db.get_setting("ui_theme") == "verde"  # preferences survive
        assert [p.name.endswith("_pre_wipe.db") for p in app.db.list_backups()] == [True]
