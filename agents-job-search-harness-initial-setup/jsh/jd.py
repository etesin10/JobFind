"""JD intelligence: a deterministic, explainable "flat" view of a job description (no AI).

``analyze_jd(text)`` returns plain JSON-able data that a human, the rule engine or an external AI agent can read:
- ``flat_text``: normalised plain text (bullets unified, whitespace collapsed).
- ``sections``: headings detected in the JD, each classified as required | preferred | responsibilities |
  benefits | about | other.
- ``keywords``: recognised skills (built-in vocabulary) with their importance — ``required`` when they appear
  under a requirements heading, ``preferred`` under nice-to-have, otherwise ``mentioned`` — plus count and a
  JD snippet as evidence.
- ``years_experience``, ``education``, ``work_authorization``, ``work_mode_hints``, ``salary_mentions``.

Everything is a HINT extracted from untrusted text; nothing here is a legal or factual determination.
"""
from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from typing import Any

from .candidate.vocabulary import BY_KEY, CATEGORY_LABELS, find_vocab, fold, phrase_regex, snippet

MAX_TEXT = 100_000
IMPORTANCE_ORDER = {"required": 0, "preferred": 1, "mentioned": 2}
SECTION_LABELS = {"required": "Requisitos", "preferred": "Deseables", "responsibilities": "Responsabilidades",
                  "benefits": "Beneficios / compensación", "about": "Sobre la empresa / el puesto",
                  "other": "Otro"}

_BULLET = re.compile(r"^\s*(?:[•·▪◦●○■□➢➤►\-–—*]|\d{1,2}[.)])\s+")
# Order matters: "preferred qualifications" must win over "qualifications".
_HEADINGS: tuple[tuple[str, re.Pattern[str]], ...] = tuple((kind, re.compile(rx)) for kind, rx in (
    ("preferred", r"^(?:preferred|desired|desirable|nice[ -]to[ -]haves?|bonus(?: points)?|pluses|plus|"
                  r"additional (?:qualifications|skills)|it would be (?:great|nice)|extra credit|deseables?|"
                  r"valorables?|se valora(?:ra)?|(?:es )?un plus|preferentemente)\b"),
    ("required", r"^(?:basic qualifications|minimum qualifications|required qualifications|qualifications|"
                 r"requirements|required skills|required experience|must[ -]haves?|what you(?:'| wi)?ll need|"
                 r"what you need|what we(?:'re| are) looking for|who you are|you have|you bring|about you|"
                 r"skills (?:and|&) experience|experience|requisitos|requerimientos|que buscamos|perfil|"
                 r"lo que necesitas|conocimientos|experiencia requerida)\b"),
    ("responsibilities", r"^(?:responsibilities|key (?:job )?responsibilities|what you(?:'| wi)?ll do|"
                         r"what you will be doing|the role|your role|in this role|role description|duties|"
                         r"day[ -]to[ -]day|your impact|responsabilidades|funciones|que haras|actividades)\b"),
    ("benefits", r"^(?:benefits|perks|what we offer|compensation|salary|pay (?:range|transparency)|"
                 r"total rewards|ofrecemos|beneficios|prestaciones|sueldo|salario)\b"),
    ("about", r"^(?:about (?:us|the (?:company|team|role|job|position))|who we are|company description|"
              r"job description|description|overview|the opportunity|sobre nosotros|acerca de|quienes somos|"
              r"descripcion)\b"),
))
_YEARS = re.compile(r"(?i)(?<![\w.])(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to|a)\s*(\d{1,2})\s*\+?\s*)?"
                    r"(?:years?|yrs?|años|anos)\b")
_EDU = re.compile(r"(?i)\b(?:bachelor'?s?|master'?s?|ph\.?d|doctorate|mba|degree|diploma|licenciatura|"
                  r"ingenier[ií]a|maestr[ií]a|titulad[oa]|carrera (?:en|de))\b|(?<![A-Za-z])(?:BS|BA|MS|MSc|BSc)"
                  r"(?![A-Za-z])")
