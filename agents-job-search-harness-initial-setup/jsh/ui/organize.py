"""Organisation widgets: saved views + filter panel, user tags, ad-liveness box (session 11)."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from nicegui import run, ui

from ..modes import ELIGIBILITY_LABELS, USER_DECISION_LABELS
from ..services.views import summarize, validate_filters
from ..i18n import t, tl

if TYPE_CHECKING:
    from .app import MainPage

APPLICATION_FILTER = {"any": "Cualquiera", "none": "Sin aplicar", "applied": "Aplicada (cualquier estado)",
                      "active": "Proceso activo"}
WORK_MODE_FILTER = {"remote": "Remoto", "hybrid": "Híbrido", "onsite": "Presencial"}
VIEW_FILTER = {"": "(según la pestaña)", "activos": "Activos", "all": "Todas", "blocked": "Bloqueadas",
               "discarded": "Descartadas"}
LIVENESS_LABELS = {"live": "Anuncio abierto (última verificación)", "likely_closed": "⚠ Posiblemente cerrada",
                   "unknown": "No se pudo determinar", "dismissed": "Marcada por usted como abierta", "": "Sin verificar"}


class Organizer:
    def __init__(self, page: "MainPage"):
        self.page = page
        self.svc = page.svc
        self.adhoc: dict[str, dict[str, Any]] = {}  # mode → unsaved filters

    # ------------------------------------------------------------------ filters state
    def filters(self, mode: str) -> dict[str, Any]:
        """Active filters of the mode: the selected saved view, or the unsaved filters."""
        if mode in self.adhoc:
            return self.adhoc[mode]
        vid = self.svc.views.selected(mode)
        if vid:
            try:
                return dict(self.svc.views.get(vid)["filters"])
            except KeyError:
                self.svc.views.select(mode, "")
        return {}

    def views_bar(self, mode: str) -> None:
        views = self.svc.views.list(mode)
        selected = self.svc.views.selected(mode) if mode not in self.adhoc else ""
        options = {"": "(sin vista guardada)", **{v["id"]: v["name"] for v in views}}
        if selected not in options:
            selected = ""
        with ui.row().classes("items-center gap-1"):
            ui.select(tl(options), value=selected, label=t("Vista guardada"),
                      on_change=lambda e: self._select(mode, e.value)).props("dense outlined").classes("w-64") \
                .mark("view_select")
            ui.button(t("Filtros…"), icon="filter_alt", on_click=lambda: self.filters_dialog(mode)) \
                .props("dense outline").mark("filters_open")
            if selected:
                ui.button(icon="delete", on_click=lambda: self._delete_view(mode, selected)) \
                    .props("dense flat").tooltip(t("Eliminar esta vista guardada")).mark("view_delete")
            current = self.filters(mode)
            chips = summarize(current) if current else []
            for c in chips[:8]:
                ui.chip(t(c)).props("dense outline color=primary")
            if current:
                ui.button(t("Quitar filtros"), icon="filter_alt_off", on_click=lambda: self._clear(mode)) \
                    .props("dense flat").mark("filters_clear")

    def _select(self, mode: str, view_id: str) -> None:
        self.adhoc.pop(mode, None)
        self.svc.views.select(mode, view_id or "")
        self.page.refresh_all()

    def _clear(self, mode: str) -> None:
        self.adhoc.pop(mode, None)
        self.svc.views.select(mode, "")
        self.page.refresh_all()

    def _delete_view(self, mode: str, view_id: str) -> None:
        with self.page.dialog() as dlg, ui.card():
            name = self.svc.views.get(view_id)["name"]
            ui.label(t("¿Eliminar la vista '{}'? Las vacantes no cambian.", name))
            with ui.row():
                def confirm():
                    self.svc.views.delete(view_id)
                    dlg.close()
                    self.page.refresh_all()
                ui.button(t("Eliminar"), color="negative", on_click=confirm).mark("view_delete_confirm")
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    def filters_dialog(self, mode: str) -> None:
        f = validate_filters(self.filters(mode))
        all_rows = self.svc.jobs.rows(mode, "all")
        countries = sorted({r["job_country"] for r in all_rows if r["job_country"]} | set(f.countries))
        sources = sorted({r["source"] for r in all_rows if r["source"]} | set(f.sources))
        tags = [t for t, _ in self.svc.jobs.all_tags()]
        vid = self.svc.views.selected(mode) if mode not in self.adhoc else ""
        base = self.svc.salary.base()
        with self.page.dialog() as dlg, ui.card().classes("w-[760px] max-w-full"):
            ui.label(t("Filtros de vacantes")).classes("text-h6")
            ui.label(t("Se combinan con la búsqueda por palabra clave. Guárdelos como vista para reutilizarlos "
                     "(p. ej. 'Canadá · Toronto · puntaje > 60 · sin aplicar').")).classes("text-caption text-grey-7")
            w: dict[str, Any] = {}
            with ui.grid(columns=2).classes("w-full"):
                w["countries"] = ui.select(tl(countries), value=f.countries, multiple=True, label=t("Países"),
                                           new_value_mode="add-unique").props("use-chips dense") \
                    .mark("flt_countries")
                w["location"] = ui.input(t("Ubicación contiene (ciudad, provincia…)"), value=f.location) \
                    .props("dense").mark("flt_location")
                w["work_modes"] = ui.select(tl(WORK_MODE_FILTER), value=f.work_modes, multiple=True,
                                            label=t("Modalidad")).props("use-chips dense")
                w["sources"] = ui.select(tl(sources), value=f.sources, multiple=True, label=t("Fuente")) \
                    .props("use-chips dense")
                w["min_score"] = ui.number(t("Puntaje mínimo"), value=f.min_score, min=0).props("dense") \
                    .mark("flt_min_score")
                w["min_skills"] = ui.number(t("Coincidencia de skills mínima (%)"), value=f.min_skills, min=0,
                                            max=100).props("dense")
                w["min_salary"] = ui.number(t("Salario anual mínimo ({}, normalizado)", base), value=f.min_salary,
                                            min=0).props("dense").mark("flt_min_salary")
                w["salary_unknown_ok"] = ui.checkbox(t("Incluir vacantes sin salario conocido"),
                                                     value=f.salary_unknown_ok)
                w["application"] = ui.select(tl(APPLICATION_FILTER), value=f.application, label=t("Aplicación")) \
                    .props("dense").mark("flt_application")
                w["decisions"] = ui.select(tl(USER_DECISION_LABELS), value=f.decisions, multiple=True,
                                           label=t("Decisión")).props("use-chips dense")
                w["eligibility"] = ui.select(tl(ELIGIBILITY_LABELS), value=f.eligibility, multiple=True,
                                             label=t("Elegibilidad")).props("use-chips dense")
                w["tags_any"] = ui.select(tl(tags), value=f.tags_any, multiple=True, label=t("Con alguna etiqueta"),
                                          new_value_mode="add-unique").props("use-chips dense").mark("flt_tags")
                w["posted_within_days"] = ui.number(t("Publicada en los últimos N días"), value=f.posted_within_days,
                                                    min=1).props("dense")
                w["view"] = ui.select(tl(VIEW_FILTER), value=f.view, label=t("Vista base (Vacantes)")).props("dense")
            w["exclude_closed"] = ui.checkbox(t("Ocultar anuncios expirados o posiblemente cerrados"),
                                              value=f.exclude_closed)
            w["q"] = ui.input(t("Palabras clave guardadas en la vista (opcional)"), value=f.q).classes("w-full") \
                .props("dense")
            name = ui.input(t("Nombre para guardar como vista"),
                            value=self.svc.views.get(vid)["name"] if vid else "").classes("w-full") \
                .mark("flt_view_name")

            def collect() -> dict[str, Any] | None:
                data = {k: (x.value if x.value not in ("", None) or k in ("view", "q", "location") else None)
                        for k, x in w.items()}
                for k in ("min_score", "min_skills", "min_salary", "posted_within_days"):
                    if data[k] is not None:
                        data[k] = int(data[k]) if k == "posted_within_days" else float(data[k])
                try:
                    return validate_filters(data).model_dump(exclude_defaults=True)
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return None

            def apply_only():
                data = collect()
                if data is None:
                    return
                self.adhoc[mode] = data
                dlg.close()
                self.page.refresh_all()

            def save(as_new: bool):
                data = collect()
                if data is None:
                    return
                try:
                    new_id = self.svc.views.save(mode, name.value or "", data, None if as_new else (vid or None))
                except (ValueError, KeyError) as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                self.adhoc.pop(mode, None)
                self.svc.views.select(mode, new_id)
                dlg.close()
                ui.notify(t("Vista guardada"), type="positive")
                self.page.refresh_all()
            with ui.row():
                ui.button(t("Aplicar"), icon="filter_alt", on_click=apply_only).mark("flt_apply")
                if vid:
                    ui.button(t("Actualizar vista"), icon="save", on_click=lambda: save(False)).props("outline") \
                        .mark("flt_update")
                ui.button(t("Guardar como vista nueva"), icon="bookmark_add", on_click=lambda: save(True)) \
                    .props("outline").mark("flt_save")
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    # ------------------------------------------------------------------ tags
    def tags_editor(self, job_id: str) -> None:
        current = self.svc.jobs.tags(job_id)
        known = sorted({t for t, _ in self.svc.jobs.all_tags()} | set(current), key=str.lower)
        ui.label(t("Etiquetas")).classes("text-bold")
        sel = ui.select(tl(known), value=current, multiple=True, new_value_mode="add-unique",
                        label=t("Escriba y pulse Enter para crear una etiqueta")).props("use-chips dense") \
            .classes("w-full").mark("tags_input")

        def save():
            try:
                saved = self.svc.jobs.set_tags(job_id, list(sel.value or []))
            except ValueError as exc:
                ui.notify(t(str(exc)), type="negative")
                return
            ui.notify(t("Etiquetas guardadas: {}", ', '.join(saved) or '—'), type="positive")
            self.page.refresh_all()
        ui.button(t("Guardar etiquetas"), icon="sell", on_click=save).props("dense").mark("tags_save")

    def bulk_tags_dialog(self, rows: list[dict[str, Any]]) -> None:
        known = [t for t, _ in self.svc.jobs.all_tags()]
        with self.page.dialog() as dlg, ui.card().classes("w-[480px]"):
            ui.label(t("Etiquetas para {} vacante(s)", len(rows))).classes("text-bold")
            sel = ui.select(tl(known), value=[], multiple=True, new_value_mode="add-unique",
                            label=t("Etiquetas (Enter para crear)")).props("use-chips dense").classes("w-full") \
                .mark("bulk_tags_input")

            def go(add: bool):
                tags = list(sel.value or [])
                if not tags:
                    ui.notify(t("Elija o escriba al menos una etiqueta"), type="warning")
                    return
                ids = [r["job_id"] for r in rows]
                try:
                    n = self.svc.jobs.add_tags(ids, tags) if add else self.svc.jobs.remove_tags(ids, tags)
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                dlg.close()
                ui.notify(t("{} vacante(s) actualizada(s)", n), type="positive")
                self.page.refresh_all()
            with ui.row():
                ui.button(t("Agregar"), icon="add", on_click=lambda: go(True)).mark("bulk_tags_add")
                ui.button(t("Quitar"), icon="remove", on_click=lambda: go(False)).props("outline")
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    # ------------------------------------------------------------------ liveness
    def liveness_box(self, job, dlg) -> None:
        info = self.svc.liveness.status(job.id)
        status = info.get("status", "")
        with ui.card().classes("w-full").mark("liveness_box"):
            with ui.row().classes("items-center"):
                ui.label(t("¿Sigue abierto el anuncio?")).classes("text-bold")
                color = {"live": "positive", "likely_closed": "negative", "unknown": "grey",
                         "dismissed": "teal"}.get(status, "grey")
                ui.badge(t(LIVENESS_LABELS.get(status, status)), color=color).mark("liveness_status")
            if info.get("checked_at"):
                ui.label(t("Última verificación: {} · {}", info['checked_at'].replace('T', ' '), info.get('reason', ''))) \
                    .classes("text-caption")
            ui.label(t("Solo se consulta la URL del anuncio; nunca se cambia ni se borra nada automáticamente.")) \
                .classes("text-caption text-grey-7")

            async def check():
                ui.notify(t("Verificando…"))
                res = await run.io_bound(self.svc.liveness.check_job, job.id, "user")
                ui.notify(f"{LIVENESS_LABELS.get(res['status'], res['status'])}: {res['reason']}",
                          type="warning" if res["status"] == "likely_closed" else "info", multi_line=True)
                dlg.close()
                self.page.refresh_all()
                self.page.detail_dialog(job.id)
            with ui.row():
                ui.button(t("Verificar ahora"), icon="travel_explore", on_click=check).props("dense outline") \
                    .mark("liveness_check")
                if status == "likely_closed":
                    def expire():
                        self.svc.liveness.mark_expired([job.id])
                        ui.notify(t("Anuncio marcado como expirado (puede revertirlo)."), type="positive")
                        dlg.close()
                        self.page.refresh_all()

                    def dismiss():
                        self.svc.liveness.dismiss(job.id)
                        ui.notify(t("Aviso ocultado."), type="info")
                        dlg.close()
                        self.page.refresh_all()
                    ui.button(t("Marcar como expirada"), icon="event_busy", on_click=expire).props("dense") \
                        .mark("liveness_expire")
                    ui.button(t("Sigue abierta"), icon="check", on_click=dismiss).props("dense flat") \
                        .mark("liveness_dismiss")
                if job.ad_status == "expired":
                    def reopen():
                        self.svc.jobs.set_ad_status(job.id, "active")
                        ui.notify(t("Anuncio marcado como activo."), type="positive")
                        dlg.close()
                        self.page.refresh_all()
                    ui.button(t("Marcar como activa otra vez"), icon="undo", on_click=reopen).props("dense flat") \
                        .mark("liveness_reopen")
