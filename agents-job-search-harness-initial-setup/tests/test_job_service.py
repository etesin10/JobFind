import shutil

import pytest

from jsh.modes import CANADA, MEXICO
from jsh.paths import get_paths
from jsh.services import AppServices, JobInput, RuleValidationError


def _job(**kw):
    base = dict(title="Senior Data Engineer", company_name="Acme", canonical_url="https://boards.example.com/acme/1",
                description="We need SQL, Azure Data Factory and Snowflake.", source="manual")
    base.update(kw)
    return JobInput(**base)


def test_seeded_modes_and_rules(app):
    assert {r.mode_id for r in app.rules.list(CANADA)} == {CANADA}
    assert len(app.rules.list(MEXICO)) >= 3


def test_upsert_is_idempotent_and_preserves_user_data(app):
    job, status = app.jobs.upsert_job(_job(), CANADA)
    assert status == "created"
    app.jobs.update_notes(job.id, "Hablé con el reclutador")
    app.jobs.set_decision(job.id, CANADA, "saved")
    app.jobs.set_application_status(job.id, CANADA, "applied")
    again, status2 = app.jobs.upsert_job(_job(canonical_url="https://boards.example.com/acme/1/?utm_source=x"), CANADA)
    assert again.id == job.id and status2 == "unchanged"
    rows = app.jobs.rows(CANADA, "all")
    assert len(rows) == 1
    assert rows[0]["notes"] == "Hablé con el reclutador"
    assert rows[0]["user_decision"] == "saved"
    assert rows[0]["application_status"] == "applied"


def test_dedupe_by_source_job_id(app):
    a, _ = app.jobs.upsert_job(_job(canonical_url="", source="greenhouse", source_job_id="123"))
    b, st = app.jobs.upsert_job(_job(canonical_url="", source="greenhouse", source_job_id="123", salary_min=100))
    assert a.id == b.id and st == "updated"
    assert len(app.jobs.rows(CANADA)) == 1


def test_invalid_url_rejected():
    with pytest.raises(ValueError):
        _job(canonical_url="javascript:alert(1)")


def test_discard_and_restore(app):
    job, _ = app.jobs.upsert_job(_job(job_country="CA", sponsorship_statement="Will support work permit"), CANADA)
    assert [r["job_id"] for r in app.jobs.rows(CANADA, "activos")] == [job.id]
    app.jobs.set_decision(job.id, CANADA, "saved")
    app.jobs.set_decision(job.id, CANADA, "discarded", "salary", "muy bajo")
    assert app.jobs.rows(CANADA, "activos") == []
    assert [r["discard_reason"] for r in app.jobs.rows(CANADA, "discarded")] == ["salary"]
    hist = app.jobs.history(CANADA)
    assert any(h["action"] == "decision_discarded" for h in hist)
    restored = app.jobs.restore(job.id, CANADA)
    assert restored == "saved"
    assert len(app.jobs.rows(CANADA, "activos")) == 1


def test_mode_evaluations_independent(app):
    job, _ = app.jobs.upsert_job(_job(job_country="US", work_mode="remote", remote_eligible_countries="US"), CANADA)
    mx = app.jobs.rows(MEXICO, "all")[0]
    assert mx["eligibility_status"] == "blocked"  # US-only remote is not Mexico-eligible
    ca = app.jobs.rows(CANADA, "all")[0]
    assert ca["eligibility_status"] == "blocked"
    app.jobs.set_decision(job.id, MEXICO, "discarded", "not_remote_from_country")
    assert app.jobs.rows(CANADA, "all")[0]["user_decision"] == "new"  # discard is per mode


def test_missing_visa_statement_is_unknown(app):
    app.jobs.upsert_job(_job(job_country="CA"), CANADA)
    row = app.jobs.rows(CANADA, "all")[0]
    assert row["eligibility_status"] == "needs_verification"
    assert row["sponsorship_statement"] == ""


def test_mexico_unknown_remote_country_needs_verification(app):
    app.jobs.upsert_job(_job(work_mode="remote"), MEXICO)
    assert app.jobs.rows(MEXICO, "all")[0]["eligibility_status"] == "needs_verification"
    app.jobs.upsert_job(_job(canonical_url="https://x.com/2", work_mode="Remoto", remote_eligible_countries="México"))
    statuses = {r["job_url"]: r["eligibility_status"] for r in app.jobs.rows(MEXICO, "all")}
    assert statuses["https://x.com/2"] == "eligible"


