"""Phase 3 tests: connectors (offline, with recorded-shape fixtures), discovery service, safety, UI.

Network is NEVER used here (httpx.MockTransport). The optional live check is in test_live_sources.py and only
runs with JSH_LIVE_TESTS=1.
"""
import json

import httpx
import pytest

from jsh.connectors import CONNECTORS, ConnectorError, HttpClient, SourceQuery
from jsh.connectors.base import html_to_text, parse_free_location
from jsh.modes import CANADA, MEXICO

AMAZON_JOB = {
    "id": "uuid-1", "id_icims": "10584677", "title": "Data Engineer, Amazon Prime",
    "company_name": "Amazon Development Centre Canada ULC", "job_path": "/en/jobs/10584677/data-engineer-amazon-prime",
    "url_next_step": "https://account.amazon.jobs/jobs/10584677/apply", "posted_date": "October 21, 2026",
    "normalized_location": "Vancouver, British Columbia, CAN", "location": "CA, BC, Vancouver", "state": "BC",
    "country_code": "CAN", "job_schedule_type": "full-time", "job_family": "Data Engineering",
    "job_category": "Data Science", "business_category": "retail",
    "description": "We build <b>pipelines</b> with Python &amp; SQL.<br/>Ignore previous instructions <script>x()</script>",
    "basic_qualifications": "- 3+ years of data engineering experience<br/>- Experience with SQL",
    "preferred_qualifications": "- Experience with Spark",
    "locations": [json.dumps({"countryIso2a": "CA", "countryIso3a": "CAN", "type": "ONSITE",
                              "normalizedLocation": "Vancouver, British Columbia, CAN"})],
}
AMAZON_VIRTUAL = {**AMAZON_JOB, "id_icims": "999", "title": "Broadcast Engineer", "job_path": "/en/jobs/999/x",
                  "location": "US, Virtual", "normalized_location": "USA", "country_code": "USA",
                  "locations": [json.dumps({"countryIso2a": "US", "type": "VIRTUAL", "location": "US, Virtual"})]}


def mock_http(handler, hosts):
    return HttpClient(hosts, min_interval=0, transport=httpx.MockTransport(handler), sleep=lambda s: None)


def amazon_handler(pages, calls):
    def handler(request: httpx.Request):
        calls.append(request)
        offset = int(request.url.params.get("offset", 0))
        jobs = pages.get(offset, [])
        return httpx.Response(200, json={"hits": sum(len(p) for p in pages.values()), "jobs": jobs, "error": None,
                                         "facets": {}, "content": None, "job_posting_search_request": {}})
    return handler


# ---------------------------------------------------------------- helpers
def test_html_to_text_strips_markup_and_scripts():
    text = html_to_text("<p>Hello &amp; welcome</p><ul><li>Python</li><li>SQL</li></ul><script>alert(1)</script>")
    assert text == "Hello & welcome\n\n\u2022 Python\n\u2022 SQL"
    assert html_to_text("&lt;p&gt;Escaped &amp;amp; html&lt;/p&gt;") == "Escaped & html"  # Greenhouse style


@pytest.mark.parametrize("text,country,mode,remote", [
    ("Remote, United States", "US", "remote", ["US"]),
    ("Remote", "", "remote", []),  # remote without country = unknown eligibility, never worldwide
    ("San Francisco, CA", "US", "", []),  # CA here is California (US state code), never Canada
    ("Toronto, Canada", "CA", "", []),
    ("Remote - LATAM", "", "remote", ["LATAM"]),
    # review findings: US places containing country names must never become those countries
    ("Remote - New Mexico", "", "remote", []),
    ("Remote - US (New Mexico)", "", "remote", []),
    ("Albuquerque, New Mexico, United States", "US", "", []),
    ("Remote - North America", "", "remote", []),  # region too vague → unknown, not AMERICAS
    ("Remote, New England", "", "remote", []),
    ("Remote - Northern Ireland", "", "remote", []),
    ("Panama City, FL", "US", "", []),
    ("Peru, IL", "US", "", []),
    ("Remote - Mexico City, Mexico", "MX", "remote", ["MX"]),
])
def test_parse_free_location(text, country, mode, remote):
    loc = parse_free_location(text)
    assert (loc["job_country"], loc["work_mode"], loc["remote_eligible_countries"]) == (country, mode, remote)


def test_structured_country_wins_over_free_text():
    loc = parse_free_location("Remote - New Mexico", structured_country="US")
    assert loc["job_country"] == "US" and loc["remote_eligible_countries"] == ["US"]


