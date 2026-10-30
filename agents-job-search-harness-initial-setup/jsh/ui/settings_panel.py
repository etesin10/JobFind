"""Configuración sections added in session 11: appearance & preferences, currency, ad checks, danger zone."""
from __future__ import annotations

from typing import TYPE_CHECKING

from nicegui import run, ui

from ..services import WIPE_CONFIRM_WORD
from ..i18n import t, tl
from . import prefs as P

if TYPE_CHECKING:
    from .app import MainPage


def appearance_section(page: "MainPage") -> None:
    svc, prefs = page.svc, page.prefs
    with ui.expansion(t("Apariencia, idioma y preferencias"), icon="palette", value=False).classes("w-full") \
            .mark("prefs_section"):
        ui.label(t("Se guardan en la carpeta portátil y se aplican al recargar la página.")).classes("text-caption")
        w: dict = {}
        with ui.grid(columns=3).classes("w-full"):
            w["ui_language"] = ui.select(tl(P.LANGUAGES), value=prefs["ui_language"], label=t("Idioma de la interfaz")) \
                .mark("pref_language")
            w["ui_theme"] = ui.select({k: t(v["label"]) for k, v in P.THEMES.items()}, value=prefs["ui_theme"],
                                      label=t("Tema de color")).mark("pref_theme")
            w["ui_dark"] = ui.select(tl(P.DARK), value=prefs["ui_dark"], label=t("Modo claro / oscuro"))
            w["ui_font_scale"] = ui.select(tl(P.FONT_SCALES), value=prefs["ui_font_scale"], label=t("Tamaño del texto"))
            w["ui_density"] = ui.select(tl(P.DENSITY), value=prefs["ui_density"], label=t("Densidad"))
            w["ui_start_tab"] = ui.select({"last": t("La última que usé"), **{n: t(lbl) for n, lbl, _ in page.tab_names}},
                                          value=prefs["ui_start_tab"], label=t("Pestaña al abrir"))
            w["ui_page_size"] = ui.select(tl(P.PAGE_SIZES), value=prefs["ui_page_size"], label=t("Filas por página (tablas)"))
            w["ui_card_limit"] = ui.select(tl(P.CARD_LIMITS), value=prefs["ui_card_limit"],
                                           label=t("Tarjetas visibles en Activos"))
        w["ui_auto_apply"] = ui.switch(t("Aplicar automáticamente los cambios de agentes cuando no hay ventanas "
                                       "abiertas (si no, se muestra un aviso)"), value=prefs["ui_auto_apply"])
        w["ui_confirm_bulk"] = ui.switch(t("Pedir confirmación en acciones masivas (Guardar/Restaurar seleccionadas)"),
                                         value=prefs["ui_confirm_bulk"])
        w["ui_shortcuts"] = ui.switch(t("Atajos de teclado ('/' buscar, '?' ayuda)"), value=prefs["ui_shortcuts"])
        with ui.row().classes("gap-1"):
            for key, theme in P.THEMES.items():
                ui.button(on_click=lambda k=key: w["ui_theme"].set_value(k)) \
                    .style(f"background-color: {theme['header']} !important; width: 28px; height: 28px") \
                    .props("round dense unelevated").tooltip(t(theme["label"]))

        def save():
            for key, widget in w.items():
                P.save(svc.db, key, widget.value)
            ui.notify(t("Preferencias guardadas"), type="positive")
            ui.navigate.reload()

        def reset():
            P.reset(svc.db)
            ui.navigate.reload()
        with ui.row():
            ui.button(t("Guardar y aplicar"), icon="check", on_click=save).mark("prefs_save")
            ui.button(t("Restablecer valores por defecto"), icon="restart_alt", on_click=reset).props("flat")
        ui.label(t("Los textos internos de las tablas (p. ej. 'Filas por página') cambian de idioma al reiniciar la "
                 "aplicación. Los documentos generados (Excel, paquetes IA, CV) mantienen su propio idioma.")) \
            .classes("text-caption text-grey-7")


