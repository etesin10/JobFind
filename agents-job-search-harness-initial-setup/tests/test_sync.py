"""External-agent integration: WAL concurrency, job_inbox contract, direct-edit repair/re-evaluation, UI banner."""
import json
import logging
import sqlite3
import threading
import time

import pytest
from nicegui.testing import user_simulation

from jsh.db import backup_database
from jsh.modes import CANADA, MEXICO
from jsh.services import JobInput
from jsh.ui.app import MainPage, configure, index


def agent_db(app) -> sqlite3.Connection:
    """A plain sqlite3 connection, exactly like an external agent would open (no app code)."""
    return sqlite3.connect(app.paths.db_file, timeout=30, check_same_thread=False)


def push(con, payload, source=None) -> int:
    body = payload if isinstance(payload, str) else json.dumps(payload)
    if source:
        cur = con.execute("INSERT INTO job_inbox(payload, source) VALUES (?, ?)", (body, source))
    else:
        cur = con.execute("INSERT INTO job_inbox(payload) VALUES (?)", (body,))
    con.commit()
    return cur.lastrowid


def inbox(con, rid):
    return con.execute("SELECT status, result, job_id, message FROM job_inbox WHERE id = ?", (rid,)).fetchone()


REMOTE_MX = {"title": "BI Developer", "company_name": "Globant", "canonical_url": "https://globant.example/j/9",
             "source": "agent-linkedin", "source_job_id": "9", "work_mode": "remote",
             "remote_eligible_countries": ["MX"], "description": "Power BI, SQL and Python. Remote from Mexico.",
             "modes": [MEXICO], "agent": {"score": 82, "recommendation": "apply", "summary": "Buen match"}}


# --------------------------------------------------------------------------- storage / concurrency
def test_wal_mode_and_standalone_backups(app):
    con = agent_db(app)
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert app.db.journal_mode == "wal"
    b = backup_database(app.paths.db_file, app.paths.backups, "t")
    bcon = sqlite3.connect(b)
    assert bcon.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # single self-contained file
    bcon.close()
    con.close()


def test_app_reads_and_writes_while_agent_holds_write_lock(app):
    job, _ = app.jobs.upsert_job(JobInput(title="Data Engineer", canonical_url="https://x.example/1"))
    con = agent_db(app)
    con.execute("UPDATE jobs SET seniority = 'senior' WHERE id = ?", (job.id,))  # open write txn
    t0 = time.time()
    assert len(app.jobs.rows(CANADA)) == 1  # WAL: readers never wait for the writer
    assert time.time() - t0 < 2
    threading.Thread(target=lambda: (time.sleep(1.0), con.commit()), daemon=True).start()
    app.jobs.update_notes(job.id, "mientras el agente escribe")  # waits for the lock (busy_timeout)
    assert app.jobs.rows(CANADA)[0]["notes"] == "mientras el agente escribe"
    con.close()


def test_concurrent_agent_writer_and_user_actions(app):
    job, _ = app.jobs.upsert_job(JobInput(title="Seed", canonical_url="https://x.example/seed"))
    errors: list[str] = []

    def agent():
        con = agent_db(app)
        try:
            for i in range(60):
                push(con, {"title": f"Job {i}", "company_name": "Co", "canonical_url": f"https://c.example/{i}"})
        except Exception as exc:  # pragma: no cover - the assertion below reports it
            errors.append(repr(exc))
        finally:
            con.close()

    th = threading.Thread(target=agent)
    th.start()
    n = 0
    while th.is_alive() or n < 3:
        app.sync.tick()
        app.jobs.set_decision(job.id, CANADA, "saved" if n % 2 else "maybe")
        n += 1
    th.join()
    app.sync.tick()
    assert not errors
    con = agent_db(app)
    assert con.execute("SELECT count(*) FROM job_inbox WHERE status = 'imported'").fetchone()[0] == 60
    con.close()
    assert len(app.jobs.rows(CANADA)) == 61


