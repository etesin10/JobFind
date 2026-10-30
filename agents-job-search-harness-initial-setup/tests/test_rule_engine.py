from datetime import date

import pytest

from jsh.rules import evaluate, parse_value, validate_rule
from jsh.rules.engine import evaluate_condition, regex_problem, RuleSpec


def rule(**kw):
    base = dict(id="r1", name="r", field="title", operator="contains", value="data", action="include",
                severity="hard", unknown_handling="flag", scope="prefilter")
    base.update(kw)
    return base


def test_include_hard_blocks_when_false():
    res = evaluate([rule()], {"title": "Chef"})
    assert res.status == "blocked"
    assert res.blockers[0].result == "false"


def test_unknown_is_not_positive_or_negative():
    res = evaluate([rule(field="sponsorship_statement", operator="any_keyword", value=["sponsor"])],
                   {"sponsorship_statement": ""})
    assert res.status == "needs_verification"
    assert res.outcomes[0].result == "unknown"


def test_unknown_handling_keep_and_exclude():
    keep = evaluate([rule(unknown_handling="keep")], {"title": None})
    assert keep.status == "eligible" and keep.outcomes[0].effect == "unknown"
    excl = evaluate([rule(unknown_handling="exclude")], {"title": None})
    assert excl.status == "blocked"


def test_us_only_remote_not_mexico_eligible():
    r = rule(field="remote_eligible_countries", operator="in", value=["MX", "LATAM", "WORLDWIDE"])
    assert evaluate([r], {"remote_eligible_countries": ["US"]}).status == "blocked"
    assert evaluate([r], {"remote_eligible_countries": ["MX"]}).status == "eligible"
    assert evaluate([r], {"remote_eligible_countries": []}).status == "needs_verification"


def test_or_group():
    rules = [rule(id="a", field="job_country", operator="in", value=["CA"], logic_group="g", group_operator="OR"),
             rule(id="b", field="remote_eligible_countries", operator="in", value=["CA"], logic_group="g",
                  group_operator="OR", priority=101)]
    assert evaluate(rules, {"job_country": "US", "remote_eligible_countries": ["CA"]}).status == "eligible"
    assert evaluate(rules, {"job_country": "US", "remote_eligible_countries": ["US"]}).status == "blocked"
    assert evaluate(rules, {"job_country": "US", "remote_eligible_countries": []}).status == "needs_verification"


def test_weight_and_keyword_evidence():
    r = rule(field="description", operator="any_keyword", value=["snowflake", "airflow"], action="weight",
             weight=30)
    res = evaluate([r], {"description": "We use Snowflake and dbt."})
    assert res.score == 30
    assert "snowflake" in res.outcomes[0].evidence.lower()


def test_keyword_word_boundary():
    r = RuleSpec.from_obj(rule(field="description", operator="any_keyword", value=["sql"]))
    assert evaluate_condition(r, {"description": "MySQL only"})[0] is False
    assert evaluate_condition(r, {"description": "Strong SQL skills"})[0] is True


def test_numeric_and_date_operators():
    r = RuleSpec.from_obj(rule(field="salary_min", operator="gte", value=50000))
    assert evaluate_condition(r, {"salary_min": 60000})[0] is True
    assert evaluate_condition(r, {"salary_min": None})[0] is None
    r = RuleSpec.from_obj(rule(field="posted_date", operator="date_within_days", value=7))
    assert evaluate_condition(r, {"posted_date": date(2026, 10, 25)}, today=date(2026, 10, 29))[0] is True
    assert evaluate_condition(r, {"posted_date": "2026-09-01"}, today=date(2026, 10, 29))[0] is False
    r = RuleSpec.from_obj(rule(field="salary_max", operator="between", value=["10", "20"]))
    assert evaluate_condition(r, {"salary_max": 15})[0] is True


def test_disabled_and_search_scope_rules_ignored():
    assert evaluate([rule(enabled=False)], {"title": "Chef"}).status == "eligible"
    assert evaluate([rule(scope="search")], {"title": "Chef"}).status == "eligible"


def test_validation():
    assert validate_rule(rule()) == []
    assert any("Campo desconocido" in e for e in validate_rule(rule(field="__import__")))
    assert validate_rule(rule(field="salary_min", operator="contains"))
    assert validate_rule(rule(operator="in", value=[]))
    assert validate_rule(rule(action="weight", weight=0))


def test_regex_safeguards():
    assert regex_problem("(a+)+$") is not None
    assert regex_problem("x" * 500) is not None
    assert regex_problem("senior|staff") is None
    res = evaluate([rule(operator="regex", value="(a+)+$")], {"title": "aaaa"})
    assert res.outcomes[0].result == "error"  # stored bad rule never crashes evaluation


def test_parse_value():
    assert parse_value("in", "job_country", "CA, MX") == ["CA", "MX"]
    assert parse_value("gte", "salary_min", "50,000") == 50000.0
    assert parse_value("exists", "title", "x") is None


@pytest.mark.parametrize("payload", ["__import__('os').system('echo hi')", "1; DROP TABLE jobs"])
def test_no_code_execution(payload):
    res = evaluate([rule(operator="eq", value=payload)], {"title": payload})
    assert res.status == "eligible"  # treated as plain string equality only
