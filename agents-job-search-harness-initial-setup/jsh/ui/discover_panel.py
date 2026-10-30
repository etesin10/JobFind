"""'Descubrir' tab: saved job searches (sources), run now, run history and connector errors."""
from __future__ import annotations

import logging

from nicegui import run, ui

from ..modes import MODE_DEFS
from ..i18n import t, tl

log = logging.getLogger(__name__)
STATUS = {"ok": ("Correcto", "positive"), "partial": ("Parcial", "warning"), "error": ("Error", "negative"),
          "running": ("En curso", "info"), "": ("Nunca ejecutada", "grey")}


class DiscoverPanel:
    def __init__(self, page):
        self.page = page
        self.svc = page.svc
        self.ds = page.svc.discovery

    def render(self) -> None:
        ui.label(t("Descubrir vacantes")).classes("text-h6")
        ui.label(t("Búsquedas guardadas en fuentes oficiales o públicas. Cada ejecución trae vacantes nuevas, actualiza "
                 "las existentes sin tocar sus decisiones ni notas, y registra cualquier error. Nunca se aplica a "
                 "vacantes ni se inicia sesión en sitios.")).classes("text-grey-7")
        with ui.row():
            ui.button(t("Nueva búsqueda"), icon="add", on_click=lambda: self._source_dialog(None)).mark("source_new")
            ui.button(t("Ejecutar todas las activas"), icon="play_circle", on_click=self._run_all) \
                .props("outline").mark("source_run_all")
        sources = self.ds.list_sources()
        if not sources:
            with ui.card().classes("w-full bg-blue-1"):
                ui.label(t("Ejemplo para empezar: Amazon · palabras clave 'data engineer' · país CA · modo Canadá.")) \
                    .classes("text-sm")
                ui.button(t("Crear este ejemplo"), icon="auto_fix_high", on_click=self._create_example) \
                    .props("dense").mark("source_example")
        for src in sources:
            self._source_card(src)
        self._history()

    def _source_card(self, src) -> None:
        conn = self.ds.providers().get(src.provider)
        label, color = STATUS.get(src.last_status or "", STATUS[""])
        with ui.card().classes("w-full").mark(f"source_{src.id}"):
            with ui.row().classes("w-full items-center justify-between no-wrap"):
                with ui.column().classes("gap-0"):
                    ui.label(src.name).classes("text-bold")
                    details = [conn.label if conn else src.provider]
                    if src.identifier:
                        details.append(f"tablero: {src.identifier}")
                    details.append("palabras: " + (", ".join(src.keywords) if src.keywords else "todas"))
                    details.append("países: " + (", ".join(src.countries) if src.countries else "todos"))
                    details.append(f"modo: {src.mode_id or '—'} · máx. {src.max_results}")
                    ui.label(t(" · ".join(details))).classes("text-sm")
                    if conn:
                        ui.label(("✓ API oficial documentada. " if conn.official_api else "⚠ Sin API oficial. ")
                                 + conn.policy_note).classes("text-caption text-grey-7")
                with ui.row().classes("items-center no-wrap"):
                    ui.badge(t(label), color=color)
                    if src.last_run_at:
                        ui.label(t("{:%Y-%m-%d %H:%M}", src.last_run_at)).classes("text-caption")
                    if not src.enabled:
                        ui.badge(t("Pausada"), color="grey")
                    ui.button(t("Ejecutar ahora"), icon="play_arrow", on_click=lambda s=src: self._run(s)) \
                        .props("dense").mark(f"source_run_{src.id}")
                    ui.button(icon="edit", on_click=lambda s=src: self._source_dialog(s)).props("dense flat")
                    ui.button(icon="delete", on_click=lambda s=src: self._delete(s)).props("dense flat")

    def _history(self) -> None:
        runs = self.ds.runs(limit=30)
        ui.label(t("Historial de ejecuciones")).classes("text-bold q-mt-md")
        if not runs:
            ui.label(t("Aún no hay ejecuciones.")).classes("text-caption text-grey-7")
            return
        rows = [{"id": r.id, "fecha": f"{r.started_at:%Y-%m-%d %H:%M:%S}", "busqueda": r.source_name,
                 "estado": STATUS.get(r.status, (r.status,))[0], "traidas": r.fetched, "nuevas": r.created,
                 "actualizadas": r.updated, "sin_cambios": r.unchanged,
                 "disponibles": "—" if r.total_available is None else r.total_available, "consultas": r.requests,
                 "detalle": (r.error or "; ".join(r.warnings or []))[:240]} for r in runs]
        cols = [("fecha", "Fecha"), ("busqueda", "Búsqueda"), ("estado", "Estado"), ("traidas", "Traídas"),
                ("nuevas", "Nuevas"), ("actualizadas", "Actualizadas"), ("sin_cambios", "Sin cambios"),
                ("disponibles", "Disponibles"), ("consultas", "Consultas"), ("detalle", "Error / avisos")]
        ui.table(rows=rows, columns=[{"name": k, "label": t(lbl), "field": k, "align": "left"} for k, lbl in cols],
                 row_key="id", pagination=10).props("dense wrap-cells").classes("w-full").mark("runs_table")

    # ------------------------------------------------------------------ actions
    def _create_example(self) -> None:
        try:
            self.ds.create_source({"name": "Amazon — Data Engineer (Canadá)", "provider": "amazon_jobs",
                                   "keywords": ["data engineer"], "countries": ["CA"], "mode_id": "CANADA_VISA",
                                   "max_results": 100})
        except ValueError as exc:
            ui.notify(t(str(exc)), type="negative")
            return
        self.page.refresh_all()

    async def _run(self, src) -> None:
        ui.notify(t("Buscando en {}… (respetando el ritmo de la fuente)", src.name), type="ongoing", timeout=3000)
        try:
            result = await run.io_bound(self.ds.run_source, src.id)
        except ValueError as exc:
            ui.notify(t(str(exc)), type="warning")
            return
        except Exception:
            log.exception("source run failed")
            ui.notify(t("Error inesperado al ejecutar la búsqueda (ver logs/jsh.log)."), type="negative")
            return
        self._notify_run(result)
        self.page.refresh_all()

    async def _run_all(self) -> None:
        sources = [s for s in self.ds.list_sources() if s.enabled]
        if not sources:
            ui.notify(t("No hay búsquedas activas."), type="warning")
            return
        for src in sources:
            await self._run(src)

    @staticmethod
    def _notify_run(r) -> None:
        if r.status == "error":
            ui.notify(f"{r.source_name}: {r.error}", type="negative", multi_line=True, timeout=10000)
        else:
            ui.notify(t("{}: {} traídas · {} nuevas · {} actualizadas · "
                      "{} sin cambios", r.source_name, r.fetched, r.created, r.updated, r.unchanged) + (t(" (parcial)") if r.status == "partial" else ""),
                      type="positive" if r.status == "ok" else "warning", multi_line=True)

    def _delete(self, src) -> None:
        with self.page.dialog() as dlg, ui.card():
            ui.label(t("¿Eliminar la búsqueda '{}'?", src.name)).classes("text-bold")
            ui.label(t("Las vacantes que ya trajo, sus decisiones y su historial se conservan.")).classes("text-caption")

            def confirm():
                self.ds.delete_source(src.id)
                dlg.close()
                self.page.refresh_all()
            with ui.row():
                ui.button(t("Eliminar"), color="negative", on_click=confirm)
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    def _source_dialog(self, src) -> None:
        providers = self.ds.providers()
        with self.page.dialog() as dlg, ui.card().classes("w-[720px] max-w-full"):
            ui.label(t("Editar búsqueda") if src else t("Nueva búsqueda")).classes("text-h6")
            provider = ui.select({k: t(c.label) for k, c in providers.items()}, label=t("Fuente"),
                                 value=src.provider if src else "amazon_jobs").classes("w-full").mark("source_provider")
            note = ui.label().classes("text-caption text-grey-7")
            name = ui.input(t("Nombre"), value=src.name if src else "").classes("w-full").mark("source_name")
            ident = ui.input(t("Identificador del tablero (Greenhouse/Lever/Ashby)"),
                             value=src.identifier if src else "").classes("w-full").mark("source_identifier")
            ui.label(t("Es la parte final de la URL del portal de empleo del empleador: boards.greenhouse.io/gitlab "
                     "→ 'gitlab'; jobs.lever.co/palantir → 'palantir'; jobs.ashbyhq.com/ashby → 'ashby'. "
                     "Para Lever en Europa agregue '.eu'.")) \
                .classes("text-caption text-grey-7")
            keywords = ui.input(t("Palabras clave (separadas por coma)"),
                                value=", ".join(src.keywords) if src else "data engineer").classes("w-full") \
                .mark("source_keywords")
            countries = ui.input(t("Países (códigos de 2 letras, separados por coma; vacío = todos)"),
                                 value=", ".join(src.countries) if src else "CA").classes("w-full") \
                .mark("source_countries")
            with ui.row().classes("w-full"):
                mode = ui.select({"": t("Sin modo"), **{m: m for m in MODE_DEFS}}, label=t("Etiquetar con modo"),
                                 value=src.mode_id if src else "").classes("w-60")
                max_results = ui.number(t("Máximo de resultados"), value=src.max_results if src else 100, min=1,
                                        max=1000, step=10).classes("w-48")
                enabled = ui.switch(t("Activa"), value=src.enabled if src else True)

            def update_note(_=None):
                conn = providers.get(provider.value)
                if conn:
                    note.text = ("✓ API oficial documentada. " if conn.official_api else "⚠ Sin API oficial. ") \
                        + conn.policy_note
                    ident.set_visibility(conn.needs_identifier)
            provider.on_value_change(update_note)
            update_note()

            def save():
                data = {"name": name.value, "provider": provider.value, "identifier": ident.value,
                        "keywords": keywords.value, "countries": countries.value, "mode_id": mode.value,
                        "max_results": max_results.value, "enabled": enabled.value}
                try:
                    if src:
                        self.ds.update_source(src.id, data)
                    else:
                        self.ds.create_source(data)
                except (ValueError, KeyError) as exc:
                    ui.notify(t(str(exc)), type="negative", multi_line=True)
                    return
                dlg.close()
                ui.notify(t("Búsqueda guardada. Presione 'Ejecutar ahora' para traer vacantes."), type="positive")
                self.page.refresh_all()
            with ui.row():
                ui.button(t("Guardar"), on_click=save).mark("source_save")
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()
