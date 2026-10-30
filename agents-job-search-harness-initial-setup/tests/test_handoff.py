"""Session 9: JD intelligence, companies/contacts, AI review protocol (Phase 4), résumé tailoring (Phase 5)."""
import json
import sqlite3

import pytest

from jsh.jd import analyze_jd, to_markdown
from jsh.modes import CANADA, MEXICO
from jsh.services import JobInput

CV = """Jane Example
Data Engineer

SUMMARY
Data engineer with 8 years building pipelines in SQL and Python.

EXPERIENCE
Senior Data Engineer, Contoso Ltd — Toronto, ON
Jan 2020 – Present
- Built ETL pipelines in Python and SQL processing 2M rows daily
- Migrated reporting to Power BI dashboards for finance
- Mentored 3 junior engineers

Data Analyst, Fabrikam — Mexico City
2016 – 2019
- Automated Excel reports with Python
- Designed a data warehouse model in SQL Server

SKILLS
SQL, Python, Power BI, SQL Server, ETL, Data warehousing, Azure Data Factory

EDUCATION
B.Sc. Computer Science, UNAM, 2015
"""

JD = """We are looking for a Data Engineer to join our team in Toronto. This is a hybrid role.

Key job responsibilities
• Build ETL pipelines with Airflow and Spark
• Partner with BI teams using Tableau

BASIC QUALIFICATIONS
- 3+ years of experience with SQL and Python
- Bachelor's degree in Computer Science
- Experience with data modeling and building ETL pipelines

PREFERRED QUALIFICATIONS
- Experience with AWS Redshift and Snowflake
- Power BI dashboards

Benefits: salary $95,000 - $120,000 CAD per year. We are unable to sponsor work visas for this role."""


@pytest.fixture
def cand_app(app):
    c = app.candidate.create_candidate("Data")
    app.candidate.import_text(c.id, CV)
    app.candidate.set_skill_status([s.id for s in app.candidate.skills(c.id)], "approved")
    app.candidate.set_item_status([i.id for i in app.candidate.items(c.id)], "approved")
    for m in (CANADA, MEXICO):
        app.candidate.set_active_candidate(m, c.id)
    app.candidate.update_contact(c.id, {"name": "Jane Example", "email": "jane@example.com",
                                        "links": "https://linkedin.com/in/jane"})
    job, _ = app.jobs.upsert_job(JobInput(title="Data Engineer", company_name="Maple Data", job_country="CA",
                                          canonical_url="https://maple.example/jobs/1", description=JD))
    app.cid, app.job_id = c.id, job.id
    return app


# --------------------------------------------------------------------------- JD intelligence
def test_jd_sections_keywords_and_hints():
    a = analyze_jd(JD)
    kinds = [s["kind"] for s in a["sections"]]
    assert kinds == ["other", "responsibilities", "required", "preferred", "benefits"]
    imp = {k["name"]: k["importance"] for k in a["keywords"]}
    assert imp["SQL"] == imp["Python"] == imp["ETL/ELT"] == "required"
    assert imp["Snowflake"] == imp["Power BI"] == "preferred"
    assert imp["Apache Airflow"] == "mentioned"
    assert a["required_keywords"][:2] and "Snowflake" in a["preferred_keywords"]
    assert a["years_experience"][0]["min"] == 3 and a["years_experience"][0]["section"] == "required"
    assert a["work_authorization"][0]["negative_wording"] is True
    assert a["salary_mentions"][0]["text"].startswith("$95,000")
    assert any(w["term"] == "hybrid" for w in a["work_mode_hints"])
    assert a["flat_text"].count("\n- ") >= 4  # bullets normalised
    a["keywords"].clear()
    assert analyze_jd(JD)["keywords"]  # cached result is never mutated by callers


