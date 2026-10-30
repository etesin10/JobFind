"""Mode definitions, triage vocabularies and default (seed) rules.

Everything here is *seed data*: it is written into SQLite on first run and is then
edited by the user through the UI/Excel. Code never re-applies seeds over user edits.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CANADA = "CANADA_VISA"
MEXICO = "MEXICO_REMOTE"
ALL = "ALL"


@dataclass(frozen=True)
class ModeField:
    key: str
    label: str
    choices: tuple[str, ...] = ()  # empty = free text


@dataclass(frozen=True)
class ModeDef:
    id: str
    name: str
    description: str
    # columns shown in the job table (keys of the job row dict)
    columns: tuple[str, ...]
    # mode-specific facts stored in JobModeEvaluation.mode_data
    mode_fields: tuple[ModeField, ...] = field(default_factory=tuple)
    workbook_name: str = ""


UNKNOWN = "unknown"

MODE_DEFS: dict[str, ModeDef] = {
    CANADA: ModeDef(
        id=CANADA,
        name="Canadá — Visa / Permiso de trabajo",
        description="Empleo en Canadá considerando permiso específico de empleador, CUSMA o LMIA. "
        "Los hechos legales no verificados se tratan como DESCONOCIDOS.",
        columns=("title", "company_name", "job_country", "region", "work_mode", "sponsorship_statement",
                 "employer_visa_support", "eligibility_status", "skills_match", "technical_score", "agent_score",
                 "ai_fit_score", "user_decision", "application_status"),
        mode_fields=(
            ModeField("employer_visa_support", "Empleador apoya permiso", ("unknown", "yes", "no", "maybe")),
            ModeField("cusma_evidence", "Evidencia CUSMA (texto/URL)"),
            ModeField("lmia_evidence", "Evidencia LMIA (texto/URL)"),
            ModeField("immigration_discussion", "Conversación de inmigración",
                      ("not_started", "asked", "employer_open", "employer_declined", "in_progress")),
            ModeField("noc_review", "Revisión NOC/ocupación"),
        ),
        workbook_name="Job_Search_Canada.xlsx",
    ),
    MEXICO: ModeDef(
        id=MEXICO,
        name="México — Remoto desde México",
        description="Ingresos rápidos con trabajo remoto realizado desde México.",
        columns=("title", "company_name", "work_mode", "remote_eligible_countries", "salary_min", "salary_max",
                 "currency", "contract_arrangement", "eligibility_status", "skills_match", "technical_score",
                 "agent_score", "ai_fit_score", "user_decision", "application_status"),
        mode_fields=(
            ModeField("contract_arrangement", "Tipo de contratación",
                      ("unknown", "local_payroll", "international_contractor", "eor")),
            ModeField("fx_note", "Tipo de cambio (fuente y fecha)"),
            ModeField("time_to_hire", "Tiempo estimado de contratación"),
        ),
        workbook_name="Job_Search_Mexico.xlsx",
    ),
}

MODE_CHOICES = {ALL: "Todas (ALL)", **{m.id: m.name for m in MODE_DEFS.values()}}

USER_DECISIONS = ("new", "saved", "maybe", "discarded")
USER_DECISION_LABELS = {"new": "Nuevo", "saved": "Guardado", "maybe": "Tal vez / Revisar", "discarded": "Descartado"}

DISCARD_REASONS: dict[str, str] = {
    "not_remote_from_country": "No remoto desde país",
    "requires_authorization": "Requiere autorización que no tengo",
    "insufficient_skills": "Skills insuficientes",
    "salary": "Salario",
    "seniority": "Seniority",
    "untrusted_company": "Empresa no confiable",
    "duplicate": "Duplicado",
    "expired": "Puesto expirado",
    "not_interested": "No me interesa",
    "other": "Otro",
}

# Application statuses with stable semantic type (spec §12).
APPLICATION_STATUSES: dict[str, tuple[str, str]] = {
    # key: (label, semantic_type)
    "discovered": ("Descubierto", "open"),
    "saved": ("Guardado", "open"),
    "preparing": ("Preparando", "open"),
    "applied": ("Aplicado", "in_process"),
    "recruiter_screening": ("Filtro reclutador", "in_process"),
    "interviewing": ("Entrevistas", "in_process"),
    "offer": ("Oferta", "in_process"),
    "rejected": ("Rechazado", "terminal"),
    "withdrawn": ("Retirado", "terminal"),
    "discarded": ("Descartado", "terminal"),
}
ACTIVE_PIPELINE_STATUSES = {k for k, (_, t) in APPLICATION_STATUSES.items() if t == "in_process"}

ELIGIBILITY_LABELS = {
    "eligible": "Elegible",
    "needs_verification": "Verificar",
    "flagged": "Con alertas",
    "blocked": "Bloqueado",
    "unevaluated": "Sin evaluar",
}

SKILL_KEYWORDS = ["sql", "t-sql", "sql server", "azure", "data factory", "synapse", "snowflake", "python",
                  "airflow", "etl", "elt", "data warehouse", "data engineering", "ssis", "fastapi"]

NO_SPONSORSHIP_PHRASES = ["no sponsorship", "unable to sponsor", "not able to sponsor", "cannot sponsor",
                          "will not sponsor", "without sponsorship", "sin patrocinio"]

# Phase 1 seed rule replaced in Phase 2 by matching against APPROVED candidate skills.
LEGACY_KEYWORD_RULE_NAME = "Skills principales en la JD"


def skills_match_rules(mode_id: str) -> list[dict[str, Any]]:
    """Rank rules over the JD-vs-approved-skills percentage. Unknown (no approved skills) = no effect."""
    return [
        dict(mode_id=mode_id, name="Skills del CV: coincidencia alta (≥ 60%)", scope="rank",
             field="skills_match_pct", operator="gte", value=60, value_type="scalar", action="weight", weight=40,
             severity="soft", unknown_handling="keep", priority=50,
             explanation="La JD pide skills que usted aprobó en 'Skills y CV' (60% o más de coincidencia)."),
        dict(mode_id=mode_id, name="Skills del CV: coincidencia media (30–59%)", scope="rank",
             field="skills_match_pct", operator="between", value=["30", "59.99"], value_type="range",
             action="weight", weight=20, severity="soft", unknown_handling="keep", priority=51,
             explanation="Coincidencia parcial con sus skills aprobados (30% a 59%)."),
    ]


def default_rules() -> list[dict[str, Any]]:
    """Seed rules per mode. Users edit/disable them visually afterwards."""
    return [
        # ---- CANADA_VISA
        dict(mode_id=CANADA, name="Puesto en Canadá", scope="prefilter", field="job_country", operator="in",
             value=["CA"], value_type="list", logic_group="ubicacion_canada", group_operator="OR",
             action="include", severity="hard", unknown_handling="flag", priority=10,
             explanation="El empleo debe estar en Canadá (o remoto autorizado desde Canadá)."),
        dict(mode_id=CANADA, name="Remoto autorizado desde Canadá", scope="prefilter",
             field="remote_eligible_countries", operator="in", value=["CA", "WORLDWIDE"], value_type="list",
             logic_group="ubicacion_canada", group_operator="OR", action="include", severity="hard",
             unknown_handling="flag", priority=11,
             explanation="Alternativa: el anuncio permite trabajar remoto desde Canadá."),
        dict(mode_id=CANADA, name="Declaración de patrocinio ausente", scope="review",
             field="sponsorship_statement", operator="missing", value=None, value_type="none",
             action="request_review", severity="informational", unknown_handling="keep", priority=20,
             explanation="Sin declaración de visa/patrocinio: DESCONOCIDO, verificar con el empleador."),
        dict(mode_id=CANADA, name="Anuncio indica que no patrocina", scope="prefilter", field="description",
             operator="any_keyword", value=NO_SPONSORSHIP_PHRASES, value_type="list",
             action="exclude", severity="soft", unknown_handling="keep", priority=30,
             explanation="El texto indica que no hay patrocinio; revisar antes de descartar."),
        *skills_match_rules(CANADA),
        # ---- MEXICO_REMOTE
        dict(mode_id=MEXICO, name="Trabajo remoto", scope="prefilter", field="work_mode", operator="eq",
             value="remote", value_type="text", action="include", severity="hard", unknown_handling="flag",
             priority=10, explanation="Debe ser 100% remoto."),
        dict(mode_id=MEXICO, name="Remoto permitido desde México", scope="prefilter",
             field="remote_eligible_countries", operator="in", value=["MX", "LATAM", "AMERICAS", "WORLDWIDE"],
             value_type="list", action="include", severity="hard", unknown_handling="flag", priority=11,
             explanation="El anuncio debe permitir trabajar desde México. Un remoto solo-EE.UU. NO es elegible."),
        *skills_match_rules(MEXICO),
        dict(mode_id=MEXICO, name="Salario publicado", scope="rank", field="salary_min", operator="exists",
             value=None, value_type="none", action="weight", weight=10, severity="soft",
             unknown_handling="keep", priority=60, explanation="Preferir anuncios con salario claro."),
    ]
