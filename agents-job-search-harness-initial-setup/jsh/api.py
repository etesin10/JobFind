"""Local REST API for agents (``/api/v1``), mounted inside the running app.

Security model (user decision, session 9): reachable ONLY on 127.0.0.1 (the app never binds another interface);
every request needs a bearer token; the ``read`` token cannot write; the Host header must be localhost (blocks DNS
rebinding from web pages); no CORS headers are sent, so web pages cannot read responses. Agents can never change the
user's decisions, notes, rules or applications through the API — they add/fill data and submit proposals that the
user approves in the UI. Interactive docs: ``/api/v1/docs`` (OpenAPI at ``/api/v1/openapi.json``).
"""
from __future__ import annotations

import hmac
import re
import secrets
from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from . import __version__
from .modes import MODE_DEFS

TOKENS_KEY = "api_tokens"
ENABLED_KEY = "api_enabled"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def ensure_tokens(services) -> dict[str, str]:
    tokens = services.db.get_setting(TOKENS_KEY)
    if not isinstance(tokens, dict) or not tokens.get("write") or not tokens.get("read"):
        tokens = {"write": "jshw_" + secrets.token_urlsafe(32), "read": "jshr_" + secrets.token_urlsafe(32)}
        services.db.set_setting(TOKENS_KEY, tokens)
    return tokens


def regenerate_token(services, kind: str) -> str:
    tokens = dict(ensure_tokens(services))
    tokens[kind] = ("jshw_" if kind == "write" else "jshr_") + secrets.token_urlsafe(32)
    services.db.set_setting(TOKENS_KEY, tokens)
    return tokens[kind]


def api_enabled(services) -> bool:
    return bool(services.db.get_setting(ENABLED_KEY, True))


