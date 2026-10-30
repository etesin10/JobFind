"""Field registry for the rule engine (spec §5).

Only fields registered here can be referenced by rules. Each field has a type that
constrains the allowed operators. New ordinary filters are rules over these fields;
new *field types* or operators require code (an isolated extension), never ``eval``.
"""
from __future__ import annotations

from dataclasses import dataclass

TEXT, LONGTEXT, LIST, NUMBER, DATE = "text", "longtext", "list", "number", "date"


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    type: str
    modes: tuple[str, ...] = ()  # empty = all modes


FIELDS: dict[str, FieldSpec] = {f.key: f for f in [
    FieldSpec("title", "Título del puesto", TEXT),
    FieldSpec("company_name", "Empresa", TEXT),
    FieldSpec("job_country", "País del puesto (ISO)", TEXT),
    FieldSpec("region", "Provincia / estado", TEXT),
    FieldSpec("location_text", "Ubicación (texto)", TEXT),
    FieldSpec("remote_eligible_countries", "Países desde donde se permite remoto", LIST),
    FieldSpec("work_mode", "Modalidad (remote/hybrid/onsite)", TEXT),
    FieldSpec("seniority", "Seniority", TEXT),
    FieldSpec("employment_type", "Tipo de empleo", TEXT),
    FieldSpec("salary_min", "Salario mínimo", NUMBER),
    FieldSpec("salary_max", "Salario máximo", NUMBER),
    FieldSpec("currency", "Moneda", TEXT),
    FieldSpec("salary_period", "Periodo salarial", TEXT),
    FieldSpec("salary_annual_min", "Salario anual mínimo normalizado (moneda base)", NUMBER),
    FieldSpec("salary_annual_max", "Salario anual máximo normalizado (moneda base)", NUMBER),
    FieldSpec("tags", "Etiquetas (del usuario)", LIST),
    FieldSpec("posted_date", "Fecha de publicación", DATE),
    FieldSpec("source", "Fuente / proveedor", TEXT),
    FieldSpec("sponsorship_statement", "Declaración de patrocinio/visa", LONGTEXT),
    FieldSpec("language", "Idioma", TEXT),
    FieldSpec("description", "Descripción del puesto (JD)", LONGTEXT),
    FieldSpec("ad_status", "Estado del anuncio", TEXT),
    FieldSpec("user_decision", "Decisión del usuario", TEXT),
    FieldSpec("discard_reason", "Motivo de descarte previo", TEXT),
    # Phase 2: computed from the JD vs the APPROVED skills of the mode's active candidate profile
    FieldSpec("skills_match_pct", "Coincidencia con skills del CV (%)", NUMBER),
    FieldSpec("matched_skills", "Skills del CV presentes en la JD", LIST),
    FieldSpec("missing_skills", "Skills de la JD que no tiene aprobados", LIST),
    # written by an EXTERNAL agent through job_inbox (payload "agent": {"score", "recommendation", ...})
    FieldSpec("agent_score", "Puntaje del agente externo (0-100)", NUMBER),
    FieldSpec("agent_recommendation", "Recomendación del agente externo", TEXT),
    # from the ACCEPTED AI review of the job in this mode (Análisis IA); unknown until one is accepted
    FieldSpec("ai_fit_score", "IA: ajuste técnico (0-100, revisión aceptada)", NUMBER),
    FieldSpec("ai_priority_score", "IA: prioridad (0-100, revisión aceptada)", NUMBER),
    FieldSpec("ai_recommendation", "IA: recomendación (high_priority/review/low_priority/discard_suggested)", TEXT),
    FieldSpec("ai_location_fit", "IA: ubicación compatible (yes/no/unknown)", TEXT),
    FieldSpec("ai_work_authorization_fit", "IA: autorización de trabajo (possible/blocked/unknown/…)", TEXT),
    # mode-specific facts (JobModeEvaluation.mode_data)
    FieldSpec("employer_visa_support", "Empleador apoya permiso", TEXT, ("CANADA_VISA",)),
    FieldSpec("cusma_evidence", "Evidencia CUSMA", LONGTEXT, ("CANADA_VISA",)),
    FieldSpec("lmia_evidence", "Evidencia LMIA", LONGTEXT, ("CANADA_VISA",)),
    FieldSpec("immigration_discussion", "Conversación de inmigración", TEXT, ("CANADA_VISA",)),
    FieldSpec("contract_arrangement", "Tipo de contratación", TEXT, ("MEXICO_REMOTE",)),
]}

OPERATORS_BY_TYPE: dict[str, tuple[str, ...]] = {
    TEXT: ("eq", "ne", "in", "not_in", "contains", "not_contains", "any_keyword", "all_keywords",
           "exists", "missing", "regex"),
    LONGTEXT: ("contains", "not_contains", "any_keyword", "all_keywords", "exists", "missing", "regex"),
    LIST: ("eq", "ne", "in", "not_in", "contains", "not_contains", "exists", "missing"),
    NUMBER: ("eq", "ne", "gte", "lte", "between", "exists", "missing"),
    DATE: ("gte", "lte", "between", "date_within_days", "exists", "missing"),
}

OPERATOR_LABELS = {
    "eq": "es igual a", "ne": "es distinto de", "in": "está en (lista)", "not_in": "no está en (lista)",
    "contains": "contiene", "not_contains": "no contiene", "any_keyword": "contiene alguna palabra",
    "all_keywords": "contiene todas las palabras", "gte": "≥", "lte": "≤", "between": "entre",
    "exists": "tiene dato", "missing": "no tiene dato (desconocido)", "regex": "regex (avanzado)",
    "date_within_days": "en los últimos N días",
}

# Value kind expected by each operator.
VALUE_KIND = {
    "eq": "single", "ne": "single", "contains": "single", "not_contains": "single", "regex": "single",
    "in": "list", "not_in": "list", "any_keyword": "list", "all_keywords": "list",
    "gte": "scalar", "lte": "scalar", "between": "range", "date_within_days": "days",
    "exists": "none", "missing": "none",
}

SCOPES = ("search", "prefilter", "rank", "review", "notify")
EVALUATED_SCOPES = ("prefilter", "rank", "review")
ACTIONS = ("include", "exclude", "flag", "weight", "request_review")
SEVERITIES = ("hard", "soft", "informational")
UNKNOWN_HANDLING = ("keep", "flag", "exclude")
GROUP_OPERATORS = ("AND", "OR")

ACTION_LABELS = {"include": "Requerir (incluir si cumple)", "exclude": "Excluir si cumple",
                 "flag": "Marcar alerta", "weight": "Sumar puntaje", "request_review": "Pedir revisión"}
SEVERITY_LABELS = {"hard": "Duro (bloquea)", "soft": "Suave (alerta)", "informational": "Informativo"}
UNKNOWN_LABELS = {"keep": "Mantener", "flag": "Marcar para verificar", "exclude": "Excluir"}
SCOPE_LABELS = {"search": "Búsqueda (fase 3)", "prefilter": "Prefiltro", "rank": "Ranking",
                "review": "Revisión", "notify": "Notificación (futuro)"}


def fields_for_mode(mode_id: str) -> list[FieldSpec]:
    return [f for f in FIELDS.values() if not f.modes or mode_id in f.modes]
