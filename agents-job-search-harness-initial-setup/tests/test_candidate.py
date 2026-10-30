"""Phase 2 tests: résumé parsing, proposed-skill extraction, approval, JD matching, migration, Excel.

All résumés here are SYNTHETIC (fictional people); real user résumés are never stored in the repo.
"""
import io
import json
import sqlite3
import zipfile

import pytest
from openpyxl import load_workbook
from sqlalchemy import text

from jsh.candidate.extraction import extract, normalize_date
from jsh.candidate.parsing import MAX_XML_BYTES, ResumeParseError, parse_resume
from jsh.candidate.vocabulary import find_vocab
from jsh.modes import CANADA, MEXICO
from jsh.paths import get_paths
from jsh.services import AppServices, JobInput

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


# ---------------------------------------------------------------- fixtures / builders
def make_docx(paragraphs, table_rows=None, doctype=False) -> bytes:
    """paragraphs: list of str | (str, style) | (str, 'bullet')."""
    def para(text, style=""):
        ppr = ""
        if style == "bullet":
            ppr = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        elif style:
            ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>'
        esc = text.replace("&", "&amp;").replace("<", "&lt;")
        return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{esc}</w:t></w:r></w:p>'
    body = "".join(para(*p) if isinstance(p, tuple) else para(p) for p in paragraphs)
    if table_rows:
        body += "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc>{para(c)}</w:tc>" for c in row) + "</w:tr>"
                                    for row in table_rows) + "</w:tbl>"
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    if doctype:
        head += '<!DOCTYPE lolz [<!ENTITY lol "lol">]>'
    xml = f'{head}<w:document xmlns:w="{W_NS}"><w:body>{body}</w:body></w:document>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
                   'package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/'
                   'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        z.writestr("word/document.xml", xml)
    return buf.getvalue()


def make_pdf(lines) -> bytes:
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = "BT /F1 10 Tf 40 760 Td 13 TL " + " ".join(f"({esc(ln)}) Tj T*" for ln in lines) + " ET"
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            "/Resources << /Font << /F1 5 0 R >> >> >>",
            f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
            "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


RESUME_EN = [
    "ANA LOPEZ",
    "Data Analyst | Python & SQL",
    "SUMMARY",
    "Data analyst with experience building dashboards and ETL pipelines for retail clients.",
    "SKILLS",
    "Programming: Python, SQL, R",
    "Tools: Power BI, Advanced Excel, Databricks (training only), Looker Studio Pro Max",
    "EXPERIENCE",
    "Data Analyst | Retail Corp | Mar 2021 – Present",
    ("Built Power BI dashboards used by 40 store managers, reducing report time by 30%.", "bullet"),
    ("Automated ETL jobs with Python and Apache Airflow.", "bullet"),
    "Junior Analyst",
    "Small Shop Inc | 2018 - 2021",
    ("Maintained SQL Server stored procedures.", "bullet"),
    "EDUCATION",
    "BSc Statistics | University of Toronto | 2017",
    "CERTIFICATIONS",
    "PL-300 Power BI Data Analyst, 2022",
    "LANGUAGES",
    "English (native) | French (intermediate)",
]

RESUME_ES = [
    "LUIS PÉREZ",
    "PERFIL PROFESIONAL",
    "Contador con experiencia en nómina y cuentas por pagar.",
    "EXPERIENCIA PROFESIONAL",
    "Contador General — Comercial del Norte — Ene 2019 – Actual",
    "• Responsable de contabilidad general, nómina y conciliaciones bancarias con SAP.",
    "HABILIDADES",
    "Contabilidad, Excel avanzado, SAP, Cuentas por pagar",
    "IDIOMAS",
    "Español (nativo), Inglés (intermedio)",
]


@pytest.fixture
def cand(app):
    return app.candidate.create_candidate("Analista")


def _job(app, url, description, **kw):
    job, _ = app.jobs.upsert_job(JobInput(title=kw.pop("title", "Data role"), company_name="Co", canonical_url=url,
                                          description=description, **kw), CANADA)
    return job


