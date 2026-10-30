"""NiceGUI local browser UI (Spanish). Phase 1 slice.

Untrusted text (JDs, imported cells) is rendered only through ``ui.label``/table cells,
which escape HTML — never through ``ui.html``/``ui.markdown``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from nicegui import background_tasks, run, ui

from ..excel.csv_import import ENCODINGS, EXTRA, IGNORE, TARGET_FIELDS, read_csv_bytes, suggest_mapping
from ..i18n import t, tl
from ..modes import (
    ALL, APPLICATION_STATUSES, CANADA, DISCARD_REASONS, ELIGIBILITY_LABELS, MEXICO, MODE_CHOICES, MODE_DEFS,
    USER_DECISION_LABELS,
)
from ..rules.registry import (
    ACTION_LABELS, FIELDS, GROUP_OPERATORS, OPERATOR_LABELS, OPERATORS_BY_TYPE, SCOPE_LABELS, SEVERITY_LABELS,
    UNKNOWN_LABELS, VALUE_KIND, fields_for_mode,
)
from ..services import AppServices, JobInput, RuleValidationError
from ..services.jobs import agent_info
from ..tolerant import parse_float

COLUMN_LABELS = {
    "title": "Puesto", "company_name": "Empresa", "job_country": "País", "region": "Provincia/Estado",
    "work_mode": "Modalidad", "sponsorship_statement": "Patrocinio", "remote_eligible_countries": "Remoto desde",
    "salary_min": "Sal. mín", "salary_max": "Sal. máx", "currency": "Moneda", "eligibility_status": "Elegibilidad",
    "technical_score": "Puntaje", "user_decision": "Decisión", "application_status": "Aplicación",
    "employer_visa_support": "Apoyo visa", "contract_arrangement": "Contratación", "discard_reason": "Motivo",
    "ca_status": "Canadá", "mx_status": "México", "location_text": "Ubicación", "skills_match": "Skills CV",
    "agent_score": "Agente", "ai_fit_score": "IA", "salary_year": "Salario anual", "tags": "Etiquetas",
}
STATUS_COLORS = {"eligible": "positive", "needs_verification": "warning", "flagged": "orange", "blocked": "negative",
                 "unevaluated": "grey"}
EFFECT_LABELS = {"blocker": "⛔ Bloqueo", "unknown_flag": "❓ Verificar", "unknown": "❔ Desconocido (se mantiene)",
                 "flag": "⚠ Alerta", "info": "ℹ Info", "review": "🔎 Revisión", "weight": "➕ Puntaje",
                 "pass": "✓ Cumple"}
WORK_MODES = {"": "Desconocido", "remote": "Remoto", "hybrid": "Híbrido", "onsite": "Presencial"}

_services: AppServices | None = None
log = logging.getLogger(__name__)


def configure(services: AppServices) -> None:
    global _services
    _services = services


def start_background_sync(services: AppServices) -> None:
    """Poll the external-agent inbox and direct DB edits while the app runs (call from app.on_startup)."""
    async def loop() -> None:
        while True:
            try:
                await run.io_bound(services.sync.tick)
            except Exception:  # never let the poller die
                log.exception("Background sync failed")
            await asyncio.sleep(services.sync.interval_s)
    background_tasks.create(loop(), name="jsh-external-sync")

    async def maintenance() -> None:
        """Hourly: refresh stale exchange rates (only if salaries in other currencies exist) and, when the user
        enabled it, check whether saved job ads are still open (flags only)."""
        await asyncio.sleep(20)
        while True:
            try:
                await run.io_bound(services.salary.refresh_if_stale)
                if services.liveness.settings()["enabled"]:
                    res = await run.io_bound(services.liveness.run_batch)
                    if res.get("checked"):
                        log.info("Ad-liveness batch: %s", res)
            except Exception:  # never let the loop die
                log.exception("Background maintenance failed")
            await asyncio.sleep(3600)
    background_tasks.create(maintenance(), name="jsh-maintenance")


def quasar_language(services: AppServices) -> str:
    """Quasar's built-in texts (table pagination, date pickers) follow the interface language chosen at start."""
    from ..i18n import QUASAR
    return QUASAR.get(str(services.db.get_setting("ui_language", "es") or "es"), "es")


def _safe_url(url: Any) -> str:
    """Only http(s) URLs become links (the DB may contain values written by an external agent)."""
    u = str(url or "").strip()
    return u if u.lower().startswith(("http://", "https://")) else ""


def _display(row: dict[str, Any], key: str) -> Any:
    v = row.get(key)
    if key == "eligibility_status":
        return t(ELIGIBILITY_LABELS.get(v, v))
    if key == "user_decision":
        return t(USER_DECISION_LABELS.get(v, v))
    if key == "application_status":
        return t(APPLICATION_STATUSES.get(v, (v or "",))[0])
    if key == "discard_reason":
        return t(DISCARD_REASONS.get(v, v))
    if key == "sponsorship_statement":
        return (v[:60] + "…") if v and len(v) > 60 else (v or t("Desconocido"))
    if key == "work_mode":
        return t(WORK_MODES.get(v or "", v))
    if key == "technical_score":
        return round(v or 0)
    if key in ("skills_match", "agent_score", "ai_fit_score"):
        return "—" if v is None else f"{v:.0f}%" if key == "skills_match" else f"{v:.0f}"
    if key in ("employer_visa_support", "contract_arrangement"):
        return v or "unknown"
    if key == "salary_year":
        lo, hi = row.get("salary_annual_min"), row.get("salary_annual_max")
        if lo is None and hi is None:
            return ""
        fmt = (lambda x: f"{x / 1000:.0f}k" if x is not None else "?")
        return (fmt(lo) if hi in (None, lo) else f"{fmt(lo)}–{fmt(hi)}" if lo is not None else fmt(hi)) + \
            f" {row.get('salary_base', '')}"
    return v if v is not None else ""