def test_jd_spanish_headings_and_unstructured_text():
    a = analyze_jd("Requisitos:\n- Python y SQL\n- 2 años de experiencia\n\nDeseable:\n- Power BI\n\n"
                   "Ofrecemos: home office")
    imp = {k["name"]: k["importance"] for k in a["keywords"]}
    assert imp["Python"] == "required" and imp["Power BI"] == "preferred"
    assert a["years_experience"][0]["min"] == 2
    b = analyze_jd("Experience with Spark is required. We use Python.")
    assert not b["structured"] and all(k["importance"] == "mentioned" for k in b["keywords"])


def test_job_document_and_markdown_export(cand_app):
    doc = cand_app.jd.document(cand_app.job_id, CANADA)
    assert doc["job"]["company_name"] == "Maple Data" and doc["jd"]["hash"]
    match = {m["name"]: m["importance"] for m in doc["match"]["matched"]}
    assert match["SQL"] == "required" and match["Power BI"] == "preferred"
    missing = {m["name"]: m["importance"] for m in doc["match"]["missing"]}
    assert missing["Snowflake"] == "preferred"
    md = to_markdown(doc)
    assert "## Palabras clave de la JD" in md and "**Requeridas:**" in md and "BASIC QUALIFICATIONS" in md
    assert json.loads(cand_app.jd.json_text(cand_app.job_id, CANADA))["job"]["job_id"] == cand_app.job_id


# --------------------------------------------------------------------------- companies & contacts
def test_companies_contacts_events_and_fill_only(cand_app):
    crm = cand_app.crm
    comps = crm.list_companies()
    maple = next(c for c in comps if c["name"] == "Maple Data")
    assert maple["jobs"] == 1
    cid, st = crm.upsert_company({"name": "maple data", "website": "https://maple.example", "watch": True})
    assert (cid, st) == (maple["company_id"], "updated")
    crm.upsert_company({"company_id": cid, "notes": "Nota del usuario"})
    _, st = crm.upsert_company({"name": "Maple Data", "notes": "agente", "careers_url": "https://maple.example/jobs"},
                               actor="agent:x", fill_only=True)
    c = crm.get_company(cid)
    assert st == "updated" and c["notes"] == "Nota del usuario" and c["careers_url"] == "https://maple.example/jobs"
    with pytest.raises(ValueError):
        crm.upsert_company({"name": "Bad", "website": "javascript:alert(1)"})
    with pytest.raises(ValueError):
        crm.delete_company(cid)  # has jobs

    pid, st = crm.upsert_contact({"name": "Ana Recruiter", "email": "ana@maple.example", "company_id": cid,
                                  "role": "Recruiter"})
    assert st == "created"
    same, st = crm.upsert_contact({"name": "Ana R.", "email": "ANA@maple.example", "phone": "+1 555"},
                                  actor="mcp:test", fill_only=True)
    assert same == pid and st == "updated"
    assert crm.get_contact(pid)["name"] == "Ana Recruiter"  # agent cannot overwrite
    with pytest.raises(ValueError):
        crm.upsert_contact({"name": "X", "email": "no-at-sign"})
    crm.add_event(pid, "email", "Envié CV", "2026-10-29", cand_app.job_id, CANADA)
    crm.set_application_contact(cand_app.job_id, CANADA, pid)
    got = crm.get_contact(pid)
    assert got["events"][0]["kind"] == "email" and got["events"][0]["job_title"] == "Data Engineer"
    assert got["applications"][0]["job_id"] == cand_app.job_id
    assert [c["contact_id"] for c in crm.job_contacts(cand_app.job_id)] == [pid]
    assert crm.list_contacts("maple")[0]["last_interaction"]
    crm.delete_contact(pid)
    assert crm.list_contacts() == []


