"""'Skills y CV' tab: candidate profiles, résumé import, proposed→approved skills/items, master versions."""
from __future__ import annotations

import logging
from typing import Any

from nicegui import run, ui

from ..candidate.parsing import ResumeParseError
from ..candidate.vocabulary import CATEGORY_LABELS
from ..modes import MODE_DEFS
from ..i18n import t, tl

log = logging.getLogger(__name__)

CONFIDENCE_LABELS = {"high": "Alta (en experiencia)", "medium": "Media (lista/resumen)", "low": "Baja",
                     "": "—"}
STATUS_LABELS = {"proposed": "Propuesto", "approved": "Aprobado", "rejected": "Rechazado"}
KIND_LABELS = {"summary": "Resumen", "position": "Puesto", "education": "Educación",
               "certification": "Certificación", "language": "Idioma"}
TAG_LABELS = {"": "—", "core": "Principal", "preferred": "Preferido"}
SECTION_LABELS = {"experience": "experiencia", "skills": "skills", "summary": "resumen", "header": "encabezado",
                  "education": "educación", "certifications": "certificaciones", "languages": "idiomas",
                  "projects": "proyectos", "manual": "manual", "agent": "agente IA", "other": "otra sección"}
ORIGIN_LABELS = {"resume": "CV", "manual": "Usted", "agent": "Agente IA", "excel": "Excel"}


def _item_title(kind: str, data: dict[str, Any]) -> str:
    if kind == "position":
        dates = f"{data.get('start', '')} → {data.get('end', '')}"
        return f"{data.get('title') or '¿Puesto?'} · {data.get('company') or '¿Empresa?'} · {dates}"
    if kind == "education":
        return " · ".join(x for x in (data.get("degree"), data.get("institution"), data.get("year")) if x)
    if kind == "certification":
        return " · ".join(x for x in (data.get("name"), data.get("issuer"), data.get("year")) if x)
    if kind == "language":
        return f"{data.get('language', '')} ({data.get('level') or 'nivel no indicado'})"
    if kind == "summary":
        text = data.get("text", "")
        return text[:160] + ("…" if len(text) > 160 else "")
    return str(data)[:160]


