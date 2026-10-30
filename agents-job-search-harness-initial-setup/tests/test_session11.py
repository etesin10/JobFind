"""Session 11: tags, saved views, salary normalisation (FX), skill-gap report, ad-liveness check, wipe all."""
import json
import sqlite3
from datetime import date

import httpx
import pytest

from jsh.modes import CANADA, MEXICO
from jsh.services import WIPE_CONFIRM_WORD, JobInput
from jsh.services.liveness import BlockedUrl, LivenessService, check_url_allowed, classify
from jsh.services.salary import describe, normalize_salary
from jsh.services.views import ViewFilters, matches, validate_filters

from .test_handoff import CV, JD


def _job(app, n, **kw):
    data = {"title": f"Data Engineer {n}", "company_name": f"Co{n}", "source": "manual", "source_job_id": f"S{n}",
            "canonical_url": f"https://jobs.example.com/jobs/{n}", "description": JD, **kw}
    job, _ = app.jobs.upsert_job(JobInput(**data))
    return job.id


@pytest.fixture
def ready(app):
    c = app.candidate.create_candidate("Data")
    app.candidate.import_text(c.id, CV)
    app.candidate.set_skill_status([s.id for s in app.candidate.skills(c.id)], "approved")
    for m in (CANADA, MEXICO):
        app.candidate.set_active_candidate(m, c.id)
    app.cid = c.id
    return app


# --------------------------------------------------------------------------- salary
def test_normalize_salary_math_and_assumptions():
    rates = {"USD": {"rate": 1.4, "as_of": "2026-10-30", "source": "ecb"}}
    n = normalize_salary(50, 60, "USD", "per hour", "US", "CAD", rates)
    assert n["status"] == "ok" and n["min"] == round(50 * 2080 * 1.4) and n["max"] == round(60 * 2080 * 1.4)
    assert "2080 horas/año" in n["assumptions"]
    assert "USD @1.4" in describe(n) and "BCE" in describe(n)
    m = normalize_salary(40000, None, "", "mensual", "MX", "MXN", {})
    assert m["status"] == "ok" and m["min"] == 480000 and "moneda supuesta" in m["assumptions"][0]
    assert normalize_salary(1, 2, "USD", "", "", "CAD", rates)["status"] == "unknown_period"
    assert normalize_salary(1, 2, "", "year", "", "CAD", rates)["status"] == "unknown_currency"
    assert normalize_salary(1, 2, "EUR", "year", "", "CAD", rates)["status"] == "no_rate"
    assert normalize_salary(None, None, "USD", "year", "", "CAD", rates)["status"] == "no_salary"


def test_fx_refresh_inverts_rates_and_keeps_manual(app):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url)
        return httpx.Response(200, json=[{"date": "2026-10-30", "base": "CAD", "quote": "USD", "rate": 0.5},
                                         {"date": "2026-10-30", "base": "CAD", "quote": "MXN", "rate": 10.0},
                                         {"date": "2026-10-30", "base": "CAD", "quote": "EUR", "rate": 0.25}])

    app.salary.transport = httpx.MockTransport(handler)
    app.salary.set_manual_rate("EUR", 1.6, "2026-10-01")
    res = app.salary.refresh_rates()
    assert calls and calls[0].params["base"] == "CAD" and "providers=ecb" in str(calls[0])
    assert res["skipped_manual"] == ["EUR"]
    rates = app.salary.rates()
    assert rates["USD"]["rate"] == pytest.approx(2.0) and rates["USD"]["source"] == "ecb"
    assert rates["MXN"]["rate"] == pytest.approx(0.1)
    assert rates["EUR"] == {**rates["EUR"], "rate": 1.6, "source": "manual", "as_of": "2026-10-01"}