# ---------------------------------------------------------------- vocabulary
@pytest.mark.parametrize("text,expected", [
    ("You will excel in a fast-paced team", set()),
    ("Advanced Excel and Power BI", {"Microsoft Excel", "Power BI"}),
    ("Python, R, SQL and Spark", {"Python", "R", "SQL", "Apache Spark"}),
    ("R&D department, Microsoft(R) partner", set()),
    ("JavaScript developer", {"JavaScript"}),
    ("React quickly to incidents", set()),
    ("Germán Pérez, Gerente", set()),
    ("Inglés avanzado y francés deseable", {"English", "French"}),
    ("Improve the sales-portal adoption", set()),
    ("Contabilidad y nómina", {"Accounting", "Payroll"}),
])
def test_vocabulary_disambiguation(text, expected):
    assert {k for k, *_ in find_vocab(text)} == expected


def test_normalize_date():
    assert normalize_date("Jul 2025") == "2025-07"
    assert normalize_date("Ene 2019") == "2019-01"
    assert normalize_date("Present") == "present" and normalize_date("Actual") == "present"
    assert normalize_date("03/2020") == "2020-03"
    assert normalize_date("2018") == "2018"


# ---------------------------------------------------------------- parsing
def test_parse_docx_paragraphs_bullets_headings_tables():
    data = make_docx([("Experience", "Heading1"), ("Built pipelines", "bullet"), "Plain line"],
                     table_rows=[["Skills: Python", "SQL"]])
    parsed = parse_resume(data, "cv.docx")
    texts = [ln.text for ln in parsed.lines]
    assert texts[:3] == ["Experience", "Built pipelines", "Plain line"] and "Skills: Python" in texts
    assert parsed.lines[0].heading and parsed.lines[1].bullet


def test_parse_rejects_doctype_doc_unsupported_and_damaged():
    with pytest.raises(ResumeParseError, match="XML"):
        parse_resume(make_docx(["x"], doctype=True), "cv.docx")
    with pytest.raises(ResumeParseError, match=r"\.doc"):
        parse_resume(b"\xd0\xcf\x11\xe0", "old.doc")
    with pytest.raises(ResumeParseError, match="no compatible"):
        parse_resume(b"x", "cv.odt")
    with pytest.raises(ResumeParseError, match="dañado"):
        parse_resume(b"not a zip", "cv.docx")
    with pytest.raises(ResumeParseError, match="grande"):
        parse_resume(b"0" * (10 * 1024 * 1024 + 1), "cv.txt")


def test_parse_docx_zip_bomb_guard():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", b" " * (MAX_XML_BYTES + 1))
    assert len(buf.getvalue()) < 1_000_000
    with pytest.raises(ResumeParseError, match="grande"):
        parse_resume(buf.getvalue(), "bomb.docx")


def test_parse_text_pdf_and_reject_scanned_pdf():
    parsed = parse_resume(make_pdf(["EXPERIENCE", "Data Analyst | Retail Corp | 2021 - Present",
                                    "Built dashboards with Power BI and Python."]), "cv.pdf")
    assert parsed.kind == "pdf"
    assert any("Power BI" in ln.text for ln in parsed.lines)
    assert parsed.warnings
    with pytest.raises(ResumeParseError, match="escaneado"):
        parse_resume(make_pdf([]), "scan.pdf")
    with pytest.raises(ResumeParseError):
        parse_resume(b"%PDF-1.4 garbage", "broken.pdf")


# ---------------------------------------------------------------- extraction
def test_extract_english_resume():
    ex = extract(parse_resume(make_docx(RESUME_EN), "ana.docx").lines)
    assert {"summary", "skills", "experience", "education", "certifications", "languages"} <= set(ex.sections_found)
    skills = {s.name: s for s in ex.skills}
    assert skills["Python"].confidence == "high"  # used in experience
    assert skills["R"].confidence == "medium"  # only in the skills list
    assert skills["Databricks"].notes == "training only"  # qualifier kept, not a stronger claim
    assert "Looker Studio Pro Max" not in skills  # vocabulary match ('Looker') covers it…
    assert skills["Looker"].confidence == "medium"
    assert all(not getattr(s, "proficiency", "") for s in ex.skills)  # never inferred
    positions = [i.data for i in ex.items if i.kind == "position"]
    assert positions[0]["title"] == "Data Analyst" and positions[0]["company"] == "Retail Corp"
    assert positions[0]["start"] == "2021-03" and positions[0]["end"] == "present"
    assert positions[0]["highlights"][0]["quantified"] is True
    assert positions[1]["title"] == "Junior Analyst" and positions[1]["company"] == "Small Shop Inc"  # 2-line layout
    edu = next(i.data for i in ex.items if i.kind == "education")
    assert edu == {"degree": "BSc Statistics", "institution": "University of Toronto", "year": "2017"}
    langs = [i.data for i in ex.items if i.kind == "language"]
    assert [(lg["language"], lg["level"]) for lg in langs] == [("English", "native"), ("French", "intermediate")]
    cert = next(i.data for i in ex.items if i.kind == "certification")
    assert cert["year"] == "2022" and "PL-300" in cert["name"]