def test_inbox_types_company_contact_and_event(cand_app):
    con = sqlite3.connect(cand_app.paths.db_file, timeout=30)
    for payload in ({"type": "company", "name": "Globant", "website": "https://globant.example", "watch": True},
                    {"type": "contact", "name": "Luis", "company_name": "Globant", "email": "luis@globant.example"},
                    {"type": "nope"}):
        con.execute("INSERT INTO job_inbox(payload) VALUES (?)", (json.dumps(payload),))
    con.commit()
    cand_app.sync.tick()
    rows = con.execute("SELECT status, result, job_id, message FROM job_inbox ORDER BY id").fetchall()
    assert [r[:2] for r in rows] == [("imported", "created"), ("imported", "created"), ("error", "")]
    assert "type desconocido" in rows[2][3]
    contact_id = rows[1][2]
    con.execute("INSERT INTO job_inbox(payload) VALUES (?)",
                (json.dumps({"type": "contact_event", "contact_id": contact_id, "kind": "call", "note": "ok"}),))
    con.commit()
    cand_app.sync.tick()
    assert cand_app.crm.get_contact(contact_id)["events"][0]["kind"] == "call"
    assert cand_app.crm.get_contact(contact_id)["company_name"] == "Globant"
    con.close()


# --------------------------------------------------------------------------- Phase 4: AI review protocol
def _review(pkg, **over):
    job = pkg["jobs"][0]
    r = {"job_id": job["job_id"], "jd_hash": job["jd_hash"], "technical_fit_score": 81,
         "technical_fit_explanation": "SQL y Python requeridos están aprobados.", "location_fit": "yes",
         "work_authorization_fit": "requires_employer_confirmation", "recommendation": "high_priority",
         "summary": "Buen ajuste", "evidence": [{"claim": "Pide SQL", "jd_quote": "3+ years of experience with SQL"}],
         "unknowns": ["LMIA"], "blockers": [], "questions_for_employer": ["¿Apoyan el permiso?"],
         "resume_adaptation_suggestions": ["Destacar ETL"]}
    r.update(over)
    return {"protocol_version": "jsh-review/1", "package_id": pkg["package_id"], "mode": pkg["mode"],
            "provider": "claude", "model": "test-model", "reviews": [r]}


def test_review_package_contents_and_privacy(cand_app):
    pkg = cand_app.reviews.build_package(CANADA, [cand_app.job_id])
    text = json.dumps(pkg)
    assert pkg["protocol_version"] == "jsh-review/1" and pkg["response_schema"]["title"] == "ReviewResult"
    assert pkg["jobs"][0]["jd"]["keywords"] and pkg["candidate"]["approved_skills"]
    assert "jane@example.com" not in text and "contact" not in pkg["candidate"]  # personal data opt-in only
    assert "UNTRUSTED" in pkg["instructions"] and "CANADA_VISA" in pkg["instructions"]
    personal = cand_app.reviews.build_package(CANADA, [cand_app.job_id], include_personal=True)
    assert personal["candidate"]["contact"]["email"] == "jane@example.com"
    md = cand_app.reviews.package_markdown(pkg)
    assert md.startswith("# Job Search Harness") and pkg["package_id"] in md
    files = cand_app.reviews.export_files(pkg)
    assert files["json"].exists() and files["markdown"].exists()
    with pytest.raises(ValueError):
        cand_app.reviews.build_package(CANADA, [])


def test_review_import_accept_reject_and_rule_field(cand_app):
    pkg = cand_app.reviews.build_package(CANADA, [cand_app.job_id])
    fenced = "Aquí está:\n```json\n" + json.dumps(_review(pkg)) + "\n```"
    rep = cand_app.reviews.import_result(fenced, source="file")
    assert len(rep["received"]) == 1 and not rep["rejected"]
    rid = rep["received"][0]["review_id"]
    before = cand_app.jobs.rows(CANADA)[0]
    assert before["ai_fit_score"] is None  # received ≠ accepted
    cand_app.rules.create(CANADA, {"name": "IA recomienda", "field": "ai_fit_score", "operator": "gte", "value": 75,
                                   "value_type": "number", "scope": "rank", "action": "weight", "weight": 15,
                                   "severity": "soft", "unknown_handling": "keep"})
    cand_app.reviews.accept(rid)
    row = cand_app.jobs.rows(CANADA)[0]
    assert row["ai_fit_score"] == 81 and row["ai_recommendation"] == "high_priority"
    assert row["user_decision"] == "new"  # a review never changes decisions
    ev = cand_app.jobs.detail(cand_app.job_id, CANADA)["evaluation"]
    assert any(o["name"] == "IA recomienda" and o["effect"] == "weight" for o in ev.explanation)
    # second accepted review supersedes the first
    rid2 = cand_app.reviews.import_result(_review(pkg, technical_fit_score=40))["received"][0]["review_id"]
    cand_app.reviews.accept(rid2)
    assert cand_app.reviews.get(rid)["status"] == "superseded"
    cand_app.reviews.reject(rid2)
    assert cand_app.jobs.rows(CANADA)[0]["ai_fit_score"] is None
    assert cand_app.reviews.counts(CANADA)["rejected"] == 1


