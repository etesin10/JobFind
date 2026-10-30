"""Job detail extras: JD keywords + flat export, AI reviews, résumé tailoring and company/contacts for one job.

All untrusted text (JD, agent/AI output) is rendered with ui.label (escaped), never as HTML/Markdown.
"""
from __future__ import annotations

from typing import Any

from nicegui import ui

from ..modes import MODE_DEFS
from ..services.reviews import RECOMMENDATIONS, REVIEW_STATUSES
from ..services.tailoring import ACTIONS
from ..i18n import t, tl

IMPORTANCE = {"required": ("Requerida", "red-8"), "preferred": ("Deseable", "orange-8"),
              "mentioned": ("Mencionada", "blue-grey-6")}
LOC_FIT = {"yes": "Sí", "no": "No", "unknown": "Desconocido"}
AUTH_FIT = {"possible": "Posible", "blocked": "Bloqueada", "unknown": "Desconocida",
            "requires_employer_confirmation": "Requiere confirmación del empleador"}
FLAG_COLORS = {"high": "negative", "low": "warning"}


def _safe_name(text: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text)[:60].strip("_") or "vacante"


def download_package(svc, pkg: dict[str, Any], stem: str) -> None:
    """Download the package as .md (paste into a web chat) and keep a JSON copy in exports/."""
    files = svc.reviews.export_files(pkg)
    ui.download.content(files["markdown"].read_text(encoding="utf-8"), f"{stem}.md", "text/markdown")
    ui.notify(t("Paquete listo: {} (copia JSON en exports/). Péguelo en ChatGPT/Claude o "
              "entrégueselo a su agente.", files['markdown'].name), type="positive", multi_line=True)


def paste_dialog(page, title: str, help_text: str, on_submit) -> None:
    """Paste or upload an AI response (JSON, optionally inside ```json fences)."""
    with page.dialog() as dlg, ui.card().classes("w-[760px] max-w-full"):
        ui.label(t(title)).classes("text-h6")
        ui.label(t(help_text)).classes("text-caption text-grey-7")
        area = ui.textarea(t("Respuesta (JSON)")).props("outlined autogrow").classes("w-full").mark("paste_json")

        async def on_upload(e):
            area.value = (await e.file.read()).decode("utf-8-sig", "replace")
        ui.upload(label=t("…o subir archivo .json"), auto_upload=True, on_upload=on_upload) \
            .props('accept=".json,.txt,.md"').classes("w-full")

        def submit():
            try:
                msg = on_submit(area.value or "")
            except (ValueError, KeyError) as exc:
                ui.notify(t("No se importó: {}", exc), type="negative", multi_line=True)
                return
            dlg.close()
            if msg:
                ui.notify(t(msg), type="positive", multi_line=True)
        with ui.row():
            ui.button(t("Importar"), icon="upload", on_click=submit).mark("paste_submit")
            ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
    dlg.open()


