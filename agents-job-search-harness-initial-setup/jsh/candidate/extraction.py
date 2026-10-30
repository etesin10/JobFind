"""Deterministic extraction of PROPOSED profile data from résumé lines.

Nothing here is trusted automatically: every skill/item is returned as a proposal with the
source excerpt, the résumé section it came from and a confidence level, for user approval.

Confidence:
- skills: high = appears in work experience (used in a role); medium = skills list/summary;
  low = only elsewhere, or a skills-list term not in the built-in vocabulary.
- items: medium when the line matched a clear pattern (e.g. "Title | Company | Jan 2020 – Present"),
  low when it was inferred from layout.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .parsing import Line
from .vocabulary import BY_KEY, find_vocab, fold, norm_phrase, snippet, vocab_exact

SECTION_KEYS: dict[str, tuple[str, ...]] = {
    "summary": ("summary", "professional summary", "profile", "professional profile", "about me", "objective",
                "career summary", "career objective", "resumen", "resumen profesional", "perfil",
                "perfil profesional", "objetivo", "objetivo profesional", "acerca de mi", "sobre mi"),
    "experience": ("experience", "professional experience", "work experience", "employment history",
                   "employment", "work history", "career history", "relevant experience", "experiencia",
                   "experiencia profesional", "experiencia laboral", "historial laboral",
                   "trayectoria profesional"),
    "skills": ("skills", "technical skills", "core competencies", "competencies", "key skills",
               "areas of expertise", "expertise", "technologies", "tools", "tech stack", "skills and tools",
               "technical proficiencies", "habilidades", "habilidades tecnicas", "competencias", "aptitudes",
               "conocimientos", "conocimientos tecnicos", "herramientas", "tecnologias"),
    "education": ("education", "academic background", "education and training", "educacion", "formacion",
                  "formacion academica", "estudios", "academic"),
    "certifications": ("certifications", "certification", "certificates", "licenses", "licenses and certifications",
                       "certifications and training", "training", "courses", "certificaciones", "certificados",
                       "cursos", "capacitacion", "licencias"),
    "languages": ("languages", "language", "idiomas", "lenguas", "language skills"),
    "projects": ("projects", "proyectos", "selected projects", "key projects"),
    "awards": ("awards", "achievements", "accomplishments", "honors", "logros", "reconocimientos", "premios"),
    "other": ("volunteer", "volunteering", "voluntariado", "references", "referencias", "interests", "intereses",
              "publications", "publicaciones"),
}
_SECTION_LOOKUP = {k: sec for sec, keys in SECTION_KEYS.items() for k in keys}
_SECTION_WORDS = {"summary": "summary", "experience": "experience", "skills": "skills", "education": "education",
                  "certifications": "certifications", "languages": "languages", "experiencia": "experience",
                  "habilidades": "skills", "educacion": "education", "certificaciones": "certifications",
                  "idiomas": "languages", "competencias": "skills", "projects": "projects"}

_MONTHS = (r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
           r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?|"
           r"ene(?:ro)?|febrero|marzo|abr(?:il)?|mayo|junio|julio|ago(?:sto)?|septiembre|setiembre|"
           r"octubre|noviembre|dic(?:iembre)?")
_DATE = rf"(?:(?:{_MONTHS})\.?\s+(?:de\s+)?)?(?:19|20)\d{{2}}|\d{{1,2}}/(?:19|20)\d{{2}}"
_END = rf"{_DATE}|present|current|now|today|ongoing|presente|actual|actualidad|la fecha|hoy"
DATE_RANGE = re.compile(rf"(?i)(?P<start>{_DATE})\s*(?:–|—|-|to|a|al|hasta)\s*(?P<end>{_END})"
                        r"(?:\s*\((?P<note>[^)]{1,60})\))?")
SINGLE_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_MONTH_NUM = {"jan": 1, "ene": 1, "feb": 2, "mar": 3, "apr": 4, "abr": 4, "may": 5, "jun": 6, "jul": 7,
              "aug": 8, "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dec": 12, "dic": 12}
_INSTITUTION = re.compile(r"(?i)universi|college|institut|polit[eé]cnic|tecnol[oó]gico|school|escuela|academ|"
                          r"conalep|unam|ipn|itesm|uvm|campus")
_QUANT = re.compile(r"\d+\s*%|\$\s?\d|\b\d{2,}\b|\b\d+(?:\.\d+)?\s*(?:k|m|mm|million|millones|x)\b", re.I)
_SEPARATORS = (r"\s+\|\s+", r"\s+[—–]\s+", r"\s+-\s+", r"\s+(?:at|en)\s+", r"\s*,\s*")
MAX_LINE = 2000  # longer lines are truncated before item splitting (protects against pathological input)


def split_items(text: str, separators: str = ",;|•·") -> list[str]:
    """Split on separators that are NOT inside parentheses. Single linear pass (no regex lookahead)."""
    items, buf, depth = [], [], 0
    for ch in text[:MAX_LINE]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if depth == 0 and ch in separators:
            items.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    items.append("".join(buf))
    return [i.strip(" .") for i in items if i.strip(" .")]


def normalize_date(text: str) -> str:
    t = fold(text).strip().rstrip(".")
    if re.fullmatch(r"present|current|now|today|ongoing|presente|actual|actualidad|la fecha|hoy", t):
        return "present"
    m = re.search(r"(\d{1,2})/((?:19|20)\d{2})", t)
    if m:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    year = SINGLE_YEAR.search(t)
    month = re.match(r"([a-z]{3})", t)
    if year and month and month.group(1) in _MONTH_NUM:
        return f"{year.group(0)}-{_MONTH_NUM[month.group(1)]:02d}"
    return year.group(0) if year else text.strip()


def section_of(line: Line) -> str | None:
    text = line.text.strip()
    if len(text) > 50 or line.bullet:
        return None
    key = re.sub(r"[^a-z ]+", " ", fold(text)).strip()
    key = re.sub(r"\s+", " ", key)
    if key in _SECTION_LOOKUP:
        return _SECTION_LOOKUP[key]
    words = key.split()
    if 1 <= len(words) <= 4 and (text.isupper() or line.heading or text.endswith(":")):
        for w in words:
            if w in _SECTION_WORDS:
                return _SECTION_WORDS[w]
    return None


@dataclass
class ProposedEvidence:
    section: str
    excerpt: str


@dataclass
class ProposedSkill:
    name: str
    vocab_key: str
    category: str
    confidence: str
    evidence: list[ProposedEvidence] = field(default_factory=list)
    notes: str = ""


@dataclass
class ProposedItem:
    kind: str  # position|education|certification|language|summary
    data: dict[str, Any]
    excerpt: str
    confidence: str
    dedupe_key: str
    position: int


@dataclass
class Extraction:
    skills: list[ProposedSkill]
    items: list[ProposedItem]
    sections_found: list[str]


def _segments(text: str) -> list[str]:
    for sep in _SEPARATORS:
        parts = [p.strip(" ,;|") for p in re.split(sep, text) if p.strip(" ,;|")]
        if len(parts) >= 2:
            return parts
    return [text.strip()]


def _parse_position(line: str) -> dict[str, Any] | None:
    m = DATE_RANGE.search(line)
    if not m:
        return None
    rest = (line[:m.start()] + " " + line[m.end():]).strip(" |,–—-")
    rest = re.sub(r"\s*\|\s*$", "", rest)
    parts = _segments(rest) if rest else []
    data: dict[str, Any] = {"title": parts[0] if parts else "", "company": parts[1] if len(parts) > 1 else "",
                            "location": parts[2] if len(parts) > 2 else "",
                            "start": normalize_date(m.group("start")), "end": normalize_date(m.group("end")),
                            "dates_text": m.group(0).strip(), "highlights": []}
    if m.group("note"):
        data["end_note"] = m.group("note").strip()
    return data


def _parse_education(line: str) -> dict[str, Any]:
    parts = _segments(line)
    year = ""
    rest = []
    for p in parts:
        y = SINGLE_YEAR.search(p)
        if y and len(p) <= 12:
            year = y.group(0)
        else:
            rest.append(p)
    inst = next((p for p in rest if _INSTITUTION.search(p)), "")
    degree = " — ".join(p for p in rest if p != inst)
    if not year:
        y = SINGLE_YEAR.search(line)
        year = y.group(0) if y else ""
    return {"degree": degree or (rest[0] if rest and not inst else ""), "institution": inst, "year": year}


def _parse_languages(line: str) -> list[dict[str, str]]:
    out = []
    for part in split_items(line, ",;|•")[:20]:
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(?P<lang>[^()\-–:]+?)\s*(?:[(\-–:]\s*(?P<level>[^)]+)\)?)?$", part)
        if not m:
            continue
        lang = m.group("lang").strip()
        hits = find_vocab(lang)
        canonical = next((k for k, *_ in hits if BY_KEY[k].category == "language"), "")
        if canonical or len(lang.split()) <= 3:
            out.append({"language": canonical or lang, "level": (m.group("level") or "").strip(),
                        "as_written": part})
    return out


def _skill_items(line: str) -> tuple[str, list[str]]:
    """'Cloud & Data Platforms: Azure, Snowflake (x)' → ('Cloud & Data Platforms', ['Azure', 'Snowflake (x)'])."""
    label = ""
    body = line[:MAX_LINE]
    m = re.match(r"^\s*([^:]{2,45}):\s+(.+)$", body)
    if m:
        label, body = m.group(1).strip(), m.group(2)
    return label, split_items(body)[:200]


def extract(lines: list[Line]) -> Extraction:
    section = "header"
    sections_found: list[str] = []
    skills: dict[str, ProposedSkill] = {}
    unknown: dict[str, ProposedSkill] = {}
    items: list[ProposedItem] = []
    summary_lines: list[str] = []
    current_pos: ProposedItem | None = None
    pending_title: str | None = None

    def add_skill(key: str, sec: str, excerpt: str, note: str = "") -> None:
        entry = BY_KEY[key]
        sk = skills.get(key)
        if sk is None:
            sk = skills[key] = ProposedSkill(entry.key, entry.key, entry.category, "low")
        if len(sk.evidence) < 6 and all(e.excerpt != excerpt for e in sk.evidence):
            sk.evidence.append(ProposedEvidence(sec, excerpt))
        rank = {"experience": 3, "projects": 3, "languages": 3, "skills": 2, "summary": 2,
                "certifications": 2}.get(sec, 1)
        current = {"high": 3, "medium": 2, "low": 1}[sk.confidence]
        if rank > current:
            sk.confidence = {3: "high", 2: "medium", 1: "low"}[rank]
        if note and not sk.notes:
            sk.notes = note

    for idx, line in enumerate(lines):
        text = line.text.strip()[:MAX_LINE]
        sec = section_of(line)
        if sec:
            section = sec
            sections_found.append(sec)
            current_pos = None
            pending_title = None
            continue

        # ---- skills: built-in vocabulary anywhere (with the section as evidence context)
        if section == "skills":
            label, entries = _skill_items(text)
            for entry in entries:
                qualifier = ""
                q = re.search(r"\(([^)]{2,60})\)\s*$", entry)
                if q:
                    qualifier = q.group(1).strip()
                base = re.sub(r"\s*\([^)]*\)\s*$", "", entry).strip()
                # A qualifier describes the skill ("integration experience", "MongoDB API"); terms inside it are
                # NOT separate claims, so only the base text is matched and the qualifier becomes a note.
                hits = list(find_vocab(base))
                exact = vocab_exact(base)
                if exact and all(k != exact for k, *_ in hits):
                    hits.append((exact, 0, len(base)))  # isolated list item like "R" or "Go"
                for key, *_ in hits:
                    same = qualifier and any(k == key for k, *_ in find_vocab(qualifier))
                    add_skill(key, "skills", text if len(text) <= 240 else entry, "" if same else qualifier)
                if not hits and 2 <= len(base) <= 60 and len(base.split()) <= 6:
                    nk = norm_phrase(base)
                    if nk and nk not in unknown and not base.endswith("."):
                        unknown[nk] = ProposedSkill(base, "", "other", "low",
                                                    [ProposedEvidence("skills", text[:240])], qualifier)
                        if label:
                            unknown[nk].notes = (f"{label}" + (f" · {qualifier}" if qualifier else ""))[:200]
        else:
            for key, start, end in find_vocab(text):
                add_skill(key, section, snippet(text, start, end, 110))

        # ---- items per section
        if section == "summary":
            summary_lines.append(text)
        elif section == "experience":
            pos = None if line.bullet else _parse_position(text)
            if pos:
                if not pos["company"] and pending_title:
                    pos["company"], pos["title"] = pos["title"], pending_title
                current_pos = ProposedItem("position", pos, text, "medium" if pos["title"] else "low",
                                           norm_phrase(f"{pos['title']}|{pos['company']}|{pos['start']}"), idx)
                items.append(current_pos)
                pending_title = None
            elif current_pos is not None and (line.bullet or len(text) > 50):
                current_pos.data["highlights"].append({"text": text, "quantified": bool(_QUANT.search(text))})
            elif not line.bullet and len(text) <= 80:
                pending_title = text  # possible title line followed by a "Company | dates" line
        elif section == "education":
            data = _parse_education(text)
            if data["degree"] or data["institution"]:
                items.append(ProposedItem("education", data, text, "medium" if data["institution"] else "low",
                                          norm_phrase(f"{data['degree']}|{data['institution']}"), idx))
        elif section == "certifications":
            y = SINGLE_YEAR.search(text)
            items.append(ProposedItem("certification", {"name": re.sub(r"\s*[|,(]?\s*(?:19|20)\d{2}\)?\s*$", "", text),
                                                        "year": y.group(0) if y else ""},
                                      text, "medium", norm_phrase(text), idx))
        elif section == "languages":
            for lang in _parse_languages(text):
                items.append(ProposedItem("language", lang, text, "medium", norm_phrase(lang["language"]), idx))

    if summary_lines:
        joined = " ".join(summary_lines)
        items.insert(0, ProposedItem("summary", {"text": joined}, joined[:240], "medium", "summary", 0))

    known_names = {norm_phrase(s.name) for s in skills.values()}
    extra = [s for k, s in unknown.items() if k not in known_names]
    ordered = sorted(skills.values(), key=lambda s: ({"high": 0, "medium": 1, "low": 2}[s.confidence], s.name.lower()))
    return Extraction(ordered + sorted(extra, key=lambda s: s.name.lower()), items, sections_found)
