"""Deterministic, safe rule engine (spec §5, §7 stage 1).

* No ``eval``/exec: every operator is an explicit Python function.
* Three-valued logic: a condition is True, False or None (= UNKNOWN, missing data).
* Hard blockers, unknowns needing verification, soft flags and score weights are kept
  separate so the UI can explain *why* a job passed or failed.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable

from .registry import (
    ACTIONS, DATE, EVALUATED_SCOPES, FIELDS, GROUP_OPERATORS, LIST, NUMBER, OPERATORS_BY_TYPE, SCOPES,
    SEVERITIES, UNKNOWN_HANDLING, VALUE_KIND,
)

MAX_REGEX_LEN = 200
MAX_TEXT_SCAN = 50_000
_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*}][^)]*\)\s*[+*{]")


@dataclass
class RuleSpec:
    """Engine-side view of a rule (decoupled from the ORM)."""
    id: str
    name: str
    field: str
    operator: str
    value: Any = None
    action: str = "flag"
    severity: str = "soft"
    unknown_handling: str = "flag"
    scope: str = "prefilter"
    weight: float = 0.0
    logic_group: str = ""
    group_operator: str = "AND"
    explanation: str = ""
    priority: int = 100
    enabled: bool = True
    version: int = 1

    @classmethod
    def from_obj(cls, obj: Any) -> "RuleSpec":
        get = (lambda k, d=None: obj.get(k, d)) if isinstance(obj, dict) else (lambda k, d=None: getattr(obj, k, d))
        return cls(
            id=str(get("id", "") or ""), name=get("name", "") or "", field=get("field", ""),
            operator=get("operator", ""), value=get("value"), action=get("action", "flag") or "flag",
            severity=get("severity", "soft") or "soft", unknown_handling=get("unknown_handling", "flag") or "flag",
            scope=get("scope", "prefilter") or "prefilter", weight=float(get("weight", 0) or 0),
            logic_group=get("logic_group", "") or "", group_operator=(get("group_operator", "AND") or "AND").upper(),
            explanation=get("explanation", "") or "", priority=int(get("priority", 100) or 100),
            enabled=bool(get("enabled", True)), version=int(get("version", 1) or 1),
        )


@dataclass
class RuleOutcome:
    rule_ids: list[str]
    name: str
    action: str
    severity: str
    result: str  # "true" | "false" | "unknown" | "error"
    effect: str  # blocker | flag | info | unknown_flag | unknown | weight | review | pass
    explanation: str = ""
    evidence: str = ""
    weight: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class EvaluationResult:
    status: str = "eligible"  # eligible | needs_verification | flagged | blocked
    score: float = 0.0
    outcomes: list[RuleOutcome] = field(default_factory=list)

    @property
    def blockers(self) -> list[RuleOutcome]:
        return [o for o in self.outcomes if o.effect == "blocker"]

    @property
    def unknowns(self) -> list[RuleOutcome]:
        return [o for o in self.outcomes if o.effect in ("unknown_flag", "unknown")]

    @property
    def flags(self) -> list[RuleOutcome]:
        return [o for o in self.outcomes if o.effect in ("flag", "review", "info")]


# --------------------------------------------------------------------------- helpers

def is_unknown(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == "" or value.strip().lower() == "unknown"
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in re.split(r"[,;\n]", value) if v.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [str(value)]


def _norm(value: Any) -> str:
    return str(value).strip().lower()


def _kw_pattern(keyword: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])")


def _find_keywords(text: str, keywords: Iterable[str]) -> tuple[list[str], str]:
    low = text[:MAX_TEXT_SCAN].lower()
    found, snippet = [], ""
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        m = _kw_pattern(kw).search(low)
        if m:
            found.append(kw)
            if not snippet:
                start = max(0, m.start() - 60)
                snippet = text[start:m.end() + 60].replace("\n", " ").strip()
    return found, snippet


def _to_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def _to_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def regex_problem(pattern: str) -> str | None:
    if not isinstance(pattern, str) or not pattern:
        return "Regex vacía"
    if len(pattern) > MAX_REGEX_LEN:
        return f"Regex demasiado larga (máx. {MAX_REGEX_LEN})"
    if _NESTED_QUANTIFIER.search(pattern):
        return "Regex con cuantificadores anidados no permitida (riesgo de bloqueo)"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"Regex inválida: {exc}"
    return None


# --------------------------------------------------------------------------- validation / parsing

def parse_value(operator: str, field_key: str, raw: Any) -> Any:
    """Convert a user-entered value (usually text from the UI/Excel) to the stored form."""
    kind = VALUE_KIND.get(operator, "single")
    ftype = FIELDS[field_key].type if field_key in FIELDS else "text"
    if kind == "none":
        return None
    if kind == "list":
        return _as_list(raw)
    if kind == "range":
        parts = _as_list(raw) if not isinstance(raw, (list, tuple)) else list(raw)
        return [str(p).strip() for p in parts[:2]]
    if kind == "days":
        n = _to_number(raw)
        return int(n) if n is not None else raw
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else ""
    if ftype == NUMBER and kind in ("single", "scalar"):
        n = _to_number(raw)
        return n if n is not None else raw
    return "" if raw is None else str(raw).strip()


def validate_rule(rule: Any) -> list[str]:
    r = RuleSpec.from_obj(rule)
    errors: list[str] = []
    if not r.name.strip():
        errors.append("El nombre es obligatorio.")
    spec = FIELDS.get(r.field)
    if spec is None:
        errors.append(f"Campo desconocido: '{r.field}'.")
        return errors
    if r.operator not in OPERATORS_BY_TYPE[spec.type]:
        errors.append(f"Operador '{r.operator}' no válido para el campo '{spec.label}' ({spec.type}).")
    if r.action not in ACTIONS:
        errors.append(f"Acción inválida: {r.action}")
    if r.severity not in SEVERITIES:
        errors.append(f"Severidad inválida: {r.severity}")
    if r.unknown_handling not in UNKNOWN_HANDLING:
        errors.append(f"Manejo de desconocido inválido: {r.unknown_handling}")
    if r.scope not in SCOPES:
        errors.append(f"Alcance inválido: {r.scope}")
    if r.group_operator not in GROUP_OPERATORS:
        errors.append(f"Operador de grupo inválido: {r.group_operator}")
    kind = VALUE_KIND.get(r.operator)
    v = r.value
    if kind == "single" and (v is None or str(v).strip() == ""):
        errors.append("Se requiere un valor.")
    if kind == "list" and not _as_list(v):
        errors.append("Se requiere al menos un valor en la lista (separados por coma).")
    if kind == "scalar":
        if spec.type == DATE and _to_date(v) is None:
            errors.append("Se requiere una fecha AAAA-MM-DD.")
        elif spec.type == NUMBER and _to_number(v) is None:
            errors.append("Se requiere un número.")
    if kind == "range":
        parts = v if isinstance(v, (list, tuple)) else _as_list(v)
        conv = _to_date if spec.type == DATE else _to_number
        if len(parts) != 2 or any(conv(p) is None for p in parts):
            errors.append("'entre' requiere dos valores: mínimo, máximo.")
    if kind == "days" and _to_number(v) is None:
        errors.append("Se requiere un número de días.")
    if r.operator == "regex":
        problem = regex_problem(str(v or ""))
        if problem:
            errors.append(problem)
    if r.action == "weight" and r.weight == 0:
        errors.append("Una regla de puntaje necesita un peso distinto de 0.")
    return errors


# --------------------------------------------------------------------------- condition evaluation

def evaluate_condition(rule: RuleSpec, facts: dict[str, Any], today: date | None = None) -> tuple[bool | None, str]:
    """Return (True/False/None, evidence). None means the fact is unknown."""
    spec = FIELDS.get(rule.field)
    if spec is None:
        raise ValueError(f"Unknown field {rule.field}")
    actual = facts.get(rule.field)
    op, expected = rule.operator, rule.value

    if op == "exists":
        return (not is_unknown(actual)), ""
    if op == "missing":
        return is_unknown(actual), ""
    if is_unknown(actual):
        return None, ""

    if spec.type == LIST:
        items = {_norm(x) for x in _as_list(actual)}
        exp = {_norm(x) for x in _as_list(expected)}
        if op == "eq":
            return (exp <= items), ""
        if op == "ne":
            return not (exp <= items), ""
        if op in ("in", "contains"):
            hit = items & exp
            return bool(hit), ", ".join(sorted(hit))
        if op in ("not_in", "not_contains"):
            return not (items & exp), ""

    if spec.type == NUMBER:
        num = _to_number(actual)
        if num is None:
            return None, ""
        if op == "eq":
            return num == _to_number(expected), ""
        if op == "ne":
            return num != _to_number(expected), ""
        if op == "gte":
            return num >= (_to_number(expected) or 0), ""
        if op == "lte":
            return num <= (_to_number(expected) or 0), ""
        if op == "between":
            lo, hi = (_to_number(x) for x in list(expected)[:2])
            return (lo or 0) <= num <= (hi or 0), ""

    if spec.type == DATE:
        d = _to_date(actual)
        if d is None:
            return None, ""
        if op == "gte":
            return d >= _to_date(expected), ""
        if op == "lte":
            return d <= _to_date(expected), ""
        if op == "between":
            lo, hi = (_to_date(x) for x in list(expected)[:2])
            return lo <= d <= hi, ""
        if op == "date_within_days":
            ref = today or date.today()
            return d >= ref - timedelta(days=int(_to_number(expected) or 0)), ""

    text = str(actual)
    low = text[:MAX_TEXT_SCAN].lower()
    if op == "eq":
        return low.strip() == _norm(expected), ""
    if op == "ne":
        return low.strip() != _norm(expected), ""
    if op == "in":
        return low.strip() in {_norm(x) for x in _as_list(expected)}, ""
    if op == "not_in":
        return low.strip() not in {_norm(x) for x in _as_list(expected)}, ""
    if op == "contains":
        found, snip = _find_keywords(text, [str(expected)])
        return bool(found) or _norm(expected) in low, snip
    if op == "not_contains":
        return _norm(expected) not in low, ""
    if op == "any_keyword":
        found, snip = _find_keywords(text, _as_list(expected))
        return bool(found), (f"[{', '.join(found)}] …{snip}…" if found else "")
    if op == "all_keywords":
        kws = _as_list(expected)
        found, snip = _find_keywords(text, kws)
        return len(found) == len(kws), (f"[{', '.join(found)}] …{snip}…" if found else "")
    if op == "regex":
        problem = regex_problem(str(expected))
        if problem:
            raise ValueError(problem)
        m = re.search(str(expected), text[:MAX_TEXT_SCAN], re.IGNORECASE)
        return bool(m), (m.group(0)[:120] if m else "")
    raise ValueError(f"Operator {op} not supported for field type {spec.type}")


def _combine(results: list[bool | None], operator: str) -> bool | None:
    if operator == "OR":
        if any(r is True for r in results):
            return True
        return None if any(r is None for r in results) else False
    if any(r is False for r in results):
        return False
    return None if any(r is None for r in results) else True


def _effect(action: str, severity: str, unknown_handling: str, cond: bool | None) -> str:
    if cond is None:
        if action in ("weight", "flag", "request_review") and unknown_handling == "keep":
            return "unknown"
        return {"keep": "unknown", "flag": "unknown_flag", "exclude": "blocker"}[unknown_handling]
    if action == "include":
        failed = not cond
    elif action == "exclude":
        failed = cond
    elif action == "weight":
        return "weight" if cond else "pass"
    elif action == "request_review":
        return "review" if cond else "pass"
    else:  # flag
        if not cond:
            return "pass"
        return "info" if severity == "informational" else "flag"
    if not failed:
        return "pass"
    return {"hard": "blocker", "soft": "flag", "informational": "info"}[severity]


def evaluate(rules: Iterable[Any], facts: dict[str, Any], today: date | None = None) -> EvaluationResult:
    specs = sorted((RuleSpec.from_obj(r) for r in rules), key=lambda r: (r.priority, r.name))
    specs = [r for r in specs if r.enabled and r.scope in EVALUATED_SCOPES]

    units: list[list[RuleSpec]] = []
    groups: dict[str, list[RuleSpec]] = {}
    for r in specs:
        if r.logic_group and r.action in ("include", "exclude"):
            if r.logic_group not in groups:
                groups[r.logic_group] = []
                units.append(groups[r.logic_group])
            groups[r.logic_group].append(r)
        else:
            units.append([r])

    result = EvaluationResult()
    for unit in units:
        lead = unit[0]
        conds, evidence, errors = [], [], []
        for r in unit:
            try:
                c, ev = evaluate_condition(r, facts, today)
            except Exception as exc:  # invalid stored rule never crashes evaluation
                c, ev = None, ""
                errors.append(f"{r.name}: {exc}")
            conds.append(c)
            if ev:
                evidence.append(ev)
        if errors:
            result.outcomes.append(RuleOutcome([r.id for r in unit], lead.name, lead.action, lead.severity,
                                               "error", "flag", "; ".join(errors)))
            continue
        cond = conds[0] if len(unit) == 1 else _combine(conds, lead.group_operator)
        effect = _effect(lead.action, lead.severity, lead.unknown_handling, cond)
        name = lead.name if len(unit) == 1 else f"{lead.logic_group}: " + f" {lead.group_operator} ".join(r.name for r in unit)
        expl = " | ".join(r.explanation for r in unit if r.explanation)
        weight = lead.weight if effect == "weight" else 0.0
        result.outcomes.append(RuleOutcome(
            [r.id for r in unit], name, lead.action, lead.severity,
            {True: "true", False: "false", None: "unknown"}[cond], effect, expl, " | ".join(evidence)[:400], weight,
        ))

    result.score = max(0.0, min(100.0, sum(o.weight for o in result.outcomes)))
    effects = {o.effect for o in result.outcomes}
    if "blocker" in effects:
        result.status = "blocked"
    elif effects & {"unknown_flag", "review"}:
        result.status = "needs_verification"
    elif "flag" in effects:
        result.status = "flagged"
    else:
        result.status = "eligible"
    return result


def ruleset_version(rules: Iterable[Any]) -> str:
    items = sorted((RuleSpec.from_obj(r) for r in rules), key=lambda r: r.id)
    basis = json.dumps([[r.id, r.version, r.enabled] for r in items])
    return hashlib.sha256(basis.encode()).hexdigest()[:12]