_AUTH = re.compile(r"(?i)\b(?:sponsor(?:ship|ed|ing)?|visas?|work permits?|work authori[sz]ation|authori[sz]ed to work|"
                   r"eligible to work|right to work|lmia|cusma|usmca|citizen(?:ship)?|permanent resident|"
                   r"security clearance|permiso de trabajo|autorizaci[oó]n para trabajar)\b")
_AUTH_NEG = re.compile(r"(?i)\b(?:not|no|unable|cannot|can't|won't|will not|without|sin)\b")
_MODE = re.compile(r"(?i)\b(?:fully remote|remote[- ]first|remote|hybrid|on[- ]?site|in[- ]office|in office|"
                   r"work from home|wfh|relocat(?:e|ion)|remoto|h[ií]brido|presencial|home office)\b")
_MONEY = re.compile(r"(?i)(?:US\$|CA\$|C\$|MX\$|\$|€|£|\b(?:USD|CAD|MXN|EUR|GBP)\s?)\s?\d[\d,.]*\s?[kK]?"
                    r"(?:\s?(?:-|–|to|a)\s?(?:US\$|CA\$|C\$|MX\$|\$|€|£|(?:USD|CAD|MXN|EUR|GBP)\s?)?\s?\d[\d,.]*"
                    r"\s?[kK]?)?(?:\s?(?:USD|CAD|MXN|EUR|GBP))?")


def jd_hash(text: str) -> str:
    """Same hash as JobSnapshot.jd_hash (whitespace-insensitive)."""
    return hashlib.sha256(" ".join((text or "").split()).encode("utf-8")).hexdigest()


def flatten(text: str) -> str:
    """Normalised plain text: one bullet style ('- '), collapsed spaces, at most one blank line in a row."""
    out: list[str] = []
    blank = False
    for raw in (text or "")[:MAX_TEXT].replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw.replace("\u00a0", " ").split())
        if not line:
            if out and not blank:
                out.append("")
            blank = True
            continue
        blank = False
        if _BULLET.match(line):
            line = "- " + _BULLET.sub("", line, count=1)
        out.append(line)
    return "\n".join(out).strip()


def _heading(line: str) -> tuple[str, str, str] | None:
    """(kind, rest-of-line, heading) when ``line`` is a section heading, e.g. 'Requirements:' or 'BASIC QUALIFICATIONS'."""
    clean = line.lstrip("#*_ ").rstrip("*_ ")
    head, sep, rest = clean.partition(":")
    candidate = fold(head).strip(" .-–—")
    if not candidate or len(candidate) > 60 or len(candidate.split()) > 7:
        return None
    if not sep and clean.endswith((".", ",", ";")):
        return None
    for kind, rx in _HEADINGS:
        m = rx.match(candidate)
        if m:
            tail = candidate[m.end():].strip()
            # "Experience with Spark" is a sentence, not a heading: allow only short connector tails.
            if tail and not re.fullmatch(r"(?:and |& |y )?(?:\w+ ?){0,3}", tail):
                return None
            if kind == "required" and candidate.startswith(("experience", "perfil")) and not sep \
                    and candidate not in ("experience", "perfil", "experience and skills"):
                return None
            return kind, rest.strip(), head.strip(" #*_")
    return None