# --------------------------------------------------------------------------- inbox contract
def test_inbox_creates_dedupes_and_records_outcome(app):
    con = agent_db(app)
    rid = push(con, {**REMOTE_MX, "foo": 1}, source="claude")
    summary = app.sync.tick()
    assert summary["inbox"]["created"] == 1
    status, result, job_id, message = inbox(con, rid)
    assert (status, result) == ("imported", "created") and job_id
    assert "foo" in message  # unknown keys are reported, not lost
    row = app.jobs.rows(MEXICO)[0]
    assert row["agent_score"] == 82 and row["agent_recommendation"] == "apply"
    assert row["eligibility_status"] == "eligible" and row["search_mode_tags"] == MEXICO
    detail = app.jobs.detail(job_id, MEXICO)
    assert detail["job"].extra["agent_fields"] == {"foo": 1}
    assert detail["job"].extra["agent"]["source"] == "claude"
    assert [s.provider for s in detail["sources"]] == ["agent-linkedin"]
    # the agent re-sends the same job: deduplicated
    rid2 = push(con, REMOTE_MX)
    app.sync.tick()
    assert inbox(con, rid2)[:3] == ("imported", "unchanged", job_id)
    assert len(app.jobs.rows(MEXICO)) == 1
    hist = [h for h in app.jobs.history() if h["action"] == "job_created"]
    assert hist and hist[0]["actor"] == "agent:claude"
    con.close()


def test_inbox_accepts_real_world_formats_and_drops_bad_optional_fields(app):
    """Found in the real-life test: amazon.jobs dates look like 'October 29, 2026'."""
    con = agent_db(app)
    rid = push(con, {"title": "Data Engineer", "canonical_url": "https://www.amazon.jobs/en/jobs/1",
                     "posted_date": "October  8, 2026", "salary_min": "95k", "salary_max": "mucho",
                     "apply_url": "javascript:alert(1)"})
    app.sync.tick()
    status, result, job_id, message = inbox(con, rid)
    assert (status, result) == ("imported", "created")
    assert "salary_max" in message and "apply_url" in message and "posted_date" not in message
    row = app.jobs.rows(CANADA)[0]
    assert (row["posted_date"], row["salary_min"], row["salary_max"], row["apply_url"]) == \
        ("2026-10-08", 95000.0, None, "")
    con.close()


@pytest.mark.parametrize("payload, fragment", [
    ("not json", "no es JSON"),
    ("[1, 2]", "objeto JSON"),
    (json.dumps({"company_name": "NoTitle"}), "title"),
    (json.dumps({"title": "X" * 400}), "title"),
])
def test_inbox_rejects_invalid_rows_without_stopping(app, payload, fragment):
    con = agent_db(app)
    bad = push(con, payload)
    good = push(con, {"title": "Valid", "canonical_url": "https://v.example/1"})
    app.sync.tick()
    status, _, _, message = inbox(con, bad)
    assert status == "error" and fragment in message
    assert inbox(con, good)[0] == "imported"
    stats = app.sync.inbox_stats()
    assert stats["error"] == 1 and stats["errors"][0]["id"] == bad
    assert app.sync.retry_errors() == 1
    assert con.execute("SELECT status FROM job_inbox WHERE id = ?", (bad,)).fetchone()[0] == "pending"
    con.close()


def test_inbox_ad_status_mode_data_fill_only_and_agent_rule(app):
    job, _ = app.jobs.upsert_job(JobInput(title="Analyst CA", canonical_url="https://ca.example/1", job_country="CA"))
    app.jobs.update_mode_data(job.id, CANADA, {"employer_visa_support": "no"})  # set by the user
    con = agent_db(app)
    rid = push(con, {"title": "Analyst CA", "canonical_url": "https://ca.example/1", "ad_status": "expired",
                     "mode_data": {CANADA: {"employer_visa_support": "yes", "lmia_evidence": "LMIA approved 2025"}},
                     "agent": {"score": 140}})
    app.sync.tick()
    status, result, _, message = inbox(con, rid)
    assert (status, result) == ("imported", "updated")
    assert "agent.score" in message and "employer_visa_support" in message
    d = app.jobs.detail(job.id, CANADA)
    assert d["job"].ad_status == "expired"
    assert d["evaluation"].mode_data["employer_visa_support"] == "no"  # user value kept
    assert d["evaluation"].mode_data["lmia_evidence"] == "LMIA approved 2025"  # empty value filled
    assert "score" not in d["job"].extra["agent"]
    # agent_score is a rule field like any other
    app.rules.create(CANADA, {"name": "Agente recomienda", "field": "agent_score", "operator": "gte", "value": 70,
                              "value_type": "number", "scope": "rank", "action": "weight", "weight": 10,
                              "severity": "soft", "unknown_handling": "keep"})
    push(con, {"title": "Analyst CA", "canonical_url": "https://ca.example/1", "agent": {"score": 90}})
    app.sync.tick()
    out = [o for o in app.jobs.detail(job.id, CANADA)["evaluation"].explanation if o["name"] == "Agente recomienda"]
    assert out and out[0]["effect"] == "weight"
    con.close()