def create_api(services) -> FastAPI:
    api = FastAPI(title="Job Search Harness — API local", version=__version__,
                  description="API local para agentes (solo 127.0.0.1). Use `Authorization: Bearer <token>` y, "
                              "opcionalmente, `X-Agent-Name: <nombre>` para identificar al agente en el historial.")

    def _mode(mode: str, required: bool = True) -> str:
        if mode in MODE_DEFS:
            return mode
        if not required and not mode:
            return ""
        raise HTTPException(422, f"mode debe ser uno de: {', '.join(MODE_DEFS)}")

    def access(request: Request, authorization: str = Header(default=""), x_api_key: str = Header(default=""),
               x_agent_name: str = Header(default="")) -> dict[str, str]:
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host not in LOCAL_HOSTS:
            raise HTTPException(403, "Solo se aceptan solicitudes a 127.0.0.1/localhost.")
        if not api_enabled(services):
            raise HTTPException(503, "La API local está desactivada (Configuración → API local y MCP).")
        token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else x_api_key.strip()
        tokens = ensure_tokens(services)
        scope = ""
        if token and hmac.compare_digest(token, tokens["write"]):
            scope = "write"
        elif token and hmac.compare_digest(token, tokens["read"]):
            scope = "read"
        if not scope:
            raise HTTPException(401, "Token inválido o ausente (Authorization: Bearer <token>).")
        if request.method not in ("GET", "HEAD") and scope != "write":
            raise HTTPException(403, "El token de solo lectura no puede modificar datos.")
        name = _NAME.sub("", x_agent_name)[:40] or "agent"
        return {"scope": scope, "actor": f"api:{name}"}

    def writer(ctx: dict[str, str] = Depends(access)) -> dict[str, str]:
        if ctx["scope"] != "write":
            raise HTTPException(403, "Requiere el token de escritura.")
        return ctx

    @api.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @api.exception_handler(KeyError)
    async def _key_error(_: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"detail": f"No encontrado: {exc.args[0] if exc.args else ''}"})

    # ------------------------------------------------------------------ status / context
    @api.get("/status", tags=["general"])
    def status(ctx=Depends(access)) -> dict[str, Any]:
        counts = services.db.table_counts()
        return {"app": "job-search-harness", "version": __version__, "scope": ctx["scope"],
                "modes": {m: d.name for m, d in MODE_DEFS.items()}, "jobs": counts.get("jobs", 0),
                "companies": counts.get("companies", 0), "contacts": counts.get("contacts", 0),
                "journal_mode": services.db.journal_mode}

    @api.get("/candidate", tags=["contexto"], summary="Perfil APROBADO del candidato del modo (sin datos personales)")
    def candidate(mode: str, _=Depends(access)) -> dict[str, Any]:
        return services.reviews.candidate_context(_mode(mode))

    @api.get("/rules", tags=["contexto"], summary="Reglas activas del modo")
    def rules(mode: str, _=Depends(access)) -> dict[str, Any]:
        version, items = services.reviews.rules_context(_mode(mode))
        return {"ruleset_version": version, "rules": items}

    # ------------------------------------------------------------------ jobs
    @api.get("/jobs", tags=["vacantes"], summary="Buscar vacantes (filas planas del modo)")
    def jobs(mode: str, view: str = Query("", pattern="^(|all|activos|blocked|discarded)$"), q: str = "",
             saved_view: str = Query("", description="id o nombre de una vista guardada (GET /views)"),
             tag: str = "", updated_since: str = "", limit: int = Query(50, ge=1, le=500),
             offset: int = Query(0, ge=0), _=Depends(access)) -> dict[str, Any]:
        rows = services.views.rows(_mode(mode), saved_view, q, view)
        if tag:
            rows = [r for r in rows if tag.casefold() in {t.strip().casefold() for t in r["tags"].split(",")}]
        if updated_since:
            rows = [r for r in rows if (r.get("last_checked") or "") >= updated_since]
        keep = ("job_id", "title", "company_name", "location_text", "job_country", "work_mode",
                "remote_eligible_countries", "salary_min", "salary_max", "currency", "salary_period",
                "salary_annual_min", "salary_annual_max", "salary_base", "source", "source_job_id",
                "job_url", "apply_url", "posted_date", "eligibility_status", "technical_score", "skills_match",
                "agent_score", "ai_fit_score", "ai_recommendation", "user_decision", "discard_reason",
                "application_status", "ad_status", "liveness", "tags", "row_version", "last_checked")
        return {"total": len(rows), "offset": offset,
                "items": [{k: r.get(k) for k in keep} for r in rows[offset:offset + limit]]}

    @api.get("/jobs/{job_id}", tags=["vacantes"], summary="Documento completo: hechos, JD plana, palabras clave, "
                                                           "evaluación y coincidencia con el CV")
    def job(job_id: str, mode: str = "", _=Depends(access)) -> dict[str, Any]:
        return services.jd.document(job_id, _mode(mode, required=False) or None)

    @api.get("/jobs/{job_id}/jd", tags=["vacantes"], summary="JD en formato markdown | text | json")
    def job_jd(job_id: str, mode: str = "", format: str = Query("markdown", pattern="^(markdown|text|json)$"),
               _=Depends(access)):
        m = _mode(mode, required=False) or None
        if format == "markdown":
            return PlainTextResponse(services.jd.markdown(job_id, m), media_type="text/markdown; charset=utf-8")
        doc = services.jd.document(job_id, m)
        if format == "text":
            return PlainTextResponse(doc["jd"]["flat_text"])
        return doc["jd"]

    @api.post("/jobs", tags=["vacantes"], summary="Crear/actualizar una vacante (mismo JSON que job_inbox)")
    def upsert_job(payload: dict[str, Any] = Body(...), ctx=Depends(writer)) -> dict[str, Any]:
        if str(payload.get("type") or "job") != "job":
            raise HTTPException(422, "Use /companies, /contacts, /reviews o /tailoring para otros tipos.")
        job_id, result, warnings = services.sync.ingest_payload(payload, source=ctx["actor"], actor=ctx["actor"])
        return {"job_id": job_id, "result": result, "warnings": warnings}

    @api.post("/jobs/{job_id}/ad-status", tags=["vacantes"], summary="Marcar el anuncio active | expired | unknown")
    def ad_status(job_id: str, status: str = Body(..., embed=True), ctx=Depends(writer)) -> dict[str, Any]:
        return {"changed": services.jobs.set_ad_status(job_id, status, actor=ctx["actor"])}

    @api.get("/views", tags=["vacantes"], summary="Vistas guardadas del modo (filtros con nombre)")
    def views(mode: str, _=Depends(access)) -> list[dict[str, Any]]:
        return services.views.list(_mode(mode))

    @api.get("/tags", tags=["vacantes"], summary="Etiquetas del usuario y cuántas vacantes tiene cada una")
    def tags(_=Depends(access)) -> list[dict[str, Any]]:
        return [{"tag": t, "jobs": n} for t, n in services.jobs.all_tags()]

    @api.get("/insights/skill-gaps", tags=["contexto"],
             summary="Skills que piden las vacantes y que el perfil aprobado no cubre (ranking)")
    def skill_gaps(mode: str, scope: str = Query("activos", pattern="^(activos|saved|all)$"),
                   limit: int = Query(30, ge=1, le=200), _=Depends(access)) -> dict[str, Any]:
        return services.insights.skill_gaps(_mode(mode), scope, limit)

    @api.get("/fx-rates", tags=["contexto"], summary="Moneda base y tipos de cambio usados para normalizar salarios")
    def fx_rates(_=Depends(access)) -> dict[str, Any]:
        return {"base": services.salary.base(), "hours_per_year": services.salary.hours_per_year(),
                "days_per_year": services.salary.days_per_year(), "rates": services.salary.rate_rows()}

    @api.get("/decisions", tags=["vacantes"], summary="Decisiones del usuario (p. ej. descartadas con motivo) para "
                                                        "no volver a proponerlas")
    def decisions(mode: str, decision: str = "discarded", _=Depends(access)) -> list[dict[str, Any]]:
        return [{k: r[k] for k in ("job_id", "title", "company_name", "source", "source_job_id", "job_url",
                                   "user_decision", "discard_reason", "application_status")}
                for r in services.jobs.rows(_mode(mode), "all") if not decision or r["user_decision"] == decision]

    # ------------------------------------------------------------------ companies / contacts
    @api.get("/companies", tags=["empresas y contactos"])
    def companies(q: str = "", watch_only: bool = False, _=Depends(access)) -> list[dict[str, Any]]:
        return services.crm.list_companies(q, watch_only)

    @api.get("/companies/{company_id}", tags=["empresas y contactos"])
    def company(company_id: str, _=Depends(access)) -> dict[str, Any]:
        return services.crm.get_company(company_id)

    @api.post("/companies", tags=["empresas y contactos"], summary="Crear o COMPLETAR una empresa (no sobrescribe "
                                                                    "lo que escribió el usuario)")
    def upsert_company(payload: dict[str, Any] = Body(...), ctx=Depends(writer)) -> dict[str, Any]:
        cid, result = services.crm.upsert_company(payload, actor=ctx["actor"], fill_only=True)
        return {"company_id": cid, "result": result}

    @api.get("/contacts", tags=["empresas y contactos"])
    def contacts(q: str = "", company_id: str = "", established: bool | None = None,
                 _=Depends(access)) -> list[dict[str, Any]]:
        return services.crm.list_contacts(q, company_id, established=established)

    @api.get("/contacts/{contact_id}", tags=["empresas y contactos"])
    def contact(contact_id: str, _=Depends(access)) -> dict[str, Any]:
        return services.crm.get_contact(contact_id)

    @api.post("/contacts", tags=["empresas y contactos"], summary="Crear o COMPLETAR un contacto")
    def upsert_contact(payload: dict[str, Any] = Body(...), ctx=Depends(writer)) -> dict[str, Any]:
        cid, result = services.crm.upsert_contact(payload, actor=ctx["actor"], fill_only=True)
        return {"contact_id": cid, "result": result}

    @api.post("/contacts/{contact_id}/events", tags=["empresas y contactos"], summary="Registrar una interacción")
    def contact_event(contact_id: str, payload: dict[str, Any] = Body(...), ctx=Depends(writer)) -> dict[str, Any]:
        eid = services.crm.add_event(contact_id, str(payload.get("kind") or "note"), str(payload.get("note") or ""),
                                     payload.get("happened_on"), str(payload.get("job_id") or ""),
                                     str(payload.get("mode_id") or ""), actor=ctx["actor"])
        return {"event_id": eid}

    # ------------------------------------------------------------------ AI review protocol
    @api.post("/packages/review", tags=["análisis IA"], summary="Paquete de revisión (JSON con instrucciones y "
                                                                 "response_schema)")
    def review_package(mode: str = Body(...), job_ids: list[str] = Body(...),
                       ctx=Depends(writer)) -> dict[str, Any]:
        return services.reviews.build_package(_mode(mode), job_ids, include_personal=False, actor=ctx["actor"])

    @api.post("/reviews", tags=["análisis IA"], summary="Enviar revisiones (protocolo jsh-review/1); quedan como "
                                                         "'recibidas' hasta que el usuario las acepte")
    def submit_reviews(payload: dict[str, Any] = Body(...), ctx=Depends(writer)) -> dict[str, Any]:
        return services.reviews.import_result(payload, source="api", actor=ctx["actor"])

    @api.get("/reviews", tags=["análisis IA"])
    def reviews(mode: str = "", status: str = "", job_id: str = "", _=Depends(access)) -> list[dict[str, Any]]:
        return services.reviews.list(_mode(mode, required=False) or None, status or None, job_id or None)

    # ------------------------------------------------------------------ tailoring
    @api.post("/packages/tailoring", tags=["CV adaptado"], summary="Paquete de adaptación de CV para una vacante")
    def tailoring_package(job_id: str = Body(...), mode: str = Body(...), ctx=Depends(writer)) -> dict[str, Any]:
        return services.tailoring.build_package(job_id, _mode(mode), include_personal=False, actor=ctx["actor"])

    @api.post("/tailoring", tags=["CV adaptado"], summary="Enviar una propuesta (protocolo jsh-tailoring/1); el "
                                                           "usuario la revisa y aprueba cambio por cambio")
    def submit_tailoring(payload: dict[str, Any] = Body(...), ctx=Depends(writer)) -> dict[str, Any]:
        return services.tailoring.import_proposal(payload, source="api", actor=ctx["actor"])

    @api.get("/tailoring", tags=["CV adaptado"])
    def tailoring_runs(job_id: str = "", mode: str = "", _=Depends(access)) -> list[dict[str, Any]]:
        return services.tailoring.runs(job_id or None, _mode(mode, required=False) or None)

    # ------------------------------------------------------------------ résumé as a source for agents
    @api.get("/resume", tags=["CV adaptado"], summary="CV maestro con ids, skills (aprobados/propuestos/rechazados), "
                                                      "versiones adaptadas y textos de los CV importados (sin datos "
                                                      "de contacto)")
    def resume(mode: str, include_source_text: bool = True, _=Depends(access)) -> dict[str, Any]:
        return services.tailoring.resume_for_agents(_mode(mode), include_source_text)

    @api.get("/resume/versions/{version_id}", tags=["CV adaptado"], summary="Un CV adaptado aprobado (estructura y "
                                                                             "texto)")
    def resume_version(version_id: str, _=Depends(access)) -> dict[str, Any]:
        return services.tailoring.tailored_for_agents(version_id)

    @api.post("/skills/propose", tags=["CV adaptado"], summary="Sugerir skills: quedan PROPUESTOS hasta que el usuario "
                                                                "los apruebe")
    def propose_skills(mode: str = Body(...), skills: list[dict[str, Any]] = Body(...),
                       ctx=Depends(writer)) -> dict[str, Any]:
        return services.tailoring.propose_skills(_mode(mode), skills, actor=ctx["actor"])

    @api.delete("/jobs/{job_id}", tags=["vacantes"], include_in_schema=False)
    def no_delete(job_id: str, _=Depends(writer)):
        raise HTTPException(403, "Los agentes no pueden eliminar vacantes; solo el usuario desde la app.")

    return api