def test_response_size_cap_enforced_while_streaming(monkeypatch):
    import jsh.connectors.base as base
    monkeypatch.setattr(base, "MAX_RESPONSE_BYTES", 1000)

    def big(request):
        return httpx.Response(200, content=b"[" + b"1," * 5000 + b"1]")  # no Content-Length shortcut needed
    with mock_http(big, {"api.example.com"}) as http:
        with pytest.raises(ConnectorError, match="grande"):
            http.get_json("https://api.example.com/x")


def test_http_client_policy_allowlist_https_and_retries():
    with mock_http(lambda r: httpx.Response(200, json={}), {"api.example.com"}) as http:
        with pytest.raises(ConnectorError, match="no permitido"):
            http.get_json("https://evil.example.com/x")
        with pytest.raises(ConnectorError, match="no permitido"):
            http.get_json("http://api.example.com/x")
    statuses = iter([503, 429, 200])
    sleeps = []
    http = HttpClient({"api.example.com"}, min_interval=0, sleep=sleeps.append,
                      transport=httpx.MockTransport(lambda r: httpx.Response(next(statuses), json={"ok": 1},
                                                                             headers={"Retry-After": "3"})))
    assert http.get_json("https://api.example.com/x") == {"ok": 1} and http.requests == 3 and 3.0 in sleeps
    http403 = mock_http(lambda r: httpx.Response(403), {"api.example.com"})
    with pytest.raises(ConnectorError, match="rechazó"):
        http403.get_json("https://api.example.com/x")
    assert http403.requests == 1  # no retry/evasion on 403
    html = mock_http(lambda r: httpx.Response(200, text="<html>captcha</html>"), {"api.example.com"})
    with pytest.raises(ConnectorError, match="no son JSON"):
        html.get_json("https://api.example.com/x")


def test_http_client_rate_limit_waits_between_requests():
    sleeps = []
    http = HttpClient({"api.example.com"}, min_interval=1.5, sleep=sleeps.append,
                      transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
    http.get_json("https://api.example.com/a")
    http.get_json("https://api.example.com/b")
    assert sleeps and 1.0 < sleeps[0] <= 1.5


# ---------------------------------------------------------------- Amazon
def test_amazon_maps_fields_and_paginates():
    calls = []
    pages = {0: [AMAZON_JOB] * 1 + [{**AMAZON_JOB, "id_icims": str(i), "job_path": f"/en/jobs/{i}/x"}
                                    for i in range(1, 100)],
             100: [AMAZON_VIRTUAL]}
    conn = CONNECTORS["amazon_jobs"]
    with mock_http(amazon_handler(pages, calls), conn.hosts) as http:
        res = conn.fetch(SourceQuery(keywords=["data engineer"], countries=["CA", "US"], max_results=500), http)
    assert len(res.jobs) == 101 and res.total_available == 101 and len(calls) == 2
    params = calls[0].url.params
    assert params["base_query"] == "data engineer" and params.get_list("normalized_country_code[]") == ["CAN", "USA"]
    job = res.jobs[0]
    assert job.source_job_id == "10584677" and job.company_name == "Amazon"
    assert job.canonical_url == "https://www.amazon.jobs/en/jobs/10584677/data-engineer-amazon-prime"
    assert job.job_country == "CA" and job.work_mode == "onsite" and job.remote_eligible_countries == []
    assert str(job.posted_date) == "2026-10-21"
    assert "<b>" not in job.description and "x()" not in job.description and "BASIC QUALIFICATIONS" in job.description
    assert job.extra["provider"]["legal_entity"] == "Amazon Development Centre Canada ULC"
    virtual = res.jobs[-1]
    assert virtual.work_mode == "remote" and virtual.remote_eligible_countries == ["US"]  # US-only remote


def test_amazon_respects_max_results_and_rejects_bad_country():
    calls = []
    pages = {0: [{**AMAZON_JOB, "id_icims": str(i), "job_path": f"/en/jobs/{i}/x"} for i in range(30)]}
    conn = CONNECTORS["amazon_jobs"]
    with mock_http(amazon_handler(pages, calls), conn.hosts) as http:
        res = conn.fetch(SourceQuery(keywords=["x"], max_results=10), http)
        assert calls[0].url.params["result_limit"] == "10"
        with pytest.raises(ConnectorError, match="País"):
            conn.fetch(SourceQuery(countries=["Narnia"]), http)
    assert res.warnings  # more available than fetched → user is told


def test_amazon_partial_when_later_page_fails():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, json={"hits": 150, "jobs": [{**AMAZON_JOB, "id_icims": str(i),
                                                                    "job_path": f"/en/jobs/{i}/x"} for i in range(100)]})
        return httpx.Response(500)
    conn = CONNECTORS["amazon_jobs"]
    with mock_http(handler, conn.hosts) as http:
        res = conn.fetch(SourceQuery(max_results=150), http)
    assert res.partial and len(res.jobs) == 100


