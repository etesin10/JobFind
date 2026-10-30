"""Normalization helpers (countries, work mode, URLs, spreadsheet safety)."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

COUNTRY_ALIASES: dict[str, str] = {
    "ca": "CA", "can": "CA", "canada": "CA", "canadá": "CA",
    "mx": "MX", "mex": "MX", "mexico": "MX", "méxico": "MX",
    "us": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US", "united states": "US",
    "united states of america": "US", "estados unidos": "US", "eeuu": "US", "ee.uu.": "US",
    "uk": "GB", "gb": "GB", "united kingdom": "GB",
    "latam": "LATAM", "latin america": "LATAM", "latinoamérica": "LATAM", "latinoamerica": "LATAM",
    "americas": "AMERICAS", "north america": "AMERICAS", "américa": "AMERICAS",
    "worldwide": "WORLDWIDE", "anywhere": "WORLDWIDE", "global": "WORLDWIDE", "remote worldwide": "WORLDWIDE",
    "cualquier lugar": "WORLDWIDE",
    # common country names (Spanish/English) used in job locations
    "colombia": "CO", "argentina": "AR", "chile": "CL", "peru": "PE", "perú": "PE", "brazil": "BR", "brasil": "BR",
    "costa rica": "CR", "uruguay": "UY", "ecuador": "EC", "guatemala": "GT", "panama": "PA", "panamá": "PA",
    "dominican republic": "DO", "república dominicana": "DO", "venezuela": "VE", "bolivia": "BO",
    "paraguay": "PY", "el salvador": "SV", "honduras": "HN", "nicaragua": "NI", "puerto rico": "PR",
    "spain": "ES", "españa": "ES", "portugal": "PT", "germany": "DE", "alemania": "DE", "france": "FR",
    "francia": "FR", "ireland": "IE", "irlanda": "IE", "netherlands": "NL", "países bajos": "NL", "poland": "PL",
    "polonia": "PL", "italy": "IT", "italia": "IT", "india": "IN", "philippines": "PH", "filipinas": "PH",
    "australia": "AU", "japan": "JP", "japón": "JP", "singapore": "SG", "singapur": "SG",
    "united kingdom of great britain": "GB", "reino unido": "GB", "england": "GB",
    "europe": "EUROPE", "european union": "EUROPE", "emea": "EMEA", "europa": "EUROPE",
}

WORK_MODE_ALIASES = {
    "remote": "remote", "remoto": "remote", "fully remote": "remote", "100% remote": "remote",
    "hybrid": "hybrid", "híbrido": "hybrid", "hibrido": "hybrid",
    "onsite": "onsite", "on-site": "onsite", "on site": "onsite", "presencial": "onsite", "in office": "onsite",
}

_TRACKING_PARAMS = re.compile(r"^(utm_.*|gh_src|lever-source.*|source|ref|refid|trk.*|fbclid|gclid)$", re.I)
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def normalize_country(value: str | None) -> str:
    if not value:
        return ""
    v = value.strip()
    key = v.lower()
    if key in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[key]
    if len(v) == 2 and v.isalpha():
        return v.upper()
    return v  # unrecognized → keep as-is (visible to user, never guessed)


def normalize_country_list(values) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = re.split(r"[,;/|\n]", values)
    out: list[str] = []
    for v in values:
        c = normalize_country(str(v))
        if c and c not in out:
            out.append(c)
    return out


def normalize_work_mode(value: str | None) -> str:
    if not value:
        return ""
    return WORK_MODE_ALIASES.get(value.strip().lower(), value.strip().lower())


def normalize_name(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def canonicalize_url(url: str | None) -> str:
    """Lower-case scheme/host, drop fragments and tracking params. Only http(s) allowed."""
    if not url:
        return ""
    url = url.strip()
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError(f"URL no válida (solo http/https): {url[:100]}")
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if not _TRACKING_PARAMS.match(k)])
    path = parts.path.rstrip("/") or ""
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def escape_formula(value):
    """Prevent spreadsheet formula injection for untrusted text written to Excel/CSV."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def unescape_formula(value):
    if isinstance(value, str) and len(value) > 1 and value[0] == "'" and value[1:].startswith(FORMULA_PREFIXES):
        return value[1:]
    return value
