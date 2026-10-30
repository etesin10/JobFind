from openpyxl import load_workbook

from jsh.excel.csv_import import read_csv_bytes, suggest_mapping, EXTRA
from jsh.modes import CANADA, MEXICO
from jsh.normalize import escape_formula, unescape_formula
from jsh.services import JobInput


def _seed(app):
    a, _ = app.jobs.upsert_job(JobInput(title="Data Engineer", company_name="Acme", job_country="CA",
                                        canonical_url="https://a.com/1", description="=HYPERLINK(\"evil\")"), CANADA)
    b, _ = app.jobs.upsert_job(JobInput(title="Analytics Engineer", company_name="Beta", job_country="CA",
                                        canonical_url="https://b.com/2"), CANADA)
    return a, b


def test_formula_escape_roundtrip():
    assert escape_formula("=1+1") == "'=1+1"
    assert escape_formula("@SUM(A1)") == "'@SUM(A1)"
    assert escape_formula("normal") == "normal"
    assert unescape_formula("'=1+1") == "=1+1"


def test_export_has_mode_specific_sheets(app):
    _seed(app)
    ca = load_workbook(app.workbooks.export_mode(CANADA))
    mx = load_workbook(app.workbooks.export_mode(MEXICO))
    assert "Visa_WorkPermit" in ca.sheetnames and "Trabajo_Remoto" not in ca.sheetnames
    assert "Trabajo_Remoto" in mx.sheetnames and "Visa_WorkPermit" not in mx.sheetnames
    headers = [c.value for c in ca["Vacantes"][1]]
    for col in ("job_id", "company", "job_title", "row_version", "user_decision", "eligibility_status"):
        assert col in headers
    assert "employer_visa_support" not in [c.value for c in mx["Trabajo_Remoto"][1]]
    assert ca["Vacantes"].freeze_panes == "B2"


def test_untrusted_text_not_written_as_formula(app):
    a, _ = _seed(app)
    app.jobs.update_notes(a.id, "=cmd|' /C calc'!A0")
    ws = load_workbook(app.workbooks.export_mode(CANADA))["Vacantes"]
    notes_col = [c.value for c in ws[1]].index("notes") + 1
    values = [ws.cell(row=r, column=notes_col) for r in range(2, ws.max_row + 1)]
    assert all(c.data_type != "f" for c in values)
    assert any(str(c.value).startswith("'=") for c in values)


def test_excel_roundtrip_dry_run_conflicts_and_apply(app):
    a, b = _seed(app)
    path = app.workbooks.export_mode(CANADA)
    wb = load_workbook(path)
    ws = wb["Vacantes"]
    hdr = [c.value for c in ws[1]]
    col = {h: i + 1 for i, h in enumerate(hdr)}
    for r in range(2, ws.max_row + 1):
        jid = ws.cell(row=r, column=col["job_id"]).value
        if jid == a.id:
            ws.cell(row=r, column=col["user_decision"]).value = "discarded"
            ws.cell(row=r, column=col["discard_reason"]).value = "salary"
            ws.cell(row=r, column=col["notes"]).value = "editado en Excel"
        if jid == b.id:
            ws.cell(row=r, column=col["application_status"]).value = "applied"
    vs = wb["CUSMA_LMIA_Evidencias"]
    vh = [c.value for c in vs[1]]
    vs.cell(row=2, column=vh.index("cusma_evidence") + 1).value = "Profesional CUSMA: verificar"
    fs = wb["Filtros"]
    fh = [c.value for c in fs[1]]
    fs.cell(row=2, column=fh.index("enabled") + 1).value = False
    wb.save(path)

    # change b in the app after export → its Excel edit becomes a conflict
    app.jobs.update_notes(b.id, "cambio en la app")
    before = {r["job_id"]: r for r in app.jobs.rows(CANADA, "all")}

    pv = app.workbooks.preview_import(path, CANADA)
    assert not pv.errors
    fields = {(c.entity_id, c.field): c for c in pv.changes}
    assert (a.id, "user_decision") in fields and not fields[(a.id, "user_decision")].conflict
    assert fields[(b.id, "application_status")].conflict
    assert any(c.kind == "mode_field" for c in pv.changes)
    assert any(c.kind == "rule_field" and c.field == "enabled" for c in pv.changes)
    # dry-run did not modify the DB
    assert {r["job_id"]: r["user_decision"] for r in app.jobs.rows(CANADA, "all")} == \
        {k: v["user_decision"] for k, v in before.items()}

    summary = app.workbooks.apply_import(pv)
    # every edit on the row changed in-app after export (b) is a conflict and skipped by default
    assert {c.entity_id for c in pv.conflicts} == {b.id}
    assert summary["skipped_conflicts"] == len(pv.conflicts) >= 1
    assert summary["backup"]
    rows = {r["job_id"]: r for r in app.jobs.rows(CANADA, "all")}
    assert rows[a.id]["user_decision"] == "discarded" and rows[a.id]["discard_reason"] == "salary"
    assert rows[a.id]["notes"] == "editado en Excel"
    assert rows[b.id]["application_status"] == ""  # conflict skipped
    assert rows[b.id]["notes"] == "cambio en la app"
    assert any(not r.enabled for r in app.rules.list(CANADA))