# --------------------------------------------------------------------------- direct edits
def test_direct_malformed_edits_never_break_the_ui_and_get_repaired(app):
    job, _ = app.jobs.upsert_job(JobInput(title="Data Engineer", canonical_url="https://x.example/1",
                                          work_mode="remote", remote_eligible_countries=["CA"]))
    app.sync.tick()
    con = agent_db(app)
    con.execute("UPDATE jobs SET remote_eligible_countries = 'MX, LATAM', posted_date = 'Oct 29 2026', "
                "salary_min = '50k', title = 123, extra = 'nota', created_at = 'ayer', pinned = 'yes', "
                "work_mode = 'Remote', job_country = 'Mexico' WHERE id = ?", (job.id,))
    con.commit()
    row = app.jobs.rows(MEXICO)[0]  # lenient reads: no crash even before the repair pass
    assert row["salary_min"] == 50000.0 and row["posted_date"] == "2026-10-29"
    summary = app.sync.tick()
    assert summary["repaired"] == 1 and summary["external_jobs"] == 1
    raw = con.execute("SELECT remote_eligible_countries, posted_date, salary_min, typeof(title), extra, pinned, "
                      "work_mode, job_country FROM jobs WHERE id = ?", (job.id,)).fetchone()
    assert json.loads(raw[0]) == ["MX", "LATAM"]
    assert raw[1:4] == ("2026-10-29", 50000.0, "text")
    assert json.loads(raw[4]) == {"external_raw": "nota"} and raw[5] == 1
    assert raw[6:] == ("remote", "MX")
    row = app.jobs.rows(MEXICO)[0]
    assert row["eligibility_status"] == "eligible"  # re-evaluated with the repaired facts
    assert any(h["action"] == "job_repaired" for h in app.jobs.history())
    con.close()


def test_direct_description_update_is_reevaluated_snapshotted_and_versioned(app):
    job, _ = app.jobs.upsert_job(JobInput(title="Remote Eng", canonical_url="https://x.example/2", work_mode="remote",
                                          remote_eligible_countries=["MX"], description="Python"))
    assert app.sync.tick()["external_jobs"] == 0  # own writes are not 'external'
    before = app.jobs.rows(MEXICO)[0]
    con = agent_db(app)
    con.execute("UPDATE jobs SET remote_eligible_countries = '[\"US\"]', description = 'Python, US only' "
                "WHERE id = ?", (job.id,))
    con.commit()
    seq = app.sync.seq
    assert app.sync.tick()["external_jobs"] == 1
    assert app.sync.seq == seq + 1 and app.sync.changes_since(seq)["external_jobs"] == 1
    after = app.jobs.rows(MEXICO)[0]
    assert before["eligibility_status"] == "eligible" and after["eligibility_status"] == "blocked"
    assert after["row_version"] == before["row_version"] + 1
    assert len(app.jobs.detail(job.id, MEXICO)["snapshots"]) == 2
    assert app.sync.tick()["external_jobs"] == 0  # stable afterwards
    con.close()


def test_direct_full_insert_is_evaluated(app):
    con = agent_db(app)
    cols = {"id": "ext000000000000000000000000000001", "company_name": "Shopify", "title": "Data Analyst",
            "job_country": "CA", "region": "", "location_text": "Toronto", "remote_eligible_countries": "[]",
            "work_mode": "onsite", "seniority": "", "employment_type": "", "currency": "", "salary_period": "",
            "canonical_url": "https://shopify.example/1", "apply_url": "", "ad_status": "active",
            "sponsorship_statement": "", "language": "", "description": "SQL", "notes": "", "mode_tags": "[]",
            "pinned": 0, "extra": "{}", "row_version": 1, "created_at": "2026-10-29T10:00:00Z",
            "updated_at": "2026-10-29 10:00:00"}
    con.execute(f"INSERT INTO jobs ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", list(cols.values()))
    con.commit()
    summary = app.sync.tick()
    assert summary["external_jobs"] == 1
    row = app.jobs.rows(CANADA)[0]
    assert row["eligibility_status"] != "unevaluated" and row["company_id"]
    assert con.execute("SELECT count(*) FROM v_job_status WHERE job_id = ?", (cols["id"],)).fetchone()[0] == 2
    con.close()