def currency_section(page: "MainPage") -> None:
    svc = page.svc
    sal = svc.salary
    with ui.expansion(t("Moneda y tipos de cambio (salarios)"), icon="currency_exchange").classes("w-full") \
            .mark("fx_section"):
        ui.label(t("Todos los salarios se convierten a un monto ANUAL en la moneda base para comparar y filtrar. "
                 "Los tipos de cambio son las referencias diarias del Banco Central Europeo (frankfurter.dev, "
                 "gratis, sin clave); solo se envían los códigos de moneda. Puede fijar tipos manuales: las "
                 "actualizaciones automáticas nunca los sobrescriben.")).classes("text-caption")
        with ui.row().classes("items-end"):
            base = ui.select(sorted(set(sal.currencies_in_use()) | {sal.base()}), value=sal.base(),
                             label=t("Moneda base"), new_value_mode="add-unique").classes("w-40").mark("fx_base")
            hours = ui.number(t("Horas por año (salario por hora)"), value=sal.hours_per_year(), min=500, max=4000) \
                .classes("w-56")
            days = ui.number(t("Días por año (salario por día)"), value=sal.days_per_year(), min=100, max=366) \
                .classes("w-56")

            def save_settings():
                try:
                    sal.configure(base=str(base.value or ""), hours_per_year=hours.value, days_per_year=days.value)
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                ui.notify(t("Configuración de moneda guardada; vacantes re-evaluadas"), type="positive")
                page.refresh_all()
            ui.button(t("Guardar"), on_click=save_settings).props("dense").mark("fx_save")
        auto = ui.switch(t("Actualizar automáticamente si los tipos tienen más de 7 días"),
                         value=bool(svc.db.get_setting("fx_auto_refresh", True)),
                         on_change=lambda e: svc.db.set_setting("fx_auto_refresh", bool(e.value)))
        auto.mark("fx_auto")
        rows = sal.rate_rows()
        ui.table(rows=[{"id": r["currency"], "moneda": r["currency"], "tipo": f"1 {r['currency']} = {r['rate']:.6g} "
                        f"{sal.base()}", "fecha": r["as_of"], "fuente": "BCE" if r["source"] == "ecb" else
                        r["source"]} for r in rows],
                 columns=[{"name": k, "label": t(lbl), "field": k, "align": "left"} for k, lbl in
                          (("moneda", "Moneda"), ("tipo", "Tipo de cambio"), ("fecha", "Fecha"),
                           ("fuente", "Fuente"))],
                 row_key="id").props("dense").classes("w-full").mark("fx_table")
        if not rows:
            ui.label(t("Aún no hay tipos de cambio. Pulse 'Actualizar (BCE)' o agregue uno manual.")) \
                .classes("text-caption text-warning")

        async def refresh():
            ui.notify(t("Consultando tipos de cambio del BCE…"))
            try:
                res = await run.io_bound(sal.refresh_rates)
            except Exception as exc:  # offline, proxy, API change
                ui.notify(t("No se pudieron actualizar: {}", exc), type="negative", multi_line=True)
                return
            msg = f"{res['updated']} tipo(s) actualizados ({res['as_of']})"
            if res["skipped_manual"]:
                msg += f"; manuales conservados: {', '.join(res['skipped_manual'])}"
            if res["missing"]:
                msg += f"; sin dato del BCE: {', '.join(res['missing'])} (agréguelos a mano)"
            ui.notify(t(msg), type="positive", multi_line=True)
            page.refresh_all()
        with ui.row().classes("items-end"):
            ui.button(t("Actualizar (BCE)"), icon="sync", on_click=refresh).props("dense outline").mark("fx_refresh")
            cur = ui.input(t("Moneda (p. ej. USD)")).classes("w-32").mark("fx_manual_currency")
            rate = ui.number(t("1 moneda = ? {}", sal.base()), format="%.6f").classes("w-40").mark("fx_manual_rate")

            def manual():
                try:
                    sal.set_manual_rate(cur.value or "", rate.value)
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                ui.notify(t("Tipo manual guardado; vacantes re-evaluadas"), type="positive")
                page.refresh_all()

            def remove():
                if not cur.value:
                    ui.notify(t("Escriba la moneda a quitar"), type="warning")
                    return
                sal.delete_rate(cur.value)
                page.refresh_all()
            ui.button(t("Guardar tipo manual"), on_click=manual).props("dense").mark("fx_manual_save")
            ui.button(t("Quitar"), on_click=remove).props("dense flat")