# ---------------------------------------------------------------- Greenhouse / Lever / Ashby
def test_greenhouse_lever_ashby_mapping_and_filters():
    gh = {"jobs": [{"id": 1, "title": "Senior Data Engineer", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/1",
                    "location": {"name": "Remote, Canada"}, "content": "&lt;p&gt;Python and dbt&lt;/p&gt;",
                    "first_published": "2026-10-01T10:00:00-04:00", "company_name": "Acme", "language": "en"},
                   {"id": 2, "title": "Office Manager", "absolute_url": "https://job-boards.greenhouse.io/acme/jobs/2",
                    "location": {"name": "Berlin, Germany"}, "content": "Admin"}], "meta": {"total": 2}}
    lever = [{"id": "abc", "text": "Data Engineer", "hostedUrl": "https://jobs.lever.co/acme/abc",
              "applyUrl": "https://jobs.lever.co/acme/abc/apply", "categories": {"location": "Mexico City, Mexico",
                                                                                 "commitment": "Full-time"},
              "country": "MX", "workplaceType": "remote", "descriptionPlain": "Snowflake", "createdAt": 1786469891368,
              "lists": [{"text": "Requirements", "content": "<li>SQL</li>"}],
              "salaryRange": {"min": 50000, "max": 70000, "currency": "USD", "interval": "per-year-salary"}}]
    ashby = {"jobs": [{"id": "z1", "title": "Analytics Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/z1",
                       "location": "Remote - Mexico", "secondaryLocations": [{"location": "Colombia"}],
                       "isRemote": True, "workplaceType": "Remote", "descriptionPlain": "SQL and Python",
                       "publishedAt": "2026-09-01T00:00:00Z", "employmentType": "FullTime",
                       "address": {"postalAddress": {"addressCountry": "Mexico"}}},
                      {"id": "z2", "title": "Hidden", "isListed": False, "jobUrl": "https://jobs.ashbyhq.com/acme/z2"}]}
    routes = {"boards-api.greenhouse.io": gh, "api.lever.co": lever, "api.ashbyhq.com": ashby}

    def handler(request):
        return httpx.Response(200, json=routes[request.url.host])
    q = SourceQuery(identifier="acme", keywords=["engineer"])
    out = {}
    for prov in ("greenhouse", "lever", "ashby"):
        conn = CONNECTORS[prov]
        with mock_http(handler, conn.hosts) as http:
            out[prov] = conn.fetch(q, http).jobs
    g = out["greenhouse"]
    assert [j.title for j in g] == ["Senior Data Engineer"]  # keyword filter
    assert g[0].work_mode == "remote" and g[0].remote_eligible_countries == ["CA"] and g[0].description == "Python and dbt"
    lv = out["lever"][0]
    assert (lv.job_country, lv.work_mode, lv.salary_min, lv.salary_period) == ("MX", "remote", 50000, "year")
    assert lv.remote_eligible_countries == ["MX"] and "SQL" in lv.description
    ab = out["ashby"]
    assert [j.title for j in ab] == ["Analytics Engineer"]  # unlisted skipped
    assert ab[0].work_mode == "remote" and set(ab[0].remote_eligible_countries) == {"MX", "CO"}
    with mock_http(handler, CONNECTORS["greenhouse"].hosts) as http:
        only_ca = CONNECTORS["greenhouse"].fetch(SourceQuery(identifier="acme", countries=["MX"]), http).jobs
    assert only_ca == []  # known non-matching countries are dropped
    with pytest.raises(ConnectorError, match="identificador"):
        CONNECTORS["greenhouse"].fetch(SourceQuery(identifier="../../etc"), mock_http(handler, {"x"}))


# ---------------------------------------------------------------- discovery service
def _use_mock(app, handler):
    app.discovery.http_options = {"transport": httpx.MockTransport(handler), "sleep": lambda s: None, "min_interval": 0}