def test_out_of_range_values_do_not_stall_reconciliation(app):
    a, _ = app.jobs.upsert_job(JobInput(title="A", canonical_url="https://x.example/a"))
    b, _ = app.jobs.upsert_job(JobInput(title="B", canonical_url="https://x.example/b", work_mode="remote",
                                        remote_eligible_countries=["MX"]))
    app.sync.tick()
    con = agent_db(app)
    con.execute("UPDATE jobs SET posted_date = '9999-12-31T23:00:00-05:00', row_version = 9e999, "
                "last_checked = '0001-01-01T00:00:00+01:00', salary_min = 'nan' WHERE id = ?", (a.id,))
    con.execute("UPDATE jobs SET ad_status = 'expired', remote_eligible_countries = '[\"US\"]' WHERE id = ?", (b.id,))
    con.commit()
    s = app.sync.tick()
    assert s is not None and s["repaired"] == 1 and s["external_jobs"] == 1 and app.sync.last_error == ""
    rows = {r["title"]: r for r in app.jobs.rows(MEXICO)}
    assert rows["B"]["eligibility_status"] == "blocked" and rows["B"]["ad_status"] == "expired"
    raw = con.execute("SELECT posted_date, row_version, last_checked, salary_min FROM jobs WHERE id = ?",
                      (a.id,)).fetchone()
    assert raw == (None, 1, None, None)  # unusable values cleared (facts unchanged → no extra bump)
    con.close()


def test_own_writes_racing_with_a_scan_are_not_external(app, monkeypatch):
    job, _ = app.jobs.upsert_job(JobInput(title="Race", canonical_url="https://x.example/race"))
    app.sync.tick()
    version = app.jobs.rows(CANADA)[0]["row_version"]
    real = type(app.sync)._stale
    calls = {"n": 0}

    def inconsistent_first_read(self, s, ids):
        calls["n"] += 1
        if calls["n"] == 1:  # the unlocked pass saw a torn snapshot (job read before, evals after a commit)
            j = s.get(type(job), job.id)
            return [(j, [CANADA], False, True)]
        return real(self, s, ids)
    monkeypatch.setattr(type(app.sync), "_stale", inconsistent_first_read)
    s = app.sync.tick(force=True)
    assert s["external_jobs"] == 0 and s["reevaluated"] == 0  # the locked re-check is authoritative
    assert app.jobs.rows(CANADA)[0]["row_version"] == version
    assert not [h for h in app.jobs.history() if h["action"] == "job_external_update"]


def test_non_http_urls_written_directly_are_neutralised(app):
    from jsh.ui.app import _safe_url
    job, _ = app.jobs.upsert_job(JobInput(title="XSS", canonical_url="https://x.example/x"))
    con = agent_db(app)
    con.execute("UPDATE jobs SET canonical_url = 'javascript:alert(1)', apply_url = ' JavaScript:alert(2)' "
                "WHERE id = ?", (job.id,))
    con.commit()
    assert _safe_url("javascript:alert(1)") == "" and _safe_url("https://ok.example") == "https://ok.example"
    app.sync.tick()
    assert con.execute("SELECT canonical_url, apply_url FROM jobs WHERE id = ?", (job.id,)).fetchone() == ("", "")
    con.close()


def test_view_exposes_user_decisions_to_the_agent(app):
    job, _ = app.jobs.upsert_job(JobInput(title="Not for me", canonical_url="https://x.example/3"))
    app.jobs.set_decision(job.id, MEXICO, "discarded", "salary", "muy bajo")
    con = agent_db(app)
    row = con.execute("SELECT user_decision, discard_reason, discard_note FROM v_job_status "
                      "WHERE job_id = ? AND mode_id = ?", (job.id, MEXICO)).fetchone()
    assert row == ("discarded", "salary", "muy bajo")
    con.close()


def test_idle_tick_is_cheap_and_legacy_hashes_are_silent(app):
    job, _ = app.jobs.upsert_job(JobInput(title="Old", canonical_url="https://x.example/4"))
    app.sync.tick()
    con = agent_db(app)
    con.execute("UPDATE job_mode_evaluations SET facts_hash = ''")  # as after upgrading from schema v3
    con.commit()
    s = app.sync.tick()
    assert s["reevaluated"] == 2 and s["external_jobs"] == 0
    assert app.sync.tick()["reevaluated"] == 0
    con.close()