class SkillsPanel:
    def __init__(self, page):
        self.page = page
        self.svc = page.svc
        self.cs = page.svc.candidate

    # ------------------------------------------------------------------ helpers
    def _selected(self):
        cands = self.cs.list_candidates()
        if not cands:
            return None, cands
        wanted = self.svc.db.get_setting("skills_ui_candidate")
        by_id = {c.id: c for c in cands}
        if wanted in by_id:
            return by_id[wanted], cands
        mode = self.page.mode if self.page.mode in MODE_DEFS else next(iter(MODE_DEFS))
        active = self.cs.active_candidate(mode)
        return (active or cands[0]), cands

    def _select(self, candidate_id: str) -> None:
        self.svc.db.set_setting("skills_ui_candidate", candidate_id)
        self.page.refresh_all()

    def _done(self, message: str, kind: str = "positive") -> None:
        ui.notify(t(message), type=kind, multi_line=True)
        self.page.refresh_all()

    # ------------------------------------------------------------------ render
    def render(self) -> None:
        ui.label(t("Skills y CV")).classes("text-h6")
        ui.label(t("Su currículum es la fuente de verdad. Todo lo que la app extrae es una PROPUESTA: solo los skills "
                 "y datos que usted aprueba se usan para comparar vacantes. Nada se inventa ni se agrega "
                 "automáticamente.")).classes("text-grey-7")
        cand, cands = self._selected()
        if cand is None:
            self._first_profile()
            return
        self._profile_bar(cand, cands)
        self._import_box(cand)
        ev_map = self.cs.evidence_map(cand.id)
        skills = self.cs.skills(cand.id)
        self._proposed_skills(cand, [s for s in skills if s.status == "proposed"], ev_map)
        self._approved_skills(cand, [s for s in skills if s.status == "approved"], ev_map)
        self._rejected_skills([s for s in skills if s.status == "rejected"])
        self._items(cand)
        self._versions(cand)
        self._contact_box(cand)

    def _contact_box(self, cand) -> None:
        contact = dict(cand.contact or {}) if isinstance(cand.contact, dict) else {}
        with ui.expansion(t("Encabezado del CV generado (datos personales)"), icon="contact_mail").classes("w-full") \
                .mark("contact_box"):
            ui.label(t("Solo se usan para generar sus CV adaptados (.docx) en esta computadora. No se incluyen en los "
                     "paquetes de IA ni en la API salvo que usted lo marque explícitamente.")).classes("text-caption")
            with ui.grid(columns=2).classes("w-full"):
                fields = {k: ui.input(t(label), value=contact.get(k, "")) for k, label in (
                    ("name", "Nombre completo"), ("headline", "Titular (p. ej. Senior Data Engineer)"),
                    ("email", "Correo"), ("phone", "Teléfono"), ("location", "Ubicación"))}
                fields["links"] = ui.input(t("Enlaces (LinkedIn, GitHub…; separados por coma)"),
                                           value=", ".join(contact.get("links", [])))
            fields["name"].mark("contact_fullname")

            def save():
                self.cs.update_contact(cand.id, {k: v.value for k, v in fields.items()})
                self._done("Encabezado guardado.")
            ui.button(t("Guardar encabezado"), icon="save", on_click=save).props("dense").mark("contact_header_save")

    def _first_profile(self) -> None:
        with ui.card().classes("w-full"):
            ui.label(t("Cree su primer perfil de candidato")).classes("text-bold")
            ui.label(t("Puede tener varios (por ejemplo 'Data Engineer' y otro no técnico) y usar uno distinto en "
                     "cada modo.")).classes("text-caption")
            name = ui.input(t("Nombre del perfil"), value="Mi perfil").mark("cand_name")

            def create():
                try:
                    c = self.cs.create_candidate(name.value or "")
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                self.svc.db.set_setting("skills_ui_candidate", c.id)
                self._done(f"Perfil '{c.name}' creado. Ahora importe su CV.")
            ui.button(t("Crear perfil"), icon="person_add", on_click=create).mark("cand_create")

    def _profile_bar(self, cand, cands) -> None:
        with ui.card().classes("w-full bg-blue-1"):
            with ui.row().classes("items-end w-full"):
                ui.select({c.id: f"{c.name} (v{c.version})" for c in cands}, value=cand.id,
                          label=t("Perfil de candidato"), on_change=lambda e: self._select(e.value)).classes("w-72") \
                    .mark("cand_select")
                ui.button(t("Nuevo perfil"), icon="person_add", on_click=self._new_profile_dialog) \
                    .props("dense outline").mark("cand_new")
                ui.button(t("Renombrar"), icon="edit", on_click=lambda: self._rename_dialog(cand)).props("dense flat")
                ui.button(t("Eliminar…"), icon="delete", on_click=lambda: self._delete_dialog(cand)).props("dense flat")
            with ui.row().classes("items-center"):
                for mode_id, mdef in MODE_DEFS.items():
                    active = self.cs.active_candidate(mode_id)
                    if active and active.id == cand.id:
                        ui.badge(t("Activo en {}", mode_id), color="positive").mark(f"cand_active_{mode_id}")
                    else:
                        ui.button(t("Usar en {}", mode_id), icon="check_circle",
                                  on_click=lambda m=mode_id: self._activate(m, cand)).props("dense outline") \
                            .mark(f"use_in_{mode_id}")
            ui.label(t("Cada modo compara las vacantes contra los skills APROBADOS de su perfil activo.")) \
                .classes("text-caption text-grey-7")

    def _activate(self, mode_id: str, cand) -> None:
        self.cs.set_active_candidate(mode_id, cand.id)
        self._done(f"'{cand.name}' ahora es el perfil activo en {mode_id}; vacantes reevaluadas.")

    def _new_profile_dialog(self) -> None:
        with self.page.dialog() as dlg, ui.card().classes("w-96"):
            ui.label(t("Nuevo perfil de candidato")).classes("text-bold")
            name = ui.input(t("Nombre")).classes("w-full").mark("cand_new_name")

            def create():
                try:
                    c = self.cs.create_candidate(name.value or "")
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                dlg.close()
                self.svc.db.set_setting("skills_ui_candidate", c.id)
                self._done(f"Perfil '{c.name}' creado.")
            with ui.row():
                ui.button(t("Crear"), on_click=create).mark("cand_new_confirm")
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    def _rename_dialog(self, cand) -> None:
        with self.page.dialog() as dlg, ui.card().classes("w-96"):
            name = ui.input(t("Nuevo nombre"), value=cand.name).classes("w-full")

            def save():
                try:
                    self.cs.rename_candidate(cand.id, name.value or "")
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                dlg.close()
                self._done("Perfil renombrado.")
            with ui.row():
                ui.button(t("Guardar"), on_click=save)
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    def _delete_dialog(self, cand) -> None:
        with self.page.dialog() as dlg, ui.card().classes("w-[420px]"):
            ui.label(t("¿Eliminar el perfil '{}'?", cand.name)).classes("text-bold")
            ui.label(t("Se eliminan sus skills, datos y versiones de la base de datos. Los archivos originales de CV "
                     "se conservan en resumes/originals. No se puede eliminar un perfil activo en algún modo.")) \
                .classes("text-caption")

            def confirm():
                try:
                    self.cs.delete_candidate(cand.id)
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                dlg.close()
                self.svc.db.set_setting("skills_ui_candidate", None)
                self._done("Perfil eliminado.")
            with ui.row():
                ui.button(t("Eliminar"), color="negative", on_click=confirm)
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    # ------------------------------------------------------------------ import
    def _import_box(self, cand) -> None:
        with ui.card().classes("w-full"):
            ui.label(t("Importar CV en '{}'", cand.name)).classes("text-bold")
            ui.label(t("Formatos: .docx, .pdf con texto seleccionable, .txt, o pegar texto. Puede importar varios CV; "
                     "los skills repetidos suman evidencia. El archivo original se guarda sin cambios.")) \
                .classes("text-caption text-grey-7")
            from ..services.candidate import AUTO_APPROVE_KEY

            def toggle_auto(e):
                self.svc.db.set_setting(AUTO_APPROVE_KEY, bool(e.value))
                ui.notify(t("Aprobación automática de confianza alta ") + (t("activada") if e.value else t("desactivada")),
                          type="info")
            ui.switch(t("Aprobar automáticamente los skills de confianza ALTA (encontrados en su experiencia) al "
                      "importar"), value=bool(self.svc.db.get_setting(AUTO_APPROVE_KEY, False)),
                      on_change=toggle_auto).mark("auto_approve")
            ui.label(t("Los de confianza media/baja y los que sugiera un agente IA quedan siempre como propuestas. "
                     "Nunca se aprueba un skill que usted rechazó.")).classes("text-caption text-grey-7")

            async def on_upload(e):
                data = await e.file.read()
                await self._run_import(lambda: self.cs.import_file(cand.id, e.file.name, data), e.file.name)
            ui.upload(label=t("Subir CV (.docx, .pdf, .txt)"), auto_upload=True, on_upload=on_upload) \
                .props('accept=".docx,.pdf,.txt"').mark("resume_upload")
            with ui.expansion(t("Pegar texto del CV")).classes("w-full"):
                label = ui.input(t("Nombre de referencia"), value="CV pegado").classes("w-80")
                text = ui.textarea(t("Texto completo del CV")).classes("w-full").mark("paste_text")

                async def paste():
                    await self._run_import(lambda: self.cs.import_text(cand.id, text.value or "", label.value or ""),
                                           label.value or "texto")
                ui.button(t("Importar texto"), icon="content_paste", on_click=paste).mark("paste_import")
            sources = self.cs.sources(cand.id)
            if sources:
                ui.label(t("CV importados ({})", len(sources))).classes("text-bold text-sm")
                for src in sources:
                    summary = src.summary or {}
                    with ui.row().classes("items-center"):
                        ui.label(t("📄 {} · {} · {:%Y-%m-%d %H:%M} · "
                                 "{} skills nuevos, {} datos", src.file_name, src.kind.upper(), src.imported_at, summary.get('new_skills', 0), summary.get('new_items', 0))) \
                            .classes("text-sm")
                        ui.button(t("Ver texto extraído"), on_click=lambda s=src: self._source_dialog(s)) \
                            .props("dense flat")
                        for w in src.warnings or []:
                            ui.label(f"⚠ {w}").classes("text-caption text-warning")

    async def _run_import(self, action, name: str) -> None:
        # Parsing + extraction + job re-evaluation run in a worker thread so the UI stays responsive.
        ui.notify(t("Leyendo '{}'…", name), type="ongoing", timeout=2000)
        try:
            res = await run.io_bound(action)
        except ResumeParseError as exc:
            ui.notify(t("No se pudo importar '{}': {}", name, exc), type="negative", multi_line=True)
            return
        except Exception:  # last-resort safety net: never leave the user without feedback
            log.exception("Unexpected error importing a résumé")
            ui.notify(t("No se pudo leer '{}' (error inesperado; detalles en logs/jsh.log).", name), type="negative",
                      multi_line=True)
            return
        if res.duplicate:
            ui.notify(t("'{}' ya estaba importado en este perfil (mismo contenido).", name), type="info")
            return
        msg = (f"'{name}' importado: {res.new_skills} skills nuevos, {res.evidence_added} evidencias, "
               f"{res.new_items} datos propuestos ({res.skipped_items} repetidos).")
        if res.auto_approved:
            msg += f" {res.auto_approved} skill(s) de confianza alta aprobados automáticamente."
        msg += " Revise y apruebe el resto abajo."
        if not res.sections:
            msg += " No se reconocieron secciones (Experiencia, Skills…); revise con atención."
        self._done(msg)

    def _source_dialog(self, src) -> None:
        intact = self.cs.verify_original(src.id)
        with self.page.dialog().props("maximized") as dlg, ui.card().classes("w-full"):
            with ui.row().classes("w-full justify-between items-center"):
                ui.label(t("Texto extraído de {}", src.file_name)).classes("text-h6")
                ui.button(icon="close", on_click=dlg.close).props("flat round")
            ui.label((t("✓ Original guardado sin cambios") if intact else t("⚠ El archivo original no se encontró o cambió"))
                     + t(" · {} · SHA-256 {}…", src.stored_path, src.sha256[:16])).classes("text-caption")
            with ui.scroll_area().classes("h-[70vh] w-full border"):
                ui.label(src.text or "").classes("whitespace-pre-wrap text-sm")
        dlg.open()

    # ------------------------------------------------------------------ skills
    def _skill_rows(self, skills, ev_map) -> list[dict[str, Any]]:
        rows = []
        for sk in skills:
            evs = ev_map.get(sk.id, [])
            first = evs[0] if evs else None
            rows.append({
                "id": sk.id, "skill": sk.name, "categoria": CATEGORY_LABELS.get(sk.category, sk.category),
                "confianza": CONFIDENCE_LABELS.get(sk.confidence, sk.confidence),
                "evidencia": (f"[{SECTION_LABELS.get(first.section, first.section)}] {first.excerpt[:140]}"
                              if first else "—"),
                "n_ev": len(evs), "nota": sk.notes or "", "etiqueta": TAG_LABELS.get(sk.tag, sk.tag),
                "peso": f"{sk.weight:g}", "alias": ", ".join(sk.aliases or []), "nivel": sk.proficiency or "—",
                "anios": "—" if sk.years is None else f"{sk.years:g}", "activo": "Sí" if sk.active else "No",
                "conf_raw": sk.confidence, "origen": ORIGIN_LABELS.get(sk.origin, sk.origin),
            })
        return rows

    def _proposed_skills(self, cand, skills, ev_map) -> None:
        with ui.card().classes("w-full"):
            ui.label(t("Skills propuestos ({}) — revise la evidencia antes de aprobar", len(skills))).classes("text-bold")
            if not skills:
                ui.label(t("No hay propuestas pendientes.")).classes("text-caption text-grey-7")
                return
            ui.label(t("Aprobar significa: 'tengo este skill y puedo demostrarlo'. Si un skill solo aparece en el "
                     "encabezado o con una nota como '(integration experience)', revise que el alcance sea correcto. "
                     "Clic en una fila para ver toda la evidencia y editar.")).classes("text-caption text-grey-7")
            cols = [("skill", "Skill"), ("categoria", "Categoría"), ("confianza", "Confianza"), ("origen", "Origen"),
                    ("evidencia", "Evidencia (primer fragmento)"), ("n_ev", "#"), ("nota", "Nota")]
            table = ui.table(rows=self._skill_rows(skills, ev_map),
                             columns=[{"name": k, "label": t(lbl), "field": k, "align": "left", "sortable": True}
                                      for k, lbl in cols],
                             row_key="id", selection="multiple", pagination=20) \
                .props("dense wrap-cells").classes("w-full").mark("skills_proposed_table")
            table.on("rowClick", lambda e: self._skill_dialog(e.args[1]["id"]))

            def select_high():
                table.selected = [r for r in table.rows if r["conf_raw"] == "high"]
                table.update()

            def apply(status: str):
                ids = [r["id"] for r in table.selected]
                if not ids:
                    ui.notify(t("Seleccione skills primero"), type="warning")
                    return
                n = self.cs.set_skill_status(ids, status)
                self._done(f"{n} skill(s) {STATUS_LABELS[status].lower()}s. Vacantes reevaluadas.")
            with ui.row():
                ui.button(t("Seleccionar confianza alta"), on_click=select_high).props("dense flat") \
                    .mark("select_high")
                ui.button(t("Aprobar seleccionados"), icon="check", color="positive",
                          on_click=lambda: apply("approved")).props("dense").mark("approve_selected")
                ui.button(t("Rechazar seleccionados"), icon="close", color="negative",
                          on_click=lambda: apply("rejected")).props("dense outline").mark("reject_selected")

    def _approved_skills(self, cand, skills, ev_map) -> None:
        with ui.card().classes("w-full"):
            ui.label(t("Skills aprobados ({}) — se usan para comparar vacantes", len(skills))).classes("text-bold")
            ui.label(t("Alias = otros nombres con los que aparece en anuncios. Nivel y años solo si usted los confirma "
                     "(nunca se infieren). Peso = importancia en el % de coincidencia.")) \
                .classes("text-caption text-grey-7")
            if skills:
                cols = [("skill", "Skill"), ("categoria", "Categoría"), ("etiqueta", "Etiqueta"), ("peso", "Peso"),
                        ("alias", "Alias"), ("nivel", "Nivel"), ("anios", "Años"), ("activo", "Activo"),
                        ("n_ev", "Evidencias"), ("nota", "Nota")]
                table = ui.table(rows=self._skill_rows(skills, ev_map),
                                 columns=[{"name": k, "label": t(lbl), "field": k, "align": "left", "sortable": True}
                                          for k, lbl in cols],
                                 row_key="id", pagination=25).props("dense wrap-cells").classes("w-full") \
                    .mark("skills_approved_table")
                table.on("rowClick", lambda e: self._skill_dialog(e.args[1]["id"]))
            ui.button(t("Agregar skill manualmente"), icon="add", on_click=lambda: self._manual_dialog(cand)) \
                .props("dense outline").mark("manual_skill")

    def _rejected_skills(self, skills) -> None:
        if not skills:
            return
        with ui.expansion(t("Skills rechazados ({})", len(skills))).classes("w-full"):
            for sk in skills:
                with ui.row().classes("items-center"):
                    ui.label(sk.name).classes("text-sm")
                    ui.button(t("Volver a propuesta"),
                              on_click=lambda s=sk: (self.cs.set_skill_status([s.id], "proposed"),
                                                     self._done(f"'{s.name}' vuelve a propuestas."))) \
                        .props("dense flat")

    def _skill_dialog(self, skill_id: str) -> None:
        sk = self.cs.get_skill(skill_id)
        if sk is None:
            return
        evidence = self.cs.evidence(skill_id)
        sources = {s.id: s.file_name for s in self.cs.sources(sk.candidate_id)}
        with self.page.dialog() as dlg, ui.card().classes("w-[760px] max-w-full"):
            ui.label(t("Skill: {}", sk.name)).classes("text-h6")
            with ui.grid(columns=3).classes("w-full"):
                name = ui.input(t("Nombre"), value=sk.name).mark("skill_name")
                category = ui.select(tl(CATEGORY_LABELS), value=sk.category if sk.category in CATEGORY_LABELS else "other",
                                     label=t("Categoría"))
                status = ui.select(tl(STATUS_LABELS), value=sk.status, label=t("Estado")).mark("skill_status")
                tag = ui.select(tl(TAG_LABELS), value=sk.tag, label=t("Etiqueta"))
                weight = ui.number(t("Peso (0.1–5)"), value=sk.weight, min=0.1, max=5, step=0.1)
                active = ui.switch(t("Activo"), value=sk.active)
                proficiency = ui.input(t("Nivel (solo si usted lo confirma)"), value=sk.proficiency)
                years = ui.input(t("Años (solo si usted lo confirma)"), value="" if sk.years is None else f"{sk.years:g}")
                aliases = ui.input(t("Alias (separados por coma)"), value=", ".join(sk.aliases or [])).mark("skill_aliases")
            notes = ui.input(t("Nota"), value=sk.notes).classes("w-full")
            ui.label(t("Evidencia ({})", len(evidence))).classes("text-bold")
            with ui.scroll_area().classes("h-48 w-full border"):
                for ev in evidence:
                    src = sources.get(ev.source_id, "manual" if ev.section == "manual" else "—")
                    ui.label(f"[{SECTION_LABELS.get(ev.section, ev.section)} · {src}] {ev.excerpt}") \
                        .classes("text-sm")

            def save():
                try:
                    self.cs.update_skill(sk.id, {
                        "name": name.value, "category": category.value, "status": status.value, "tag": tag.value,
                        "weight": weight.value, "active": active.value, "proficiency": proficiency.value,
                        "years": years.value, "aliases": aliases.value, "notes": notes.value})
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                dlg.close()
                self._done("Skill guardado; vacantes reevaluadas.")
            with ui.row():
                ui.button(t("Guardar"), on_click=save).mark("skill_save")
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    def _manual_dialog(self, cand) -> None:
        with self.page.dialog() as dlg, ui.card().classes("w-[520px]"):
            ui.label(t("Agregar skill manualmente")).classes("text-bold")
            ui.label(t("Úselo para skills reales que su CV no menciona. Quedará marcado como 'agregado por el usuario'.")) \
                .classes("text-caption")
            name = ui.input(t("Skill")).classes("w-full").mark("manual_name")
            category = ui.select(tl(CATEGORY_LABELS), value="other", label=t("Categoría"))
            aliases = ui.input(t("Alias (separados por coma)")).classes("w-full")

            def save():
                try:
                    self.cs.add_manual_skill(cand.id, name.value or "", category.value, aliases.value or "")
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                dlg.close()
                self._done("Skill agregado y aprobado.")
            with ui.row():
                ui.button(t("Agregar"), on_click=save).mark("manual_save")
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    # ------------------------------------------------------------------ items
    def _items(self, cand) -> None:
        items = self.cs.items(cand.id)
        proposed = [i for i in items if i.status == "proposed"]
        approved = [i for i in items if i.status == "approved"]
        with ui.card().classes("w-full"):
            ui.label(t("Datos del perfil propuestos ({}) — puestos, educación, idiomas…", len(proposed))) \
                .classes("text-bold")
            ui.label(t("Revise títulos, empresas y fechas: la lectura automática del formato puede equivocarse. "
                     "Edite antes de aprobar.")).classes("text-caption text-grey-7")
            for it in proposed:
                self._item_row(it, approved=False)
            if proposed:
                ui.button(t("Aprobar todos los datos propuestos"), icon="done_all",
                          on_click=lambda: (self.cs.set_item_status([i.id for i in proposed], "approved"),
                                            self._done(f"{len(proposed)} dato(s) aprobados."))) \
                    .props("dense outline").mark("items_approve_all")
        if approved:
            with ui.expansion(t("Datos aprobados ({})", len(approved))).classes("w-full"):
                for it in approved:
                    self._item_row(it, approved=True)

    def _item_row(self, it, approved: bool) -> None:
        with ui.row().classes("w-full items-center no-wrap").mark(f"item_{it.id}"):
            ui.badge(t(KIND_LABELS.get(it.kind, it.kind)), color="blue-grey")
            ui.label(_item_title(it.kind, it.data or {})).classes("text-sm grow")
            if it.kind == "position":
                n = len((it.data or {}).get("highlights", []))
                ui.label(t("{} logros/tareas", n)).classes("text-caption text-grey-7")
            ui.button(icon="edit", on_click=lambda i=it: self._item_dialog(i)).props("dense flat")
            if approved:
                ui.button(t("Volver a propuesta"), on_click=lambda i=it: (self.cs.set_item_status([i.id], "proposed"),
                                                                       self._done("Dato devuelto a propuestas."))) \
                    .props("dense flat")
            else:
                ui.button(icon="check", color="positive",
                          on_click=lambda i=it: (self.cs.set_item_status([i.id], "approved"),
                                                 self._done("Dato aprobado."))) \
                    .props("dense flat").mark(f"item_approve_{it.id}")
                ui.button(icon="close", color="negative",
                          on_click=lambda i=it: (self.cs.set_item_status([i.id], "rejected"),
                                                 self._done("Dato rechazado."))).props("dense flat")

    def _item_dialog(self, it) -> None:
        from ..services.candidate import ITEM_FIELDS
        data = dict(it.data or {})
        with self.page.dialog() as dlg, ui.card().classes("w-[760px] max-w-full"):
            ui.label(t("Editar {}", KIND_LABELS.get(it.kind, it.kind).lower())).classes("text-h6")
            ui.label(t("Texto original: {}", it.excerpt)).classes("text-caption text-grey-7")
            inputs = {}
            for f in ITEM_FIELDS.get(it.kind, ()):
                if f == "highlights":
                    value = "\n".join(h.get("text", "") if isinstance(h, dict) else str(h)
                                      for h in data.get("highlights", []))
                    inputs[f] = ui.textarea(t("Logros / tareas (uno por línea)"), value=value).classes("w-full")
                elif f == "text":
                    inputs[f] = ui.textarea(t("Texto"), value=data.get(f, "")).classes("w-full")
                else:
                    inputs[f] = ui.input(t(f), value=str(data.get(f, "") or "")).classes("w-full")

            def save():
                payload = {k: (w.value.split("\n") if k == "highlights" else w.value) for k, w in inputs.items()}
                self.cs.update_item(it.id, payload)
                dlg.close()
                self._done("Dato guardado.")
            with ui.row():
                ui.button(t("Guardar"), on_click=save)
                ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
        dlg.open()

    # ------------------------------------------------------------------ versions
    def _versions(self, cand) -> None:
        versions = self.cs.versions(cand.id)
        with ui.card().classes("w-full"):
            ui.label(t("CV maestro aprobado (versiones)")).classes("text-bold")
            ui.label(t("Guarda una foto inmutable de sus skills y datos APROBADOS. Las futuras adaptaciones por vacante "
                     "(fase 5) partirán de esta versión.")).classes("text-caption text-grey-7")

            def save():
                try:
                    v = self.cs.save_master_version(cand.id)
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="warning")
                    return
                self._done(f"Versión maestra v{v.version} guardada.")
            ui.button(t("Guardar versión maestra"), icon="save", on_click=save).props("dense").mark("save_master")
            for v in versions:
                content = v.content or {}
                ui.label(t("v{} · {} · {:%Y-%m-%d %H:%M} · "
                         "{} skills · {} datos", v.version, v.name, v.created_at, len(content.get('skills', [])), len(content.get('items', [])))) \
                    .classes("text-sm")
