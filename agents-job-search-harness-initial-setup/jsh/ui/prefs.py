"""Interface preferences (appearance + quality of life), stored as ``ui_*`` settings in the portable database.

``ui_*`` settings survive "Borrar todos los datos" (they are not job-search data).
"""
from __future__ import annotations

from typing import Any

from nicegui import ui

from ..i18n import LANGUAGES, set_language

THEMES: dict[str, dict[str, str]] = {
    # name → Quasar brand colours + header background
    "azul": {"label": "Azul (clásico)", "primary": "#1976d2", "secondary": "#26a69a", "accent": "#9c27b0",
             "header": "#0d47a1"},
    "verde": {"label": "Verde bosque", "primary": "#2e7d32", "secondary": "#00897b", "accent": "#f9a825",
              "header": "#1b5e20"},
    "morado": {"label": "Morado", "primary": "#6a1b9a", "secondary": "#ad1457", "accent": "#00acc1",
               "header": "#4a148c"},
    "naranja": {"label": "Naranja atardecer", "primary": "#e65100", "secondary": "#6d4c41", "accent": "#1e88e5",
                "header": "#bf360c"},
    "turquesa": {"label": "Turquesa", "primary": "#00838f", "secondary": "#5e35b1", "accent": "#ffb300",
                 "header": "#006064"},
    "grafito": {"label": "Grafito", "primary": "#455a64", "secondary": "#78909c", "accent": "#ff7043",
                "header": "#263238"},
    "contraste": {"label": "Alto contraste", "primary": "#000000", "secondary": "#424242", "accent": "#ffd600",
                  "header": "#000000"},
}
DARK = {"light": "Claro", "dark": "Oscuro", "auto": "Automático (sistema)"}
FONT_SCALES = {90: "90 %", 100: "100 %", 110: "110 %", 125: "125 %", 150: "150 %"}
DENSITY = {"comfortable": "Normal", "compact": "Compacta (más filas en pantalla)"}
PAGE_SIZES = {10: "10", 25: "25", 50: "50", 100: "100"}
CARD_LIMITS = {20: "20", 50: "50", 100: "100", 0: "Todas"}

DEFAULTS: dict[str, Any] = {
    "ui_language": "es", "ui_theme": "azul", "ui_dark": "light", "ui_font_scale": 100, "ui_density": "comfortable",
    "ui_start_tab": "last", "ui_page_size": 25, "ui_card_limit": 50, "ui_auto_apply": False,
    "ui_confirm_bulk": True, "ui_shortcuts": True,
}
CHOICES: dict[str, Any] = {
    "ui_language": LANGUAGES, "ui_theme": THEMES, "ui_dark": DARK, "ui_font_scale": FONT_SCALES,
    "ui_density": DENSITY, "ui_page_size": PAGE_SIZES, "ui_card_limit": CARD_LIMITS,
}
COMPACT_CSS = """
.q-card { padding: 8px !important; }
.nicegui-content { padding: 8px !important; gap: 6px !important; }
.nicegui-row, .nicegui-column { gap: 6px !important; }
.q-tab { min-height: 36px !important; padding: 0 8px !important; }
.q-field__control, .q-field__native { min-height: 32px !important; }
.q-btn { min-height: 28px !important; }
"""


def load(db) -> dict[str, Any]:
    prefs = dict(DEFAULTS)
    for key, default in DEFAULTS.items():
        val = db.get_setting(key, None)
        if val is None:
            continue
        choices = CHOICES.get(key)
        if choices is not None and val not in choices:
            continue
        if isinstance(default, bool):
            val = bool(val)
        prefs[key] = val
    if db.get_setting("ui_dark", None) is None and db.get_setting("dark_mode", False):
        prefs["ui_dark"] = "dark"  # setting of v0.5 and earlier
    return prefs


def save(db, key: str, value: Any) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    choices = CHOICES.get(key)
    if choices is not None and value not in choices:
        raise ValueError(f"Valor inválido para {key}: {value}")
    db.set_setting(key, value)


def reset(db) -> None:
    for key, value in DEFAULTS.items():
        db.set_setting(key, value)


def apply(prefs: dict[str, Any]) -> dict[str, str]:
    """Apply language, colours, dark mode, zoom and density to the current page. Returns the theme."""
    set_language(prefs["ui_language"])
    theme = THEMES.get(prefs["ui_theme"], THEMES["azul"])
    ui.colors(primary=theme["primary"], secondary=theme["secondary"], accent=theme["accent"])
    css = [f".jsh-header {{ background-color: {theme['header']} !important; }}"]
    scale = int(prefs["ui_font_scale"] or 100)
    if scale != 100:
        css.append(f"body {{ zoom: {scale / 100:.2f}; }}")
    if prefs["ui_density"] == "compact":
        css.append(COMPACT_CSS)
    ui.add_css("\n".join(css))
    return theme