def test_import_wrong_mode_rejected(app):
    _seed(app)
    path = app.workbooks.export_mode(MEXICO)
    pv = app.workbooks.preview_import(path, CANADA)
    assert pv.errors


def test_csv_mapping_preview_and_import(app):
    data = ("Puesto;Empresa;Link;País;Modalidad;Columna Rara\n"
            "Data Engineer;Acme;https://a.com/x?utm_source=li;Canadá;Remoto;foo\n"
            "Data Engineer;Acme;https://a.com/x;Canadá;Remoto;foo\n"
            ";SinTitulo;https://c.com;MX;remote;bar\n").encode("cp1252")
    headers, rows, enc = read_csv_bytes(data)
    assert enc == "cp1252" and headers[0] == "Puesto"
    mapping = suggest_mapping(headers)
    assert mapping["Puesto"] == "title" and mapping["Link"] == "canonical_url"
    assert mapping["Columna Rara"] == EXTRA
    pv = app.csv.preview(rows, mapping)
    assert [r.status for r in pv.rows] == ["new", "duplicate_in_file", "error"]
    summary = app.csv.apply(rows, mapping, CANADA, "legacy.csv")
    assert summary["created"] == 1 and summary["unchanged"] == 1 and summary["errors"] == 1
    rows_db = app.jobs.rows(CANADA, "all")
    assert len(rows_db) == 1 and rows_db[0]["job_country"] == "CA" and rows_db[0]["work_mode"] == "remote"
    assert app.db.get_setting("csv_mapping_last")["Puesto"] == "title"
    # re-import: still one job
    app.csv.apply(rows, mapping, CANADA, "legacy.csv")
    assert len(app.jobs.rows(CANADA, "all")) == 1


def _edit(path, sheet, job_or_rule_id, values):
    wb = load_workbook(path)
    ws = wb[sheet]
    hdr = [c.value for c in ws[1]]
    for r in range(2, ws.max_row + 1):
        if ws.cell(row=r, column=1).value == job_or_rule_id:
            for k, v in values.items():
                ws.cell(row=r, column=hdr.index(k) + 1).value = v
    wb.save(path)


def test_rule_edits_validated_as_whole_and_failures_isolated(app):
    a, _ = _seed(app)
    rule = next(r for r in app.rules.list(CANADA) if r.action == "include")
    path = app.workbooks.export_mode(CANADA)
    _edit(path, "Filtros", rule.id, {"action": "weight", "weight": 5})  # valid only together
    _edit(path, "Vacantes", a.id, {"notes": "nota ok"})
    pv = app.workbooks.preview_import(path, CANADA)
    summary = app.workbooks.apply_import(pv)
    assert not summary["failures"]
    updated = app.rules.get(rule.id)
    assert updated.action == "weight" and updated.weight == 5
    assert next(r for r in app.jobs.rows(CANADA) if r["job_id"] == a.id)["notes"] == "nota ok"

    path = app.workbooks.export_mode(CANADA)
    _edit(path, "Filtros", rule.id, {"weight": 0})  # invalid for a weight rule
    _edit(path, "Vacantes", a.id, {"notes": "segunda nota"})
    pv = app.workbooks.preview_import(path, CANADA)
    assert any("cambios inválidos" in w for w in pv.warnings)
    app.workbooks.apply_import(pv)
    assert app.rules.get(rule.id).weight == 5
    assert next(r for r in app.jobs.rows(CANADA) if r["job_id"] == a.id)["notes"] == "segunda nota"


def test_invalid_mode_choice_rejected(app):
    import pytest
    a, _ = _seed(app)
    path = app.workbooks.export_mode(CANADA)
    _edit(path, "Visa_WorkPermit", a.id, {"employer_visa_support": "Sí claro"})
    pv = app.workbooks.preview_import(path, CANADA)
    assert not [c for c in pv.changes if c.field == "employer_visa_support"]
    assert any("employer_visa_support" in w for w in pv.warnings)
    with pytest.raises(ValueError):
        app.jobs.update_mode_data(a.id, CANADA, {"employer_visa_support": "Sí claro"})
