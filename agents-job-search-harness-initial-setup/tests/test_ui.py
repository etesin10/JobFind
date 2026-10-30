"""UI tests using NiceGUI's simulated user (no browser required)."""
import logging

import pytest
from nicegui import ui
from nicegui.testing import user_simulation

from jsh.modes import CANADA, MEXICO
from jsh.services import JobInput
from jsh.ui.app import configure, index


@pytest.fixture(autouse=True)
def no_error_logs(caplog):
    caplog.set_level(logging.WARNING)
    yield
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, [r.getMessage() for r in errors]


async def test_detail_dialog_kanban_and_vacantes(app):
    configure(app)
    app.current_mode = CANADA
    job, _ = app.jobs.upsert_job(JobInput(title="Data Eng CA", company_name="Maple", job_country="CA",
                                          canonical_url="https://m.ca/1", description="<script>x</script> SQL"))
    app.jobs.set_application_status(job.id, CANADA, "applied")
    app.jobs.set_decision(job.id, CANADA, "discarded", "other")
    async with user_simulation(root=index) as user:
        await user.open("/")
        await user.should_see("Aplicado (1)")  # kanban column
        app.jobs.restore(job.id, CANADA)
        user.find(marker="vacantes_table").trigger("rowClick", [{}, {"job_id": job.id}, 0])
        await user.should_see("Datos específicos del modo (CANADA_VISA)")
        await user.should_see("<script>x</script> SQL")  # rendered as escaped text
        user.find(marker="notes").type("nota UI")
        user.find("Guardar notas").click()
        assert app.jobs.rows(CANADA, "all")[0]["notes"] == "nota UI"
        for dlg in user.find(ui.dialog).elements:
            dlg.close()
        await user.should_see(marker=f"save_{job.id}")  # refresh runs as a background task
        user.find(marker=f"save_{job.id}").click()
        assert app.jobs.rows(CANADA, "all")[0]["user_decision"] == "saved"


async def test_mode_switch_add_discard_restore(app):
    configure(app)
    app.current_mode = MEXICO
    async with user_simulation(root=index) as user:
        await user.open("/")
        await user.should_see("México — Remoto desde México")

        # add a manual job through the form
        user.find(marker="add_title").type("Remote Data Engineer")
        user.find(marker="add_company").type("LatamCo")
        user.find(marker="add_url").type("https://jobs.example.com/latamco/42")
        user.find(marker="add_remote_countries").type("LATAM")
        for el in user.find(marker="add_work_mode").elements:
            el.value = "remote"
        user.find(marker="add_save").click()
        rows = app.jobs.rows(MEXICO, "activos")
        assert [r["title"] for r in rows] == ["Remote Data Engineer"]
        job_id = rows[0]["job_id"]
        assert rows[0]["eligibility_status"] == "eligible"
        await user.should_see(marker=f"card_{job_id}")

        # duplicate add is detected, not duplicated
        user.find(marker="add_title").type("Remote Data Engineer")
        user.find(marker="add_url").type("https://jobs.example.com/latamco/42/")
        user.find(marker="add_save").click()
        assert len(app.jobs.rows(MEXICO, "all")) == 1

        # discard with reason
        user.find(marker=f"discard_{job_id}").click()
        for el in user.find(marker="discard_reason").elements:
            el.value = "salary"
        user.find(marker="discard_confirm").click()
        assert app.jobs.rows(MEXICO, "activos") == []
        await user.should_not_see(marker=f"card_{job_id}")
        assert app.jobs.rows(MEXICO, "discarded")[0]["discard_reason"] == "salary"

        # restore from Historial
        user.find(marker=f"restore_{job_id}").click()
        assert len(app.jobs.rows(MEXICO, "activos")) == 1
        await user.should_see(marker=f"card_{job_id}")

        # switch mode → Canada view, job persists, different evaluation
        for el in user.find(marker="mode_select").elements:
            el.value = CANADA
        await user.should_see("Canadá — Visa / Permiso de trabajo")
        assert app.current_mode == CANADA
        assert len(app.jobs.rows(CANADA, "all")) == 1