def test_extract_unknown_skill_list_term_is_low_confidence():
    ex = extract(parse_resume(make_docx(["SKILLS", "Tools: Cognite Data Fusion, Python"]), "x.docx").lines)
    unknown = next(s for s in ex.skills if s.name == "Cognite Data Fusion")
    assert unknown.confidence == "low" and unknown.vocab_key == "" and unknown.notes == "Tools"


def test_extract_spanish_resume():
    ex = extract(parse_resume("\n".join(RESUME_ES).encode("utf-8"), "luis.txt").lines)
    skills = {s.name for s in ex.skills}
    assert {"Accounting", "Payroll", "SAP", "Accounts payable", "Microsoft Excel", "Spanish", "English"} <= skills
    pos = next(i.data for i in ex.items if i.kind == "position")
    assert pos["title"] == "Contador General" and pos["company"] == "Comercial del Norte"
    assert pos["start"] == "2019-01" and pos["end"] == "present"
    assert any(i.kind == "summary" for i in ex.items)


# ---------------------------------------------------------------- service: import, approval, matching
def test_import_creates_only_proposals_and_keeps_original(app, cand):
    data = make_docx(RESUME_EN)
    res = app.candidate.import_file(cand.id, "../../evil/ana.docx", data)
    assert res.new_skills > 10 and res.new_items >= 6
    assert all(s.status == "proposed" for s in app.candidate.skills(cand.id))
    assert all(i.status == "proposed" for i in app.candidate.items(cand.id))
    src = app.candidate.sources(cand.id)[0]
    assert src.stored_path.startswith("resumes/originals/") and ".." not in src.stored_path
    assert (app.paths.home / src.stored_path).read_bytes() == data
    assert app.candidate.verify_original(src.id)
    assert app.candidate.import_file(cand.id, "ana.docx", data).duplicate
    (app.paths.home / src.stored_path).write_bytes(data + b"tampered")
    assert not app.candidate.verify_original(src.id)
    audit = json.dumps(app.jobs.history(limit=50), default=str)
    assert "Retail Corp" not in audit and "stored procedures" not in audit  # no résumé content in the audit log


def test_matching_uses_only_approved_skills(app, cand):
    app.candidate.import_file(cand.id, "ana.docx", make_docx(RESUME_EN))
    job = _job(app, "https://x.example/1", "We need Python, SQL, Power BI, Snowflake and dbt. Kafka is a plus.")
    row = app.jobs.rows(CANADA)[0]
    assert row["skills_match"] is None  # nothing approved yet → unknown, not 0
    skills = {s.name: s for s in app.candidate.skills(cand.id)}
    app.candidate.set_skill_status([skills["Python"].id, skills["SQL"].id], "approved")
    app.candidate.set_skill_status([skills["Power BI"].id], "rejected")
    ev = app.jobs.detail(job.id, CANADA)["evaluation"]
    assert ev.skills_match == pytest.approx(100 * 2 / 6, abs=0.1)
    missing = {m["name"]: m["status"] for m in ev.skills_detail["missing"]}
    assert missing == {"Power BI": "rejected", "Snowflake": "absent", "dbt": "absent", "Apache Kafka": "absent"}
    assert {m["name"] for m in ev.skills_detail["matched"]} == {"Python", "SQL"}
    assert ev.skills_detail["matched"][0]["cv"]  # CV evidence travels with the match
    # rank rule from the seeded profile applies (30–59% → +20)
    assert app.jobs.rows(CANADA)[0]["technical_score"] == 20