def review_card(page, r: dict[str, Any], row: dict[str, Any] | None = None, on_change=None) -> None:
    svc = page.svc
    res = r.get("result") or {}
    color = {"accepted": "positive", "received": "warning", "rejected": "grey", "superseded": "grey"}[r["status"]]
    with ui.card().classes("w-full").mark(f"review_{r['review_id']}"):
        with ui.row().classes("items-center gap-2"):
            ui.badge(t(REVIEW_STATUSES.get(r["status"], r["status"])), color=color)
            ui.badge(t("Ajuste {}/100", res.get('technical_fit_score', '—')), color="deep-purple")
            ui.badge(t(RECOMMENDATIONS.get(res.get("recommendation", ""), res.get("recommendation", ""))), color="indigo")
            ui.label(f"{r['provider'] or 'IA'} {r['model']} · {r['source']} · {r['created_at']}") \
                .classes("text-caption text-grey-7")
            if r["stale"]:
                ui.badge(t("JD cambió desde el análisis"), color="orange")
        if res.get("summary"):
            ui.label(res["summary"]).classes("whitespace-pre-wrap")
        ui.label(t("Ubicación compatible: {}", LOC_FIT.get(res.get('location_fit'), '—'))
                 + (f" — {res['location_evidence']}" if res.get("location_evidence") else "")).classes("text-sm")
        ui.label(t("Autorización de trabajo: {}", AUTH_FIT.get(res.get('work_authorization_fit'), '—'))
                 + (f" — {res['work_authorization_evidence']}" if res.get("work_authorization_evidence") else "")
                 + t(" (no es una determinación legal)")).classes("text-sm")
        with ui.expansion(t("Evidencia, dudas y sugerencias")).classes("w-full"):
            if res.get("technical_fit_explanation"):
                ui.label(res["technical_fit_explanation"]).classes("whitespace-pre-wrap text-sm")
            for e in res.get("evidence", []):
                ui.label(f"• {e.get('claim', '')}" + (f" — «{e['jd_quote']}»" if e.get("jd_quote") else "")
                         + (t(" [skill: {}]", e['candidate_skill']) if e.get("candidate_skill") else "")).classes("text-sm")
            for key, label in (("blockers", "Bloqueos"), ("unknowns", "Desconocidos"),
                               ("questions_for_employer", "Preguntas para el empleador"),
                               ("resume_adaptation_suggestions", "Sugerencias para el CV")):
                if res.get(key):
                    ui.label(t(label)).classes("text-bold text-sm")
                    for x in res[key]:
                        ui.label(f"• {x}").classes("text-sm")
            for w in r.get("warnings", []):
                ui.label(f"⚠ {w}").classes("text-caption text-warning")

        def decide(action: str):
            (svc.reviews.accept if action == "accept" else svc.reviews.reject)(r["review_id"])
            ui.notify(t("Análisis aceptado: ahora alimenta los campos 'IA: …' de las reglas.") if action == "accept"
                      else t("Análisis rechazado."), type="positive")
            page.refresh_all()
            if on_change:
                on_change()
        with ui.row():
            if r["status"] in ("received", "superseded"):
                ui.button(t("Aceptar análisis"), icon="check", on_click=lambda: decide("accept")) \
                    .props("dense").mark(f"accept_review_{r['review_id']}")
            if r["status"] in ("received", "accepted"):
                ui.button(t("Rechazar"), icon="close", on_click=lambda: decide("reject")).props("dense flat")
            if (r["status"] == "accepted" and res.get("recommendation") == "discard_suggested" and row
                    and row.get("user_decision") != "discarded"):
                ui.button(t("Descartar (sugerido por la IA)"), icon="delete", color="negative",
                          on_click=lambda: page.discard_dialog(row)).props("dense outline")


