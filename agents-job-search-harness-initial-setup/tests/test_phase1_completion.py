"""Tests for Phase 1 completion: profiles, discard suggestions, backup restore, full workbook sheets."""
import sqlite3

import pytest
from openpyxl import load_workbook

from jsh.modes import CANADA, MEXICO
from jsh.services import JobInput


def _job(i, **kw):
    base = dict(title=f"Data Engineer {i}", company_name=f"Co{i}", canonical_url=f"https://jobs.example.com/{i}",
                work_mode="remote", remote_eligible_countries="MX", description="SQL and Python")
    base.update(kw)
    return JobInput(**base)


# ---------------------------------------------------------------- profiles
def test_clone_switch_profile_changes_evaluation_only(app):
    job, _ = app.jobs.upsert_job(_job(1, description="Java shop"))
    app.jobs.update_notes(job.id, "mi nota")
    app.jobs.set_decision(job.id, MEXICO, "saved")
    original = app.rules.active_profile(MEXICO)
    n_rules = len(app.rules.list(MEXICO))

    new = app.rules.clone_profile(MEXICO, "Sin Java")
    assert app.rules.active_profile(MEXICO).id == new.id
    assert len(app.rules.list(MEXICO)) == n_rules  # rules copied
    app.rules.create(MEXICO, {"name": "Excluir Java", "field": "description", "operator": "any_keyword",
                              "value": "java", "action": "exclude", "severity": "hard", "unknown_handling": "keep"})
    assert app.jobs.rows(MEXICO)[0]["eligibility_status"] == "blocked"
    assert app.rules.active_profile(MEXICO).version > 1

    app.rules.set_active_profile(MEXICO, original.id)  # original profile untouched
    assert len(app.rules.list(MEXICO)) == n_rules
    row = app.jobs.rows(MEXICO)[0]
    assert row["eligibility_status"] == "eligible"
    assert row["notes"] == "mi nota" and row["user_decision"] == "saved"  # decisions survive profile switches
    assert len(app.rules.list(CANADA)) > 0  # other mode unaffected


def test_profile_name_rules_and_delete_guards(app):
    default = app.rules.active_profile(MEXICO)
    with pytest.raises(ValueError):
        app.rules.clone_profile(MEXICO, "  ")
    p = app.rules.clone_profile(MEXICO, "B")
    with pytest.raises(ValueError):
        app.rules.clone_profile(MEXICO, "B")
    with pytest.raises(ValueError):
        app.rules.delete_profile(p.id)  # active
    with pytest.raises(ValueError):
        app.rules.delete_profile(default.id)  # default
    app.rules.rename_profile(p.id, "B2")
    app.rules.set_active_profile(MEXICO, default.id)
    app.rules.delete_profile(p.id)
    assert [x.name for x in app.rules.profiles(MEXICO)] == [default.name]


def test_excel_filtros_ignored_when_profile_differs(app):
    app.jobs.upsert_job(_job(1))
    path = app.workbooks.export_mode(MEXICO)
    app.rules.clone_profile(MEXICO, "Otro")
    wb = load_workbook(path)
    ws = wb["Filtros"]
    ws.cell(row=2, column=[c.value for c in ws[1]].index("enabled") + 1).value = False
    wb.save(path)
    pv = app.workbooks.preview_import(path, MEXICO)
    assert any("perfil" in w for w in pv.warnings)
    assert not [c for c in pv.changes if c.kind == "rule_field"]


# ---------------------------------------------------------------- suggestions
def test_no_suggestion_below_threshold_and_dismiss(app):
    for i in range(2):
        j, _ = app.jobs.upsert_job(_job(i, title=f"Junior Analyst {i}"))
        app.jobs.set_decision(j.id, MEXICO, "discarded", "seniority")
    assert app.suggestions.suggestions(MEXICO) == []
    j, _ = app.jobs.upsert_job(_job(9, title="Junior Analyst 9"))
    app.jobs.set_decision(j.id, MEXICO, "discarded", "seniority")
    sugs = app.suggestions.suggestions(MEXICO)
    assert len(sugs) == 1 and sugs[0].rule["field"] == "title" and sugs[0].rule["value"] == ["junior"]
    assert sugs[0].count == 3
    # suggestions never create rules by themselves
    assert not any(r.name == sugs[0].rule["name"] for r in app.rules.list(MEXICO))
    app.suggestions.dismiss(MEXICO, sugs[0].key)
    assert app.suggestions.suggestions(MEXICO) == []
    assert app.suggestions.suggestions(CANADA) == []