def test_pending_proposals_flagged_and_jd_without_skills_is_unknown(app, cand):
    app.candidate.import_file(cand.id, "ana.docx", make_docx(RESUME_EN))
    skills = {s.name: s for s in app.candidate.skills(cand.id)}
    app.candidate.set_skill_status([skills["Python"].id], "approved")
    job = _job(app, "https://x.example/2", "Python and Apache Airflow required.")
    ev = app.jobs.detail(job.id, CANADA)["evaluation"]
    assert [(m["name"], m["status"]) for m in ev.skills_detail["missing"]] == [("Apache Airflow", "proposed")]
    job2 = _job(app, "https://x.example/3", "Great team culture and benefits.")
    assert app.jobs.detail(job2.id, CANADA)["evaluation"].skills_match is None


def test_manual_skill_aliases_and_validation(app, cand):
    sk = app.candidate.add_manual_skill(cand.id, "Cognite Data Fusion", aliases="CDF, Cognite")
    assert sk.status == "approved" and sk.origin == "manual"
    with pytest.raises(ValueError):
        app.candidate.add_manual_skill(cand.id, "cognite data fusion")
    job = _job(app, "https://x.example/4", "Experience with Cognite and Python.")
    ev = app.jobs.detail(job.id, CANADA)["evaluation"]
    assert [m["name"] for m in ev.skills_detail["matched"]] == ["Cognite Data Fusion"]
    for bad in ({"weight": 9}, {"years": -1}, {"tag": "best"}, {"name": ""}):
        with pytest.raises(ValueError):
            app.candidate.update_skill(sk.id, bad)
    app.candidate.update_skill(sk.id, {"weight": 2, "years": "3", "proficiency": "avanzado", "tag": "core"})
    updated = app.candidate.get_skill(sk.id)
    assert (updated.weight, updated.years, updated.proficiency, updated.tag) == (2, 3, "avanzado", "core")


def test_multiple_resumes_merge_evidence_and_profiles_per_mode(app, cand):
    app.candidate.import_file(cand.id, "ana.docx", make_docx(RESUME_EN))
    n_before = len(app.candidate.skills(cand.id))
    res = app.candidate.import_file(cand.id, "ana.pdf", make_pdf(["SKILLS", "Programming languages: Python, SQL",
                                                                  "BI tools used daily: Tableau"]))
    assert res.new_skills == 1 and len(app.candidate.skills(cand.id)) == n_before + 1  # only Tableau is new
    python = next(s for s in app.candidate.skills(cand.id) if s.name == "Python")
    assert len({e.source_id for e in app.candidate.evidence(python.id)}) == 2

    other = app.candidate.create_candidate("Contador")
    app.candidate.import_text(other.id, "\n".join(RESUME_ES))
    app.candidate.set_skill_status([s.id for s in app.candidate.skills(other.id)], "approved")
    app.candidate.set_skill_status([s.id for s in app.candidate.skills(cand.id)], "approved")
    app.candidate.set_active_candidate(MEXICO, other.id)
    job, _ = app.jobs.upsert_job(JobInput(title="Accountant", company_name="Co", canonical_url="https://x.example/5",
                                          description="Accounting, SAP, Excel avanzado and accounts payable experience."), MEXICO)
    assert app.jobs.detail(job.id, MEXICO)["evaluation"].skills_match == 100
    assert app.jobs.detail(job.id, CANADA)["evaluation"].skills_match < 50  # other profile in CANADA
    with pytest.raises(ValueError):
        app.candidate.delete_candidate(other.id)  # active in MEXICO
    app.candidate.set_active_candidate(MEXICO, cand.id)
    app.candidate.delete_candidate(other.id)
    assert [c.name for c in app.candidate.list_candidates()] == ["Analista"]