class JobExtras:
    def __init__(self, page):
        self.page = page
        self.svc = page.svc

    def render(self, job_id: str, mode: str, dlg) -> None:
        doc = self.svc.jd.document(job_id, mode)
        with ui.expansion(t("Palabras clave de la JD y exportar para IA"), icon="key", value=True) \
                .classes("w-full").mark("jd_keywords"):
            self._keywords(job_id, mode, doc)
        with ui.expansion(t("Análisis IA"), icon="psychology").classes("w-full").mark("ai_section"):
            self.reviews_view = ui.refreshable(self._reviews)
            self.reviews_view(job_id, mode)
        with ui.expansion(t("CV adaptado a esta vacante"), icon="description").classes("w-full").mark("tailor_section"):
            self.tailor_view = ui.refreshable(self._tailoring)
            self.tailor_view(job_id, mode)
        with ui.expansion(t("Empresa y contactos"), icon="business").classes("w-full").mark("company_section"):
            self._company(job_id, mode, doc, dlg)

    # ------------------------------------------------------------------ keywords + flat JD export
    def _keywords(self, job_id: str, mode: str, doc: dict[str, Any]) -> None:
        jd = doc["jd"]
        if not jd["flat_text"]:
            ui.label(t("Esta vacante no tiene JD guardada.")).classes("text-grey-7")
        matched = {m["name"] for m in (doc.get("match") or {}).get("matched", [])}
        for imp, (label, color) in IMPORTANCE.items():
            kws = [k for k in jd["keywords"] if k["importance"] == imp]
            if not kws:
                continue
            with ui.row().classes("items-center gap-1"):
                ui.label(t({"required": "Requeridas:", "preferred": "Deseables:", "mentioned": "Mencionadas:"}[imp])).classes("text-bold text-sm w-28")
                for k in kws:
                    ui.badge(("✓ " if k["name"] in matched else "") + k["name"]
                             + (f" ×{k['count']}" if k["count"] > 1 else ""),
                             color=color if k["name"] not in matched else "positive") \
                        .tooltip(k["evidence"])
        if not jd["structured"] and jd["keywords"]:
            ui.label(t("La JD no tiene secciones reconocibles (requisitos/deseables): las palabras clave se marcan "
                     "como 'mencionadas'.")).classes("text-caption text-grey-7")
        ui.label(t("✓ verde = skill aprobado en su CV.")).classes("text-caption text-grey-7")
        facts = []
        if jd["years_experience"]:
            facts.append(t("Años: ") + "; ".join(f"{y['min']}{'-' + str(y['max']) if y['max'] else '+'}"
                                              for y in jd["years_experience"]))
        if jd["education"]:
            facts.append(t("Educación: ") + ", ".join(sorted({e["term"] for e in jd["education"]})))
        if jd["work_mode_hints"]:
            facts.append(t("Modalidad en el texto: ") + ", ".join(sorted({w["term"] for w in jd["work_mode_hints"]})))
        if jd["salary_mentions"]:
            facts.append(t("Salario en el texto: ") + " | ".join(s["text"] for s in jd["salary_mentions"]))
        for f in facts:
            ui.label(f).classes("text-sm")
        for a in jd["work_authorization"][:3]:
            ui.label(("⚠ " if a["negative_wording"] else t("ℹ ")) + t("Visa/autorización: {}", a['evidence'])) \
                .classes("text-sm " + ("text-negative" if a["negative_wording"] else ""))
        ui.label(t("Pistas automáticas del texto del anuncio: verifíquelas.")).classes("text-caption text-grey-7")
        title = _safe_name(f"{doc['job']['company_name']}_{doc['job']['title']}")

        def dl(kind: str):
            if kind == "md":
                ui.download.content(self.svc.jd.markdown(job_id, mode), f"{title}.md", "text/markdown")
            else:
                ui.download.content(self.svc.jd.json_text(job_id, mode), f"{title}.json", "application/json")

        async def copy():
            ui.clipboard.write(self.svc.jd.markdown(job_id, mode))
            ui.notify(t("JD en Markdown copiada: péguela en su chat de IA."), type="positive")
        with ui.row():
            ui.button(t("Copiar JD para IA (Markdown)"), icon="content_copy", on_click=copy).props("dense outline") \
                .mark("copy_jd")
            ui.button(t("Descargar .md"), icon="download", on_click=lambda: dl("md")).props("dense flat")
            ui.button(t("Descargar .json"), icon="data_object", on_click=lambda: dl("json")).props("dense flat")

    # ------------------------------------------------------------------ AI reviews
    def _reviews(self, job_id: str, mode: str) -> None:
        svc = self.svc
        ui.label(t("La app no llama a ninguna IA: genere un paquete, analícelo con ChatGPT/Claude/su agente y pegue la "
                 "respuesta. Los análisis no cambian sus decisiones; al aceptarlos alimentan los campos 'IA: …' de "
                 "las reglas.")).classes("text-caption text-grey-7")
        row = next((r for r in svc.jobs.rows(mode, "all") if r["job_id"] == job_id), None)

        def package():
            pkg = svc.reviews.build_package(mode, [job_id])
            download_package(svc, pkg, f"revision_{_safe_name(row['title'] if row else job_id)}")

        def submit(text: str) -> str:
            rep = svc.reviews.import_result(text, source="file")
            self.reviews_view.refresh()
            self.page.refresh_all()
            return (f"{len(rep['received'])} análisis recibido(s); revíselos y acéptelos."
                    + (f" Rechazados: {'; '.join(r['reason'] for r in rep['rejected'])}" if rep["rejected"] else ""))
        with ui.row():
            ui.button(t("Paquete de revisión (esta vacante)"), icon="outbox", on_click=package) \
                .props("dense outline").mark("review_package_one")
            ui.button(t("Pegar respuesta de la IA"), icon="content_paste",
                      on_click=lambda: paste_dialog(self.page, "Importar análisis IA", "Protocolo jsh-review/1.", submit)) \
                .props("dense outline").mark("review_paste")
        reviews = svc.reviews.list(mode, job_id=job_id)
        if not reviews:
            ui.label(t("Sin análisis IA para esta vacante en este modo.")).classes("text-grey-7")
        for r in reviews[:6]:
            review_card(self.page, r, row, on_change=self.reviews_view.refresh)

    # ------------------------------------------------------------------ tailoring
    def _tailoring(self, job_id: str, mode: str) -> None:
        svc = self.svc
        ui.label(t("Nunca se inventan empleos, fechas, cifras ni skills. Usted aprueba cada cambio; los cambios "
                 "marcados en rojo requieren que confirme que son verdaderos.")).classes("text-caption text-grey-7")
        cand = svc.candidate.active_candidate(mode)
        if cand is None:
            ui.label(t("Active un perfil de candidato para este modo en 'Skills y CV'.")).classes("text-warning")
            return

        def auto():
            try:
                rep = svc.tailoring.deterministic_proposal(job_id, mode)
            except ValueError as exc:
                ui.notify(t(str(exc)), type="negative", multi_line=True)
                return
            tailoring_dialog(self.page, rep["run_id"], self.tailor_view.refresh)
            self.tailor_view.refresh()

        def package():
            try:
                pkg = svc.tailoring.build_package(job_id, mode)
            except ValueError as exc:
                ui.notify(t(str(exc)), type="negative", multi_line=True)
                return
            download_package(svc, pkg, f"cv_{_safe_name(job_id[:8])}")

        def submit(text: str) -> str:
            rep = svc.tailoring.import_proposal(text, source="file")
            tailoring_dialog(self.page, rep["run_id"], self.tailor_view.refresh)
            self.tailor_view.refresh()
            return f"Propuesta importada: {rep['changes']} cambio(s), {rep['flagged']} marcado(s)."
        with ui.row():
            ui.button(t("Propuesta automática (sin IA)"), icon="auto_fix_high", on_click=auto) \
                .props("dense outline").mark("tailor_auto")
            ui.button(t("Paquete para IA"), icon="outbox", on_click=package).props("dense outline").mark("tailor_package")
            ui.button(t("Pegar propuesta de la IA"), icon="content_paste",
                      on_click=lambda: paste_dialog(self.page, "Importar propuesta de CV", "Protocolo jsh-tailoring/1.", submit)) \
                .props("dense outline")
        for run in svc.tailoring.runs(job_id=job_id)[:6]:
            with ui.row().classes("items-center gap-2"):
                ui.badge({"proposed": "Pendiente", "approved": "Aprobada", "discarded": "Descartada"}[run["status"]],
                         color={"proposed": "warning", "approved": "positive", "discarded": "grey"}[run["status"]])
                ui.label(t("{} · {} · {} cambio(s)", run['created_at'], run['provider'] or run['source'], len(run['changes'])))
                ui.button(t("Revisar") if run["status"] == "proposed" else t("Ver"),
                          on_click=lambda rid=run["run_id"]: tailoring_dialog(self.page, rid, self.tailor_view.refresh))\
                    .props("dense flat")
        for ver in svc.tailoring.tailored_versions(job_id)[:6]:
            with ui.row().classes("items-center gap-2").mark(f"tailored_{ver.id}"):
                ui.icon("verified").classes("text-positive")
                ui.label(t("{} · {:%Y-%m-%d %H:%M}", ver.name, ver.approved_at))
                ui.button(t("Ver texto"), on_click=lambda v=ver.id: self._text_dialog(v)).props("dense flat")
                ui.button(t("Descargar .docx"), icon="download",
                          on_click=lambda v=ver.id: self._docx(v)).props("dense flat").mark(f"docx_{ver.id}")

    def _docx(self, version_id: str) -> None:
        try:
            rel = self.svc.tailoring.generate_docx(version_id)
        except Exception as exc:  # rendering problems must be visible, never crash the UI
            ui.notify(t("No se pudo generar el .docx: {}", exc), type="negative", multi_line=True)
            return
        ui.download.file(self.svc.paths.home / rel)
        ui.notify(t("Generado: {}", rel), type="positive")

    def _text_dialog(self, version_id: str) -> None:
        with self.page.dialog() as dlg, ui.card().classes("w-[800px] max-w-full"):
            ui.label(t("CV adaptado (texto)")).classes("text-h6")
            with ui.scroll_area().classes("h-[60vh] w-full border"):
                ui.label(self.svc.tailoring.render(version_id)).classes("whitespace-pre-wrap text-sm")
            ui.button(t("Cerrar"), on_click=dlg.close).props("flat")
        dlg.open()

    # ------------------------------------------------------------------ company & contacts
    def _company(self, job_id: str, mode: str, doc: dict[str, Any], dlg) -> None:
        from .crm_panel import company_dialog, contact_dialog
        svc = self.svc
        company_id = doc["job"]["company_id"]
        if company_id:
            ui.button(t("Ver empresa: {}", doc['job']['company_name']), icon="business",
                      on_click=lambda: company_dialog(self.page, company_id)).props("dense outline") \
                .mark("open_company")
        else:
            ui.label(t("La vacante no tiene empresa registrada.")).classes("text-grey-7")
        contacts = svc.crm.job_contacts(job_id)
        detail = svc.jobs.detail(job_id, mode)
        current = detail["application"].contact_id if detail["application"] else None
        for c in contacts:
            with ui.row().classes("items-center gap-2"):
                ui.icon("person")
                ui.label(" · ".join(x for x in (c["name"], c["role"], c["email"], c["phone"]) if x)
                         + ("  ✓ establecido" if c.get("established") else ""))
                if c["contact_id"] == current:
                    ui.badge(t("Contacto de la aplicación"), color="teal")
                ui.button(t("Abrir"), on_click=lambda cid=c["contact_id"]: contact_dialog(self.page, cid)) \
                    .props("dense flat")
        options = {"": "— ninguno —", **{c["contact_id"]: c["name"] for c in contacts}}

        def link(e):
            svc.crm.set_application_contact(job_id, mode, e.value or None)
            ui.notify(t("Contacto vinculado a la aplicación."), type="positive")
            self.page.refresh_all()
        if contacts:
            ui.select(tl(options), value=current or "", label=t("Contacto de la aplicación ({})", mode), on_change=link) \
                .classes("w-80").mark("app_contact")
        ui.button(t("Nuevo contacto en esta empresa"), icon="person_add",
                  on_click=lambda: contact_dialog(self.page, None, company_id=company_id, job_id=job_id,
                                                  mode=mode)).props("dense flat")