async def test_rule_toggle_from_ui_changes_results(app):
    configure(app)
    app.current_mode = MEXICO
    app.jobs.upsert_job(JobInput(title="Onsite job", company_name="X", canonical_url="https://x.com/1",
                                 work_mode="onsite"), MEXICO)
    assert app.jobs.rows(MEXICO, "activos") == []
    rule = next(r for r in app.rules.list(MEXICO) if r.name == "Trabajo remoto")
    async with user_simulation(root=index) as user:
        await user.open("/")
        for el in user.find(marker=f"rule_switch_{rule.id}").elements:
            el.value = False
        assert not app.rules.get(rule.id).enabled
        row = app.jobs.rows(MEXICO, "all")[0]
        assert row["eligibility_status"] == "needs_verification"  # remote-country still unknown
        explanation_rules = app.jobs.detail(row["job_id"], MEXICO)["evaluation"].explanation
        assert all("Trabajo remoto" not in o["name"] for o in explanation_rules)
        assert len(app.jobs.rows(MEXICO, "activos")) == 1


async def test_new_rule_dialog_simulate_and_save(app):
    configure(app)
    app.current_mode = MEXICO
    app.jobs.upsert_job(JobInput(title="Java Dev", company_name="X", canonical_url="https://x.com/2",
                                 work_mode="remote", remote_eligible_countries="MX",
                                 description="Java Spring"), MEXICO)
    async with user_simulation(root=index) as user:
        await user.open("/")
        user.find(marker="new_rule").click()
        user.find(marker="rule_name").type("Excluir Java")
        for el in user.find(marker="rule_field").elements:
            el.value = "description"
        for el in user.find(marker="rule_operator").elements:
            el.value = "any_keyword"
        user.find(marker="rule_value").type("java")
        for el in user.find(marker="rule_severity").elements:
            el.value = "hard"
        user.find(marker="rule_simulate").click()
        await user.should_see(marker="sim_summary")
        assert app.jobs.rows(MEXICO, "all")[0]["eligibility_status"] == "eligible"  # not saved yet
        user.find(marker="rule_save").click()
        assert app.jobs.rows(MEXICO, "all")[0]["eligibility_status"] == "blocked"


async def test_export_button_and_all_view(app, tmp_path):
    configure(app)
    app.current_mode = CANADA
    app.jobs.upsert_job(JobInput(title="DE", company_name="Y", canonical_url="https://y.com/1", job_country="CA"))
    async with user_simulation(root=index) as user:
        await user.open("/")
        user.find(marker="export_excel").click()
        assert (app.paths.excel / "Job_Search_Canada.xlsx").exists()
        user.find(marker="backup_now").click()
        assert list(app.paths.backups.glob("*manual.db"))
        for el in user.find(marker="mode_select").elements:
            el.value = "ALL"
        await user.should_see("Seleccione un modo (Canadá o México) arriba para ver esta sección.")


async def test_kanban_drag_and_drop(app):
    configure(app)
    app.current_mode = MEXICO
    job, _ = app.jobs.upsert_job(JobInput(title="Kanban job", company_name="K", canonical_url="https://k.com/1"))
    app.jobs.set_application_status(job.id, MEXICO, "applied")
    async with user_simulation(root=index) as user:
        await user.open("/")
        await user.should_see(marker=f"kanban_card_{job.id}")
        user.find(marker=f"kanban_card_{job.id}").trigger("dragstart")
        user.find(marker="kanban_col_interviewing").trigger("drop")
        assert app.jobs.rows(MEXICO)[0]["application_status"] == "interviewing"
        events = app.jobs.detail(job.id, MEXICO)["app_events"]
        assert events[0].to_status == "interviewing" and events[0].note == "Kanban"


async def test_profile_clone_and_suggestion_flow_in_ui(app):
    configure(app)
    app.current_mode = MEXICO
    for i in range(3):
        j, _ = app.jobs.upsert_job(JobInput(title=f"Junior Dev {i}", company_name=f"C{i}",
                                            canonical_url=f"https://c.com/{i}", work_mode="remote",
                                            remote_eligible_countries="MX"))
        app.jobs.set_decision(j.id, MEXICO, "discarded", "seniority")
    sug = app.suggestions.suggestions(MEXICO)[0]
    async with user_simulation(root=index) as user:
        await user.open("/")
        await user.should_see(marker="suggestions_banner")
        user.find(marker="profile_clone").click()
        user.find(marker="profile_name").type("Perfil sin junior")
        user.find(marker="profile_confirm").click()
        assert app.rules.active_profile(MEXICO).name == "Perfil sin junior"
        await user.should_see(marker=f"suggestion_open_{sug.key}")
        user.find(marker=f"suggestion_open_{sug.key}").click()
        await user.should_see("Nueva regla (sugerida — revise y confirme)")
        user.find(marker="rule_simulate").click()
        await user.should_see(marker="sim_summary")
        user.find(marker="rule_save").click()
        assert any(r.name == sug.rule["name"] for r in app.rules.list(MEXICO))
        default = next(p for p in app.rules.profiles(MEXICO) if p.is_default)
        app.rules.set_active_profile(MEXICO, default.id)
        assert not any(r.name == sug.rule["name"] for r in app.rules.list(MEXICO))