def test_company_and_authorization_suggestions_and_covered(app):
    for i in range(3):
        j, _ = app.jobs.upsert_job(_job(i, company_name="ShadyCorp", job_country="US",
                                        description="Must hold security clearance. US citizen only."))
        app.jobs.set_decision(j.id, CANADA, "discarded", "untrusted_company")
    for i in range(3, 6):
        j, _ = app.jobs.upsert_job(_job(i, description="Requires security clearance"))
        app.jobs.set_decision(j.id, CANADA, "discarded", "requires_authorization")
    sugs = {s.reason_code: s for s in app.suggestions.suggestions(CANADA)}
    assert sugs["untrusted_company"].rule["value"] == ["ShadyCorp"]
    assert "security clearance" in sugs["requires_authorization"].rule["value"]
    # creating the suggested rule makes the suggestion disappear (covered)
    rule = dict(sugs["untrusted_company"].rule)
    app.rules.create(CANADA, rule)
    assert "untrusted_company" not in {s.reason_code for s in app.suggestions.suggestions(CANADA)}


def test_authorization_suggestion_ignores_phrases_in_kept_or_rare(app):
    eeo = "We hire without regard to citizenship status. Requires security clearance."
    for i in range(3):
        j, _ = app.jobs.upsert_job(_job(i, description=eeo if i == 0 else "Requires security clearance"))
        app.jobs.set_decision(j.id, CANADA, "discarded", "requires_authorization")
    keep, _ = app.jobs.upsert_job(_job(5, description="without regard to citizenship status. Green card."))
    app.jobs.set_decision(keep.id, CANADA, "saved")
    sug = [s for s in app.suggestions.suggestions(CANADA) if s.reason_code == "requires_authorization"]
    assert sug and sug[0].rule["value"] == ["security clearance"]
    sim = app.rules.simulate(CANADA, sug[0].rule)
    kept_row = next(j for j in sim["jobs"] if j["job_id"] == keep.id)
    assert kept_row["draft_effect"] == "pass"
    # a phrase present in a kept JD is never suggested
    app.jobs.upsert_job(_job(5, description="Requires security clearance"))
    assert not [s for s in app.suggestions.suggestions(CANADA) if s.reason_code == "requires_authorization"]


def test_kept_jobs_prevent_bad_suggestions(app):
    for i in range(3):
        j, _ = app.jobs.upsert_job(_job(i, title=f"Lead Analyst {i}"))
        app.jobs.set_decision(j.id, MEXICO, "discarded", "seniority")
    keep, _ = app.jobs.upsert_job(_job(7, title="Lead Data Engineer"))
    app.jobs.set_decision(keep.id, MEXICO, "saved")
    assert app.suggestions.suggestions(MEXICO) == []  # 'lead' appears in a saved job → no suggestion


def test_salary_suggestion_requires_single_currency(app):
    for i in range(3):
        j, _ = app.jobs.upsert_job(_job(i, salary_max=1000 * (i + 1), currency="USD"))
        app.jobs.set_decision(j.id, MEXICO, "discarded", "salary")
    sug = [s for s in app.suggestions.suggestions(MEXICO) if s.reason_code == "salary"]
    assert sug and sug[0].rule["value"] == 3000
    app.jobs.upsert_job(_job(8, salary_max=90000, currency="MXN"))
    assert not [s for s in app.suggestions.suggestions(MEXICO) if s.reason_code == "salary"]


def test_salary_and_remote_suggestions_respect_kept_jobs(app):
    for r in app.rules.list(CANADA):  # the Canada seed already covers remote-from-CA; disable to test
        if r.field == "remote_eligible_countries":
            app.rules.set_enabled(r.id, False)
    for i in range(3):
        j, _ = app.jobs.upsert_job(_job(i, salary_max=3000, currency="USD", remote_eligible_countries="US"))
        app.jobs.set_decision(j.id, MEXICO, "discarded", "salary")
        j, _ = app.jobs.upsert_job(_job(10 + i, remote_eligible_countries="US"))
        app.jobs.set_decision(j.id, CANADA, "discarded", "not_remote_from_country")
    assert {s.reason_code for s in app.suggestions.suggestions(MEXICO)} == {"salary"}
    assert {s.reason_code for s in app.suggestions.suggestions(CANADA)} == {"not_remote_from_country"}
    keep, _ = app.jobs.upsert_job(_job(20, salary_max=2500, currency="USD", remote_eligible_countries="US"))
    app.jobs.set_decision(keep.id, MEXICO, "saved")
    app.jobs.set_decision(keep.id, CANADA, "saved")
    assert app.suggestions.suggestions(MEXICO) == []
    assert app.suggestions.suggestions(CANADA) == []


