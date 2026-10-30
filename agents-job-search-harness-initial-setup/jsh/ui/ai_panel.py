"""'Análisis IA' tab: export review packages for many jobs, import results, review queue, prompt templates and
pending résumé proposals. The app never calls a model itself (user decision, session 9)."""
from __future__ import annotations

from nicegui import ui

from ..modes import ALL
from ..services.reviews import MAX_JOBS_PER_PACKAGE, RECOMMENDATIONS, REVIEW_STATUSES
from ..i18n import t, tl
from .job_extras import download_package, paste_dialog, review_card, tailoring_dialog


class AIPanel:
    def __init__(self, page):
        self.page = page
        self.svc = page.svc
        self.status = "received"

    def render(self) -> None:
        ui.label(t("Análisis IA")).classes("text-h6")
        ui.label(t("Flujo: 1) elija vacantes y genere un paquete → 2) péguelo en ChatGPT/Claude (web) o déselo a su "
                 "agente local (API/MCP) → 3) pegue aquí la respuesta JSON → 4) acepte o rechace cada análisis. "
                 "Nada cambia sus decisiones automáticamente.")).classes("text-grey-7")
        if self.page.mode == ALL:
            ui.label(t("Seleccione Canadá o México arriba.")).classes("text-grey-7")
            return
        mode = self.page.mode
        self._export(mode)
        self._queue(mode)
        self._tailoring(mode)
        self._templates()

    def _export(self, mode: str) -> None:
        with ui.card().classes("w-full"):
            ui.label(t("1. Paquete de revisión")).classes("text-bold")
            rows = [{k: r[k] for k in ("job_id", "title", "company_name", "eligibility_status", "technical_score",
                                       "skills_match", "ai_fit_score")} for r in self.svc.jobs.rows(mode, "activos")]
            for r in rows:
                r["skills_match"] = "—" if r["skills_match"] is None else f"{r['skills_match']:.0f}%"
                r["ai_fit_score"] = "—" if r["ai_fit_score"] is None else f"{r['ai_fit_score']:.0f}"
            cols = [("title", "Puesto"), ("company_name", "Empresa"), ("eligibility_status", "Elegibilidad"),
                    ("technical_score", "Puntaje"), ("skills_match", "Skills CV"), ("ai_fit_score", "IA")]
            table = ui.table(rows=rows, columns=[{"name": k, "label": t(lbl), "field": k, "sortable": True,
                                                  "align": "left"} for k, lbl in cols],
                             row_key="job_id", selection="multiple", pagination=10) \
                .props("dense").classes("w-full").mark("ai_jobs_table")
            personal = ui.checkbox(t("Incluir mis datos de contacto (nombre, correo, teléfono) — normalmente NO es "
                                   "necesario")).mark("ai_personal")

            def build():
                ids = [s["job_id"] for s in table.selected]
                if not ids:
                    ui.notify(t("Seleccione vacantes en la tabla."), type="warning")
                    return
                try:
                    pkg = self.svc.reviews.build_package(mode, ids, include_personal=bool(personal.value))
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative", multi_line=True)
                    return
                download_package(self.svc, pkg, f"revision_{mode}_{len(ids)}_vacantes")
            with ui.row().classes("items-center"):
                ui.button(t("Generar paquete (.md + .json)"), icon="outbox", on_click=build).mark("ai_build")
                ui.label(t("Máximo {} vacantes por paquete. Se envía: su perfil APROBADO "
                         "(skills, experiencia), reglas, JDs y el formato de respuesta.", MAX_JOBS_PER_PACKAGE)).classes("text-caption")

    def _queue(self, mode: str) -> None:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center w-full justify-between"):
                ui.label(t("2. Respuestas y cola de revisión")).classes("text-bold")
                counts = self.svc.reviews.counts(mode)
                ui.label(t(" · ".join(f"{REVIEW_STATUSES[k]}: {v}" for k, v in counts.items()))) \
                    .classes("text-caption").mark("ai_counts")

            def submit(text: str) -> str:
                rep = self.svc.reviews.import_result(text, source="file")
                self.page.refresh_all()
                return (f"{len(rep['received'])} análisis recibido(s)."
                        + (f" Rechazados: {'; '.join(r['job_id'][:8] + ': ' + r['reason'] for r in rep['rejected'])}"
                           if rep["rejected"] else "")
                        + (f" Avisos: {'; '.join(rep['warnings'])}" if rep["warnings"] else ""))
            with ui.row().classes("items-center"):
                ui.button(t("Pegar / subir respuesta de la IA"), icon="content_paste",
                          on_click=lambda: paste_dialog(self.page, "Importar análisis IA",
                                                        "Pegue el JSON que devolvió la IA (protocolo jsh-review/1). "
                                                        "Se validan los ids y el hash de cada JD.", submit)) \
                    .mark("ai_paste")
                ui.toggle({k: t(v) for k, v in REVIEW_STATUSES.items()}, value=self.status,
                          on_change=lambda e: (setattr(self, "status", e.value), self.page.refresh_all())) \
                    .props("dense")
            reviews = self.svc.reviews.list(mode, self.status)
            if not reviews:
                ui.label(t("Nada en esta lista.")).classes("text-grey-7")
            rows = {r["job_id"]: r for r in self.svc.jobs.rows(mode, "all")}
            for r in reviews[:50]:
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"{r['title']} — {r['company_name']}").classes("text-bold")
                    ui.button(t("Ver vacante"), on_click=lambda jid=r["job_id"]: self.page.detail_dialog(jid)) \
                        .props("dense flat")
                review_card(self.page, r, rows.get(r["job_id"]))

    def _tailoring(self, mode: str) -> None:
        runs = self.svc.tailoring.runs(mode_id=mode, status="proposed")
        with ui.card().classes("w-full"):
            ui.label(t("3. Propuestas de CV pendientes ({})", len(runs))).classes("text-bold")

            def submit(text: str) -> str:
                rep = self.svc.tailoring.import_proposal(text, source="file")
                tailoring_dialog(self.page, rep["run_id"])
                self.page.refresh_all()
                return f"Propuesta importada: {rep['changes']} cambio(s), {rep['flagged']} marcado(s)."
            ui.button(t("Pegar propuesta de CV de la IA"), icon="content_paste",
                      on_click=lambda: paste_dialog(self.page, "Importar propuesta de CV", "Protocolo jsh-tailoring/1.", submit)) \
                .props("dense outline")
            for run in runs:
                with ui.row().classes("items-center gap-2"):
                    ui.label(t("{} — {} · {} · "
                             "{} cambio(s)", run['title'], run['company_name'], run['provider'] or run['source'], len(run['changes'])))
                    ui.button(t("Revisar"), on_click=lambda rid=run["run_id"]: tailoring_dialog(self.page, rid)) \
                        .props("dense flat")

    def _templates(self) -> None:
        with ui.expansion(t("Plantillas de instrucciones (avanzado)"), icon="edit_note").classes("w-full"):
            tpls = {tp.key: tp for tp in self.svc.reviews.templates()}
            key = ui.select({k: f"{tp.name} (v{tp.version})" for k, tp in tpls.items()}, value="review_default",
                            label=t("Plantilla")).classes("w-96")
            body = ui.textarea(t("Instrucciones"), value=tpls["review_default"].body if "review_default" in tpls else "") \
                .props("outlined autogrow").classes("w-full").mark("tpl_body")
            key.on_value_change(lambda e: setattr(body, "value", tpls[e.value].body))

            def save():
                try:
                    self.svc.reviews.update_template(key.value, body.value or "")
                except (ValueError, KeyError) as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                ui.notify(t("Plantilla guardada (nueva versión)."), type="positive")
                self.page.refresh_all()

            def reset():
                self.svc.reviews.reset_template(key.value)
                ui.notify(t("Plantilla restaurada."), type="positive")
                self.page.refresh_all()
            with ui.row():
                ui.button(t("Guardar plantilla"), on_click=save).props("dense")
                ui.button(t("Restaurar original"), on_click=reset).props("dense flat")
            ui.label(t("Las reglas de veracidad (no inventar, JD = texto no confiable) deben mantenerse.")) \
                .classes("text-caption text-warning")
            ui.label(t("Recomendaciones posibles: ") + ", ".join(RECOMMENDATIONS.values())).classes("text-caption")