def split_sections(flat: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = [{"kind": "other", "heading": "", "lines": [], "start": 0}]
    pos = 0
    for line in flat.split("\n"):
        h = _heading(line) if line and not line.startswith("- ") else None
        if h:
            sections.append({"kind": h[0], "heading": h[2], "lines": [], "start": pos})
            if h[1]:
                sections[-1]["lines"].append(h[1])
        elif line:
            sections[-1]["lines"].append(line)
        pos += len(line) + 1
    return [{"kind": s["kind"], "label": SECTION_LABELS[s["kind"]], "heading": s["heading"], "start": s["start"],
             "text": "\n".join(s["lines"])} for s in sections if s["lines"] or s["heading"]]


def _hits(rx: re.Pattern[str], text: str, width: int = 60, limit: int = 8) -> list[tuple[re.Match, str]]:
    out = []
    for m in rx.finditer(text):
        out.append((m, snippet(text, m.start(), m.end(), width)))
        if len(out) >= limit:
            break
    return out


def _section_of(sections: list[dict[str, Any]], needle_folded_rx: re.Pattern[str]) -> str:
    """Highest-importance section kind whose text matches (required > preferred > anything else)."""
    best = "mentioned"
    for sec in sections:
        if sec["kind"] in ("required", "preferred") and needle_folded_rx.search(fold(sec["text"])):
            if sec["kind"] == "required":
                return "required"
            best = "preferred"
    return best


@lru_cache(maxsize=256)
def _analyze(text: str) -> dict[str, Any]:
    flat = flatten(text)
    sections = split_sections(flat)
    structured = any(s["kind"] in ("required", "preferred") for s in sections)
    folded = fold(flat)

    keywords = []
    for key, start, end in find_vocab(flat):
        entry = BY_KEY[key]
        rx = phrase_regex([*entry.aliases, key]) if not entry.patterns else None
        count = max(1, len(rx.findall(folded))) if rx else 1
        importance = "mentioned"
        if structured:
            if rx is not None:
                importance = _section_of(sections, rx)
            else:  # pattern-only entries (Excel, React…): use the sections where find_vocab sees them
                kinds = {sec["kind"] for sec in sections if sec["kind"] in ("required", "preferred")
                         and any(k == key for k, *_ in find_vocab(sec["text"]))}
                importance = "required" if "required" in kinds else ("preferred" if kinds else "mentioned")
        keywords.append({"name": key, "category": entry.category,
                         "category_label": CATEGORY_LABELS.get(entry.category, entry.category),
                         "kind": entry.kind, "importance": importance, "count": count,
                         "evidence": snippet(flat, start, end, 60)})
    keywords.sort(key=lambda k: (IMPORTANCE_ORDER[k["importance"]], -k["count"], k["name"].lower()))

    def kind_at(pos: int) -> str:
        found = "other"
        for sec in sections:
            if sec["start"] <= pos:
                found = sec["kind"]
            else:
                break
        return found

    years = []
    for m, snip in _hits(_YEARS, flat):
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else None
        if 0 < lo <= 40 and (hi is None or lo <= hi <= 40):
            years.append({"min": lo, "max": hi, "section": kind_at(m.start()), "evidence": snip})
    auth = []
    for m, snip in _hits(_AUTH, flat):
        window = flat[max(0, m.start() - 40):m.end() + 25]
        auth.append({"term": m.group(0), "negative_wording": bool(_AUTH_NEG.search(window)), "evidence": snip})
    return {
        "hash": jd_hash(text), "chars": len(flat), "flat_text": flat, "structured": structured,
        "sections": sections, "keywords": keywords,
        "required_keywords": [k["name"] for k in keywords if k["importance"] == "required"],
        "preferred_keywords": [k["name"] for k in keywords if k["importance"] == "preferred"],
        "years_experience": years,
        "education": [{"term": m.group(0), "evidence": s} for m, s in _hits(_EDU, flat, limit=5)],
        "work_authorization": auth,
        "work_mode_hints": [{"term": m.group(0).lower(), "evidence": s} for m, s in _hits(_MODE, flat, limit=6)],
        "salary_mentions": [{"text": m.group(0).strip(), "evidence": s} for m, s in _hits(_MONEY, flat, limit=4)
                            if re.search(r"\d{2}", m.group(0))],
        "note": "Extracción automática del texto del anuncio (no confiable): verifique contra la JD.",
    }


def analyze_jd(text: str) -> dict[str, Any]:
    """Deterministic JD analysis. Returns a NEW dict each call (safe to modify)."""
    import copy
    return copy.deepcopy(_analyze(text or ""))


def importance_of(analysis: dict[str, Any], phrases: list[str]) -> str:
    """Importance of a skill (given its name/aliases) inside an analysed JD."""
    rx = phrase_regex(phrases)
    if rx is None or not analysis.get("structured"):
        return "mentioned"
    return _section_of(analysis["sections"], rx)


def to_markdown(doc: dict[str, Any]) -> str:
    """Human/AI-readable Markdown of a job document built by ``JDService.document``."""
    job, jd, match = doc["job"], doc["jd"], doc.get("match") or {}
    lines = [f"# {job['title']} — {job['company_name'] or 'Empresa desconocida'}", ""]
    facts = [("job_id", job["job_id"]), ("URL", job["canonical_url"]), ("Aplicar", job["apply_url"]),
             ("Ubicación", ", ".join(x for x in (job["location_text"], job["region"], job["job_country"]) if x)),
             ("Modalidad", job["work_mode"] or "desconocida"),
             ("Remoto desde", ", ".join(job["remote_eligible_countries"]) or "desconocido"),
             ("Salario", " ".join(str(x) for x in (job["salary_min"], job["salary_max"], job["currency"],
                                                   job["salary_period"]) if x not in (None, ""))),
             ("Publicada", job["posted_date"]), ("Fuente", job["source"]), ("Estado del anuncio", job["ad_status"]),
             ("Patrocinio (texto)", job["sponsorship_statement"]), ("jd_hash", jd["hash"])]
    lines += [f"- **{k}:** {v}" for k, v in facts if v]
    ev = doc.get("evaluation") or {}
    if ev:
        lines += ["", f"## Evaluación determinista ({doc.get('mode_id', '')})",
                  f"- Elegibilidad: {ev.get('eligibility_status')} · Puntaje: {ev.get('technical_score')} · "
                  f"Skills CV: {ev.get('skills_match') if ev.get('skills_match') is not None else '—'}"]
        lines += [f"- {o['effect']}: {o['name']}" + (f" — {o['explanation']}" if o.get("explanation") else "")
                  for o in ev.get("explanation", []) if o.get("effect") not in ("pass", "info")][:15]
    lines += ["", "## Palabras clave de la JD"]
    for imp, label in (("required", "Requeridas"), ("preferred", "Deseables"), ("mentioned", "Mencionadas")):
        names = [f"{k['name']}" + (f" ×{k['count']}" if k["count"] > 1 else "") for k in jd["keywords"]
                 if k["importance"] == imp]
        if names:
            lines.append(f"- **{label}:** {', '.join(names)}")
    if jd["years_experience"]:
        lines.append("- **Años de experiencia:** " + "; ".join(
            f"{y['min']}{'-' + str(y['max']) if y['max'] else '+'} ({y['section']})" for y in jd["years_experience"]))
    for key, label in (("education", "Educación"), ("work_authorization", "Autorización/visa (pistas)"),
                       ("salary_mentions", "Salario en el texto")):
        if jd[key]:
            lines.append(f"- **{label}:** " + " | ".join(x.get("evidence") or x.get("text", "") for x in jd[key][:3]))
    if match:
        lines += ["", f"## Coincidencia con el CV aprobado ({match.get('pct') if match.get('pct') is not None else '—'}%)"]
        if match.get("matched"):
            lines.append("- **Tiene (aprobado):** " + ", ".join(
                f"{m['name']} [{m.get('importance', 'mentioned')}]" for m in match["matched"]))
        if match.get("missing"):
            lines.append("- **Falta:** " + ", ".join(
                f"{m['name']} [{m.get('importance', 'mentioned')}]" for m in match["missing"]))
    lines += ["", "## Descripción (texto plano)", "", jd["flat_text"] or "_Sin JD guardada._", ""]
    return "\n".join(lines)