async def test_restore_backup_and_bulk_confirm_in_ui(app):
    configure(app)
    app.current_mode = CANADA
    a, _ = app.jobs.upsert_job(JobInput(title="A", company_name="X", canonical_url="https://x.com/a"))
    app.db.backup("manual")
    app.jobs.upsert_job(JobInput(title="B", company_name="X", canonical_url="https://x.com/b"))
    async with user_simulation(root=index) as user:
        await user.open("/")
        table = user.find(marker="vacantes_table").elements.pop()
        table.selected = [r for r in table.rows if r["title"] == "A"]
        user.find("Guardar seleccionadas").click()
        assert app.jobs.rows(CANADA, "all")[0]["user_decision"] == "new"  # nothing until confirmed
        user.find(marker="bulk_confirm").click()
        assert {r["title"]: r["user_decision"] for r in app.jobs.rows(CANADA)}["A"] == "saved"

        user.find(marker="restore_backup_0").click()
        user.find(marker="restore_confirm").click()
        assert [r["title"] for r in app.jobs.rows(CANADA)] == ["A"]
        assert any("pre_restore" in b.name for b in app.db.list_backups())


async def test_skills_tab_flow(app):
    configure(app)
    app.current_mode = CANADA
    job, _ = app.jobs.upsert_job(JobInput(title="Analyst", company_name="Co", canonical_url="https://s.example/1",
                                          job_country="CA", description="Python, SQL and Tableau required."))
    resume = ("SUMMARY\nAnalyst building reports.\nSKILLS\nPython, SQL, Tableau\nEXPERIENCE\n"
              "Analyst | Retail Corp | 2020 - Present\n- Automated reports with Python and SQL.\n")
    async with user_simulation(root=index) as user:
        await user.open("/")
        user.find(marker="cand_name").type(" Datos")
        user.find(marker="cand_create").click()
        cand = app.candidate.list_candidates()[0]
        assert cand.name == "Mi perfil Datos"
        await user.should_see(marker="paste_text")
        user.find(marker="paste_text").type(resume)
        user.find(marker="paste_import").click()
        await user.should_see(marker="select_high")  # import runs in a worker thread
        assert {s.status for s in app.candidate.skills(cand.id)} == {"proposed"}
        assert app.jobs.rows(CANADA)[0]["skills_match"] is None  # proposals never count
        user.find(marker="select_high").click()
        user.find(marker="approve_selected").click()
        approved = {s.name for s in app.candidate.skills(cand.id, "approved")}
        assert {"Python", "SQL"} <= approved and "Tableau" not in approved  # Tableau only in the list (medium)
        row = app.jobs.rows(CANADA)[0]
        assert row["skills_match"] == pytest.approx(66.7, abs=0.1)
        await user.should_see(marker=f"skills_badge_{job.id}")

        user.find(marker="manual_skill").click()
        user.find(marker="manual_name").type("Tableau")
        user.find(marker="manual_save").click()
        # Tableau already exists as a proposal: the dialog reports it and no duplicate is created.
        assert len([s for s in app.candidate.skills(cand.id) if s.name == "Tableau"]) == 1

        for dlg in user.find(ui.dialog).elements:
            dlg.close()
        user.find(marker="save_master").click()
        assert app.candidate.versions(cand.id)[0].version == 1

        user.find(marker="vacantes_table").trigger("rowClick", [{}, {"job_id": job.id}, 0])
        await user.should_see(marker="jd_cv_pct")


async def test_discover_tab_example_and_run(app):
    import httpx
    from tests.test_discovery import AMAZON_JOB, amazon_handler
    configure(app)
    app.current_mode = CANADA
    app.discovery.http_options = {"transport": httpx.MockTransport(amazon_handler({0: [AMAZON_JOB]}, [])),
                                  "sleep": lambda s: None, "min_interval": 0}
    async with user_simulation(root=index) as user:
        await user.open("/")
        user.find(marker="source_example").click()
        src = app.discovery.list_sources()[0]
        assert src.provider == "amazon_jobs" and src.countries == ["CA"]
        await user.should_see(marker=f"source_run_{src.id}")
        user.find(marker=f"source_run_{src.id}").click()
        await user.should_see(marker="runs_table")  # run happens in a worker thread
        assert [r["title"] for r in app.jobs.rows(CANADA)] == ["Data Engineer, Amazon Prime"]
        assert app.discovery.runs()[0].status == "ok"