def tailoring_dialog(page, run_id: str, on_change=None) -> None:
    svc = page.svc
    run = svc.tailoring.get_run(run_id)
    pending = run["status"] == "proposed"
    include: dict[str, Any] = {}
    confirm: dict[str, Any] = {}
    with page.dialog().props("maximized") as dlg, ui.card().classes("w-full"):
        with ui.row().classes("w-full justify-between items-center"):
            ui.label(t("Propuesta de CV — {} ({})", run['title'], run['company_name'])).classes("text-h6")
            ui.button(icon="close", on_click=dlg.close).props("flat round")
        ui.label(t("Origen: {} {} · {} · estado: "
                 "{}", run['provider'] or run['source'], run['model'], run['created_at'], run['status'])).classes("text-grey-7")
        for w in run["warnings"]:
            ui.label(f"⚠ {w}").classes("text-warning")
        if run["notes"]:
            ui.label(run["notes"]).classes("text-sm whitespace-pre-wrap")
        if run["missing_requirements"]:
            ui.label(t("Requisitos que NO tiene (no se agregan al CV): ") + ", ".join(run["missing_requirements"])) \
                .classes("text-sm text-negative")
        for q in run["questions_for_user"]:
            ui.label(f"❓ {q}").classes("text-sm")
        if not run["changes"]:
            ui.label(t("La propuesta no tiene cambios aplicables.")).classes("text-grey-7")
        doc = svc.tailoring.master_doc(run["mode_id"]) if run["mode_id"] in MODE_DEFS else None
        names = {s["id"]: s["name"] for s in (doc or {}).get("skills", [])}
        texts = {b["id"]: b["text"] for p in (doc or {}).get("experience", []) for b in p["bullets"]}
        for ch in run["changes"]:
            flagged = ch["requires_confirmation"]
            with ui.card().classes("w-full").mark(f"change_{ch['change_id']}"):
                with ui.row().classes("items-center gap-2"):
                    include[ch["change_id"]] = ui.checkbox(
                        f"{ACTIONS.get(ch['action'], ch['action'])} · {ch['target_id']}",
                        value=pending and not flagged and ch["action"] in ("reorder", "remove")) \
                        .props("" if pending else "disable") \
                        .mark(f"include_{ch['change_id']}")
                    for f in ch.get("flags", []):
                        ui.badge(f["message"], color=FLAG_COLORS.get(f["severity"], "grey"))
                if ch["action"] == "reorder":
                    order = [names.get(i, texts.get(i, i)) for i in ch["order"]]
                    ui.label(t("Nuevo orden: ") + " → ".join(o[:60] for o in order[:15])
                             + (" …" if len(order) > 15 else "")).classes("text-sm")
                else:
                    if ch["original"]:
                        ui.label(t("Antes: {}", ch['original'])).classes("text-sm bg-red-1 text-black q-pa-xs w-full")
                    if ch["proposed"]:
                        ui.label(t("Después: {}", ch['proposed'])).classes("text-sm bg-green-1 text-black q-pa-xs w-full")
                    if ch.get("evidence_ids"):
                        ui.label(t("Evidencia: ") + ", ".join(ch["evidence_ids"])).classes("text-caption text-grey-7")
                if ch.get("rationale"):
                    ui.label(t("Motivo: {}", ch['rationale'])).classes("text-caption")
                if flagged and pending:
                    confirm[ch["change_id"]] = ui.checkbox(t("Verifiqué que este texto es verdadero y comprobable")) \
                        .mark(f"confirm_{ch['change_id']}")
        if run["rejected_changes"]:
            with ui.expansion(t("Cambios rechazados automáticamente ({})", len(run['rejected_changes']))):
                for r in run["rejected_changes"]:
                    ui.label(f"{r['change_id']} ({r['target_id']}): {r['reason']}").classes("text-sm")

        def approve():
            chosen = [cid for cid, cb in include.items() if cb.value]
            if not chosen:
                ui.notify(t("Seleccione al menos un cambio."), type="warning")
                return
            try:
                vid = svc.tailoring.approve(run_id, chosen, [cid for cid, cb in confirm.items() if cb.value])
            except ValueError as exc:
                ui.notify(t(str(exc)), type="negative", multi_line=True)
                return
            dlg.close()
            ui.notify(t("CV adaptado aprobado y vinculado a la aplicación. Descárguelo como .docx en "
                      "'CV adaptado a esta vacante'."), type="positive", multi_line=True)
            page.refresh_all()
            if on_change:
                on_change()
            return vid

        def discard():
            svc.tailoring.discard(run_id)
            dlg.close()
            page.refresh_all()
            if on_change:
                on_change()
        if pending:
            with ui.row():
                ui.button(t("Aprobar cambios seleccionados"), icon="check", on_click=approve).mark("tailor_approve")
                ui.button(t("Descartar propuesta"), on_click=discard).props("flat")
        elif run["resume_version_id"]:
            ui.label(t("Aprobada → versión {} (cambios: "
                     "{})", run['resume_version_id'], ', '.join(run['approved_change_ids']))).classes("text-positive")
    dlg.open()

