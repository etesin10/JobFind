"""'Empresas' and 'Contactos' tabs plus their edit dialogs (spec §4)."""
from __future__ import annotations

from datetime import date

from nicegui import ui

from ..modes import MODE_DEFS
from ..services.crm import EVENT_KINDS, LEGITIMACY
from ..i18n import t, tl


def _err(exc: Exception) -> None:
    ui.notify(t("No se guardó: {}", exc), type="negative", multi_line=True)


def company_dialog(page, company_id: str | None) -> None:
    svc = page.svc
    c = svc.crm.get_company(company_id) if company_id else {
        "company_id": "", "name": "", "website": "", "careers_url": "", "ats_provider": "", "ats_board_id": "",
        "watch": False, "legitimacy": "unknown", "evidence": "", "notes": "", "jobs": [], "contacts": []}
    with page.dialog() as dlg, ui.card().classes("w-[860px] max-w-full"):
        ui.label(c["name"] or "Nueva empresa").classes("text-h6")
        with ui.grid(columns=2).classes("w-full"):
            name = ui.input(t("Nombre"), value=c["name"]).mark("company_name")
            website = ui.input(t("Sitio web"), value=c["website"])
            careers = ui.input(t("Página de empleos"), value=c["careers_url"])
            ats = ui.input(t("ATS (greenhouse/lever/ashby/…)"), value=c["ats_provider"])
            board = ui.input(t("ID del tablero ATS"), value=c["ats_board_id"])
            legit = ui.select(tl(LEGITIMACY), value=c["legitimacy"], label=t("Legitimidad"))
        watch = ui.switch(t("En mi lista de seguimiento"), value=c["watch"]).mark("company_watch")
        evidence = ui.textarea(t("Evidencia (fuentes: apoyo de visa, legitimidad, cultura…)"), value=c["evidence"]) \
            .classes("w-full")
        notes = ui.textarea(t("Notas"), value=c["notes"]).classes("w-full")

        def save():
            data = {"website": website.value, "careers_url": careers.value, "ats_provider": ats.value,
                    "ats_board_id": board.value, "legitimacy": legit.value, "watch": watch.value,
                    "evidence": evidence.value, "notes": notes.value}
            try:
                if company_id:
                    if (name.value or "").strip() != c["name"]:
                        svc.crm.rename_company(company_id, name.value or "")
                    svc.crm.upsert_company({"company_id": company_id, **data})
                else:
                    svc.crm.upsert_company({"name": name.value, **data})
            except (ValueError, KeyError) as exc:
                _err(exc)
                return
            dlg.close()
            ui.notify(t("Empresa guardada."), type="positive")
            page.refresh_all()

        def delete():
            try:
                svc.crm.delete_company(company_id)
            except ValueError as exc:
                _err(exc)
                return
            dlg.close()
            page.refresh_all()
        with ui.row():
            ui.button(t("Guardar"), icon="save", on_click=save).mark("company_save")
            if company_id:
                ui.button(t("Eliminar"), color="negative", on_click=delete).props("flat")
            ui.button(t("Cerrar"), on_click=dlg.close).props("flat")
        if company_id:
            ui.label(t("Vacantes ({})", len(c['jobs']))).classes("text-bold")
            for j in c["jobs"][:50]:
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"• {j['title']} — {j['location_text'] or '—'} ({j['ad_status']})").classes("text-sm")
                    ui.button(t("Ver"), on_click=lambda jid=j["job_id"]: (dlg.close(), page.detail_dialog(jid))) \
                        .props("dense flat")
            ui.label(t("Contactos ({})", len(c['contacts']))).classes("text-bold")
            for p in c["contacts"]:
                with ui.row().classes("items-center gap-2"):
                    ui.label(f"• {p['name']} · {p['role'] or '—'} · {p['email'] or '—'}").classes("text-sm")
                    ui.button(t("Abrir"), on_click=lambda pid=p["contact_id"]: (dlg.close(),
                                                                            contact_dialog(page, pid))) \
                        .props("dense flat")
            ui.button(t("Nuevo contacto"), icon="person_add",
                      on_click=lambda: (dlg.close(), contact_dialog(page, None, company_id=company_id))) \
                .props("dense flat")
    dlg.open()