def test_master_version_snapshot_only_approved(app, cand):
    with pytest.raises(ValueError):
        app.candidate.save_master_version(cand.id)
    app.candidate.import_file(cand.id, "ana.docx", make_docx(RESUME_EN))
    skills = {s.name: s for s in app.candidate.skills(cand.id)}
    app.candidate.set_skill_status([skills["Python"].id], "approved")
    app.candidate.set_skill_status([skills["R"].id], "rejected")
    pos = next(i for i in app.candidate.items(cand.id) if i.kind == "position")
    app.candidate.update_item(pos.id, {"title": "Data Analyst II", "bogus": "x"})
    app.candidate.set_item_status([pos.id], "approved")
    v1 = app.candidate.save_master_version(cand.id)
    assert [s["name"] for s in v1.content["skills"]] == ["Python"]
    assert v1.content["items"][0]["data"]["title"] == "Data Analyst II" and "bogus" not in v1.content["items"][0]["data"]
    assert v1.approved and v1.provenance["source_ids"]
    assert app.candidate.save_master_version(cand.id).version == 2


# ---------------------------------------------------------------- rules / migration / excel
def test_skills_rules_seeded_and_simulation(app, cand):
    names = [r.name for r in app.rules.list(CANADA)]
    assert any("Skills del CV" in n for n in names) and "Skills principales en la JD" not in names
    app.candidate.add_manual_skill(cand.id, "Python")
    _job(app, "https://x.example/6", "Python and Java required.")
    sim = app.rules.simulate(CANADA, {"name": "Exigir 50%", "field": "skills_match_pct", "operator": "gte",
                                      "value": "75", "action": "include", "severity": "hard",
                                      "unknown_handling": "keep", "scope": "prefilter"})
    assert sim["jobs"][0]["after"] == "blocked"


def test_migration_from_v1(tmp_path):
    home = tmp_path / "old"
    app = AppServices(get_paths(home))
    app.jobs.upsert_job(JobInput(title="T", company_name="C", canonical_url="https://x.example/m",
                                 description="Python and SQL"))
    app.close()
    con = sqlite3.connect(get_paths(home).db_file)
    con.execute("DROP VIEW v_job_status")  # v4 objects did not exist in a v1 database
    con.execute("DROP TABLE job_inbox")
    con.execute("ALTER TABLE job_mode_evaluations DROP COLUMN facts_hash")
    for t in ("skill_evidence", "skills", "profile_items", "resume_versions", "resume_sources", "candidate_profiles"):
        con.execute(f"DROP TABLE {t}")
    con.execute("ALTER TABLE job_mode_evaluations DROP COLUMN skills_match")
    con.execute("ALTER TABLE job_mode_evaluations DROP COLUMN skills_detail")
    con.execute("DELETE FROM rules WHERE field = 'skills_match_pct'")
    prof = con.execute("SELECT id FROM search_profiles WHERE mode_id='CANADA_VISA'").fetchone()[0]
    con.execute("INSERT INTO rules (id, mode_id, profile_id, name, enabled, scope, field, operator, value, value_type,"
                " logic_group, group_operator, action, weight, severity, unknown_handling, explanation, priority,"
                " version, created_at, updated_at) VALUES ('legacy1', 'CANADA_VISA', ?, 'Skills principales en la JD',"
                " 1, 'rank', 'description', 'any_keyword', '[\"python\"]', 'list', '', 'AND', 'weight', 40, 'soft',"
                " 'keep', '', 50, 1, '2026-01-01', '2026-01-01')", (prof,))
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()

    app = AppServices(get_paths(home))  # runs migrations 2+
    from jsh.db import SCHEMA_VERSION
    with app.db.engine.connect() as c:
        assert c.execute(text("PRAGMA user_version")).scalar() == SCHEMA_VERSION
    rules = {r.name: r for r in app.rules.list(CANADA)}
    assert not rules["Skills principales en la JD"].enabled  # unmodified legacy seed disabled
    assert any(r.field == "skills_match_pct" for r in rules.values())
    assert list(app.paths.backups.glob("*pre_migration_v1.db"))
    c = app.candidate.create_candidate("Yo")
    app.candidate.add_manual_skill(c.id, "Python")
    app.candidate.add_manual_skill(c.id, "SQL")
    assert app.jobs.rows(CANADA)[0]["skills_match"] == 100
    app.close()