# ---------------------------------------------------------------- backup restore
def test_backup_restore_roundtrip(app):
    a, _ = app.jobs.upsert_job(_job(1))
    backup = app.db.backup("manual")
    app.jobs.upsert_job(_job(2))
    app.jobs.set_decision(a.id, MEXICO, "discarded", "salary")
    assert len(app.jobs.rows(MEXICO)) == 2
    info = app.db.inspect_backup(backup)
    assert info["jobs"] == 1
    safety = app.db.restore(backup)
    rows = app.jobs.rows(MEXICO)
    assert len(rows) == 1 and rows[0]["user_decision"] == "new"
    assert safety and safety.exists()
    app.db.restore(safety)  # undo the restore
    assert len(app.jobs.rows(MEXICO)) == 2


def test_restore_rejects_bad_files(app, tmp_path):
    with pytest.raises(ValueError):
        app.db.inspect_backup(tmp_path / "elsewhere.db")  # outside backups/
    junk = app.paths.backups / "junk.db"
    junk.write_bytes(b"not a database at all" * 100)
    with pytest.raises(ValueError):
        app.db.inspect_backup(junk)
    other = app.paths.backups / "other.db"
    con = sqlite3.connect(other)
    con.execute("create table x(a)")
    con.commit()
    con.close()
    with pytest.raises(ValueError):
        app.db.inspect_backup(other)
    newer = app.paths.backups / "newer.db"
    con = sqlite3.connect(newer)
    con.execute("create table jobs(a)")
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    with pytest.raises(ValueError):
        app.db.inspect_backup(newer)


# ---------------------------------------------------------------- workbook sheets
SPEC_COMMON = ["Dashboard", "Activos", "Vacantes", "Empresas", "Contactos", "Skills", "Filtros", "Ubicaciones",
               "Salarios", "Fuentes", "Prompts", "Analisis_IA", "CV_Versiones", "Estados", "Historial",
               "Configuracion"]


def test_all_spec_sheets_present(app):
    j, _ = app.jobs.upsert_job(_job(1, salary_max=5000, currency="USD", job_country="CA"))
    app.jobs.update_mode_data(j.id, CANADA, {"employer_visa_support": "yes"})
    ca = load_workbook(app.workbooks.export_mode(CANADA))
    mx = load_workbook(app.workbooks.export_mode(MEXICO))
    for name in SPEC_COMMON:
        assert name in ca.sheetnames and name in mx.sheetnames, name
    for name in ("Visa_WorkPermit", "CUSMA_LMIA_Evidencias", "Empleadores_Permisos"):
        assert name in ca.sheetnames and name not in mx.sheetnames
    for name in ("Trabajo_Remoto", "Contratacion_Internacional", "Ingresos_Prioridad"):
        assert name in mx.sheetnames and name not in ca.sheetnames
    assert ca["Empresas"].cell(row=2, column=2).value == "Co1"
    perm = ca["Empleadores_Permisos"]
    assert [c.value for c in perm[2]][3] == 1  # apoyo_yes
    assert mx["Salarios"].cell(row=2, column=5).value == 5000
    assert ca["Prompts"].cell(row=2, column=2).value in ("review_default", "tailoring_default")  # Phase 4 data
    assert [c.value for c in ca["Analisis_IA"][1]][0] == "review_id"
    assert "Sin perfil de candidato" in ca["Skills"].cell(row=2, column=1).value  # Phase 2: guidance, no data
    # Mexico sheets never carry Canadian visa columns
    for name in mx.sheetnames:
        headers = [c.value for c in mx[name][1]]
        assert "employer_visa_support" not in headers and "cusma_evidence" not in headers


def test_mode_fields_import_from_split_sheets(app):
    j, _ = app.jobs.upsert_job(_job(1))
    path = app.workbooks.export_mode(MEXICO)
    wb = load_workbook(path)
    ws = wb["Contratacion_Internacional"]
    ws.cell(row=2, column=[c.value for c in ws[1]].index("contract_arrangement") + 1).value = "eor"
    ws = wb["Ingresos_Prioridad"]
    ws.cell(row=2, column=[c.value for c in ws[1]].index("fx_note") + 1).value = "Banxico 2026-10-29"
    wb.save(path)
    pv = app.workbooks.preview_import(path, MEXICO)
    assert {c.field for c in pv.changes} == {"contract_arrangement", "fx_note"}
    app.workbooks.apply_import(pv)
    row = app.jobs.rows(MEXICO)[0]
    assert row["contract_arrangement"] == "eor" and row["fx_note"] == "Banxico 2026-10-29"