def test_salary_in_rows_rules_and_base_change(app):
    jid = _job(app, 1, salary_min=100000, salary_max=120000, currency="USD", salary_period="year", job_country="US")
    row = app.jobs.rows(CANADA, "all")[0]
    assert row["salary_status"] == "no_rate" and row["salary_annual_min"] is None
    app.salary.set_manual_rate("USD", 1.4)
    row = app.jobs.rows(CANADA, "all")[0]
    assert row["salary_annual_min"] == 140000 and row["salary_annual_max"] == 168000 and "CAD/año" in row["salary_note"]
    app.rules.create(CANADA, {"name": "Salario ≥ 150k CAD", "field": "salary_annual_max",
                           "operator": "gte", "value": 150000, "action": "weight", "weight": 10, "scope": "rank"})
    ev = app.jobs.detail(jid, CANADA)["evaluation"]
    assert any(o["name"] == "Salario ≥ 150k CAD" and o["effect"] == "weight" for o in ev.explanation)
    app.salary.configure(base="USD")
    row = app.jobs.rows(CANADA, "all")[0]
    assert row["salary_base"] == "USD" and row["salary_annual_min"] == 100000
    with pytest.raises(ValueError):
        app.salary.configure(base="DOLLARS")
    with pytest.raises(ValueError):
        app.salary.set_manual_rate("USD", -1)


# --------------------------------------------------------------------------- tags + views
def test_tags_are_normalised_searchable_and_usable_in_rules(app):
    a, b = _job(app, 1), _job(app, 2)
    assert app.jobs.set_tags(a, ["Zeta", " zeta ", "Prioridad  alta", ""]) == ["Prioridad alta", "Zeta"]
    assert app.jobs.add_tags([a, b], ["Remoto"]) == 2
    assert app.jobs.tags(b) == ["Remoto"]
    assert app.jobs.all_tags()[0] == ("Remoto", 2)
    assert [r["job_id"] for r in app.jobs.rows(CANADA, "all", "zeta")] == [a]
    assert app.jobs.remove_tags([a, b], ["REMOTO"]) == 2 and app.jobs.tags(b) == []
    with pytest.raises(ValueError):
        app.jobs.set_tags(a, [f"t{i}" for i in range(21)])
    app.rules.create(CANADA, {"name": "Etiqueta prioridad", "field": "tags", "operator": "contains",
                           "value": "Prioridad alta", "action": "weight", "weight": 7, "scope": "rank"})
    ev = app.jobs.detail(a, CANADA)["evaluation"]
    assert any(o["name"] == "Etiqueta prioridad" and o["effect"] == "weight" for o in ev.explanation)
    con = sqlite3.connect(app.paths.db_file)
    assert con.execute("SELECT actor FROM audit_events WHERE action='tags_updated'").fetchone() == ("user",)


def test_saved_view_filters(app):
    toronto = _job(app, 1, job_country="CA", location_text="Toronto, ON", work_mode="hybrid", posted_date=date.today())
    mexico = _job(app, 2, job_country="MX", location_text="CDMX", work_mode="remote", posted_date=date(2020, 1, 1))
    app.jobs.set_tags(toronto, ["top"])
    app.jobs.set_application_status(mexico, CANADA, "applied")
    vid = app.views.save(CANADA, "Canadá · Toronto · sin aplicar",
                         {"countries": "ca", "location": "toronto", "application": "none", "tags_any": ["TOP"]})
    assert [r["job_id"] for r in app.views.rows(CANADA, vid)] == [toronto]
    assert [r["job_id"] for r in app.views.rows(CANADA, "canadá · toronto · sin aplicar")] == [toronto]
    assert app.views.list(CANADA)[0]["summary"][:2] == ["CA", "📍toronto"]
    assert [r["job_id"] for r in app.jobs.rows(CANADA, "all", filters={"application": "applied"})] == [mexico]
    assert [r["job_id"] for r in app.jobs.rows(CANADA, "all", filters={"posted_within_days": 30})] == [toronto]
    with pytest.raises(ValueError):
        app.views.save(CANADA, "Canadá · Toronto · sin aplicar", {})
    with pytest.raises(ValueError):
        app.views.save(CANADA, "Mala", {"min_score": -5})
    app.views.select(CANADA, vid)
    app.views.delete(vid)
    assert app.views.list(CANADA) == [] and app.views.selected(CANADA) == ""


def test_view_filter_salary_and_closed():
    f = validate_filters({"min_salary": 100000, "salary_unknown_ok": False, "exclude_closed": True})
    assert isinstance(f, ViewFilters)
    assert matches({"salary_annual_max": 120000}, f)
    assert not matches({"salary_annual_max": 90000}, f)
    assert not matches({"salary_annual_max": None, "salary_annual_min": None}, f)
    assert not matches({"salary_annual_max": 120000, "liveness": "likely_closed"}, f)
    assert not matches({"salary_annual_max": 120000, "ad_status": "expired"}, f)