def test_review_import_rejections_and_stale(cand_app):
    pkg = cand_app.reviews.build_package(MEXICO, [cand_app.job_id])
    other, _ = cand_app.jobs.upsert_job(JobInput(title="Other", canonical_url="https://o.example/1"))
    bad = _review(pkg)
    bad["reviews"].append({**bad["reviews"][0], "job_id": other.id})
    bad["reviews"].append({**bad["reviews"][0], "job_id": "nope"})
    rep = cand_app.reviews.import_result(bad)
    reasons = {r["job_id"]: r["reason"] for r in rep["rejected"]}
    assert "no pertenece" in reasons[other.id] and "no existe" in reasons["nope"]
    rep = cand_app.reviews.import_result(_review(pkg, jd_hash="0" * 64))
    assert "jd_hash" in rep["rejected"][0]["reason"]
    for broken, msg in (({**_review(pkg), "protocol_version": "v0"}, "protocol_version"),
                        ({**_review(pkg), "mode": CANADA}, "modo"),
                        ({**_review(pkg), "package_id": "x" * 32}, "package_id"),
                        (_review(pkg, technical_fit_score=150), "technical_fit_score"),
                        ("not json at all", "JSON")):
        with pytest.raises(ValueError, match=msg):
            cand_app.reviews.import_result(broken)
    cand_app.jobs.upsert_job(JobInput(title="Data Engineer", canonical_url="https://maple.example/jobs/1",
                                      description=JD + "\nNEW: Kafka required."))
    rep = cand_app.reviews.import_result(_review(pkg))
    assert rep["received"][0]["stale"] is True


def test_review_via_inbox_and_banner(cand_app):
    pkg = cand_app.reviews.build_package(CANADA, [cand_app.job_id])
    seq = cand_app.sync.seq
    con = sqlite3.connect(cand_app.paths.db_file, timeout=30)
    con.execute("INSERT INTO job_inbox(payload, source) VALUES (?, 'claude')",
                (json.dumps({"type": "review", **_review(pkg)}),))
    con.commit()
    con.close()
    cand_app.sync.tick()
    assert cand_app.reviews.list(CANADA, "received")[0]["provider"] == "claude"
    assert cand_app.sync.changes_since(seq)["reviews"] == 1
    cand_app.crm.upsert_company({"name": "Via MCP"}, actor="mcp:claude")
    cand_app.sync.tick()
    assert cand_app.sync.changes_since(seq)["crm"] == 1


def test_prompt_templates_editable(cand_app):
    tpls = {t.key: t for t in cand_app.reviews.templates()}
    assert set(tpls) == {"review_default", "tailoring_default"}
    cand_app.reviews.update_template("review_default", "Instrucciones nuevas")
    pkg = cand_app.reviews.build_package(CANADA, [cand_app.job_id])
    assert pkg["instructions"].startswith("Instrucciones nuevas") and pkg["template"]["version"] == 2
    with pytest.raises(ValueError):
        cand_app.reviews.update_template("review_default", "  ")
    cand_app.reviews.reset_template("review_default")
    assert "UNTRUSTED" in cand_app.reviews.template("review_default").body


