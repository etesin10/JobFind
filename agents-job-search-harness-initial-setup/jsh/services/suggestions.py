"""Rule suggestions derived from repeated discard reasons (spec §4).

Suggestions are DRAFTS only: the UI opens them in the rule editor where the user can
simulate, edit and explicitly save. Nothing here writes rules or suppresses jobs.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..modes import ACTIVE_PIPELINE_STATUSES, CANADA, DISCARD_REASONS, MEXICO

DEFAULT_THRESHOLD = 3
TARGET_REMOTE_COUNTRIES = {MEXICO: ["MX", "LATAM", "AMERICAS", "WORLDWIDE"], CANADA: ["CA", "WORLDWIDE"]}
SENIORITY_WORDS = ["intern", "internship", "trainee", "junior", "jr", "entry level", "graduate", "principal", "staff",
                   "director", "head", "vp", "manager", "lead", "chief"]
AUTHORIZATION_PHRASES = ["security clearance", "us citizen", "u.s. citizen", "citizenship required", "green card",
                         "no sponsorship", "unable to sponsor", "will not sponsor", "cannot sponsor",
                         "must be a citizen", "active clearance"]
STOPWORDS = {"the", "and", "for", "with", "de", "del", "la", "el", "los", "las", "en", "y", "con", "para", "remote",
             "remoto", "senior", "sr", "engineer", "data", "developer", "ingeniero", "job", "role", "position"}


@dataclass
class RuleSuggestion:
    key: str
    reason_code: str
    count: int
    title: str
    rule: dict[str, Any]
    examples: list[str] = field(default_factory=list)

    @property
    def reason_label(self) -> str:
        return DISCARD_REASONS.get(self.reason_code, self.reason_code)


def _key(reason: str, rule: dict[str, Any]) -> str:
    val = rule.get("value")
    basis = f"{reason}|{rule['field']}|{rule['operator']}|{sorted(val) if isinstance(val, list) else val}"
    return hashlib.sha1(basis.encode()).hexdigest()[:12]


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-záéíóúñü0-9+#.]{3,}", (text or "").lower()) if t not in STOPWORDS}


def _covered(rules: list[Any], field_key: str, operator: str, values: list[str]) -> bool:
    """True if an enabled rule already covers all suggested values for the same field/operator."""
    wanted = {str(v).lower() for v in values}
    for r in rules:
        if not r.enabled or r.field != field_key or r.operator != operator:
            continue
        existing = r.value if isinstance(r.value, list) else [r.value]
        if wanted <= {str(v).lower() for v in existing}:
            return True
    return False


def _phrase_in(text: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", (text or "").lower()) is not None


class SuggestionService:
    def __init__(self, app):
        self.app = app

    def threshold(self) -> int:
        return int(self.app.db.get_setting("suggestion_threshold", DEFAULT_THRESHOLD) or DEFAULT_THRESHOLD)

    def dismissed(self, mode_id: str) -> set[str]:
        return set(self.app.db.get_setting(f"dismissed_suggestions:{mode_id}", []) or [])

    def dismiss(self, mode_id: str, key: str) -> None:
        keys = sorted(self.dismissed(mode_id) | {key})
        self.app.db.set_setting(f"dismissed_suggestions:{mode_id}", keys)

    def suggestions(self, mode_id: str) -> list[RuleSuggestion]:
        rows = self.app.jobs.rows(mode_id, "all")
        rules = self.app.rules.list(mode_id)
        threshold = self.threshold()
        discarded = [r for r in rows if r["user_decision"] == "discarded"]
        kept = [r for r in rows if r["user_decision"] in ("saved", "maybe")
                or r["application_status"] in ACTIVE_PIPELINE_STATUSES]
        by_reason: dict[str, list[dict[str, Any]]] = {}
        for r in discarded:
            by_reason.setdefault(r["discard_reason"] or "other", []).append(r)
        descriptions = self._descriptions([r["job_id"] for r in discarded + kept])

        out: list[RuleSuggestion] = []

        def add(reason: str, title: str, rule: dict[str, Any], examples: list[dict[str, Any]]) -> None:
            val = rule["value"]
            if _covered(rules, rule["field"], rule["operator"], val if isinstance(val, list) else [val]):
                return
            rule = {"scope": "prefilter", "weight": 0, "logic_group": "", "group_operator": "AND", "priority": 40,
                    **rule}
            out.append(RuleSuggestion(_key(reason, rule), reason, len(by_reason.get(reason, [])), title, rule,
                                      [f"{e['title']} — {e['company_name']}" for e in examples[:5]]))

        # 1) Same company discarded repeatedly as untrusted / not interesting
        for reason in ("untrusted_company", "not_interested"):
            group = by_reason.get(reason, [])
            kept_companies = {r["company_name"].lower() for r in kept}
            counts = Counter(r["company_name"] for r in group if r["company_name"])
            companies = [c for c, n in counts.items() if n >= threshold and c.lower() not in kept_companies]
            if companies:
                add(reason, f"Excluir empresas descartadas {threshold}+ veces: {', '.join(companies)}",
                    {"name": f"Excluir empresas ({DISCARD_REASONS[reason]})", "field": "company_name",
                     "operator": "in", "value": companies, "action": "exclude", "severity": "hard",
                     "unknown_handling": "keep",
                     "explanation": f"Sugerida por descartes repetidos: {DISCARD_REASONS[reason]}."},
                    [r for r in group if r["company_name"] in companies])

        # 2) Remote not allowed from the target country
        group = by_reason.get("not_remote_from_country", [])
        targets = TARGET_REMOTE_COUNTRIES.get(mode_id, [])
        kept_conflict = any(r["remote_eligible_countries"] and not
                            {c.strip().upper() for c in r["remote_eligible_countries"].split(",")} & set(targets)
                            for r in kept)
        if len(group) >= threshold and targets and not kept_conflict:
            add("not_remote_from_country", "Requerir que el anuncio permita trabajar remoto desde su país",
                {"name": "Remoto permitido desde mi país", "field": "remote_eligible_countries", "operator": "in",
                 "value": TARGET_REMOTE_COUNTRIES[mode_id], "action": "include", "severity": "hard",
                 "unknown_handling": "flag",
                 "explanation": "Sugerida por descartes 'No remoto desde país'. Desconocido → Verificar."}, group)

        # 3) Seniority: explicit field values, else seniority words in titles
        group = by_reason.get("seniority", [])
        if len(group) >= threshold:
            kept_sen = {r["seniority"].lower() for r in kept if r["seniority"]}
            values = sorted({r["seniority"] for r in group if r["seniority"] and r["seniority"].lower() not in kept_sen})
            if values:
                add("seniority", f"Excluir seniority: {', '.join(values)}",
                    {"name": "Excluir seniority no deseado", "field": "seniority", "operator": "in", "value": values,
                     "action": "exclude", "severity": "soft", "unknown_handling": "keep",
                     "explanation": "Sugerida por descartes 'Seniority'."}, group)
            words = [w for w in SENIORITY_WORDS
                     if sum(_phrase_in(r["title"], w) for r in group) >= threshold
                     and not any(_phrase_in(r["title"], w) for r in kept)]
            if words:
                add("seniority", f"Excluir títulos con: {', '.join(words)}",
                    {"name": "Excluir nivel en el título", "field": "title", "operator": "any_keyword",
                     "value": words, "action": "exclude", "severity": "soft", "unknown_handling": "keep",
                     "explanation": "Sugerida por descartes 'Seniority' (palabras en el título)."}, group)

        # 4) Salary: only when every salary in this mode uses a single currency
        group = by_reason.get("salary", [])
        maxes = [r["salary_max"] for r in group if r["salary_max"]]
        currencies = {r["currency"] for r in rows if r["salary_max"] or r["salary_min"]}
        kept_low = maxes and any(r["salary_max"] is not None and r["salary_max"] <= max(maxes) for r in kept)
        if len(group) >= threshold and maxes and len(currencies) == 1 and not kept_low:
            cur = next(iter(currencies)) or "moneda desconocida"
            add("salary", f"Alertar si el salario máximo ≤ {max(maxes):,.0f} {cur}",
                {"name": f"Salario bajo (≤ {max(maxes):,.0f} {cur})", "field": "salary_max", "operator": "lte",
                 "value": max(maxes), "action": "exclude", "severity": "soft", "unknown_handling": "keep",
                 "explanation": f"Sugerida por descartes 'Salario'. Válida solo para salarios en {cur}."}, group)

        # 5) Work authorization phrases: frequent in discarded JDs AND absent from every kept JD
        group = by_reason.get("requires_authorization", [])
        if len(group) >= threshold:
            found = [p for p in AUTHORIZATION_PHRASES
                     if sum(_phrase_in(descriptions.get(r["job_id"], ""), p) for r in group) >= threshold
                     and not any(_phrase_in(descriptions.get(r["job_id"], ""), p) for r in kept)]
            if found:
                add("requires_authorization", f"Alertar si la JD menciona: {', '.join(found)}",
                    {"name": "Requisito de autorización en la JD", "field": "description", "operator": "any_keyword",
                     "value": found, "action": "exclude", "severity": "soft", "unknown_handling": "keep",
                     "explanation": "Sugerida por descartes 'Requiere autorización'. Revise antes de descartar."},
                    group)

        # 6) Not interested: title words common in discards and absent from kept jobs
        group = by_reason.get("not_interested", [])
        if len(group) >= threshold:
            kept_tokens = set().union(*(_tokens(r["title"]) for r in kept)) if kept else set()
            counts = Counter(t for r in group for t in _tokens(r["title"]))
            words = [w for w, n in counts.most_common(5) if n >= threshold and w not in kept_tokens]
            if words:
                add("not_interested", f"Excluir títulos con: {', '.join(words)}",
                    {"name": "Excluir títulos no deseados", "field": "title", "operator": "any_keyword",
                     "value": words, "action": "exclude", "severity": "soft", "unknown_handling": "keep",
                     "explanation": "Sugerida por descartes 'No me interesa'."},
                    [r for r in group if _tokens(r["title"]) & set(words)])

        hidden = self.dismissed(mode_id)
        return [s for s in out if s.key not in hidden]

    def _descriptions(self, job_ids: list[str]) -> dict[str, str]:
        from sqlalchemy import select

        from ..models import Job
        if not job_ids:
            return {}
        with self.app.db.session() as s:
            return dict(s.execute(select(Job.id, Job.description).where(Job.id.in_(job_ids))).all())

    def reason_stats(self, mode_id: str) -> dict[str, int]:
        stats: dict[str, int] = {}
        for r in self.app.jobs.rows(mode_id, "discarded"):
            k = r["discard_reason"] or "other"
            stats[k] = stats.get(k, 0) + 1
        return stats