def test_add_and_disable_rule_changes_results(app):
    app.jobs.upsert_job(_job(work_mode="remote", remote_eligible_countries="MX", description="Java developer"))
    assert app.jobs.rows(MEXICO)[0]["eligibility_status"] == "eligible"
    sim = app.rules.simulate(MEXICO, {"name": "Excluir Java", "field": "description", "operator": "any_keyword",
                                      "value": "java", "action": "exclude", "severity": "hard",
                                      "unknown_handling": "keep", "scope": "prefilter"})
    assert sim["changed"] == 1 and sim["jobs"][0]["after"] == "blocked"
    assert app.jobs.rows(MEXICO)[0]["eligibility_status"] == "eligible"  # simulation did not persist
    rule = app.rules.create(MEXICO, {"name": "Excluir Java", "field": "description", "operator": "any_keyword",
                                     "value": "java", "action": "exclude", "severity": "hard",
                                     "unknown_handling": "keep", "scope": "prefilter",
                                     "explanation": "No quiero Java"})
    row = app.jobs.rows(MEXICO)[0]
    assert row["eligibility_status"] == "blocked"
    assert app.jobs.rows(MEXICO, "activos") == []
    app.rules.set_enabled(rule.id, False)
    assert app.jobs.rows(MEXICO)[0]["eligibility_status"] == "eligible"
    versions = app.rules.versions(rule.id)
    assert [v.version for v in versions] == [2, 1]


def test_invalid_rule_rejected(app):
    with pytest.raises(RuleValidationError):
        app.rules.create(MEXICO, {"name": "x", "field": "salary_min", "operator": "contains", "value": "a",
                                  "action": "exclude"})


def test_override_includes_blocked_job_in_activos(app):
    job, _ = app.jobs.upsert_job(_job(job_country="US", remote_eligible_countries="US"))
    assert app.jobs.rows(CANADA, "activos") == []
    with pytest.raises(ValueError):
        app.jobs.set_override(job.id, CANADA, "include", "")
    app.jobs.set_override(job.id, CANADA, "include", "El reclutador confirmó reubicación a Toronto")
    assert len(app.jobs.rows(CANADA, "activos")) == 1


def test_interviewing_job_stays_active_even_if_blocked(app):
    job, _ = app.jobs.upsert_job(_job(job_country="US"))
    app.jobs.set_application_status(job.id, CANADA, "interviewing")
    assert len(app.jobs.rows(CANADA, "activos")) == 1


def test_mode_data_updates_and_persist_across_restart(tmp_path):
    home = tmp_path / "p1"
    a = AppServices(get_paths(home))
    job, _ = a.jobs.upsert_job(_job(job_country="CA"), CANADA)
    a.jobs.update_mode_data(job.id, CANADA, {"employer_visa_support": "yes", "bogus": 1})
    a.current_mode = MEXICO
    a.close()
    moved = tmp_path / "moved" / "JobSearchManager"
    shutil.copytree(home, moved)  # portable folder relocation
    b = AppServices(get_paths(moved))
    row = b.jobs.rows(CANADA, "all")[0]
    assert row["employer_visa_support"] == "yes"
    assert "bogus" not in row
    assert b.current_mode == MEXICO
    b.close()


def test_migration_backup_and_newer_schema_guard(tmp_path):
    from sqlalchemy import text
    from jsh.db import make_engine, migrate

    home = tmp_path / "p2"
    a = AppServices(get_paths(home))
    a.close()
    eng = make_engine(get_paths(home).db_file)
    with eng.begin() as c:
        c.execute(text("PRAGMA user_version = 99"))
    with pytest.raises(RuntimeError):
        migrate(eng)
    eng.dispose()


def test_restore_is_noop_for_non_discarded(app):
    job, _ = app.jobs.upsert_job(_job())
    app.jobs.set_decision(job.id, CANADA, "saved")
    assert app.jobs.restore(job.id, CANADA) == "saved"
    assert app.jobs.rows(CANADA)[0]["user_decision"] == "saved"
    app.jobs.set_decision(job.id, CANADA, "discarded", "other")
    app.jobs.restore(job.id, CANADA)
    app.jobs.set_decision(job.id, CANADA, "maybe")
    assert app.jobs.restore(job.id, CANADA) == "maybe"
