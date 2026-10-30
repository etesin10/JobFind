"""Informes tab: skill-gap report and normalised salary overview (session 11)."""
from __future__ import annotations

import statistics
from typing import TYPE_CHECKING, Any

from nicegui import ui

from ..modes import MODE_DEFS
from ..i18n import t, tl

if TYPE_CHECKING:
    from .app import MainPage

SCOPES = {"activos": "Activos", "saved": "Guardadas / en proceso", "all": "Todas (sin descartadas)"}
GAP_STATUS = {"absent": "No está en su perfil", "proposed": "Propuesto (sin aprobar)",
              "rejected": "Rechazado / desactivado"}


class InsightsPanel:
    def __init__(self, page: "MainPage"):
        self.page = page
        self.svc = page.svc
        self.scope = "activos"

    def render(self) -> None:
        if self.page._need_mode():
            return
        mode = self.page.mode
        self._skill_gaps(mode)
        ui.separator()
        self._salaries(mode)

    # ------------------------------------------------------------------ skill gaps
    def _skill_gaps(self, mode: str) -> None:
        with ui.row().classes("items-center"):
            ui.label(t("Brechas de skills — {}", t(MODE_DEFS[mode].name))).classes("text-h6")
            ui.toggle(tl(SCOPES), value=self.scope, on_change=lambda e: self._set_scope(e.value)).props("dense") \
                .mark("gaps_scope")
        rep = self.svc.insights.skill_gaps(mode, self.scope, limit=40)
        ui.label(t("Vacantes analizadas: {} · con coincidencia calculada: "
                 "{}. Puntaje: requerido ×3, deseable ×2, mencionado ×1.", rep['jobs_analysed'], rep['jobs_with_match'])) \
            .classes("text-grey-7").mark("gaps_summary")
        ui.label(t("Basado en el vocabulario de skills y en los skills APROBADOS del perfil activo del modo. "
                 "Las vacantes sin JD o con poca evidencia no cuentan.")).classes("text-caption text-grey-7")
        if not rep["gaps"]:
            ui.label(t("Sin brechas: o no hay vacantes con JD en este alcance, o su perfil ya cubre lo que piden "
                     "(o aún no hay skills aprobados en 'Skills y CV').")).classes("text-grey-7").mark("gaps_empty")
        else:
            rows = [{"rank": i, "skill": g["name"], "jobs": f"{g['jobs']} ({g['share_pct']:.0f}%)",
                     "required": g["required"], "preferred": g["preferred"], "mentioned": g["mentioned"],
                     "score": g["score"], "status": t(GAP_STATUS.get(g["status"], g["status"]))}
                    for i, g in enumerate(rep["gaps"], 1)]
            cols = (("rank", "#"), ("skill", "Skill"), ("jobs", "Vacantes"), ("required", "Requerido"),
                    ("preferred", "Deseable"), ("mentioned", "Mencionado"), ("score", "Puntaje"),
                    ("status", "Estado en su perfil"))
            ui.table(rows=rows, columns=[{"name": k, "label": t(lbl), "field": k, "align": "left", "sortable": True}
                                         for k, lbl in cols],
                     row_key="rank", pagination=self.page.page_size).props("dense").classes("w-full") \
                .mark("gaps_table")
            with ui.expansion(t("Detalle y acciones por skill"), icon="tips_and_updates").classes("w-full"):
                for g in rep["gaps"][:20]:
                    self._gap_detail(mode, g)
        if rep["strengths"]:
            ui.label(t("Fortalezas: skills aprobados que más piden las vacantes")).classes("text-bold")
            with ui.row().classes("gap-1"):
                for s in rep["strengths"]:
                    ui.chip(f"{s['name']} · {s['jobs']}", icon="check", color="positive", text_color="white") \
                        .props("dense")
        md = self.svc.insights.markdown(rep)
        with ui.row():
            ui.button(t("Copiar como Markdown"), icon="content_copy",
                      on_click=lambda: (ui.clipboard.write(md), ui.notify(t("Copiado"), type="positive"))) \
                .props("dense outline").mark("gaps_copy")
            ui.button(t("Descargar .md"), icon="download",
                      on_click=lambda: ui.download.content(md, f"brechas_skills_{mode}.md")).props("dense flat")

    def _gap_detail(self, mode: str, g: dict[str, Any]) -> None:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center"):
                ui.label(g["name"]).classes("text-bold")
                ui.badge(t(GAP_STATUS.get(g["status"], g["status"])),
                         color="warning" if g["status"] == "proposed" else "negative")
                if g["status"] == "proposed":
                    sid = self.svc.insights.proposed_skill_id(mode, g["name"])
                    if sid:
                        def approve(sid=sid, name=g["name"]):
                            self.svc.candidate.set_skill_status([sid], "approved")
                            ui.notify(t("'{}' aprobado; vacantes re-evaluadas", name), type="positive")
                            self.page.refresh_all()
                        ui.button(t("Aprobar (lo tengo)"), icon="check", on_click=approve).props("dense outline") \
                            .mark(f"gap_approve_{g['name']}")
            ui.label(t(g["recommendation"])).classes("text-caption")
            for ex in g["examples"]:
                imp = t({"required": "requerido", "preferred": "deseable", "mentioned": "mencionado"}[ex["importance"]])
                ui.label(f"• {ex['title']} — {ex['company_name']} ({imp}): {ex['jd']}") \
                    .classes("text-caption text-grey-8 cursor-pointer") \
                    .on("click", lambda ex=ex: self.page.detail_dialog(ex["job_id"]))

    def _set_scope(self, scope: str) -> None:
        self.scope = scope
        self.page.refresh_all()

    # ------------------------------------------------------------------ salaries
    def _salaries(self, mode: str) -> None:
        base = self.svc.salary.base()
        rows = self.svc.jobs.rows(mode, "activos" if self.scope == "activos" else "all")
        if self.scope != "activos":
            rows = [r for r in rows if r["user_decision"] != "discarded"]
        if self.scope == "saved":
            rows = [r for r in rows if r["user_decision"] in ("saved", "maybe")
                    or r["application_status"] not in ("", "discovered")]
        ok = [r for r in rows if r["salary_status"] == "ok"]
        pending = [r for r in rows if r["salary_status"] in ("no_rate", "unknown_period", "unknown_currency")]
        ui.label(t("Salarios normalizados ({} por año)", base)).classes("text-h6")
        ui.label(t("{} de {} vacantes con salario comparable; {} con salario que no se "
                 "pudo convertir (falta tipo de cambio o periodo).", len(ok), len(rows), len(pending))).classes("text-grey-7").mark("salary_summary")
        if ok:
            tops = [r["salary_annual_max"] if r["salary_annual_max"] is not None else r["salary_annual_min"]
                    for r in ok]
            with ui.row():
                for label, val in (("Mediana (máx. del rango)", statistics.median(tops)),
                                   ("Mínimo", min(tops)), ("Máximo", max(tops))):
                    with ui.card().classes("w-48 items-center"):
                        ui.label(t("{:,.0f}", val)).classes("text-h6")
                        ui.label(f"{label} · {base}").classes("text-caption")
            best = sorted(ok, key=lambda r: -(r["salary_annual_max"] or r["salary_annual_min"] or 0))[:10]
            ui.table(rows=[{"id": r["job_id"], "puesto": r["title"], "empresa": r["company_name"],
                            "original": f"{r['salary_min'] or ''}–{r['salary_max'] or ''} {r['currency']} "
                                        f"/{r['salary_period'] or '?'}",
                            "anual": r["salary_note"]} for r in best],
                     columns=[{"name": k, "label": t(lbl), "field": k, "align": "left"} for k, lbl in
                              (("puesto", "Puesto"), ("empresa", "Empresa"), ("original", "Original"),
                               ("anual", "Normalizado"))],
                     row_key="id").props("dense wrap-cells").classes("w-full").mark("salary_table")
        rates = self.svc.salary.rate_rows()
        if rates:
            txt = ", ".join(f"{r['currency']} {r['rate']:.4g} ({r['as_of']}, {r['source']})" for r in rates)
            ui.label(t("Tipos de cambio usados (1 moneda = x {}): {}", base, txt)).classes("text-caption text-grey-7")
        ui.label(t("Cambie la moneda base o los tipos de cambio en Configuración → Moneda y tipos de cambio.")) \
            .classes("text-caption text-grey-7")