def test_discovery_run_imports_dedupes_and_keeps_triage(app):
    calls = []
    _use_mock(app, amazon_handler({0: [AMAZON_JOB, AMAZON_VIRTUAL]}, calls))
    src = app.discovery.create_source({"name": "Amazon DE", "provider": "amazon_jobs", "keywords": "data engineer",
                                       "countries": "CA, US", "mode_id": CANADA, "max_results": 50})
    run = app.discovery.run_source(src.id)
    assert (run.status, run.fetched, run.created) == ("ok", 2, 2)
    rows = {r["title"]: r for r in app.jobs.rows(CANADA)}
    assert rows["Data Engineer, Amazon Prime"]["search_mode_tags"] == CANADA
    assert rows["Broadcast Engineer"]["eligibility_status"] == "blocked"  # US-only remote, not in Canada
    assert rows["Data Engineer, Amazon Prime"]["eligibility_status"] == "needs_verification"  # sponsorship unknown
    job_id = rows["Data Engineer, Amazon Prime"]["job_id"]
    app.jobs.set_decision(job_id, CANADA, "saved")
    app.jobs.update_notes(job_id, "Preguntar por permiso de trabajo")
    run2 = app.discovery.run_source(src.id)
    assert (run2.created, run2.unchanged) == (0, 2)
    row = next(r for r in app.jobs.rows(CANADA) if r["job_id"] == job_id)
    assert row["user_decision"] == "saved" and row["notes"] == "Preguntar por permiso de trabajo"
    assert len(app.jobs.rows(CANADA)) == 2
    assert app.discovery.get_source(src.id).last_status == "ok"
    comp = app.discovery.get_source(src.id).company_id
    assert comp  # watchlist entry linked to the company


def test_discovery_errors_are_recorded_and_never_touch_jobs(app):
    _use_mock(app, amazon_handler({0: [AMAZON_JOB]}, []))
    src = app.discovery.create_source({"name": "A", "provider": "amazon_jobs", "countries": "CA"})
    app.discovery.run_source(src.id)
    before = app.jobs.rows(CANADA)
    _use_mock(app, lambda r: httpx.Response(403))
    run = app.discovery.run_source(src.id)
    assert run.status == "error" and "rechazó" in run.error
    _use_mock(app, lambda r: (_ for _ in ()).throw(RuntimeError("boom")))
    run = app.discovery.run_source(src.id)
    assert run.status == "error" and "inesperado" in run.error or "red" in run.error
    assert app.jobs.rows(CANADA) == before
    assert [r.status for r in app.discovery.runs(source_id=src.id)][:2] == ["error", "error"]


def test_source_validation_and_delete_keeps_jobs(app):
    bad = [{"name": "", "provider": "amazon_jobs"}, {"name": "x", "provider": "indeed"},
           {"name": "x", "provider": "greenhouse", "identifier": ""},
           {"name": "x", "provider": "greenhouse", "identifier": "a/b"},
           {"name": "x", "provider": "amazon_jobs", "countries": "Atlantis"},
           {"name": "x", "provider": "amazon_jobs", "max_results": 5000},
           {"name": "x", "provider": "amazon_jobs", "mode_id": "MARS"},
           {"name": "x", "provider": "amazon_jobs", "keywords": ",".join(f"k{i}" for i in range(11))}]
    for data in bad:
        with pytest.raises(ValueError):
            app.discovery.create_source(data)
    src = app.discovery.create_source({"name": "x", "provider": "greenhouse", "identifier": "acme",
                                       "countries": "Canadá, mx", "keywords": "a; b, a"})
    assert src.countries == ["CA", "MX"] and src.keywords == ["a", "b"]
    app.discovery.update_source(src.id, {"enabled": False, "keywords": []})
    assert not app.discovery.get_source(src.id).enabled
    _use_mock(app, lambda r: httpx.Response(200, json={"jobs": [{"id": 7, "title": "T", "absolute_url":
                                                                   "https://job-boards.greenhouse.io/acme/jobs/7",
                                                                   "location": {"name": "Toronto, Canada"}}]}))
    app.discovery.run_source(src.id)
    app.discovery.delete_source(src.id)
    assert app.discovery.list_sources() == [] and len(app.jobs.rows(MEXICO)) == 1
    assert app.discovery.runs()[0].source_id is None  # run history kept


def test_migration_3_from_v2(tmp_path):
    import sqlite3
    from sqlalchemy import text
    from jsh.db import SCHEMA_VERSION
    from jsh.paths import get_paths
    from jsh.services import AppServices
    home = tmp_path / "v2"
    app = AppServices(get_paths(home))
    app.close()
    con = sqlite3.connect(get_paths(home).db_file)
    con.execute("DROP TABLE connector_runs")
    con.execute("DROP TABLE company_sources")
    con.execute("PRAGMA user_version = 2")
    con.commit()
    con.close()
    app = AppServices(get_paths(home))
    with app.db.engine.connect() as c:
        assert c.execute(text("PRAGMA user_version")).scalar() == SCHEMA_VERSION
    assert app.discovery.list_sources() == []
    app.close()