# --------------------------------------------------------------------------- code-review regressions (session 9)
def test_personal_data_never_leaves_without_opt_in(app):
    from jsh.privacy import mask, scrub
    header = "Jane Example | Python & SQL Data Engineer | jane.example@gmail.com | +1 416 555 0199 | Toronto"
    c = app.candidate.create_candidate("Leak")
    app.candidate.import_text(c.id, header + "\n\n" + CV.split("\n", 2)[2])
    app.candidate.set_skill_status([s.id for s in app.candidate.skills(c.id)], "approved")
    app.candidate.set_item_status([i.id for i in app.candidate.items(c.id)], "approved")
    app.candidate.set_active_candidate(CANADA, c.id)
    app.candidate.update_contact(c.id, {"name": "Jane Example", "email": "jane.example@gmail.com",
                                        "phone": "+1 416 555 0199", "links": "https://linkedin.com/in/jane-x"})
    job, _ = app.jobs.upsert_job(JobInput(title="DE", canonical_url="https://x.example/p", description=JD))
    blobs = [json.dumps(app.reviews.build_package(CANADA, [job.id]), ensure_ascii=False),
             json.dumps(app.tailoring.build_package(job.id, CANADA), ensure_ascii=False),
             json.dumps(app.reviews.candidate_context(CANADA), ensure_ascii=False),
             json.dumps(app.jd.document(job.id, CANADA), ensure_ascii=False)]
    for blob in blobs:
        assert "jane.example@gmail.com" not in blob and "555 0199" not in blob and "linkedin.com/in/jane-x" not in blob
    assert "jane.example@gmail.com" in json.dumps(app.reviews.build_package(CANADA, [job.id], include_personal=True))
    assert scrub(header) == "" and mask("call +52 55 1234 5678 now") == "call [teléfono] now"
    assert mask("processed 2,000,000 rows in 2019-2020") == "processed 2,000,000 rows in 2019-2020"


def test_tailoring_flags_new_names_titles_dates_and_spelled_numbers(cand_app):
    from jsh.services.tailoring import new_fact_terms
    support = "Built SQL reports for the finance team"
    allowed = {"sql", "python"}
    assert new_fact_terms("Built SQL reports for the finance team at Google and Amazon", support, allowed) == \
        ["Google", "Amazon"]
    assert set(new_fact_terms("Built SQL reports for a team of twelve, saving three million dollars", support,
                              allowed)) >= {"twelve", "three", "million"}
    assert {"Director", "March"} <= set(new_fact_terms("Built SQL reports; led the company as Director of "
                                                       "Analytics since March", support, allowed))
    assert new_fact_terms("Built and maintained SQL reports for the finance team", support, allowed) == []
    pkg = cand_app.tailoring.build_package(cand_app.job_id, CANADA)
    rep = cand_app.tailoring.import_proposal({
        "protocol_version": "jsh-tailoring/1", "package_id": pkg["package_id"], "job_id": cand_app.job_id,
        "jd_hash": pkg["jd_hash"], "master_version_id": pkg["master_version_id"],
        "changes": [{"change_id": "x", "action": "rewrite", "target_id": "P1.H2",
                     "proposed": "Migrated reporting to Power BI dashboards for finance at Google"}],
        "skills_order": ["Snowflake-not-mine", "Power BI", "Power BI", "SQL"]})
    run = cand_app.tailoring.get_run(rep["run_id"])
    by_id = {c["change_id"]: c for c in run["changes"]}
    assert by_id["x"]["requires_confirmation"] and "new_terms" in {f["code"] for f in by_id["x"]["flags"]}
    order = by_id["skills_order"]["order"]  # skills_order became a normal change, de-duplicated
    assert order[:2] == ["skill:Power BI", "skill:SQL"] and len(order) == len(set(order))
    vid = cand_app.tailoring.approve(rep["run_id"], ["x"], confirmed_ids=["x"])
    ver = cand_app.tailoring.tailored_versions(cand_app.job_id)[0]
    assert vid == ver.id and ver.content["skills"][0]["name"] != "Power BI"  # skills_order NOT applied implicitly


