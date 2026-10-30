"""Interface language (Spanish source text → other languages).

The Spanish text in the code IS the key (gettext style): ``t("Guardar")`` returns "Save" when the language is English.
Text with values uses ``str.format`` placeholders: ``t("{} vacantes", n)`` — translations may reorder them with
``{0}``/``{1}``. Missing translations fall back to Spanish, so the app never breaks because of a missing entry
(tests/test_i18n.py checks that every UI text has an English entry).
"""
from __future__ import annotations

from typing import Any

LANGUAGES = {"es": "Español", "en": "English"}
QUASAR = {"es": "es", "en": "en-US"}
_lang = "es"
_MISSING: set[str] | None = None  # tests: collect texts without a translation (enable_missing_log)


def enable_missing_log() -> set[str]:
    global _MISSING
    _MISSING = set()
    return _MISSING


def set_language(code: str) -> None:
    global _lang
    _lang = code if code in LANGUAGES else "es"


def get_language() -> str:
    return _lang


def _catalog() -> dict[str, str]:
    if _lang == "en":
        from .i18n_en import EN
        return EN
    return {}


def t(text: Any, *args: Any, **kwargs: Any) -> str:
    """Translate ``text`` (Spanish) into the current language and format it with ``args``/``kwargs``."""
    if text is None:
        return ""
    src = str(text)
    if _lang != "es":
        cat = _catalog()
        out = cat.get(src)
        if out is None:
            out = src
            if _MISSING is not None and src.strip() and any(ch.isalpha() for ch in src):
                _MISSING.add(src)
    else:
        out = src
    if args or kwargs:
        try:
            return out.format(*args, **kwargs)
        except (IndexError, KeyError, ValueError):
            return src.format(*args, **kwargs)
    return out


def tl(mapping: Any) -> Any:
    """Translate the VALUES of a {key: Spanish label} mapping (for selects, toggles, radios). Lists are option
    VALUES (data) and are returned unchanged."""
    if not isinstance(mapping, dict):
        return mapping
    return {k: t(v) for k, v in mapping.items()}
