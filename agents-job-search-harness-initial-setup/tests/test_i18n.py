"""Interface language: every UI text has an English translation with the same placeholders, and the English UI
renders without untranslated Spanish texts."""
import ast
import logging
import string
from pathlib import Path

import pytest
from nicegui import ui
from nicegui.testing import user_simulation

from jsh import i18n
from jsh.i18n_en import EN
from jsh.modes import CANADA, MODE_DEFS, default_rules
from jsh.rules.registry import ACTION_LABELS, FIELDS, OPERATOR_LABELS, SCOPE_LABELS, SEVERITY_LABELS
from jsh.services import JobInput
from jsh.ui.app import configure, index

from .test_handoff import CV, JD

ROOT = Path(__file__).resolve().parents[1] / "jsh"


def _fields(text: str) -> list[tuple]:
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError:
        return []
    fields = [(f or "", spec or "", conv or "") for _, f, spec, conv in parsed if f is not None]
    if any(f and not f.isdigit() for f, _, _ in fields):
        return []  # literal braces in a message (e.g. a JSON example), not a format template
    return sorted(fields)


def _t_literals() -> set[str]:
    found: set[str] = set()
    for path in [*sorted((ROOT / "ui").glob("*.py")), ROOT / "services" / "views.py"]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found.add(first.value)
                elif isinstance(first, ast.Dict):
                    found |= {v.value for v in first.values if isinstance(v, ast.Constant)}
    return {s for s in found if any(ch.isalpha() for ch in s)}


def test_every_ui_text_has_an_english_translation():
    missing = sorted(_t_literals() - set(EN))
    assert not missing, f"{len(missing)} UI texts without English translation, e.g. {missing[:10]}"


def test_label_dicts_are_translated():
    labels = [*ACTION_LABELS.values(), *SEVERITY_LABELS.values(), *SCOPE_LABELS.values(), *OPERATOR_LABELS.values(),
              *(f.label for f in FIELDS.values()), *(m.name for m in MODE_DEFS.values()),
              *(m.description for m in MODE_DEFS.values()),
              *(mf.label for m in MODE_DEFS.values() for mf in m.mode_fields),
              *(r[k] for r in default_rules() for k in ("name", "explanation") if r.get(k))]
    missing = sorted({x for x in labels if x not in EN})
    assert not missing, missing[:10]


def test_translations_keep_placeholders_and_spacing():
    bad = [k for k, v in EN.items() if _fields(k) != _fields(v) or k[:1].isspace() != v[:1].isspace()
           or k[-1:].isspace() != v[-1:].isspace()]
    assert not bad, bad[:10]
    for k, v in EN.items():
        if _fields(k) and not any("%" in spec for _, spec, _ in _fields(k)):
            args = [1] * len(_fields(k))
            try:
                v.format(*args)
            except (ValueError, IndexError, KeyError):
                pytest.fail(f"bad format in translation of {k!r}: {v!r}")


def test_t_falls_back_and_formats():
    i18n.set_language("en")
    try:
        assert i18n.t("Guardar") == "Save"
        assert i18n.t("texto sin traducción {}", 3) == "texto sin traducción 3"
        assert i18n.tl({"a": "Guardar"}) == {"a": "Save"} and i18n.tl(["Guardar"]) == ["Guardar"]
    finally:
        i18n.set_language("es")
    assert i18n.t("Guardar") == "Guardar"


@pytest.fixture
def english(app, caplog):
    caplog.set_level(logging.WARNING)
    c = app.candidate.create_candidate("Data")
    app.candidate.import_text(c.id, CV)
    app.candidate.set_active_candidate(CANADA, c.id)
    app.jobs.upsert_job(JobInput(title="Data Engineer", company_name="Maple Data", job_country="CA",
                                 canonical_url="https://maple.example/jobs/1", description=JD,
                                 salary_min=90000, salary_max=100000, currency="CAD", salary_period="year"))
    app.current_mode = CANADA
    app.db.set_setting("ui_language", "en")
    configure(app)
    yield app
    i18n.set_language("es")
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_english_ui_has_no_untranslated_texts(english):
    spanish_sources = {k for k, v in EN.items() if k != v}

    def leftovers(user) -> set:
        found = set()
        for el in user.find(ui.element).elements:
            for text in (getattr(el, "text", None), el.props.get("label")):
                if isinstance(text, str) and text in spanish_sources:
                    found.add(text)
        return found
    async with user_simulation(root=index) as user:
        await user.open("/")
        await user.should_see("Active")
        await user.should_see("Settings")
        assert not leftovers(user), " | ".join(sorted(leftovers(user)))
        job_id = english.jobs.rows(CANADA)[0]["job_id"]
        user.find(marker=f"detail_{job_id}").click()
        await user.should_see(marker="tags_input")
        user.find(marker="filters_open").click()
        await user.should_see(marker="flt_location")
        assert not leftovers(user), " | ".join(sorted(leftovers(user)))