def liveness_section(page: "MainPage") -> None:
    svc = page.svc
    st = svc.liveness.settings()
    with ui.expansion(t("Verificación de anuncios (¿siguen abiertos?)"), icon="travel_explore").classes("w-full") \
            .mark("liveness_section"):
        ui.label(t("Visita la URL de cada vacante guardada (no descartada como expirada) y marca 'posiblemente "
                 "cerrada' si la página responde 404/410, dice que ya no está disponible o redirige a una página "
                 "genérica. NUNCA cambia el estado ni borra: usted confirma. Solo se solicita la URL pública del "
                 "anuncio (sin datos suyos); se bloquean direcciones locales/privadas.")).classes("text-caption")
        with ui.row().classes("items-end"):
            enabled = ui.switch(t("Verificación automática"), value=st["enabled"]).mark("liveness_enabled")
            hours = ui.number(t("Volver a verificar cada (horas)"), value=st["interval_hours"], min=1, max=720) \
                .classes("w-56")
            batch = ui.number(t("Máximo por ronda"), value=st["batch"], min=1, max=200).classes("w-40")

            def save():
                try:
                    svc.liveness.configure(bool(enabled.value), int(hours.value or 24), int(batch.value or 25))
                except ValueError as exc:
                    ui.notify(t(str(exc)), type="negative")
                    return
                ui.notify(t("Guardado"), type="positive")
            ui.button(t("Guardar"), on_click=save).props("dense").mark("liveness_save")
        ui.label(t("Última ronda: {} · también: JobSearchManager --check-ads", st['last_run'] or '—')) \
            .classes("text-caption text-grey-7")


def danger_zone(page: "MainPage") -> None:
    svc = page.svc
    with ui.card().classes("w-full border-red-500 border").mark("danger_zone"):
        ui.label(t("Zona peligrosa")).classes("text-h6 text-negative")
        ui.label(t("Borrar todos los datos: vacantes, empresas, contactos, CV y skills, reglas personalizadas, vistas, "
                 "historial, Excel generados, exportaciones, backups y logs. Quedan las reglas por defecto y sus "
                 "preferencias de apariencia. Los tokens de la API se regeneran.")).classes("text-sm")

        def open_dialog():
            with page.dialog() as dlg, ui.card().classes("w-[520px] max-w-full"):
                ui.label(t("Borrar TODOS los datos")).classes("text-h6 text-negative")
                keep = ui.checkbox(t("Conservar UNA copia final de la base (backups/…_pre_wipe.db) por si me "
                                   "arrepiento"), value=True).mark("wipe_keep")
                ui.label(t("Si no la conserva, no habrá forma de recuperar nada.")).classes("text-caption text-warning")
                local_word = t(WIPE_CONFIRM_WORD)
                word = ui.input(t("Escriba {} para confirmar", local_word)).classes("w-72").mark("wipe_word")

                async def confirm():
                    if " ".join((word.value or "").upper().split()) not in {local_word, WIPE_CONFIRM_WORD}:
                        ui.notify(t("Escriba {} para confirmar.", local_word), type="warning")
                        return
                    rep = await run.io_bound(svc.wipe_all, bool(keep.value), WIPE_CONFIRM_WORD)
                    dlg.close()
                    msg = "Todos los datos fueron borrados."
                    if rep["backup"]:
                        msg += f" Copia final: {rep['backup']}"
                    if rep["locked"]:
                        msg += f" No se pudieron borrar (en uso): {', '.join(rep['locked'][:5])}"
                    ui.notify(t(msg), type="positive", multi_line=True, timeout=10000)
                    ui.navigate.reload()
                with ui.row():
                    ui.button(t("Borrar todo"), icon="delete_forever", color="negative", on_click=confirm) \
                        .mark("wipe_confirm")
                    ui.button(t("Cancelar"), on_click=dlg.close).props("flat")
            dlg.open()
        ui.button(t("Borrar todos los datos…"), icon="delete_forever", color="negative", on_click=open_dialog) \
            .props("outline").mark("wipe_open")