def contact_dialog(page, contact_id: str | None, company_id: str = "", job_id: str = "", mode: str = "") -> None:
    svc = page.svc
    c = svc.crm.get_contact(contact_id) if contact_id else {
        "contact_id": "", "name": "", "role": "", "company_id": company_id or "", "email": "", "phone": "",
        "profile_url": "", "evidence": "", "notes": "", "source": "user", "established": False, "events": [],
        "applications": []}
    companies = {"": "— sin empresa —", **{x["company_id"]: x["name"] for x in svc.crm.list_companies()}}
    with page.dialog() as dlg, ui.card().classes("w-[860px] max-w-full"):
        ui.label(c["name"] or "Nuevo contacto").classes("text-h6")
        if c.get("source") and c["source"] != "user":
            ui.label(t("Agregado por: {} (verifique la identidad antes de confiar)", c['source'])) \
                .classes("text-caption text-warning")
        with ui.grid(columns=2).classes("w-full"):
            name = ui.input(t("Nombre"), value=c["name"]).mark("contact_name")
            role = ui.input(t("Cargo (recruiter, hiring manager…)"), value=c["role"])
            comp = ui.select(tl(companies), value=c["company_id"] if c["company_id"] in companies else "",
                             label=t("Empresa"), with_input=True)
            email = ui.input(t("Correo"), value=c["email"]).mark("contact_email")
            phone = ui.input(t("Teléfono"), value=c["phone"])
            url = ui.input(t("Perfil (LinkedIn u otro)"), value=c["profile_url"])
        evidence = ui.textarea(t("Evidencia (dónde encontró el contacto)"), value=c["evidence"]).classes("w-full")
        notes = ui.textarea(t("Notas"), value=c["notes"]).classes("w-full")
        established = ui.switch(t("Contacto establecido (ya hubo una conversación en ambos sentidos)"),
                                value=bool(c.get("established"))).mark("contact_established")

        def save():
            data = {"name": name.value, "role": role.value, "company_id": comp.value or "", "email": email.value,
                    "phone": phone.value, "profile_url": url.value, "evidence": evidence.value, "notes": notes.value,
                    "established": bool(established.value)}
            try:
                cid, _ = svc.crm.upsert_contact({"contact_id": contact_id, **data} if contact_id else data)
                if job_id and mode and not contact_id:
                    svc.crm.set_application_contact(job_id, mode, cid)
            except (ValueError, KeyError) as exc:
                _err(exc)
                return
            dlg.close()
            ui.notify(t("Contacto guardado.") + (t(" Vinculado a la aplicación.") if job_id and mode and not contact_id
                                               else ""), type="positive")
            page.refresh_all()

        def delete():
            svc.crm.delete_contact(contact_id)
            dlg.close()
            page.refresh_all()
        with ui.row():
            ui.button(t("Guardar"), icon="save", on_click=save).mark("contact_save")
            if contact_id:
                ui.button(t("Eliminar"), color="negative", on_click=delete).props("flat")
            ui.button(t("Cerrar"), on_click=dlg.close).props("flat")
        if contact_id:
            ui.label(t("Interacciones")).classes("text-bold")
            with ui.row().classes("items-end"):
                kind = ui.select(tl(EVENT_KINDS), value="email", label=t("Tipo")).classes("w-36")
                when = ui.input(t("Fecha (AAAA-MM-DD)"), value=date.today().isoformat()).classes("w-40")
                note = ui.input(t("Nota")).classes("w-80").mark("event_note")

                def add_event():
                    try:
                        svc.crm.add_event(contact_id, kind.value, note.value or "", when.value or None,
                                          job_id, mode if mode in MODE_DEFS else "")
                    except (ValueError, KeyError) as exc:
                        _err(exc)
                        return
                    dlg.close()
                    contact_dialog(page, contact_id, job_id=job_id, mode=mode)
                    page.refresh_all()
                ui.button(t("Registrar"), icon="add", on_click=add_event).props("dense").mark("event_add")
            for e in c["events"][:100]:
                ui.label(f"{e['happened_on'] or e['created_at']} · {EVENT_KINDS.get(e['kind'], e['kind'])}"
                         + (f" · {e['job_title']}" if e["job_title"] else "") + f" — {e['note']}"
                         + (f" ({e['actor']})" if e["actor"] != "user" else "")).classes("text-sm")
            if c["applications"]:
                ui.label(t("Aplicaciones vinculadas")).classes("text-bold")
                for a in c["applications"]:
                    ui.label(f"• {a['job_title']} [{a['mode_id']}] — {a['status']}").classes("text-sm")
    dlg.open()