class MainPage:
    POLL_S = 3.0  # how often the page checks whether the background sync saw external changes

    def __init__(self, svc: AppServices):
        self.svc = svc
        self.search = {"activos": "", "vacantes": ""}
        self.vacantes_view = "all"
        self.refreshers: list[Callable[[], None]] = []
        self.seen_seq = svc.sync.seq
        self.search_inputs: dict[str, Any] = {}
        self.open_dialogs = 0
        self.show_all_cards = False
        from . import prefs as P
        from .organize import Organizer
        self.prefs = P.load(svc.db)
        self.org = Organizer(self)

    @property
    def page_size(self) -> int:
        return int(self.prefs.get("ui_page_size") or 25)

    # ------------------------------------------------------------------ utils
    @property
    def mode(self) -> str:
        return self.svc.current_mode

    def refresh_all(self) -> None:
        for r in self.refreshers:
            r()

    def dialog(self) -> ui.dialog:
        """A dialog that survives refresh_all(): created outside the refreshable panels (a dialog created inside a
        panel is destroyed when that panel refreshes). Closed dialogs are deleted shortly after."""
        with self.dialog_host:
            dlg = ui.dialog()

        def closed(e) -> None:
            if e.value:
                self.open_dialogs += 1
                return
            self.open_dialogs = max(0, self.open_dialogs - 1)
            if not e.value:
                def drop() -> None:
                    if not dlg.is_deleted:
                        dlg.delete()
                asyncio.get_running_loop().call_later(1.0, drop)
        dlg.on_value_change(closed)
        return dlg

    def _mount(self, panel: Callable[[], None]) -> None:
        r = ui.refreshable(panel)
        r()
        self.refreshers.append(r.refresh)

    def _need_mode(self) -> bool:
        if self.mode == ALL:
            ui.label(t("Seleccione un modo (Canadá o México) arriba para ver esta sección.")).classes("text-grey-7")
            return True
        return False

    # ------------------------------------------------------------------ build
    def build(self) -> None:
        from . import prefs as P
        self.prefs = P.load(self.svc.db)
        P.apply(self.prefs)
        dark = ui.dark_mode({"light": False, "dark": True, "auto": None}[self.prefs["ui_dark"]])
        with ui.header().classes("items-center justify-between jsh-header"):
            ui.label(t("Job Search Harness")).classes("text-h6")
            with ui.row().classes("items-center"):
                ui.select(tl(MODE_CHOICES), value=self.mode, label=t("Modo"),
                          on_change=lambda e: self._set_mode(e.value)).props("dark dense outlined").classes("w-72") \
                    .mark("mode_select")

                def toggle_dark(e):
                    dark.value = e.value
                    self.prefs["ui_dark"] = "dark" if e.value else "light"
                    self.svc.db.set_setting("ui_dark", self.prefs["ui_dark"])
                ui.switch(t("Oscuro"), value=bool(dark.value), on_change=toggle_dark).props("dark color=white")
                ui.button(icon="sync", on_click=self.sync_now).props("flat round color=white") \
                    .tooltip(t("Actualizar: leer ahora los cambios del agente externo")).mark("sync_now")

        with ui.row().classes("w-full items-center bg-amber-2 text-black q-pa-sm rounded") as self.banner:
            self.banner_label = ui.label("").mark("sync_banner")
            ui.button(t("Mostrar cambios"), icon="refresh", on_click=self.apply_external).props("dense") \
                .mark("sync_apply")
        self.banner.set_visibility(False)
        ui.timer(self.POLL_S, self.check_external)
        self.dialog_host = ui.element("div")  # dialogs live here, outside refreshable panels

        tab_names = [("inicio", "Inicio", "dashboard"), ("descubrir", "Descubrir", "travel_explore"),
                     ("activos", "Activos", "bolt"),
                     ("vacantes", "Vacantes", "list"), ("kanban", "Kanban", "view_kanban"),
                     ("empresas", "Empresas", "business"), ("contactos", "Contactos", "contacts"),
                     ("agregar", "Agregar vacante", "add"), ("skills", "Skills y CV", "badge"),
                     ("analisis", "Análisis IA", "psychology"), ("informes", "Informes", "insights"),
                     ("reglas", "Reglas y Filtros", "rule"),
                     ("excel", "Excel / Importar", "table_view"), ("historial", "Historial", "history"),
                     ("config", "Configuración / Backups", "settings")]
        self.tab_names = tab_names
        start = self.prefs["ui_start_tab"]
        last_tab = start if start in {n for n, _, _ in tab_names} else \
            self.svc.db.get_setting("last_tab", "activos")
        with ui.tabs(on_change=lambda e: self.svc.db.set_setting("last_tab", e.value)).classes("w-full") as tabs:
            for name, label, icon in tab_names:
                ui.tab(name, label=t(label), icon=icon)
        self.tabs = tabs
        with ui.tab_panels(tabs, value=last_tab).classes("w-full"):
            with ui.tab_panel("inicio"):
                self._mount(self.dashboard_panel)
            with ui.tab_panel("descubrir"):
                from .discover_panel import DiscoverPanel
                self.discover = DiscoverPanel(self)
                self._mount(self.discover.render)
            with ui.tab_panel("activos"):
                self._mount(self.activos_panel)
            with ui.tab_panel("vacantes"):
                self._mount(self.vacantes_panel)
            with ui.tab_panel("kanban"):
                self._mount(self.kanban_panel)
            from .crm_panel import CrmPanel
            self.crm = CrmPanel(self)
            with ui.tab_panel("empresas"):
                self._mount(self.crm.companies)
            with ui.tab_panel("contactos"):
                self._mount(self.crm.contacts)
            with ui.tab_panel("agregar"):
                self.add_job_panel()
            with ui.tab_panel("skills"):
                from .skills_panel import SkillsPanel
                self.skills = SkillsPanel(self)
                self._mount(self.skills.render)
            with ui.tab_panel("analisis"):
                from .ai_panel import AIPanel
                self.ai = AIPanel(self)
                self._mount(self.ai.render)
            with ui.tab_panel("informes"):
                from .insights_panel import InsightsPanel
                self.insights = InsightsPanel(self)
                self._mount(self.insights.render)
            with ui.tab_panel("reglas"):
                self._mount(self.rules_panel)
            with ui.tab_panel("excel"):
                self._mount(self.excel_panel)
            with ui.tab_panel("historial"):
                self._mount(self.history_panel)
            with ui.tab_panel("config"):
                self._mount(self.config_panel)
        if self.prefs["ui_shortcuts"]:
            ui.keyboard(on_key=self._on_key, ignore=["input", "select", "button", "textarea"])

    def _on_key(self, e) -> None:
        """'/' focuses the search box of Activos/Vacantes; '?' lists the shortcuts."""
        if not e.action.keydown:
            return
        if e.key == "/":
            tab = self.tabs.value if self.tabs.value in ("activos", "vacantes") else "activos"
            self.tabs.set_value(tab)
            box = self.search_inputs.get(tab)
            if box is not None and not box.is_deleted:
                box.run_method("focus")
        elif e.key == "?":
            ui.notify(t("Atajos: '/' buscar · '?' esta ayuda. Desactívelos en Configuración → Apariencia."),
                      multi_line=True)

    def _set_mode(self, mode_id: str) -> None:
        self.svc.current_mode = mode_id
        self.refresh_all()

    # ------------------------------------------------------------------ external agent
    def check_external(self) -> None:
        """Timer: show a banner (never an automatic refresh that would close open dialogs)."""
        if self.svc.sync.seq == self.seen_seq:
            return
        ch = self.svc.sync.changes_since(self.seen_seq)
        parts = []
        if ch["created"]:
            parts.append(t("{} vacante(s) nueva(s)", ch["created"]))
        if ch["updated"]:
            parts.append(t("{} actualizada(s)", ch["updated"]))
        if ch["external_jobs"]:
            parts.append(t("{} cambiada(s) directamente en la base", ch["external_jobs"]))
        if ch["reviews"]:
            parts.append(t("{} análisis IA recibido(s)", ch["reviews"]))
        if ch["tailoring"]:
            parts.append(t("{} propuesta(s) de CV", ch["tailoring"]))
        if ch.get("skills"):
            parts.append(t("{} sugerencia(s) de skills para revisar", ch["skills"]))
        if ch["crm"]:
            parts.append(t("{} cambio(s) en empresas/contactos", ch["crm"]))
        if ch["error"]:
            parts.append(t("{} rechazada(s) — vea Configuración", ch["error"]))
        self.banner_label.text = t("🔄 Cambios del agente externo ({}): ", ch["at"]) + (", ".join(parts) or t("datos"))
        if self.prefs.get("ui_auto_apply") and self.open_dialogs == 0:
            self.apply_external()  # nothing open that a refresh could disturb
            ui.notify(self.banner_label.text, type="info")
            return
        self.banner.set_visibility(True)

    def apply_external(self) -> None:
        self.seen_seq = self.svc.sync.seq
        self.banner.set_visibility(False)
        self.refresh_all()

    async def sync_now(self) -> None:
        summary = await run.io_bound(self.svc.sync.tick, True)
        if summary is None:
            ui.notify(t("Sincronización en curso o base ocupada; reintente en unos segundos."), type="warning")
            return
        inbox = summary["inbox"]
        ui.notify(t("Sincronizado: {} nuevas, {} actualizadas, "
                  "{} rechazadas, {} cambios directos.", inbox['created'], inbox['updated'], inbox['error'], summary['external_jobs']), type="positive")
        self.apply_external()

    # ------------------------------------------------------------------ dashboard
    def dashboard_panel(self) -> None:
        if self._need_mode():
            total = len(self.svc.jobs.rows(CANADA, "all"))
            ui.label(t("Vacantes registradas (compartidas entre modos): {}", total))
            return
        d = self.svc.jobs.dashboard(self.mode)
        ui.label(t(MODE_DEFS[self.mode].name)).classes("text-h6")
        ui.label(t(MODE_DEFS[self.mode].description)).classes("text-grey-7")
        with ui.row():
            for label, val in (("Vacantes", d["total"]), ("Activos", d["activos"]), ("Descartadas", d["discarded"]),
                               ("Seguimientos vencidos", len(d["followups_due"]))):
                with ui.card().classes("w-44 items-center"):
                    ui.label(str(val)).classes("text-h4")
                    ui.label(t(label))
        with ui.row():
            with ui.card():
                ui.label(t("Elegibilidad")).classes("text-bold")
                for k, v in d["eligibility"].items():
                    ui.label(f"{ELIGIBILITY_LABELS.get(k, k)}: {v}")
            with ui.card():
                ui.label(t("Embudo de aplicaciones")).classes("text-bold")
                for k in APPLICATION_STATUSES:
                    if d["applications"].get(k):
                        ui.label(f"{APPLICATION_STATUSES[k][0]}: {d['applications'][k]}")
                if not d["applications"]:
                    ui.label(t("Sin aplicaciones registradas todavía."))
            self._agent_status_card()
        self._liveness_card()
        if d["followups_due"]:
            ui.label(t("Seguimientos pendientes")).classes("text-bold")
            for r in d["followups_due"]:
                ui.label(f"{r['next_followup']} — {r['title']} ({r['company_name']})")

    # ------------------------------------------------------------------ activos
    def _liveness_card(self) -> None:
        flagged = self.svc.liveness.flagged()
        st = self.svc.liveness.settings()
        with ui.card().classes("w-full").mark("liveness_card"):
            with ui.row().classes("items-center"):
                ui.label(t("Verificación de anuncios")).classes("text-bold")
                ui.badge("automática cada {} h".format(st["interval_hours"]) if st["enabled"] else t("manual"),
                         color="positive" if st["enabled"] else "grey")

                async def run_now():
                    ui.notify(t("Verificando anuncios (puede tardar)…"))
                    res = await run.io_bound(self.svc.liveness.run_batch, None, "user", None, 0)
                    if res.get("busy"):
                        ui.notify(t("Ya hay una verificación en curso."), type="warning")
                        return
                    ui.notify(t("Verificados: {} · posiblemente cerrados: {} · "
                              "sin determinar: {}", res['checked'], res['likely_closed'], res['unknown']), type="positive")
                    self.refresh_all()
                ui.button(t("Verificar ahora"), icon="travel_explore", on_click=run_now).props("dense flat") \
                    .mark("liveness_run")
            if not flagged:
                ui.label(t("Ningún anuncio marcado como posiblemente cerrado.")).classes("text-caption text-grey-7")
                return
            ui.label(t("⚠ {} anuncio(s) posiblemente cerrado(s). Revise y confirme; nada se cambia solo.", len(flagged))) \
                .classes("text-negative")
            for f in flagged[:15]:
                with ui.row().classes("items-center"):
                    ui.label(f"{f['title']} — {f['company_name']}").classes("text-sm cursor-pointer text-primary") \
                        .on("click", lambda f=f: self.detail_dialog(f["job_id"]))
                    ui.label(f["reason"]).classes("text-caption text-grey-7")

            def expire_all():
                n = self.svc.liveness.mark_expired([f["job_id"] for f in flagged])
                ui.notify(t("{} anuncio(s) marcados como expirados.", n), type="positive")
                self.refresh_all()
            ui.button(t("Marcar los {} como expirados", len(flagged)), icon="event_busy", on_click=expire_all) \
                .props("dense outline").mark("liveness_expire_all")

    def _agent_status_card(self) -> None:
        st = self.svc.sync.inbox_stats(recent_errors=0)
        with ui.card().mark("agent_status"):
            ui.label(t("Agente externo (bandeja job_inbox)")).classes("text-bold")
            ui.label(t("Importadas: {} · Pendientes: {} · Rechazadas: {}", st['imported'], st['pending'], st['error']))
            ui.label(t("Última sincronización: {}", st['last_tick'] or '—')).classes("text-caption")
            if st["error"]:
                ui.label(t("Vea los errores en 'Configuración / Backups'.")).classes("text-caption text-negative")

    def activos_panel(self) -> None:
        if self._need_mode():
            return
        with ui.row().classes("items-center w-full"):
            inp = ui.input(t("Buscar (palabra clave)"), value=self.search["activos"]).props("clearable dense") \
                .classes("w-80").mark("activos_search")
            self.search_inputs["activos"] = inp

            def do_search():
                self.search["activos"] = inp.value or ""
                self.refresh_all()
            inp.on("keydown.enter", do_search)
            ui.button(t("Buscar"), on_click=do_search).props("dense")
            ui.button(t("Limpiar búsqueda"), on_click=lambda: (self.search.update(activos=""), self.refresh_all())) \
                .props("flat dense")
            self.org.views_bar(self.mode)
        rows = self.svc.jobs.rows(self.mode, "activos", self.search["activos"], filters=self.org.filters(self.mode))
        profile = self.svc.rules.active_profile(self.mode)
        ui.label(t("{} vacantes activas en {} · perfil '{}'", len(rows), self.mode, profile.name if profile else '—')) \
            .classes("text-grey-7")
        n_sug = len(self.svc.suggestions.suggestions(self.mode))
        if n_sug:
            ui.label(t("💡 Hay {} sugerencia(s) de reglas basadas en sus descartes: vea 'Reglas y Filtros'.", n_sug)) \
                .classes("text-amber-9 text-sm").mark("suggestions_banner")
        if not rows:
            self._zero_results("activos")
        limit = int(self.prefs.get("ui_card_limit") or 0)
        shown = rows if not limit or self.show_all_cards else rows[:limit]
        for row in shown:
            self.job_card(row)
        if len(shown) < len(rows):
            def more():
                self.show_all_cards = True
                self.refresh_all()
            ui.button(t("Mostrar las {} restantes", len(rows) - len(shown)), icon="expand_more", on_click=more) \
                .props("flat").mark("cards_more")

    def _zero_results(self, view: str) -> None:
        allr = self.svc.jobs.rows(self.mode, "all")
        blocked = sum(1 for r in allr if r["eligibility_status"] == "blocked" and r["user_decision"] != "discarded")
        disc = sum(1 for r in allr if r["user_decision"] == "discarded")
        with ui.card().classes("bg-grey-2 w-full"):
            ui.label(t("Sin resultados. Explicación:")).classes("text-bold")
            ui.label(t("• Total de vacantes en la base: {}", len(allr)))
            if self.search.get(view):
                ui.label(t("• Hay un filtro de búsqueda activo: '{}' (use 'Limpiar búsqueda').", self.search[view]))
            if self.org.filters(self.mode):
                ui.label(t("• Hay filtros o una vista guardada activos (use 'Quitar filtros')."))
            ui.label(t("• Bloqueadas por reglas duras: {} (vea 'Vacantes' → Bloqueadas, o ajuste reglas).", blocked))
            ui.label(t("• Descartadas por usted: {} (vea 'Historial' para restaurar).", disc))
            if not allr:
                ui.label(t("• Agregue una vacante en 'Agregar vacante' o importe un CSV/Excel."))

    def job_card(self, row: dict[str, Any]) -> None:
        with ui.card().classes("w-full").mark(f"card_{row['job_id']}"):
            with ui.row().classes("w-full items-center justify-between"):
                with ui.column().classes("gap-0"):
                    ui.label(("📌 " if row["pinned"] else "") + row["title"]).classes("text-subtitle1 text-bold")
                    loc = ", ".join(x for x in (row["location_text"], row["region"], row["job_country"]) if x)
                    ui.label(t("{} · {} · "
                             "{}", row['company_name'] or 'Empresa desconocida', loc or 'Ubicación desconocida', _display(row, 'work_mode'))).classes("text-grey-8")
                    if row.get("salary_note"):
                        ui.label(row["salary_note"]).classes("text-caption text-grey-8") \
                            .mark(f"salary_note_{row['job_id']}")
                    if row.get("tags"):
                        with ui.row().classes("gap-1"):
                            for tag in row["tags"].split(", "):
                                ui.chip(tag, icon="sell").props("dense outline color=secondary")
                with ui.row().classes("items-center"):
                    ui.badge(t(ELIGIBILITY_LABELS.get(row["eligibility_status"], row["eligibility_status"])),
                             color=STATUS_COLORS.get(row["eligibility_status"], "grey"))
                    ui.badge(t("Puntaje {}", round(row['technical_score'] or 0)), color="blue-grey")
                    if row.get("skills_match") is not None:
                        ui.badge(t("Skills CV {:.0f}%", row['skills_match']), color="cyan-8") \
                            .mark(f"skills_badge_{row['job_id']}")
                    if row.get("agent_score") is not None:
                        ui.badge(t("Agente {:.0f}", row['agent_score']), color="deep-purple") \
                            .mark(f"agent_badge_{row['job_id']}")
                    if row.get("ai_fit_score") is not None:
                        ui.badge(t("IA {:.0f}", row['ai_fit_score']), color="purple-9") \
                            .tooltip(row.get("ai_recommendation") or "").mark(f"ai_badge_{row['job_id']}")
                    ui.badge(t(USER_DECISION_LABELS.get(row["user_decision"], row["user_decision"])), color="indigo")
                    if row["application_status"]:
                        ui.badge(t(_display(row, "application_status")), color="teal")
                    if row["override"]:
                        ui.badge(t("Anulación manual: {}", row['override']), color="purple")
                    if row.get("liveness") == "likely_closed":
                        ui.badge(t("⚠ Posiblemente cerrada"), color="negative").tooltip(row.get("liveness_reason") or "") \
                            .mark(f"closed_badge_{row['job_id']}")
                    elif row.get("ad_status") == "expired":
                        ui.badge(t("Anuncio expirado"), color="grey-7")
            with ui.row().classes("gap-1"):
                ui.button(t("Guardar"), icon="bookmark", on_click=lambda r=row: self._decide(r, "saved")) \
                    .props("dense outline").mark(f"save_{row['job_id']}")
                ui.button(t("Tal vez / Revisar"), icon="help", on_click=lambda r=row: self._decide(r, "maybe")) \
                    .props("dense outline")
                ui.button(t("Descartar"), icon="delete", color="negative",
                          on_click=lambda r=row: self.discard_dialog(r)).props("dense outline") \
                    .mark(f"discard_{row['job_id']}")
                url = _safe_url(row["apply_url"]) or _safe_url(row["job_url"])
                if url:
                    ui.button(t("Aplicar (página externa)"), icon="open_in_new",
                              on_click=lambda u=url: ui.navigate.to(u, new_tab=True)).props("dense outline")
                ui.button(t("Ver JD / detalle"), icon="description",
                          on_click=lambda r=row: self.detail_dialog(r["job_id"])).props("dense outline") \
                    .mark(f"detail_{row['job_id']}")
                if row.get("company_id"):
                    ui.button(t("Ver empresa"), icon="business",
                              on_click=lambda r=row: self._open_company(r["company_id"])).props("dense flat")
                ui.button(t("Fijar") if not row["pinned"] else t("Desfijar"), icon="push_pin",
                          on_click=lambda r=row: (self.svc.jobs.toggle_pin(r["job_id"]), self.refresh_all())) \
                    .props("dense flat")

    def _open_company(self, company_id: str) -> None:
        from .crm_panel import company_dialog
        company_dialog(self, company_id)

    def _decide(self, row: dict[str, Any], decision: str) -> None:
        self.svc.jobs.set_decision(row["job_id"], self.mode, decision)
        ui.notify(f"'{row['title']}': {USER_DECISION_LABELS[decision]}", type="positive")
        self.refresh_all()

    def discard_dialog(self, row: dict[str, Any] | list[dict[str, Any]]) -> None:
        rows = row if isinstance(row, list) else [row]
        with self.dialog() as dlg, ui.card().classes("w-96"):
            ui.label(t("Descartar {} vacante(s)", len(rows)) if len(rows) > 1 else t("Descartar: {}", rows[0]['title'])) \
                .classes("text-bold")
            ui.label(t("La vacante se oculta de Activos pero se conserva (JD, historial) y puede restaurarse.")) \
                .classes("text-grey-7 text-caption")
            reason = ui.radio(tl(DISCARD_REASONS), value=None).mark("discard_reason")
            note = ui.input(t("Nota (opcional)")).classes("w-full")

            def confirm():
                for r in rows:
                    self.svc.jobs.set_decision(r["job_id"], self.mode, "discarded", reason.value or "", note.value or "")
                dlg.close()
                ui.notify(t("{} descartada(s). Puede restaurarlas desde Historial o Vacantes.", len(rows)), type="info")
                self.refresh_all()
            with ui.row():
                ui.button(t("Descartar"), color="negative", on_click=confirm).mark("discard_confirm")
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    # ------------------------------------------------------------------ detail
    def detail_dialog(self, job_id: str) -> None:
        mode = self.mode if self.mode != ALL else CANADA
        d = self.svc.jobs.detail(job_id, mode)
        job, ev, app = d["job"], d["evaluation"], d["application"]
        with self.dialog().props("maximized") as dlg, ui.card().classes("w-full"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(f"{job.title} — {job.company_name}").classes("text-h6")
                ui.button(icon="close", on_click=dlg.close).props("flat round")
            ui.label(t("Evaluación para el modo {}", mode)).classes("text-grey-7")
            with ui.row().classes("gap-4"):
                if _safe_url(job.canonical_url):
                    ui.link(t("Ver anuncio"), _safe_url(job.canonical_url), new_tab=True)
                if _safe_url(job.apply_url):
                    ui.link(t("Página para aplicar"), _safe_url(job.apply_url), new_tab=True)
                ui.label(t("Fuentes: {}", ', '.join(s.provider for s in d['sources']) or '—'))
                ui.label(t("row_version: {}", job.row_version))
                ui.button(t("Eliminar vacante…"), icon="delete_forever", color="negative",
                          on_click=lambda: self.delete_jobs_dialog(
                              [{"job_id": job.id, "title": job.title, "company_name": job.company_name}],
                              on_done=dlg.close)).props("dense flat").mark("detail_delete")
            with ui.row().classes("w-full no-wrap items-start"):
                with ui.column().classes("w-1/2"):
                    self._explanation(ev)
                    self._skills_match_box(ev, mode)
                    self._decision_box(job.id, mode, ev, dlg)
                    self._application_box(job.id, mode, app, d["app_events"], dlg)
                    self._override_box(job.id, mode, ev, dlg)
                with ui.column().classes("w-1/2"):
                    self.org.tags_editor(job.id)
                    self.org.liveness_box(job, dlg)
                    self._agent_box(job)
                    self._mode_fields_box(job.id, mode, ev, dlg)
                    ui.label(t("Notas")).classes("text-bold")
                    notes = ui.textarea(value=job.notes).classes("w-full").mark("notes")

                    def save_notes():
                        self.svc.jobs.update_notes(job.id, notes.value or "")
                        ui.notify(t("Notas guardadas"), type="positive")
                        self.refresh_all()
                    ui.button(t("Guardar notas"), on_click=save_notes).props("dense")
                    ui.label(t("Descripción (JD) — {} snapshot(s); texto no confiable", len(d['snapshots']))) \
                        .classes("text-bold")
                    with ui.scroll_area().classes("h-72 w-full border"):
                        ui.label(job.description or "Sin JD guardada.").classes("whitespace-pre-wrap text-sm")
                    with ui.expansion(t("Historial de decisiones")).classes("w-full"):
                        for dec in d["decisions"]:
                            ui.label(t("{:%Y-%m-%d %H:%M} [{}] {} → "
                                     "{} {} {}", dec.created_at, dec.mode_id, dec.previous_decision or '—', dec.decision, DISCARD_REASONS.get(dec.reason_code, ''), dec.note))
            from .job_extras import JobExtras
            JobExtras(self).render(job.id, mode, dlg)
        dlg.open()

    def _agent_box(self, job) -> None:
        agent = agent_info(job)
        if not agent:
            return
        with ui.card().classes("w-full bg-indigo-1 text-black").mark("agent_box"):
            ui.label(t("Evaluación del agente externo")).classes("text-bold")
            score = parse_float(agent.get("score"))
            if score is not None:
                ui.label(t("Puntaje del agente: {:.0f}/100", score))
            for key, label in (("recommendation", "Recomendación"), ("summary", "Resumen")):
                if agent.get(key):
                    ui.label(t("{}: {}", t(label), agent[key])).classes("whitespace-pre-wrap")
            others = {k: v for k, v in agent.items() if k not in ("score", "recommendation", "summary")}
            if others:
                with ui.expansion(t("Otros datos del agente")).classes("w-full"):
                    ui.label(json.dumps(others, ensure_ascii=False, indent=2, default=str)) \
                        .classes("whitespace-pre-wrap text-caption")
            ui.label(t("Generado por un agente: verifique contra la JD antes de decidir.")) \
                .classes("text-caption text-grey-8")

    def _skills_match_box(self, ev, mode: str) -> None:
        ui.label(t("JD vs CV (skills aprobados)")).classes("text-bold")
        detail = (ev.skills_detail or {}) if ev else {}
        cand = self.svc.candidate.active_candidate(mode)
        if not detail:
            if cand is None:
                msg = "Aún no hay perfil de candidato: importe su CV en la pestaña 'Skills y CV'."
            else:
                msg = (f"Sin datos: el perfil '{cand.name}' no tiene skills aprobados, o la JD no menciona "
                       "skills reconocibles. Coincidencia = desconocida (no afecta el puntaje).")
            ui.label(t(msg)).classes("text-caption text-grey-7").mark("jd_cv_empty")
            return
        pct_text = ("datos insuficientes (la JD menciona muy pocos skills concretos; no afecta el puntaje)"
                    if detail.get("pct") is None else f"{detail['pct']:.0f}%")
        ui.label(t("Coincidencia: {} · perfil '{}' "
                 "(v{})", pct_text, detail['candidate_name'], detail['candidate_version'])).mark("jd_cv_pct")
        with ui.row().classes("gap-1"):
            for m in detail.get("matched", []):
                with ui.chip(m["name"], icon="check", color="positive", text_color="white").props("dense"):
                    ui.tooltip(t("JD: {}\nCV: {}", m['jd'], m['cv'] or '—') + (t("\nNota: {}", m['notes'])
                                                                          if m.get("notes") else ""))
        missing = detail.get("missing", [])
        if missing:
            ui.label(t("Requisitos de la JD que no están en sus skills aprobados:")).classes("text-caption")
            labels = {"proposed": "propuesto en su CV, sin aprobar", "rejected": "usted lo rechazó",
                      "absent": "no aparece en su CV"}
            with ui.row().classes("gap-1"):
                for m in missing:
                    color = "warning" if m["status"] == "proposed" else "negative"
                    with ui.chip(m["name"], icon="close", color=color, text_color="white").props("dense"):
                        ui.tooltip(t("{}\nJD: {}", labels.get(m['status'], m['status']), m['jd']))
            if any(m["status"] == "proposed" for m in missing):
                ui.label(t("Amarillo = el skill aparece en su CV pero aún no lo aprueba (pestaña 'Skills y CV').")) \
                    .classes("text-caption text-grey-7")
        ui.label(t("Los skills faltantes NO se agregan a su perfil: solo usted puede aprobar skills con evidencia real.")) \
            .classes("text-caption text-grey-7")

    def _explanation(self, ev) -> None:
        ui.label(t("¿Por qué? (reglas deterministas)")).classes("text-bold")
        if ev is None:
            ui.label(t("Sin evaluación."))
            return
        ui.badge(t(ELIGIBILITY_LABELS.get(ev.eligibility_status, ev.eligibility_status)),
                 color=STATUS_COLORS.get(ev.eligibility_status, "grey"))
        ui.label(t("Puntaje técnico: {} · ruleset {}", round(ev.technical_score), ev.ruleset_version))
        rows = [{"id": i, "efecto": EFFECT_LABELS.get(o["effect"], o["effect"]), "regla": t(o["name"]),
                 "resultado": o["result"], "explicación": t(o["explanation"]), "evidencia": o["evidence"]}
                for i, o in enumerate(ev.explanation or [])]
        ui.table(rows=rows, columns=[{"name": k, "label": t(k.capitalize()), "field": k, "align": "left"}
                                     for k in ("efecto", "regla", "resultado", "explicación", "evidencia")],
                 row_key="id").props("dense wrap-cells").classes("w-full")

    def _decision_box(self, job_id: str, mode: str, ev, dlg) -> None:
        ui.label(t("Decisión")).classes("text-bold")
        current = ev.user_decision if ev else "new"
        with ui.row():
            ui.label(t("Actual: {}", USER_DECISION_LABELS.get(current, current))
                     + (f" ({DISCARD_REASONS.get(ev.discard_reason, '')})" if ev and ev.discard_reason else ""))
            if current == "discarded":
                def restore():
                    target = self.svc.jobs.restore(job_id, mode)
                    ui.notify(t("Restaurada como '{}'", USER_DECISION_LABELS[target]), type="positive")
                    dlg.close()
                    self.refresh_all()
                ui.button(t("Restaurar"), icon="restore", on_click=restore).props("dense").mark("restore")

    def _application_box(self, job_id: str, mode: str, app, events, dlg) -> None:
        ui.label(t("Aplicación")).classes("text-bold")
        with ui.row().classes("items-end"):
            status = ui.select({k: t(v[0]) for k, v in APPLICATION_STATUSES.items()},
                               value=app.status if app else None, label=t("Estado")).classes("w-48")
            follow = ui.input(t("Próximo seguimiento (AAAA-MM-DD)"),
                              value=app.next_followup.isoformat() if app and app.next_followup else "")
            note = ui.input(t("Nota"))

            def save():
                if not status.value:
                    ui.notify(t("Seleccione un estado"), type="warning")
                    return
                try:
                    nf = date.fromisoformat(follow.value) if follow.value else None
                except ValueError:
                    ui.notify(t("Fecha inválida"), type="negative")
                    return
                self.svc.jobs.set_application_status(job_id, mode, status.value, note.value or "", next_followup=nf)
                ui.notify(t("Estado actualizado"), type="positive")
                dlg.close()
                self.refresh_all()
            ui.button(t("Guardar estado"), on_click=save).props("dense")
        for e in events[:10]:
            ui.label(t("{:%Y-%m-%d %H:%M} {} → {} {}", e.created_at, e.from_status, e.to_status, e.note)).classes("text-caption")

    def _override_box(self, job_id: str, mode: str, ev, dlg) -> None:
        ui.label(t("Anulación manual (con motivo)")).classes("text-bold")
        with ui.row().classes("items-end"):
            ov = ui.select({"": t("Sin anulación"), "include": t("Incluir pese a reglas"), "exclude": t("Excluir")},
                           value=ev.override if ev else "", label=t("Anulación")).classes("w-48")
            reason = ui.input(t("Motivo"), value=ev.override_reason if ev else "")

            def save():
                try:
                    self.svc.jobs.set_override(job_id, mode, ov.value or "", reason.value or "")
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                ui.notify(t("Anulación guardada"), type="positive")
                dlg.close()
                self.refresh_all()
            ui.button(t("Guardar anulación"), on_click=save).props("dense")

    def _mode_fields_box(self, job_id: str, mode: str, ev, dlg) -> None:
        ui.label(t("Datos específicos del modo ({})", mode)).classes("text-bold")
        data = (ev.mode_data if ev else {}) or {}
        inputs = {}
        for mf in MODE_DEFS[mode].mode_fields:
            if mf.choices:
                stored = data.get(mf.key)
                options = list(mf.choices) + ([stored] if stored and stored not in mf.choices else [])
                inputs[mf.key] = ui.select(tl(options), value=stored or mf.choices[0], label=t(mf.label)).classes("w-full")
            else:
                inputs[mf.key] = ui.input(t(mf.label), value=data.get(mf.key, "")).classes("w-full")

        def save():
            self.svc.jobs.update_mode_data(job_id, mode, {k: w.value for k, w in inputs.items()})
            ui.notify(t("Datos del modo guardados y vacante re-evaluada"), type="positive")
            dlg.close()
            self.refresh_all()
        ui.button(t("Guardar datos del modo"), on_click=save).props("dense")

    # ------------------------------------------------------------------ vacantes
    def vacantes_panel(self) -> None:
        mode = self.mode
        with ui.row().classes("items-center w-full"):
            inp = ui.input(t("Buscar"), value=self.search["vacantes"]).props("clearable dense").classes("w-72")
            self.search_inputs["vacantes"] = inp

            def do_search():
                self.search["vacantes"] = inp.value or ""
                self.refresh_all()
            inp.on("keydown.enter", do_search)
            ui.button(t("Buscar"), on_click=do_search).props("dense")
            if mode != ALL:
                ui.toggle({"all": t("Todas"), "activos": t("Activos"), "blocked": t("Bloqueadas"), "discarded": t("Descartadas")},
                          value=self.vacantes_view,
                          on_change=lambda e: (setattr(self, "vacantes_view", e.value), self.refresh_all())) \
                    .props("dense")
            ui.button(t("Limpiar filtros"), on_click=lambda: (self.search.update(vacantes=""),
                                                           setattr(self, "vacantes_view", "all"),
                                                           self.refresh_all())).props("flat dense")
            if mode != ALL:
                self.org.views_bar(mode)
        if mode == ALL:
            ca = {r["job_id"]: r for r in self.svc.jobs.rows(CANADA, "all", self.search["vacantes"])}
            mx = {r["job_id"]: r for r in self.svc.jobs.rows(MEXICO, "all", self.search["vacantes"])}
            cols = ["title", "company_name", "location_text", "job_country", "work_mode", "ca_status", "mx_status"]
            rows = []
            for jid, r in ca.items():
                rows.append({**{c: _display(r, c) for c in cols[:5]}, "job_id": jid,
                             "ca_status": f"{_display(r, 'eligibility_status')} / {_display(r, 'user_decision')}",
                             "mx_status": f"{_display(mx[jid], 'eligibility_status')} / "
                                          f"{_display(mx[jid], 'user_decision')}" if jid in mx else ""})
        else:
            cols = list(MODE_DEFS[mode].columns) + ["salary_year", "tags"]
            flt = self.org.filters(mode)
            view = self.vacantes_view if self.vacantes_view != "all" or not flt.get("view") else flt["view"]
            raw = self.svc.jobs.rows(mode, view, self.search["vacantes"], filters=flt)
            rows = [{**{c: _display(r, c) for c in cols}, "job_id": r["job_id"]} for r in raw]
        ui.label(t("{} vacantes", len(rows))).classes("text-grey-7")
        table = ui.table(rows=rows, columns=[{"name": c, "label": t(COLUMN_LABELS.get(c, c)), "field": c,
                                              "sortable": True, "align": "left"} for c in cols],
                         row_key="job_id", selection="multiple" if mode != ALL else None,
                         pagination=self.page_size).classes("w-full").props("dense").mark("vacantes_table")
        table.on("rowClick", lambda e: self.detail_dialog(e.args[1]["job_id"]))
        if mode != ALL:
            with ui.row():
                def bulk(decision: str):
                    sel = table.selected
                    if not sel:
                        ui.notify(t("Seleccione filas primero"), type="warning")
                        return
                    if decision == "discarded":
                        self.discard_dialog([{"job_id": s["job_id"], "title": s["title"]} for s in sel])
                        return
                    if decision == "restore":
                        sel = [s for s in sel if s["user_decision"] == t(USER_DECISION_LABELS["discarded"])]
                        if not sel:
                            ui.notify(t("Ninguna de las filas seleccionadas está descartada"), type="warning")
                            return
                    verb = "Restaurar" if decision == "restore" else "Marcar como Guardadas"
                    if not self.prefs.get("ui_confirm_bulk", True):
                        for s in sel:
                            if decision == "restore":
                                self.svc.jobs.restore(s["job_id"], mode)
                            else:
                                self.svc.jobs.set_decision(s["job_id"], mode, decision)
                        ui.notify(t("{} vacante(s) actualizadas", len(sel)), type="positive")
                        self.refresh_all()
                        return
                    with self.dialog() as dlg, ui.card():
                        ui.label(t("¿{} {} vacante(s)?", verb, len(sel))).classes("text-bold")
                        for s in sel[:8]:
                            ui.label(f"• {s['title']} — {s['company_name']}").classes("text-caption")
                        if len(sel) > 8:
                            ui.label(t("… y {} más", len(sel) - 8)).classes("text-caption")

                        def confirm():
                            for s in sel:
                                if decision == "restore":
                                    self.svc.jobs.restore(s["job_id"], mode)
                                else:
                                    self.svc.jobs.set_decision(s["job_id"], mode, decision)
                            dlg.close()
                            ui.notify(t("{} vacante(s) actualizadas", len(sel)), type="positive")
                            self.refresh_all()
                        with ui.row():
                            ui.button(t("Confirmar"), on_click=confirm).mark("bulk_confirm")
                            ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
                    dlg.open()
                ui.button(t("Guardar seleccionadas"), on_click=lambda: bulk("saved")).props("dense outline")
                ui.button(t("Descartar seleccionadas"), on_click=lambda: bulk("discarded"), color="negative") \
                    .props("dense outline")
                ui.button(t("Restaurar seleccionadas"), on_click=lambda: bulk("restore")).props("dense outline")

                def bulk_tags():
                    if not table.selected:
                        ui.notify(t("Seleccione filas primero"), type="warning")
                        return
                    self.org.bulk_tags_dialog(list(table.selected))
                ui.button(t("Etiquetas…"), icon="sell", on_click=bulk_tags).props("dense outline").mark("bulk_tags")

                def bulk_delete():
                    if not table.selected:
                        ui.notify(t("Seleccione filas primero"), type="warning")
                        return
                    self.delete_jobs_dialog([{"job_id": s["job_id"], "title": s["title"],
                                              "company_name": s.get("company_name", "")} for s in table.selected])
                ui.button(t("Eliminar seleccionadas…"), icon="delete_forever", color="negative",
                          on_click=bulk_delete).props("dense flat").mark("bulk_delete")
        if not rows and mode != ALL:
            self._zero_results("vacantes")

    # ------------------------------------------------------------------ kanban
    def kanban_panel(self) -> None:
        if self._need_mode():
            return
        rows = [r for r in self.svc.jobs.rows(self.mode, "all") if r["application_status"]]
        ui.label(t("Arrastre una tarjeta a otra columna o use su selector (cada cambio queda en el historial). "
                 "Agregue vacantes al pipeline desde su detalle → Aplicación.")).classes("text-grey-7")
        with ui.row().classes("w-full no-wrap overflow-auto items-start"):
            for status, (label, _) in APPLICATION_STATUSES.items():
                col_rows = [r for r in rows if r["application_status"] == status]
                column = ui.card().classes("min-w-[220px] min-h-[120px] bg-grey-1").mark(f"kanban_col_{status}")
                column.on("dragover.prevent", lambda c=column: c.classes(add="bg-blue-1", remove="bg-grey-1"))
                column.on("dragleave", lambda c=column: c.classes(add="bg-grey-1", remove="bg-blue-1"))
                column.on("drop", lambda s=status: self._kanban_drop(s))
                with column:
                    ui.label(f"{label} ({len(col_rows)})").classes("text-bold")
                    for r in col_rows:
                        card = ui.card().classes("w-full cursor-move").props("draggable") \
                            .mark(f"kanban_card_{r['job_id']}")
                        card.on("dragstart", lambda r=r: self._kanban_drag_start(r))
                        with card:
                            ui.label(r["title"]).classes("text-bold text-sm cursor-pointer") \
                                .on("click", lambda r=r: self.detail_dialog(r["job_id"]))
                            ui.label(r["company_name"]).classes("text-caption")
                            ui.select({k: t(v[0]) for k, v in APPLICATION_STATUSES.items()}, value=status,
                                      on_change=lambda e, r=r: self._kanban_move(r, e.value)).props("dense")

    def _kanban_drag_start(self, row: dict[str, Any]) -> None:
        self.dragged = row

    def _kanban_drop(self, status: str) -> None:
        row, self.dragged = getattr(self, "dragged", None), None
        if row is None:
            return
        if status == row["application_status"]:
            self.refresh_all()
            return
        self._kanban_move(row, status)

    def _kanban_move(self, row: dict[str, Any], status: str) -> None:
        if status == row["application_status"]:
            return
        self.svc.jobs.set_application_status(row["job_id"], self.mode, status, "Kanban")
        ui.notify(f"{row['title']} → {APPLICATION_STATUSES[status][0]}", type="positive")
        self.refresh_all()

    # ------------------------------------------------------------------ add job
    def add_job_panel(self) -> None:
        ui.label(t("Agregar vacante manualmente (URL pegada, LinkedIn u otra fuente sin API)")).classes("text-h6")
        ui.label(t("Datos desconocidos déjelos vacíos: se tratarán como DESCONOCIDOS, no como positivos/negativos.")) \
            .classes("text-grey-7")
        f: dict[str, Any] = {}
        with ui.grid(columns=3).classes("w-full"):
            f["title"] = ui.input(t("Título del puesto *")).mark("add_title")
            f["company_name"] = ui.input(t("Empresa")).mark("add_company")
            f["canonical_url"] = ui.input(t("URL del anuncio")).mark("add_url")
            f["apply_url"] = ui.input(t("URL para aplicar"))
            f["source"] = ui.input(t("Fuente"), value="manual")
            f["source_job_id"] = ui.input(t("ID en la fuente (opcional)"))
            f["job_country"] = ui.input(t("País del puesto (CA, MX, US…)")).mark("add_country")
            f["region"] = ui.input(t("Provincia / estado"))
            f["location_text"] = ui.input(t("Ubicación (texto)"))
            f["remote_eligible_countries"] = ui.input(t("Remoto permitido desde (CA, MX, LATAM, WORLDWIDE…)")) \
                .mark("add_remote_countries")
            f["work_mode"] = ui.select(tl(WORK_MODES), value="", label=t("Modalidad")).mark("add_work_mode")
            f["seniority"] = ui.input(t("Seniority"))
            f["employment_type"] = ui.input(t("Tipo de empleo (full-time, contractor…)"))
            f["salary_min"] = ui.input(t("Salario mínimo"))
            f["salary_max"] = ui.input(t("Salario máximo"))
            f["currency"] = ui.select(["", "MXN", "USD", "CAD"], value="", label=t("Moneda"))
            f["salary_period"] = ui.select(["", "year", "month", "hour"], value="", label=t("Periodo"))
            f["posted_date"] = ui.input(t("Fecha publicación (AAAA-MM-DD)"))
        f["sponsorship_statement"] = ui.input(t("Declaración de visa/patrocinio (copiar texto exacto, si existe)")) \
            .classes("w-full")
        f["description"] = ui.textarea(t("Descripción completa (JD)")).classes("w-full").mark("add_description")

        def save():
            data = {k: w.value for k, w in f.items()}
            try:
                inp = JobInput(**data)
            except Exception as exc:  # pydantic ValidationError / ValueError
                ui.notify(t("Datos inválidos: {}", exc), type="negative", multi_line=True)
                return
            mode = self.mode if self.mode != ALL else None
            job, status = self.svc.jobs.upsert_job(inp, mode)
            msg = {"created": "Vacante creada", "updated": "Vacante existente actualizada (duplicado detectado)",
                   "unchanged": "Duplicado: la vacante ya existía sin cambios"}[status]
            ui.notify(t(msg), type="positive" if status == "created" else "info")
            for k, w in f.items():
                w.value = "manual" if k == "source" else ""
            self.refresh_all()
        ui.button(t("Guardar vacante"), icon="save", on_click=save).mark("add_save")

    # ------------------------------------------------------------------ rules
    def rules_panel(self) -> None:
        if self._need_mode():
            return
        mode = self.mode
        self._profile_bar(mode)
        self._suggestions_box(mode)
        with ui.row().classes("items-center"):
            ui.label(t("Reglas del perfil activo ({})", mode)).classes("text-h6")
            ui.button(t("Nueva regla"), icon="add", on_click=lambda: self.rule_dialog(None)).mark("new_rule")
        ui.label(t("Las reglas se evalúan en orden de prioridad. 'Requerir' + 'Duro' bloquea; los datos faltantes "
                 "siguen la política de 'desconocido'. Use 'Probar' para simular antes de guardar.")) \
            .classes("text-grey-7")
        for r in self.svc.rules.list(mode):
            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center justify-between no-wrap"):
                    with ui.row().classes("items-center no-wrap"):
                        ui.switch(value=r.enabled, on_change=lambda e, r=r: self._toggle_rule(r.id, e.value)) \
                            .mark(f"rule_switch_{r.id}")
                        with ui.column().classes("gap-0"):
                            ui.label(t(r.name)).classes("text-bold")
                            val = ", ".join(map(str, r.value)) if isinstance(r.value, list) else (r.value or "")
                            ui.label(f"{t(FIELDS[r.field].label) if r.field in FIELDS else r.field} "
                                     f"{t(OPERATOR_LABELS.get(r.operator, r.operator))} {val}"[:200]).classes("text-sm")
                            ui.label(t(r.explanation)).classes("text-caption text-grey-7")
                    with ui.row().classes("items-center no-wrap"):
                        ui.badge(t(ACTION_LABELS.get(r.action, r.action)), color="blue")
                        ui.badge(t(SEVERITY_LABELS.get(r.severity, r.severity)),
                                 color="negative" if r.severity == "hard" else "grey")
                        if r.action == "weight":
                            ui.badge(t("+{:g}", r.weight), color="teal")
                        if r.logic_group:
                            ui.badge(t("grupo {} ({})", r.logic_group, r.group_operator), color="purple")
                        ui.label(t("v{}", r.version)).classes("text-caption")
                        ui.button(icon="arrow_upward", on_click=lambda r=r: self._move_rule(r.id, -1)).props("flat dense")
                        ui.button(icon="arrow_downward", on_click=lambda r=r: self._move_rule(r.id, 1)).props("flat dense")
                        ui.button(icon="edit", on_click=lambda r=r: self.rule_dialog(r)).props("flat dense")
                        ui.button(icon="delete", on_click=lambda r=r: self._delete_rule(r)).props("flat dense")

    def _profile_bar(self, mode: str) -> None:
        profiles = self.svc.rules.profiles(mode)
        active = self.svc.rules.active_profile(mode)
        with ui.card().classes("w-full bg-blue-1"):
            with ui.row().classes("items-end w-full"):
                ui.select({p.id: f"{p.name} (v{p.version})" for p in profiles}, value=active.id if active else None,
                          label=t("Perfil de búsqueda activo"),
                          on_change=lambda e: self._activate_profile(mode, e.value)).classes("w-72") \
                    .mark("profile_select")
                ui.button(t("Guardar como nuevo perfil"), icon="content_copy",
                          on_click=lambda: self._profile_name_dialog(mode, None)).props("dense outline") \
                    .mark("profile_clone")
                if active:
                    ui.button(t("Renombrar"), icon="edit",
                              on_click=lambda: self._profile_name_dialog(mode, active)).props("dense flat")
                    if not active.is_default:
                        ui.button(t("Eliminar perfil…"), icon="delete",
                                  on_click=lambda: self._delete_profile_dialog(mode, profiles, active)) \
                            .props("dense flat")
            ui.label(t("Un perfil es un conjunto de reglas con nombre. Cambiar de perfil reevalúa las vacantes; "
                     "no borra decisiones, notas ni aplicaciones.")).classes("text-caption text-grey-7")

    def _activate_profile(self, mode: str, profile_id: str) -> None:
        active = self.svc.rules.active_profile(mode)
        if not profile_id or (active and active.id == profile_id):
            return
        self.svc.rules.set_active_profile(mode, profile_id)
        ui.notify(t("Perfil activado; vacantes reevaluadas"), type="positive")
        self.refresh_all()

    def _profile_name_dialog(self, mode: str, profile) -> None:
        with self.dialog() as dlg, ui.card().classes("w-96"):
            ui.label(t("Renombrar perfil") if profile else t("Guardar reglas actuales como nuevo perfil")).classes("text-bold")
            if not profile:
                ui.label(t("Se copian las reglas del perfil activo; el original no cambia.")).classes("text-caption")
            name = ui.input(t("Nombre del perfil"), value=profile.name if profile else "").classes("w-full") \
                .mark("profile_name")

            def confirm():
                try:
                    if profile:
                        self.svc.rules.rename_profile(profile.id, name.value or "")
                    else:
                        self.svc.rules.clone_profile(mode, name.value or "")
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                dlg.close()
                ui.notify(t("Perfil guardado"), type="positive")
                self.refresh_all()
            with ui.row():
                ui.button(t("Guardar"), on_click=confirm).mark("profile_confirm")
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    def _delete_profile_dialog(self, mode: str, profiles, active) -> None:
        others = {p.id: p.name for p in profiles if p.id != active.id}
        with self.dialog() as dlg, ui.card().classes("w-96"):
            ui.label(t("Eliminar el perfil '{}'", active.name)).classes("text-bold")
            ui.label(t("Elija el perfil que quedará activo. El historial de versiones de las reglas se conserva.")) \
                .classes("text-caption")
            target = ui.select(tl(others), value=next(iter(others)), label=t("Activar en su lugar"))

            def confirm():
                try:
                    self.svc.rules.set_active_profile(mode, target.value)
                    self.svc.rules.delete_profile(active.id)
                except (ValueError, KeyError) as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                dlg.close()
                self.refresh_all()
            with ui.row():
                ui.button(t("Eliminar"), color="negative", on_click=confirm)
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    def _suggestions_box(self, mode: str) -> None:
        suggestions = self.svc.suggestions.suggestions(mode)
        if not suggestions:
            return
        with ui.card().classes("w-full bg-amber-1"):
            ui.label(t("💡 Sugerencias basadas en sus descartes ({})", len(suggestions))).classes("text-bold")
            ui.label(t("Nada se aplica automáticamente: revise la regla, pruébela y guárdela si está de acuerdo.")) \
                .classes("text-caption text-grey-8")
            for sug in suggestions:
                with ui.row().classes("w-full items-center justify-between no-wrap").mark(f"suggestion_{sug.key}"):
                    with ui.column().classes("gap-0"):
                        ui.label(f"{sug.title}").classes("text-sm text-bold")
                        ui.label(t("Motivo: {} · {} descartes · ej.: ", sug.reason_label, sug.count)
                                 + "; ".join(sug.examples[:3])).classes("text-caption")
                    with ui.row().classes("no-wrap"):
                        ui.button(t("Revisar y crear regla"), icon="rule",
                                  on_click=lambda s=sug: self.rule_dialog(None, preset=s.rule)).props("dense") \
                            .mark(f"suggestion_open_{sug.key}")
                        ui.button(t("Ignorar"), on_click=lambda s=sug: self._dismiss_suggestion(mode, s.key)) \
                            .props("dense flat").mark(f"suggestion_dismiss_{sug.key}")

    def _dismiss_suggestion(self, mode: str, key: str) -> None:
        self.svc.suggestions.dismiss(mode, key)
        self.refresh_all()

    def _toggle_rule(self, rule_id: str, enabled: bool) -> None:
        self.svc.rules.set_enabled(rule_id, enabled)
        ui.notify(t("Regla ") + (t("activada") if enabled else t("desactivada")) + t("; vacantes re-evaluadas"), type="info")
        self.refresh_all()

    def _move_rule(self, rule_id: str, delta: int) -> None:
        self.svc.rules.move(rule_id, delta)
        self.refresh_all()

    def _delete_rule(self, rule) -> None:
        with self.dialog() as dlg, ui.card():
            ui.label(t("¿Eliminar la regla '{}'? Se conserva su historial de versiones.", rule.name))
            with ui.row():
                def confirm():
                    self.svc.rules.delete(rule.id)
                    dlg.close()
                    self.refresh_all()
                ui.button(t("Eliminar"), color="negative", on_click=confirm)
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    def rule_dialog(self, rule, preset: dict[str, Any] | None = None) -> None:
        mode = self.mode
        fields = {f.key: f.label for f in fields_for_mode(mode)}
        defaults = {"name": "", "field": "title", "operator": None, "value": "", "action": "exclude",
                    "severity": "soft", "unknown_handling": "keep", "weight": 0, "scope": "prefilter",
                    "logic_group": "", "group_operator": "AND", "priority": 100, "explanation": ""}
        src = rule if rule is not None else SimpleNamespace(**{**defaults, **(preset or {})})
        val0 = ", ".join(map(str, src.value)) if isinstance(src.value, list) else str(src.value or "")
        with self.dialog() as dlg, ui.card().classes("w-[900px] max-w-full"):
            ui.label(t("Editar regla") if rule else (t("Nueva regla (sugerida — revise y confirme)") if preset
                                                  else t("Nueva regla"))).classes("text-h6")
            with ui.grid(columns=3).classes("w-full"):
                name = ui.input(t("Nombre"), value=src.name).mark("rule_name")
                field = ui.select(tl(fields), value=src.field, label=t("Campo")).mark("rule_field")
                op = ui.select({}, label=t("Operador")).mark("rule_operator")
                value = ui.input(t("Valor"), value=val0).mark("rule_value")
                action = ui.select(tl(ACTION_LABELS), value=src.action, label=t("Acción")).mark("rule_action")
                severity = ui.select(tl(SEVERITY_LABELS), value=src.severity, label=t("Severidad")).mark("rule_severity")
                unknown = ui.select(tl(UNKNOWN_LABELS), value=src.unknown_handling, label=t("Si el dato es desconocido"))
                weight = ui.number(t("Peso (para 'Sumar puntaje')"), value=src.weight)
                scope = ui.select(tl(SCOPE_LABELS), value=src.scope, label=t("Alcance"))
                group = ui.input(t("Grupo lógico (opcional)"), value=src.logic_group)
                group_op = ui.select(list(GROUP_OPERATORS), value=src.group_operator, label=t("Operador de grupo"))
                priority = ui.number(t("Prioridad (menor = primero)"), value=src.priority)
            explanation = ui.input(t("Explicación visible"), value=src.explanation).classes("w-full")
            hint = ui.label().classes("text-caption text-grey-7")

            def update_ops(_=None):
                ftype = FIELDS[field.value].type
                ops = {o: OPERATOR_LABELS[o] for o in OPERATORS_BY_TYPE[ftype]}
                keep = op.value if op.value in ops else (src.operator if src.operator in ops else next(iter(ops)))
                op.set_options(ops, value=keep)
                update_hint()

            def update_hint(_=None):
                kind = VALUE_KIND.get(op.value or "", "single")
                hint.text = {"list": "Valor: lista separada por comas (ej. CA, MX).",
                             "range": "Valor: mínimo, máximo.", "days": "Valor: número de días.",
                             "none": "Este operador no usa valor.", "scalar": "Valor: número o fecha AAAA-MM-DD.",
                             }.get(kind, "Valor: texto.")
            field.on_value_change(update_ops)
            op.on_value_change(update_hint)
            update_ops()
            result_box = ui.column().classes("w-full")

            def data() -> dict[str, Any]:
                return {"name": name.value or "", "field": field.value, "operator": op.value, "value": value.value,
                        "action": action.value, "severity": severity.value, "unknown_handling": unknown.value,
                        "weight": weight.value or 0, "scope": scope.value, "logic_group": group.value or "",
                        "group_operator": group_op.value, "priority": priority.value or 100,
                        "explanation": explanation.value or ""}

            def simulate():
                result_box.clear()
                try:
                    sim = self.svc.rules.simulate(mode, data(), rule.id if rule else None)
                except RuleValidationError as exc:
                    with result_box:
                        for e in exc.errors:
                            ui.label(f"⚠ {e}").classes("text-negative")
                    return
                with result_box:
                    counts = ", ".join(f"{ELIGIBILITY_LABELS.get(k, k)}: {v}" for k, v in sim["counts"].items())
                    ui.label(t("Simulación sobre {} vacantes — cambiarían {}. "
                             "Resultado: {}", sim['total'], sim['changed'], counts or 'sin vacantes')).classes("text-bold").mark("sim_summary")
                    rows = [{"id": j["job_id"], "puesto": j["title"], "empresa": j["company"],
                             "regla": f"{j['draft_result']} → {EFFECT_LABELS.get(j['draft_effect'], j['draft_effect'])}",
                             "antes": ELIGIBILITY_LABELS.get(j["before"], j["before"]),
                             "después": ELIGIBILITY_LABELS.get(j["after"], j["after"])}
                            for j in sim["jobs"]]
                    ui.table(rows=rows, columns=[{"name": k, "label": t(k.capitalize()), "field": k, "align": "left"}
                                                 for k in ("puesto", "empresa", "regla", "antes", "después")],
                             row_key="id", pagination=10).props("dense").classes("w-full")

            def save():
                try:
                    if rule:
                        self.svc.rules.update(rule.id, data())
                    else:
                        self.svc.rules.create(mode, data())
                except RuleValidationError as exc:
                    ui.notify(t("Regla inválida: ") + "; ".join(exc.errors), type="negative", multi_line=True)
                    return
                ui.notify(t("Regla guardada (nueva versión) y vacantes re-evaluadas"), type="positive")
                dlg.close()
                self.refresh_all()
            with ui.row():
                ui.button(t("Probar (simular)"), icon="science", on_click=simulate).mark("rule_simulate")
                ui.button(t("Guardar"), icon="save", on_click=save).mark("rule_save")
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
            if rule:
                with ui.expansion(t("Versiones anteriores")).classes("w-full"):
                    for v in self.svc.rules.versions(rule.id):
                        ui.label(t("v{} {} {:%Y-%m-%d %H:%M}: "
                                 "{} {} "
                                 "enabled={}", v.version, v.change, v.created_at, v.snapshot.get('operator'), v.snapshot.get('value'), v.snapshot.get('enabled'))).classes("text-caption")
        dlg.open()

    # ------------------------------------------------------------------ excel / csv
    def excel_panel(self) -> None:
        if self._need_mode():
            return
        mode = self.mode
        ui.label(t("Libro Excel del modo {}: {}", mode, MODE_DEFS[mode].workbook_name)).classes("text-h6")
        ui.label(t("SQLite es la fuente de verdad. Columnas verdes = editables. Reimportar muestra una vista previa "
                 "y crea un backup antes de aplicar.")).classes("text-grey-7")

        def export():
            path = self.svc.workbooks.export_mode(mode)
            ui.notify(t("Exportado: {}", path), type="positive")
            ui.download.file(path)
        ui.button(t("Exportar libro Excel"), icon="download", on_click=export).mark("export_excel")

        ui.separator()
        ui.label(t("Reimportar libro editado (vista previa primero)")).classes("text-bold")
        preview_box = ui.column().classes("w-full")

        async def on_xlsx(e):
            tmp = Path(tempfile.mkdtemp()) / (Path(e.file.name).name or "upload.xlsx")
            await e.file.save(tmp)
            pv = self.svc.workbooks.preview_import(tmp, mode)
            preview_box.clear()
            with preview_box:
                for err in pv.errors:
                    ui.label(f"⛔ {err}").classes("text-negative")
                if pv.errors:
                    return
                for w in pv.warnings:
                    ui.label(f"⚠ {w}").classes("text-warning")
                ui.label(t("{} cambios, {} conflictos, {} vacantes "
                         "nuevas, {} reglas nuevas", len(pv.changes), len(pv.conflicts), len(pv.new_jobs), len(pv.new_rules))).classes("text-bold")
                rows = [{"id": i, "hoja": c.sheet, "registro": c.label, "campo": c.field, "actual": str(c.old),
                         "excel": str(c.new), "conflicto": "⚠ " + c.note if c.conflict else ""}
                        for i, c in enumerate(pv.changes)]
                ui.table(rows=rows, columns=[{"name": k, "label": t(k.capitalize()), "field": k, "align": "left"}
                                             for k in ("hoja", "registro", "campo", "actual", "excel", "conflicto")],
                         row_key="id", pagination=20).props("dense").classes("w-full")
                inc = ui.checkbox(t("Aplicar también conflictos (Excel sobrescribe cambios hechos en la app)"))

                def apply():
                    summary = self.svc.workbooks.apply_import(pv, include_conflicts=inc.value)
                    preview_box.clear()
                    with preview_box:
                        ui.label(t("Aplicado: {} cambios, {} creados, "
                                 "{} conflictos omitidos. Backup: {}", summary['applied'], summary['created'], summary['skipped_conflicts'], summary['backup']))
                    self.refresh_all()
                ui.button(t("Aplicar cambios"), color="primary", on_click=apply)
        ui.upload(label=t("Subir .xlsx"), auto_upload=True, on_upload=on_xlsx).props('accept=".xlsx"')

        ui.separator()
        ui.label(t("Importar CSV (p. ej. su CSV anterior) con mapeo de columnas")).classes("text-bold")
        ui.label(t("Los encabezados del CSV anterior no están verificados: revise el mapeo sugerido. Las columnas no "
                 "mapeadas se conservan como metadatos.")).classes("text-grey-7")
        csv_box = ui.column().classes("w-full")

        async def on_csv(e):
            data = await e.file.read()
            render_csv(data, e.file.name, "auto")

        def render_csv(data: bytes, file_name: str, encoding: str) -> None:
            headers, rows, enc = read_csv_bytes(data, encoding)
            saved = self.svc.db.get_setting("csv_mapping_last", {}) or {}
            mapping = {h: saved.get(h, m) for h, m in suggest_mapping(headers).items()}
            options = {IGNORE: "(ignorar)", EXTRA: "(guardar como metadato)", **TARGET_FIELDS}
            csv_box.clear()
            with csv_box:
                with ui.row().classes("items-center"):
                    ui.label(t("{}: {} filas · codificación detectada: {}", file_name, len(rows), enc))
                    ui.select(tl(ENCODINGS), value=encoding, label=t("Codificación"),
                              on_change=lambda ev: render_csv(data, file_name, ev.value)).classes("w-80") \
                        .mark("csv_encoding")
                ui.label(t("Si ve acentos raros (p. ej. 'Ã©' o '√©') en los ejemplos, cambie la codificación. "
                         "Excel para Mac suele usar 'Mac Roman'.")).classes("text-caption text-grey-7")
                selects = {}
                with ui.grid(columns=3):
                    for h in headers:
                        sample = next((r.get(h) for r in rows if r.get(h)), "")
                        selects[h] = ui.select(tl(options), value=mapping[h], label=t("{} (ej: {})", h, str(sample)[:30]))
                source = ui.input(t("Nombre de la fuente"), value="csv")
                result = ui.column().classes("w-full")

                def current_mapping():
                    return {h: s.value for h, s in selects.items()}

                def preview():
                    pv = self.svc.csv.preview(rows, current_mapping(), source.value or "csv")
                    result.clear()
                    with result:
                        ui.label(t("Nuevas: {} · Existentes (se actualizan): {} · "
                                 "Duplicadas en archivo: {} · Errores: {}", pv.count('new'), pv.count('update'), pv.count('duplicate_in_file'), pv.count('error')))
                        ui.table(rows=[{"id": r.index, "fila": r.index, "estado": r.status, "puesto": r.title,
                                        "empresa": r.company, "mensaje": r.message} for r in pv.rows],
                                 columns=[{"name": k, "label": t(k.capitalize()), "field": k, "align": "left"}
                                          for k in ("fila", "estado", "puesto", "empresa", "mensaje")],
                                 row_key="id", pagination=15).props("dense").classes("w-full")

                        def do_import():
                            s = self.svc.csv.apply(rows, current_mapping(), mode, file_name, source.value or "csv")
                            ui.notify(t("Importado: {} nuevas, {} actualizadas, "
                                      "{} sin cambios, {} errores", s['created'], s['updated'], s['unchanged'], s['errors'])
                                      + (t(", {} omitidas (usted las eliminó)", s['skipped_deleted'])
                                         if s.get("skipped_deleted") else ""), type="positive")
                            result.clear()
                            self.refresh_all()
                        if pv.count("new") + pv.count("update"):
                            ui.button(t("Importar (crea backup antes)"), on_click=do_import)
                ui.button(t("Vista previa"), on_click=preview)
        ui.upload(label=t("Subir .csv"), auto_upload=True, on_upload=on_csv).props('accept=".csv"')

    # ------------------------------------------------------------------ history
    def history_panel(self) -> None:
        mode = self.mode
        if mode != ALL:
            disc = self.svc.jobs.rows(mode, "discarded")
            ui.label(t("Descartadas en {} ({})", mode, len(disc))).classes("text-h6")
            for r in disc:
                with ui.row().classes("items-center"):
                    ui.label(f"{r['title']} — {r['company_name']} · {DISCARD_REASONS.get(r['discard_reason'], 'sin motivo')}")

                    def restore(r=r):
                        target = self.svc.jobs.restore(r["job_id"], mode)
                        ui.notify(t("Restaurada como {}", USER_DECISION_LABELS[target]), type="positive")
                        self.refresh_all()
                    ui.button(t("Restaurar"), icon="restore", on_click=restore).props("dense outline") \
                        .mark(f"restore_{r['job_id']}")
        ui.label(t("Bitácora de auditoría")).classes("text-h6")
        hist = self.svc.jobs.history(mode, limit=300)
        ui.table(rows=[{"id": i, "fecha": h["created_at"][:19], "acción": h["action"], "vacante": h["job"],
                        "modo": h["mode_id"], "actor": h["actor"], "detalles": str(h["details"])[:150]}
                       for i, h in enumerate(hist)],
                 columns=[{"name": k, "label": t(k.capitalize()), "field": k, "align": "left", "sortable": True}
                          for k in ("fecha", "acción", "vacante", "modo", "actor", "detalles")],
                 row_key="id", pagination=25).props("dense").classes("w-full")
        deleted = self.svc.jobs.deleted_jobs()
        with ui.expansion(t("Vacantes eliminadas definitivamente ({})", len(deleted)), icon="delete_forever") \
                .classes("w-full").mark("deleted_jobs"):
            ui.label(t("No se vuelven a importar desde Descubrir, agentes, CSV ni Excel. Para recuperar los datos use "
                     "un backup (Configuración → Restaurar). 'Permitir reimportar' solo deja que vuelva a entrar como "
                     "vacante nueva.")).classes("text-caption text-grey-7")
            for tomb in deleted[:200]:
                with ui.row().classes("items-center"):
                    ui.label(f"{tomb['deleted_at'][:16]} · {tomb['title']} — {tomb['company_name'] or '—'}") \
                        .classes("text-sm")

                    def forget(tomb=tomb):
                        self.svc.jobs.forget_deleted(tomb["id"])
                        ui.notify(t("Podrá volver a importarse."), type="info")
                        self.refresh_all()
                    ui.button(t("Permitir reimportar"), on_click=forget).props("dense flat")

    def delete_jobs_dialog(self, rows: list[dict[str, Any]], on_done=None) -> None:
        """Permanent deletion with an explicit confirmation word; a backup is created first."""
        with self.dialog() as dlg, ui.card().classes("w-[560px] max-w-full"):
            ui.label(t("Eliminar definitivamente {} vacante(s)", len(rows))).classes("text-h6 text-negative")
            for r in rows[:8]:
                ui.label(f"• {r['title']} — {r.get('company_name') or ''}").classes("text-caption")
            if len(rows) > 8:
                ui.label(t("… y {} más", len(rows) - 8)).classes("text-caption")
            ui.label(t("Se borran la vacante, su JD, evaluaciones, decisiones, aplicación y su historial de estados, "
                     "análisis IA y propuestas de CV. Se conservan los CV adaptados ya aprobados y las interacciones "
                     "con contactos (sin vínculo). Antes se crea un backup automático. Descubrir, los agentes y las "
                     "importaciones no la volverán a crear.")).classes("text-sm")
            ui.label(t("Si solo quiere ocultarla, use 'Descartar' (reversible).")).classes("text-caption text-grey-7")
            confirm_word = t("ELIMINAR")
            word = ui.input(t("Escriba {} para confirmar", confirm_word)).classes("w-64").mark("delete_confirm_word")

            def confirm():
                if (word.value or "").strip().upper() not in {confirm_word, "ELIMINAR"}:
                    ui.notify(t("Escriba {} para confirmar.", confirm_word), type="warning")
                    return
                res = self.svc.jobs.delete_jobs([r["job_id"] for r in rows])
                dlg.close()
                ui.notify(t("{} vacante(s) eliminada(s). Backup: {}", res['deleted'], res['backup']), type="positive",
                          multi_line=True)
                if on_done:
                    on_done()
                self.refresh_all()
            with ui.row():
                ui.button(t("Eliminar definitivamente"), icon="delete_forever", color="negative", on_click=confirm) \
                    .mark("delete_confirm")
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    # ------------------------------------------------------------------ config
    def config_panel(self) -> None:
        from .settings_panel import appearance_section, currency_section, danger_zone, liveness_section
        appearance_section(self)
        currency_section(self)
        liveness_section(self)
        p = self.svc.paths
        ui.label(t("Carpeta portátil")).classes("text-h6")
        for label, path in (("Inicio", p.home), ("Base de datos", p.db_file), ("Excel", p.excel),
                            ("Backups", p.backups), ("Exportaciones", p.exports), ("Logs", p.logs)):
            ui.label(t("{}: {}", t(label), path)).classes("text-sm")
        ui.label(t("Servidor local solo en 127.0.0.1 (no expuesto a la red). La app no llama a modelos de IA: use "
                 "'Análisis IA' (paquetes) o la API/MCP de abajo.")).classes("text-grey-7")
        self._inbox_section()
        self._api_section()

        def backup():
            path = self.svc.db.backup("manual")
            ui.notify(t("Backup creado: {}", path), type="positive")
            self.refresh_all()
        ui.button(t("Crear backup ahora"), icon="backup", on_click=backup).mark("backup_now")
        backups = self.svc.db.list_backups()[:30]
        ui.label(t("Backups recientes ({})", len(backups))).classes("text-bold")
        ui.label(t("Restaurar reemplaza TODOS los datos actuales por los del backup. Antes se crea automáticamente "
                 "una copia de seguridad del estado actual ('pre_restore').")).classes("text-caption text-grey-7")
        for idx, b in enumerate(backups):
            with ui.row().classes("items-center"):
                ui.label(t("{} — {} KB", b.name, b.stat().st_size // 1024)).classes("text-caption")
                ui.button(t("Restaurar…"), icon="settings_backup_restore",
                          on_click=lambda b=b: self._restore_dialog(b)).props("dense flat") \
                    .mark(f"restore_backup_{idx}")
        ui.separator()
        danger_zone(self)

    def _inbox_section(self) -> None:
        st = self.svc.sync.inbox_stats()
        ui.label(t("Agente externo")).classes("text-h6")
        ui.label(t("Modo de diario SQLite: {} (WAL = la app y el agente pueden trabajar a "
                 "la vez). Bandeja job_inbox: {} importadas, {} pendientes, "
                 "{} rechazadas. Última sincronización: {}.", st['journal_mode'].upper(), st['imported'], st['pending'], st['error'], st['last_tick'] or '—')).classes("text-sm")
        if st["last_error"]:
            ui.label(t("Último aviso: {}", st['last_error'])).classes("text-caption text-warning")
        ui.label(t("Contrato para el agente: vea INSTRUCCIONES.md → 'Agente externo'.")).classes("text-caption")
        if st["errors"]:
            ui.label(t("Filas rechazadas recientes (corrija el agente y pulse Reintentar):")).classes("text-bold")
            ui.table(rows=st["errors"], columns=[{"name": k, "label": t(lbl), "field": k, "align": "left"} for k, lbl in
                                                 (("id", "#"), ("created_at", "Fecha"), ("source", "Fuente"),
                                                  ("message", "Error"), ("payload", "Payload (inicio)"))],
                     row_key="id").props("dense wrap-cells").classes("w-full").mark("inbox_errors")

            def retry():
                n = self.svc.sync.retry_errors()
                ui.notify(t("{} fila(s) vuelven a 'pending'; se procesarán en la próxima sincronización.", n))
                self.refresh_all()
            ui.button(t("Reintentar rechazadas"), icon="replay", on_click=retry).props("dense outline")

    def _api_section(self) -> None:
        from ..api import api_enabled, ensure_tokens, regenerate_token
        from ..mcp_server import config_snippets, launch_command
        svc = self.svc
        port = getattr(svc, "api_port", 8765)
        base = f"http://127.0.0.1:{port}/api/v1"
        ui.label(t("API local y MCP (para agentes)")).classes("text-h6")
        ui.label(t("Agentes locales (Claude Code/Desktop, VS Code Copilot, scripts, modelos locales) pueden leer y "
                 "completar sus datos. Solo desde esta computadora (127.0.0.1); nunca cambian sus decisiones. Para "
                 "ChatGPT/Claude en la web use los paquetes de 'Análisis IA'.")).classes("text-sm")

        def toggle(e):
            svc.db.set_setting("api_enabled", bool(e.value))
            ui.notify(t("API local ") + (t("activada") if e.value else t("desactivada")), type="info")
        ui.switch(t("API REST local activa"), value=api_enabled(svc), on_change=toggle).mark("api_enabled")
        ui.label(t("URL: {}   ·   Documentación interactiva: {}/docs", base, base)).classes("text-sm")
        tokens = ensure_tokens(svc)
        for kind, label in (("write", "Token de lectura y escritura"), ("read", "Token de solo lectura")):
            with ui.row().classes("items-center"):
                ui.label(t("{}: {}", t(label), tokens[kind][:9] + "…")).classes("text-sm w-96")
                ui.button(t("Copiar"), icon="content_copy",
                          on_click=lambda k=kind: (ui.clipboard.write(ensure_tokens(svc)[k]),
                                                   ui.notify(t("Token copiado"), type="positive"))) \
                    .props("dense flat").mark(f"copy_token_{kind}")

                def regen(k=kind):
                    regenerate_token(svc, k)
                    ui.notify(t("Token regenerado: actualice la configuración de sus agentes."), type="warning")
                    self.refresh_all()
                ui.button(t("Regenerar"), icon="autorenew", on_click=regen).props("dense flat")
        command, prefix = launch_command()
        snippets = config_snippets(command, str(svc.paths.home), prefix)
        with ui.expansion(t("Configurar un cliente MCP (copiar y pegar)"), icon="hub").classes("w-full"):
            for key, label in (("claude_desktop", "Claude Desktop (claude_desktop_config.json)"),
                               ("vscode", "VS Code (.vscode/mcp.json)"), ("claude_code", "Claude Code (terminal)")):
                with ui.row().classes("items-center"):
                    ui.label(t(label)).classes("text-bold text-sm")
                    ui.button(t("Copiar"), icon="content_copy",
                              on_click=lambda k=key: (ui.clipboard.write(snippets[k]),
                                                      ui.notify(t("Copiado"), type="positive"))).props("dense flat")
                ui.label(snippets[key]).classes("whitespace-pre-wrap text-caption font-mono")
            ui.label(t("Agregue \"--read-only\" a los argumentos para que el agente solo pueda leer.")) \
                .classes("text-caption")
        ui.select({"en": t("Inglés"), "es": t("Español")}, value=svc.db.get_setting("resume_language", "en"),
                  label=t("Idioma de los títulos del CV generado (.docx)"),
                  on_change=lambda e: svc.db.set_setting("resume_language", e.value)).classes("w-80")

    def _restore_dialog(self, backup_file: Path) -> None:
        try:
            info = self.svc.db.inspect_backup(backup_file)
        except ValueError as exc:
            ui.notify(t("No se puede restaurar: {}", exc), type="negative", multi_line=True)
            return
        with self.dialog() as dlg, ui.card().classes("w-[480px]"):
            ui.label(t("Restaurar backup")).classes("text-h6")
            ui.label(t("Archivo: {}", info['file'].name))
            ui.label(t("Vacantes en el backup: {} · esquema v{} · "
                     "{} KB", info['jobs'], info['schema_version'], info['size_kb']))
            ui.label(t("⚠ Los cambios hechos después de ese backup se perderán (quedan guardados en la copia "
                     "'pre_restore' que se crea ahora).")).classes("text-warning")

            def confirm():
                try:
                    safety = self.svc.db.restore(backup_file)
                except Exception as exc:  # restore errors must be visible, never crash the UI
                    ui.notify(t("Error al restaurar: {}", exc), type="negative", multi_line=True)
                    return
                dlg.close()
                ui.notify(t("Datos restaurados. Copia del estado anterior: {}", safety.name if safety else '—'),
                          type="positive", multi_line=True)
                self.refresh_all()
                ui.navigate.reload()  # header (mode selector) is not refreshable; reload the whole page
            with ui.row():
                ui.button(t("Restaurar"), color="negative", on_click=confirm).mark("restore_confirm")
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()


def index() -> None:
    """Root page builder used by ui.run (one MainPage per browser client)."""
    if _services is None:
        raise RuntimeError("UI not configured; call configure(services) first")
    ui.page_title(t("Job Search Harness"))
    MainPage(_services).build()