def test_agents_cannot_flip_user_watch_or_legitimacy(cand_app):
    crm = cand_app.crm
    cid = crm.list_companies()[0]["company_id"]
    crm.upsert_company({"company_id": cid, "legitimacy": "suspicious"})
    crm.upsert_company({"company_id": cid, "legitimacy": "unknown"})  # the user changed their mind on purpose
    crm.upsert_company({"name": "Maple Data", "watch": True, "legitimacy": "verified"}, actor="api:x",
                       fill_only=True)
    c = crm.get_company(cid)
    assert c["watch"] is False and c["legitimacy"] == "unknown"
    new_id, st = crm.upsert_company({"name": "Fresh Co", "watch": True, "legitimacy": "verified"},
                                    actor="api:x", fill_only=True)
    assert st == "created" and crm.get_company(new_id)["watch"] is True  # agents may create with values
    _, st = crm.upsert_company({"name": "Fresh Co", "evidence": "nueva fuente"}, actor="api:x", fill_only=True)
    assert st == "updated" and crm.get_company(new_id)["evidence"] == "nueva fuente"


# --------------------------------------------------------------------------- upgrades & housekeeping
def test_backup_retention_keeps_manual_copies(app):
    for i in range(5):
        app.db.backup("pre_import")
    app.db.backup("manual")
    assert app.db.prune_backups(keep_auto=2) == 3
    names = [p.name for p in app.db.list_backups()]
    assert sum(n.endswith("_pre_import.db") for n in names) == 2 and any(n.endswith("_manual.db") for n in names)


def test_migration_5_from_v4(tmp_path):
    from sqlalchemy import text

    from jsh.db import SCHEMA_VERSION
    from jsh.paths import get_paths
    from jsh.services import AppServices
    home = tmp_path / "v4"
    svc = AppServices(get_paths(home))
    svc.jobs.upsert_job(JobInput(title="T", canonical_url="https://x.example/1"))
    svc.close()
    con = sqlite3.connect(get_paths(home).db_file)
    for t in ("contact_events", "prompt_templates", "ai_packages", "ai_reviews", "tailoring_runs"):
        con.execute(f"DROP TABLE {t}")
    for table, col in (("companies", "careers_url"), ("companies", "watch"), ("companies", "legitimacy"),
                       ("companies", "evidence"), ("contacts", "phone"), ("contacts", "source"),
                       ("contacts", "updated_at"), ("candidate_profiles", "contact"),
                       ("job_mode_evaluations", "ai_review")):
        con.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
    con.execute("PRAGMA user_version = 4")
    con.commit()
    con.close()
    svc = AppServices(get_paths(home))
    with svc.db.engine.connect() as c:
        assert c.execute(text("PRAGMA user_version")).scalar() == SCHEMA_VERSION
    assert {t.key for t in svc.reviews.templates()} == {"review_default", "tailoring_default"}
    assert svc.crm.list_companies() == [] and svc.jobs.rows(CANADA)[0]["ai_fit_score"] is None
    assert list(svc.paths.backups.glob("*pre_migration_v4.db"))
    svc.close()


# --------------------------------------------------------------------------- Phase 5: tailoring
def test_deterministic_tailoring_approve_and_docx(cand_app):
    from docx import Document
    rep = cand_app.tailoring.deterministic_proposal(cand_app.job_id, CANADA)
    run = cand_app.tailoring.get_run(rep["run_id"])
    assert run["source"] == "deterministic" and run["changes"]
    assert all(c["action"] == "reorder" for c in run["changes"])  # never invents text
    skills = next(c for c in run["changes"] if c["target_id"] == "skills")
    assert skills["order"][0] in ("skill:SQL", "skill:Python", "skill:ETL/ELT")
    assert any("Snowflake" in m for m in run["missing_requirements"])
    vid = cand_app.tailoring.approve(rep["run_id"], [c["change_id"] for c in run["changes"]])
    ver = cand_app.tailoring.tailored_versions(cand_app.job_id)[0]
    assert ver.id == vid and ver.provenance["tailoring_run_id"] == rep["run_id"]
    assert cand_app.jobs.rows(CANADA)[0]["resume_version_id"] == vid
    rel = cand_app.tailoring.generate_docx(vid)
    d = Document(cand_app.paths.home / rel)
    text = "\n".join(p.text for p in d.paragraphs)
    assert "Jane Example" in text and "Built ETL pipelines in Python and SQL processing 2M rows daily" in text
    assert "jane@example.com" in text  # header is rendered locally only
    assert "EXPERIENCE" in cand_app.tailoring.render(vid)
    with pytest.raises(ValueError):
        cand_app.tailoring.approve(rep["run_id"], [])  # already decided


