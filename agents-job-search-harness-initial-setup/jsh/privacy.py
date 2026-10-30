"""Scrub personal contact data from CV excerpts that leave the app (AI packages, REST API, MCP).

Résumé headers ("Name | Title | email | phone") are often captured as skill evidence. Unless the user explicitly
opts in, outgoing excerpts that contain an email or phone number are dropped entirely, URLs are removed and the
candidate's own header values (name, email, phone, links, location) are masked.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

_EMAIL = re.compile(r"[^@\s|,;<>()]+@[^@\s|,;<>()]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"\+?\(?\d[\d\s().\-]{6,}\d")
_URL = re.compile(r"(?i)\b(?:https?://|www\.)\S+|\b(?:linkedin|github)\.com/\S*")


def _has_phone(text: str) -> bool:
    return any(len(re.sub(r"\D", "", m.group(0))) >= 9 for m in _PHONE.finditer(text))


def personal_terms(contact: dict[str, Any] | None) -> list[str]:
    contact = contact if isinstance(contact, dict) else {}
    terms = [str(contact.get(k) or "") for k in ("name", "email", "phone", "location")]
    terms += [str(x) for x in (contact.get("links") or [])]
    return [t.strip() for t in terms if t and len(t.strip()) >= 3]


def scrub(text: str, terms: Iterable[str] = ()) -> str:
    """'' when the excerpt looks like a contact header; otherwise the text without URLs or personal terms."""
    if not text:
        return ""
    if _EMAIL.search(text) or _has_phone(text):
        return ""
    out = _URL.sub("[enlace]", text)
    for t in terms:
        out = re.sub(re.escape(t), "[dato personal]", out, flags=re.I)
    return out


def mask(text: str, terms: Iterable[str] = ()) -> str:
    """Keep the text, masking emails, phone numbers, URLs and the user's own header values."""
    if not text:
        return text
    out = _EMAIL.sub("[correo]", text)
    out = _PHONE.sub(lambda m: "[teléfono]" if len(re.sub(r"\D", "", m.group(0))) >= 9 else m.group(0), out)
    out = _URL.sub("[enlace]", out)
    for t in terms:
        out = re.sub(re.escape(t), "[dato personal]", out, flags=re.I)
    return out


def mask_data(value: Any, terms: Iterable[str] = ()) -> Any:
    terms = list(terms)
    if isinstance(value, str):
        return mask(value, terms)
    if isinstance(value, list):
        return [mask_data(v, terms) for v in value]
    if isinstance(value, dict):
        return {k: mask_data(v, terms) for k, v in value.items()}
    return value


def scrub_list(items: Iterable[str], terms: Iterable[str] = ()) -> list[str]:
    terms = list(terms)
    return [s for s in (scrub(x, terms) for x in items) if s]


_SECTION_HEAD = re.compile(
    r"(?i)^\W*(professional\s+summary|summary|profile|about\s+me|objective|career\s+objective|experience|"
    r"work\s+experience|professional\s+experience|employment(\s+history)?|work\s+history|skills|technical\s+skills|"
    r"core\s+competencies|education|resumen(\s+profesional)?|perfil(\s+profesional)?|objetivo|experiencia"
    r"(\s+(laboral|profesional))?|habilidades|competencias|educaci[oó]n|formaci[oó]n)\W*$")


def mask_document(text: str, terms: Iterable[str] = ()) -> str:
    """Full résumé text for AI: the header block before the first section title (name, title, contact line) is
    removed when such a title appears near the top; lines with an email or phone number are removed; URLs and the
    user's header values elsewhere are masked."""
    terms = list(terms)
    lines = (text or "").splitlines()
    for i, line in enumerate(lines[:25]):
        if _SECTION_HEAD.match(line.strip()):
            lines = lines[i:]
            break
    out = []
    for line in lines:
        if _EMAIL.search(line) or _has_phone(line):
            continue
        out.append(mask(line, terms))
    return "\n".join(out)