def test_search_is_accent_insensitive_multi_word_and_covers_the_jd(app):
    app.jobs.upsert_job(JobInput(title="Analista de Datos", canonical_url="https://x.example/5",
                                 description="Experiencia con Snowflake y dbt"))
    app.jobs.upsert_job(JobInput(title="Data Analyst", canonical_url="https://x.example/6", description="Excel"))
    assert [r["title"] for r in app.jobs.rows(CANADA, search="snowflake")] == ["Analista de Datos"]
    assert [r["title"] for r in app.jobs.rows(CANADA, search="ANALISTA dbt")] == ["Analista de Datos"]
    assert app.jobs.rows(CANADA, search='"data analyst"')[0]["title"] == "Data Analyst"
    assert app.jobs.rows(CANADA, search="analista excel") == []


def test_user_guide_contract_examples_are_valid(app):
    """The SQL and Python examples in INSTRUCCIONES.md §5c must work verbatim for an agent."""
    import re
    from pathlib import Path
    guide = (Path(__file__).resolve().parents[1] / "INSTRUCCIONES.md").read_text(encoding="utf-8")
    section = guide[guide.index("## 5c."):guide.index("## 6.")]
    sql = re.search(r"```sql\n(INSERT INTO job_inbox.*?)\n```", section, re.S).group(1)
    py = re.search(r"```python\n(.*?)\n```", section, re.S).group(1)
    py = re.sub(r'sqlite3\.connect\(r".*?"', lambda _: f'sqlite3.connect(r"{app.paths.db_file}"', py)
    con = agent_db(app)
    con.executescript(sql)
    con.close()
    exec(compile(py, "guide_example", "exec"), {})
    app.sync.tick()
    con = agent_db(app)
    rows = con.execute("SELECT status, result, message FROM job_inbox ORDER BY id").fetchall()
    con.close()
    assert [r[:2] for r in rows] == [("imported", "created"), ("imported", "created")], rows
    assert all(not r[2] for r in rows)  # no warnings either
    by_title = {r["title"]: r for r in app.jobs.rows(MEXICO)}
    assert by_title["BI Developer"]["agent_score"] == 82
    assert by_title["BI Developer"]["eligibility_status"] == "eligible"
    reads = re.findall(r"```sql\n(SELECT .*?)\n```", section, re.S)
    con = agent_db(app)
    for q in reads:
        con.execute(q).fetchall()
    con.close()


def test_sync_cli(tmp_path, capsys):
    from jsh.main import main
    from jsh.paths import get_paths
    from jsh.services import AppServices
    home = tmp_path / "cli"
    svc = AppServices(get_paths(home))
    con = agent_db(svc)
    push(con, REMOTE_MX)
    push(con, "oops")
    con.close()
    svc.close()
    with pytest.raises(SystemExit) as exc:
        main(["--home", str(home), "--sync"])
    out = capsys.readouterr().out
    assert exc.value.code == 1  # one rejected row
    assert "1 nuevas" in out and "1 rechazadas" in out and "no es JSON" in out


# --------------------------------------------------------------------------- UI
@pytest.fixture
def quiet_errors(caplog):
    caplog.set_level(logging.WARNING)
    yield
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_ui_banner_shows_agent_changes_and_refreshes(app, quiet_errors, monkeypatch):
    monkeypatch.setattr(MainPage, "POLL_S", 0.2)
    configure(app)
    app.current_mode = MEXICO
    async with user_simulation(root=index) as user:
        await user.open("/")
        await user.should_not_see("BI Developer")
        con = agent_db(app)
        push(con, REMOTE_MX)
        con.close()
        app.sync.tick()  # what the background loop does every few seconds
        await user.should_see("1 vacante(s) nueva(s)", retries=20)
        user.find(marker="sync_apply").click()
        await user.should_see("BI Developer")
        job_id = app.jobs.rows(MEXICO)[0]["job_id"]
        await user.should_see(marker=f"agent_badge_{job_id}")
        user.find(marker=f"detail_{job_id}").click()
        await user.should_see("Evaluación del agente externo")
        await user.should_see("Recomendación: apply")


async def test_ui_sync_button_and_rejected_rows_listed(app, quiet_errors):
    configure(app)
    app.current_mode = CANADA
    con = agent_db(app)
    push(con, "{bad")
    con.close()
    async with user_simulation(root=index) as user:
        await user.open("/")
        user.find(marker="sync_now").click()
        await user.should_see("1 rechazadas", retries=20)
        await user.should_see(marker="inbox_errors")
        await user.should_see("Reintentar rechazadas")