# --------------------------------------------------------------------------- skill gaps
def test_skill_gap_report_ranks_required_over_preferred(ready):
    app = ready
    a, b = _job(app, 1), _job(app, 2)
    app.jobs.set_decision(a, CANADA, "saved")
    rep = app.insights.skill_gaps(CANADA, "all")
    assert rep["jobs_analysed"] == 2 and rep["jobs_with_match"] == 2
    names = [g["name"] for g in rep["gaps"]]
    assert "Snowflake" in names and "Apache Airflow" in names
    snow = next(g for g in rep["gaps"] if g["name"] == "Snowflake")
    assert snow["preferred"] == 2 and snow["jobs"] == 2 and snow["score"] == 4 and snow["status"] == "absent"
    assert snow["share_pct"] == 100.0 and len(snow["examples"]) == 2
    assert any(s["name"] == "Python" for s in rep["strengths"])
    saved = app.insights.skill_gaps(CANADA, "saved")
    assert saved["jobs_analysed"] == 1
    app.jobs.set_decision(b, CANADA, "discarded", "other")
    assert app.insights.skill_gaps(CANADA, "all")["jobs_analysed"] == 1
    md = app.insights.markdown(rep)
    assert "| Snowflake | 2 (100.0%) | 0 | 2 | 0 | absent |" in md
    with pytest.raises(ValueError):
        app.insights.skill_gaps(CANADA, "bogus")


# --------------------------------------------------------------------------- liveness
PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731


def test_url_guard_blocks_private_and_odd_urls():
    for url in ("http://127.0.0.1/x", "http://10.0.0.5/x", "http://169.254.169.254/latest", "file:///c:/x",
                "https://user:pw@example.com/", "https://example.com:8080/x", "http://[::1]/"):
        with pytest.raises(BlockedUrl):
            check_url_allowed(url, PUBLIC)
    with pytest.raises(BlockedUrl):
        check_url_allowed("https://intranet.example/x", lambda h: ["192.168.1.10"])
    check_url_allowed("https://example.com/jobs/1", PUBLIC)


def test_classifier():
    u = "https://boards.greenhouse.io/acme/jobs/1"
    assert classify(u, u, 404, "")[0] == "likely_closed"
    assert classify(u, u, 200, "Apply now")[0] == "live"
    assert classify(u, u, 200, "Sorry, this job is no longer available.")[0] == "likely_closed"
    assert classify(u, "https://boards.greenhouse.io/acme?error=true", 200, "Open roles")[0] == "likely_closed"
    assert classify("https://acme.com/careers/job/9", "https://acme.com/careers", 200, "Careers")[0] == "likely_closed"
    assert classify(u, u, 403, "")[0] == "unknown"
    assert classify(u, u, None, "Error de red")[0] == "unknown"


def test_liveness_flags_never_changes_ad_status(app):
    open_id = _job(app, 1)
    closed_id = _job(app, 2)
    gone_id = _job(app, 3)
    redirect_id = _job(app, 4, canonical_url="https://jobs.example.com/jobs/4")

    def handler(request: httpx.Request):
        path = request.url.path
        if path == "/jobs/1":
            return httpx.Response(200, html="<html><body><h1>Data Engineer</h1>Apply</body></html>")
        if path == "/jobs/2":
            return httpx.Response(200, html="<p>This position has been filled.</p>")
        if path == "/jobs/3":
            return httpx.Response(404)
        if path == "/jobs/4":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        return httpx.Response(500)

    app.liveness = LivenessService(app, transport=httpx.MockTransport(handler), resolver=PUBLIC, sleep=lambda s: None)
    res = app.liveness.run_batch(limit=10)
    assert res == {"checked": 4, "likely_closed": 2, "live": 1, "unknown": 1}
    rows = {r["job_id"]: r for r in app.jobs.rows(CANADA, "all")}
    assert rows[open_id]["liveness"] == "live"
    assert rows[closed_id]["liveness"] == "likely_closed" and "filled" in rows[closed_id]["liveness_reason"]
    assert rows[gone_id]["liveness"] == "likely_closed"
    assert rows[redirect_id]["liveness"] == "unknown" and "Bloqueada" in rows[redirect_id]["liveness_reason"]
    assert all(r["ad_status"] != "expired" for r in rows.values())
    assert {f["job_id"] for f in app.liveness.flagged()} == {closed_id, gone_id}
    assert app.liveness.due_jobs() == []  # all checked recently
    app.liveness.dismiss(closed_id)
    assert {f["job_id"] for f in app.liveness.flagged()} == {gone_id}
    assert app.jobs.rows(CANADA, "all", filters={"exclude_closed": True}).__len__() == 3
    assert app.liveness.mark_expired([gone_id]) == 1
    assert app.jobs.detail(gone_id, CANADA)["job"].ad_status == "expired"
    con = sqlite3.connect(app.paths.db_file)
    assert con.execute("SELECT count(*) FROM audit_events WHERE action='ad_likely_closed'").fetchone()[0] == 2
    app.jobs.delete_jobs([closed_id])
    assert con.execute("SELECT count(*) FROM job_liveness WHERE job_id=?", (closed_id,)).fetchone()[0] == 0