def test_ai_tailoring_truth_checks_and_confirmation(cand_app):
    pkg = cand_app.tailoring.build_package(cand_app.job_id, CANADA)
    assert "header" not in pkg["master_resume"] and "jane@example.com" not in json.dumps(pkg)
    ids = [b["id"] for p in pkg["master_resume"]["experience"] for b in p["bullets"]]
    assert "P1.H1" in ids and pkg["master_resume"]["summary"][0]["id"] == "S1"
    base = {"protocol_version": "jsh-tailoring/1", "package_id": pkg["package_id"], "job_id": cand_app.job_id,
            "jd_hash": pkg["jd_hash"], "master_version_id": pkg["master_version_id"], "provider": "chatgpt",
            "model": "x"}
    changes = [
        {"change_id": "ok", "action": "rewrite", "target_id": "P1.H1",
         "proposed": "Built and maintained ETL pipelines in Python and SQL processing 2M rows daily",
         "evidence_ids": ["P1.H1"]},
        {"change_id": "nums", "action": "rewrite", "target_id": "P1.H2",
         "proposed": "Migrated reporting to Power BI, cutting report time by 40%", "evidence_ids": ["P1.H2"]},
        {"change_id": "skill", "action": "rewrite", "target_id": "S1",
         "proposed": "Data engineer with 8 years building pipelines in SQL, Python and Snowflake."},
        {"change_id": "title", "action": "rewrite", "target_id": "P1", "proposed": "Lead Data Engineer"},
        {"change_id": "add", "action": "add", "target_id": "P2", "proposed": "Built dashboards",
         "evidence_ids": ["skill:Power BI"]},
        {"change_id": "badorder", "action": "reorder", "target_id": "P1", "order": ["P1.H1"]},
    ]
    rep = cand_app.tailoring.import_proposal({**base, "changes": changes,
                                              "missing_requirements": ["Snowflake", "AWS Redshift"]})
    rejected = {r["change_id"] for r in rep["rejected"]}
    assert rejected == {"title", "badorder"}
    run = cand_app.tailoring.get_run(rep["run_id"])
    flags = {c["change_id"]: {f["code"] for f in c["flags"]} for c in run["changes"]}
    assert flags["ok"] == set() and "new_numbers" in flags["nums"] and "unapproved_skill" in flags["skill"]
    assert "new_text" in flags["add"]
    with pytest.raises(ValueError, match="Confirme"):
        cand_app.tailoring.approve(rep["run_id"], ["ok", "nums"])
    vid = cand_app.tailoring.approve(rep["run_id"], ["ok", "add"], confirmed_ids=["add"])
    ver = cand_app.tailoring.tailored_versions(cand_app.job_id)[0]
    bullets = [b["text"] for p in ver.content["experience"] for b in p["bullets"]]
    assert "Built and maintained ETL pipelines in Python and SQL processing 2M rows daily" in bullets
    assert "Built dashboards" in bullets and not any("40%" in b for b in bullets)
    assert ver.provenance["confirmed_flagged_ids"] == ["add"] and ver.provenance["provider"] == "chatgpt"
    assert ver.provenance["prompt_template"]["key"] == "tailoring_default"
    assert vid == ver.id
    for broken, msg in (({**base, "jd_hash": "0" * 64}, "jd_hash"),
                        ({**base, "master_version_id": "nope"}, "master_version_id"),
                        ({**base, "protocol_version": "x"}, "protocol_version")):
        with pytest.raises(ValueError, match=msg):
            cand_app.tailoring.import_proposal({**broken, "changes": []})