def test_excel_skills_sheet_roundtrip(app, cand):
    app.candidate.import_file(cand.id, "ana.docx", make_docx(RESUME_EN))
    path = app.workbooks.export_mode(CANADA)
    wb = load_workbook(path)
    ws = wb["Skills"]
    hdr = [c.value for c in ws[1]]
    rows = {ws.cell(row=r, column=3).value: r for r in range(2, ws.max_row + 1)}
    ws.cell(row=rows["Python"], column=hdr.index("status") + 1).value = "approved"
    ws.cell(row=rows["Python"], column=hdr.index("weight") + 1).value = 2
    ws.cell(row=rows["SQL"], column=hdr.index("weight") + 1).value = 99  # invalid → warning, ignored
    wb.save(path)
    pv = app.workbooks.preview_import(path, CANADA)
    assert {(c.label, c.field) for c in pv.changes if c.kind == "skill_field"} == {("Python", "status"),
                                                                                  ("Python", "weight")}
    assert any("SQL" in w for w in pv.warnings)
    app.workbooks.apply_import(pv)
    py = next(s for s in app.candidate.skills(cand.id) if s.name == "Python")
    assert py.status == "approved" and py.weight == 2
    cv = load_workbook(app.workbooks.export_mode(CANADA))["CV_Versiones"]
    assert [c.value for c in cv[1]][0] == "resume_version_id"


# ---------------------------------------------------------------- hostile / malformed input (review findings)
def test_pdf_decompression_bomb_is_a_clean_error():
    import zlib
    bomb = zlib.compress(b" " * 120_000_000, 9)
    pdf = make_pdf(["placeholder"])
    head = (b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(bomb)) + bomb + b"\nendstream"
    start = pdf.index(b"4 0 obj\n") + len(b"4 0 obj\n")
    end = pdf.index(b"endstream", start) + len(b"endstream")
    broken = pdf[:start] + head + pdf[end:]  # xref offsets now wrong too: pypdf must still fail cleanly
    with pytest.raises(ResumeParseError):
        parse_resume(broken, "bomb.pdf")


def test_aes_encrypted_pdf_without_crypto_library_is_a_clean_error(monkeypatch):
    import pypdf
    from pypdf.errors import DependencyError

    class FakeReader:
        is_encrypted = True

        def __init__(self, *_):
            pass

        def decrypt(self, _pw):
            raise DependencyError("cryptography is required for AES algorithm")
    monkeypatch.setattr(pypdf, "PdfReader", FakeReader)
    with pytest.raises(ResumeParseError, match="cifrado"):
        parse_resume(b"%PDF-1.7 fake", "locked.pdf")


def test_corrupt_docx_payload_is_a_clean_error():
    data = bytearray(make_docx(["SKILLS"] + [f"Python, SQL line {i}" for i in range(200)]))
    info = zipfile.ZipFile(io.BytesIO(bytes(data))).getinfo("word/document.xml")
    payload = info.header_offset + 30 + len(info.filename) + len(info.extra)
    for i in range(payload + 20, payload + 60):
        data[i] ^= 0xFF
    with pytest.raises(ResumeParseError):
        parse_resume(bytes(data), "corrupt.docx")


def test_deeply_nested_docx_is_a_clean_error():
    inner = f'<w:p><w:r><w:t>x</w:t></w:r></w:p>'
    xml = (f'<?xml version="1.0"?><w:document xmlns:w="{W_NS}"><w:body>' + "<w:sdt>" * 3000 + inner
           + "</w:sdt>" * 3000 + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml)
    with pytest.raises(ResumeParseError, match="profunda"):
        parse_resume(buf.getvalue(), "deep.docx")


def test_long_skill_line_is_processed_in_linear_time():
    import time
    from jsh.candidate.extraction import split_items
    assert split_items("Python, Databricks (a, b), SQL; Go | R") == ["Python", "Databricks (a, b)", "SQL", "Go", "R"]
    started = time.perf_counter()
    extract(parse_resume(("SKILLS\n" + "x, " * 60_000).encode(), "long.txt").lines)
    assert time.perf_counter() - started < 3.0

def test_insufficient_evidence_is_unknown_not_a_percentage(app, cand):
    app.candidate.add_manual_skill(cand.id, "English")
    app.candidate.add_manual_skill(cand.id, "Technical documentation")
    job = _job(app, "https://x.example/thin", "Fluent English required. Write documentation for machines.")
    ev = app.jobs.detail(job.id, CANADA)["evaluation"]
    assert ev.skills_match is None and ev.skills_detail["insufficient"] is True
    assert {m["name"] for m in ev.skills_detail["matched"]} == {"English", "Technical documentation"}