class CrmPanel:
    def __init__(self, page):
        self.page = page
        self.svc = page.svc
        self.q = {"companies": "", "contacts": ""}
        self.watch_only = False
        self.established = "all"

    def _search(self, key: str, label: str) -> None:
        inp = ui.input(t(label), value=self.q[key]).props("clearable dense").classes("w-72")

        def go():
            self.q[key] = inp.value or ""
            self.page.refresh_all()
        inp.on("keydown.enter", go)
        ui.button(t("Buscar"), on_click=go).props("dense")

    def companies(self) -> None:
        ui.label(t("Empresas")).classes("text-h6")
        ui.label(t("Se crean solas al registrar vacantes; aquí agrega datos de investigación, lista de seguimiento y "
                 "legitimidad. Los agentes pueden completar campos vacíos pero no sobrescriben los suyos.")) \
            .classes("text-grey-7")
        with ui.row().classes("items-center"):
            self._search("companies", "Buscar empresa")
            ui.switch(t("Solo en seguimiento"), value=self.watch_only,
                      on_change=lambda e: (setattr(self, "watch_only", e.value), self.page.refresh_all()))
            ui.button(t("Nueva empresa"), icon="add_business", on_click=lambda: company_dialog(self.page, None)) \
                .props("dense outline").mark("company_new")
        rows = self.svc.crm.list_companies(self.q["companies"], self.watch_only)
        for r in rows:
            r["legit"] = LEGITIMACY.get(r["legitimacy"], r["legitimacy"])
            r["seguimiento"] = "★" if r["watch"] else ""
        cols = [("seguimiento", "★"), ("name", "Empresa"), ("jobs", "Vacantes"), ("contacts", "Contactos"),
                ("legit", "Legitimidad"), ("ats_provider", "ATS"), ("website", "Sitio web")]
        table = ui.table(rows=rows, columns=[{"name": k, "label": t(lbl), "field": k, "sortable": True, "align": "left"}
                                             for k, lbl in cols], row_key="company_id", pagination=25) \
            .props("dense").classes("w-full").mark("companies_table")
        table.on("rowClick", lambda e: company_dialog(self.page, e.args[1]["company_id"]))
        if not rows:
            ui.label(t("Sin empresas (con este filtro).")).classes("text-grey-7")

    def contacts(self) -> None:
        ui.label(t("Contactos")).classes("text-h6")
        ui.label(t("Recruiters y hiring managers, con su historial de interacciones. Vincule un contacto a una "
                 "aplicación desde el detalle de la vacante.")).classes("text-grey-7")
        with ui.row().classes("items-center"):
            self._search("contacts", "Buscar contacto")
            ui.toggle({"all": t("Todos"), "yes": t("Establecidos"), "no": t("Sin establecer")}, value=self.established,
                      on_change=lambda e: (setattr(self, "established", e.value), self.page.refresh_all())) \
                .props("dense").mark("contacts_established_filter")
            ui.button(t("Nuevo contacto"), icon="person_add", on_click=lambda: contact_dialog(self.page, None)) \
                .props("dense outline").mark("contact_new")
        flag = {"all": None, "yes": True, "no": False}[self.established]
        rows = self.svc.crm.list_contacts(self.q["contacts"], established=flag)
        for r in rows:
            r["establecido"] = "✓ Sí" if r["established"] else "No"
        cols = [("establecido", "Establecido"), ("name", "Nombre"), ("role", "Cargo"), ("company_name", "Empresa"),
                ("email", "Correo"), ("phone", "Teléfono"), ("last_interaction", "Última interacción"),
                ("source", "Origen")]
        table = ui.table(rows=rows, columns=[{"name": k, "label": t(lbl), "field": k, "sortable": True, "align": "left"}
                                             for k, lbl in cols], row_key="contact_id", pagination=25) \
            .props("dense").classes("w-full").mark("contacts_table")
        table.on("rowClick", lambda e: contact_dialog(self.page, e.args[1]["contact_id"]))
        if not rows:
            ui.label(t("Sin contactos todavía.")).classes("text-grey-7")