def test_liveness_settings_default_off(app):
    st = app.liveness.settings()
    assert st["enabled"] is False and st["interval_hours"] == 24 and st["batch"] == 25
    app.liveness.configure(enabled=True, interval_hours=12, batch=10)
    assert app.liveness.settings()["enabled"] is True
    with pytest.raises(ValueError):
        app.liveness.configure(batch=0)


# --------------------------------------------------------------------------- wipe all
def test_wipe_all_keeps_one_backup_and_reseeds(ready):
    app = ready
    jid = _job(app, 1)
    app.jobs.set_tags(jid, ["x"])
    app.views.save(CANADA, "v", {})
    app.salary.set_manual_rate("USD", 1.4)
    app.db.set_setting("ui_language", "en")
    old_tokens = dict(app.db.get_setting("api_tokens") or {})
    app.db.backup("manual")
    (app.paths.resumes / "cv.txt").write_text("cv", encoding="utf-8")
    (app.paths.exports / "sub").mkdir()
    (app.paths.exports / "sub" / "x.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError):
        app.wipe_all(confirm="borrar")
    rep = app.wipe_all(keep_backup=True, confirm=WIPE_CONFIRM_WORD)
    assert rep["backup"].endswith("_pre_wipe.db") and not rep["locked"]
    assert [p.name for p in app.db.list_backups()] == [rep["backup"].split("\\")[-1].split("/")[-1]]
    counts = app.db.table_counts()
    assert counts["jobs"] == counts["skills"] == counts["job_tags"] == counts["fx_rates"] == 0
    assert counts["saved_views"] == counts["candidate_profiles"] == 0
    assert counts["rules"] > 0 and counts["modes"] == 2 and counts["prompt_templates"] > 0
    assert app.db.get_setting("ui_language") == "en"
    tokens = app.db.get_setting("api_tokens")
    assert tokens["write"] and tokens["write"] != old_tokens.get("write")
    assert not any(app.paths.resumes.iterdir()) and not any(app.paths.exports.iterdir())
    # the final backup really contains the old data
    con = sqlite3.connect(rep["backup"])
    assert con.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    con.close()
    # the app keeps working after the wipe, and agent events are detected again
    _job(app, 9)
    assert len(app.jobs.rows(CANADA, "all")) == 1
    app.jobs.upsert_job(JobInput(title="Agent job", company_name="A", source="manual", source_job_id="AG1"),
                        actor="agent:test")
    app.sync.tick(force=True)
    assert app.sync.changes_since(0)["created"] >= 1


def test_wipe_all_without_backup(app):
    _job(app, 1)
    app.db.backup("manual")
    rep = app.wipe_all(keep_backup=False, confirm=WIPE_CONFIRM_WORD)
    assert rep["backup"] == "" and app.db.list_backups() == []
    assert app.db.table_counts()["jobs"] == 0
    json.dumps(rep)


# --------------------------------------------------------------------------- API / MCP
def test_api_and_mcp_expose_views_tags_gaps_and_fx(ready):
    import asyncio

    from fastapi.testclient import TestClient
    from mcp import Client

    from jsh.api import create_api, ensure_tokens
    from jsh.mcp_server import build_server

    app = ready
    a, b = _job(app, 1, job_country="CA"), _job(app, 2, job_country="MX")
    app.jobs.set_tags(a, ["Top"])
    app.views.save(CANADA, "Solo Canadá", {"countries": ["CA"]})
    app.salary.set_manual_rate("USD", 1.4)
    tokens = ensure_tokens(app)
    c = TestClient(create_api(app), base_url="http://127.0.0.1:8765")
    h = {"Authorization": f"Bearer {tokens['read']}"}
    assert [r["job_id"] for r in c.get("/jobs", params={"mode": CANADA, "saved_view": "Solo Canadá"},
                                       headers=h).json()["items"]] == [a]
    assert [r["job_id"] for r in c.get("/jobs", params={"mode": CANADA, "tag": "top"}, headers=h).json()["items"]] == [a]
    assert c.get("/jobs", params={"mode": CANADA, "saved_view": "nope"}, headers=h).status_code == 404
    assert c.get("/views", params={"mode": CANADA}, headers=h).json()[0]["name"] == "Solo Canadá"
    assert c.get("/tags", headers=h).json() == [{"tag": "Top", "jobs": 1}]
    gaps = c.get("/insights/skill-gaps", params={"mode": CANADA, "scope": "all"}, headers=h).json()
    assert gaps["jobs_analysed"] == 2 and gaps["gaps"]
    fx = c.get("/fx-rates", headers=h).json()
    assert fx["base"] == "CAD" and fx["rates"][0]["currency"] == "USD"

    async def mcp():
        async with Client(build_server(app, "t", read_only=True)) as m:
            names = {t.name for t in (await m.list_tools()).tools}
            assert {"list_saved_views", "get_skill_gaps"} <= names
            r = await m.call_tool("search_jobs", {"mode": CANADA, "view": "all", "saved_view": "Solo Canadá"})
            assert [i["job_id"] for i in r.structured_content["items"]] == [a]
            assert r.structured_content["items"][0]["tags"] == "Top"
            g = await m.call_tool("get_skill_gaps", {"mode": CANADA, "scope": "all"})
            assert g.structured_content["gaps"]
    asyncio.run(mcp())

# --------------------------------------------------------------------------- review regressions
def test_liveness_bad_url_never_blocks_the_queue(app):
    bad = _job(app, 1, canonical_url="https://example.com:99999/x")
    good = _job(app, 2)
    app.liveness = LivenessService(app, transport=httpx.MockTransport(lambda r: httpx.Response(200, html="ok")),
                                   resolver=PUBLIC, sleep=lambda s: None)
    res = app.liveness.run_batch(limit=10)
    assert res["checked"] == 2 and res["live"] == 1 and res["unknown"] == 1
    assert app.liveness.status(bad)["status"] == "unknown" and app.liveness.status(good)["status"] == "live"


def test_saved_view_keywords_apply_in_jobs_rows(app):
    a = _job(app, 1, description="Python and SQL")
    _job(app, 2, description="Java only")
    assert [r["job_id"] for r in app.jobs.rows(CANADA, "all", "", filters={"q": "python"})] == [a]


def test_unpublished_currency_is_not_stale_forever_and_no_change_no_reevaluation(app):
    _job(app, 1, salary_min=1, salary_max=2, currency="COP", salary_period="year")
    _job(app, 2, salary_min=1, salary_max=2, currency="USD", salary_period="year")
    app.salary.transport = httpx.MockTransport(lambda r: httpx.Response(200, json=[
        {"date": "2026-10-30", "base": "CAD", "quote": "USD", "rate": 0.7}]))
    assert app.salary.stale()
    res = app.salary.refresh_rates()
    assert "COP" in res["missing"] and not app.salary.stale()
    con = sqlite3.connect(app.paths.db_file)
    n = con.execute("SELECT count(*) FROM audit_events WHERE action='fx_refreshed'").fetchone()[0]
    app.salary.refresh_rates()  # same rates again: nothing changes, no audit, no re-evaluation
    assert con.execute("SELECT count(*) FROM audit_events WHERE action='fx_refreshed'").fetchone()[0] == n