"""Connector framework: polite HTTP client, HTML→text, location helpers (spec §6).

Policy (enforced here, not left to each connector):
- GET only, to an explicit host allowlist per connector; no logins, cookies or CAPTCHA tricks.
- Identifies itself with a clear User-Agent; at least ``min_interval`` seconds between requests to a host.
- Retries only on 429/5xx/timeouts with backoff (honouring Retry-After, capped); response size capped.
- All job text is UNTRUSTED: it is converted to plain text and never executed or followed.
"""
from __future__ import annotations

import html
import json
import re
import ssl
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx

from .. import __version__
from ..normalize import COUNTRY_ALIASES

USER_AGENT = f"JobSearchHarness/{__version__} (personal job search; local desktop app; low request rate)"
MAX_RESPONSE_BYTES = 15 * 1024 * 1024
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,79}$")


class ConnectorError(RuntimeError):
    """User-facing (Spanish) connector failure. Never contains secrets."""


@dataclass
class SourceQuery:
    identifier: str = ""  # board token / company slug (validated with IDENTIFIER_RE)
    keywords: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)  # ISO-2 (e.g. CA, MX); provider-specific mapping
    max_results: int = 100


@dataclass
class FetchResult:
    jobs: list[Any] = field(default_factory=list)  # list[JobInput]
    warnings: list[str] = field(default_factory=list)
    requests: int = 0
    total_available: int | None = None
    partial: bool = False  # True when some pages failed after others succeeded


def ssl_context() -> ssl.SSLContext:
    """Trust the public CA bundle AND the OS store (corporate TLS inspection on Windows)."""
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # pragma: no cover - certifi is bundled with httpx
        ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()
    except Exception:  # pragma: no cover
        pass
    return ctx


def ipv4_transport() -> httpx.HTTPTransport:
    """Direct transport that connects over IPv4. Some networks publish IPv6 routes that silently black-hole
    (seen in practice: ~60 s per request to Cloudflare-hosted APIs before falling back), and httpx has no
    "happy eyeballs". Every site this app talks to also serves IPv4."""
    return httpx.HTTPTransport(local_address="0.0.0.0", verify=ssl_context())


class HttpClient:
    def __init__(self, allowed_hosts: set[str], min_interval: float = 1.0, timeout: float = 20.0,
                 retries: int = 2, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.min_interval = min_interval
        self.retries = retries
        self.requests = 0
        self._sleep = sleep
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        kwargs: dict[str, Any] = {"timeout": timeout, "follow_redirects": False,
                                  "headers": {"User-Agent": USER_AGENT, "Accept": "application/json"}}
        if transport is not None:
            kwargs["transport"] = transport
        else:
            kwargs["verify"] = ssl_context()
        self._client = httpx.Client(**kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _wait_turn(self, host: str) -> None:
        with self._lock:
            last = self._last.get(host)
            now = time.monotonic()
            if last is not None and now - last < self.min_interval:
                self._sleep(self.min_interval - (now - last))
            self._last[host] = time.monotonic()

    def get_json(self, url: str, params: dict[str, Any] | list[tuple[str, Any]] | None = None) -> Any:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or host not in self.allowed_hosts:
            raise ConnectorError(f"Destino no permitido para este conector: {host or url[:60]}")
        last_error = ""
        for attempt in range(self.retries + 1):
            self._wait_turn(host)
            self.requests += 1
            try:
                with self._client.stream("GET", url, params=params) as resp:
                    status = resp.status_code
                    headers = resp.headers
                    body = b""
                    if status == 200:
                        declared = headers.get("Content-Length", "")
                        if declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
                            raise ConnectorError("La respuesta de la fuente es demasiado grande.")
                        chunks, size = [], 0
                        for chunk in resp.iter_bytes():  # decoded bytes → also bounds compression bombs
                            size += len(chunk)
                            if size > MAX_RESPONSE_BYTES:
                                raise ConnectorError("La respuesta de la fuente es demasiado grande.")
                            chunks.append(chunk)
                        body = b"".join(chunks)
            except httpx.TimeoutException:
                last_error = "tiempo de espera agotado"
            except httpx.HTTPError as exc:
                last_error = f"error de red ({type(exc).__name__})"
            else:
                resp_status = status
                if resp_status == 200:
                    try:
                        return json.loads(body)
                    except ValueError as exc:
                        raise ConnectorError("La fuente devolvió datos que no son JSON (¿cambió su formato?).") \
                            from exc
                if status in (401, 403):
                    raise ConnectorError(f"La fuente rechazó el acceso (HTTP {status}). No se intenta "
                                         "evadir restricciones; use la importación manual para esta fuente.")
                if status == 404:
                    raise ConnectorError("No encontrado (HTTP 404): revise el identificador del tablero/empresa.")
                if status in (301, 302, 303, 307, 308):
                    raise ConnectorError(f"La fuente redirige a otra dirección (HTTP {status}); "
                                         "revise el identificador.")
                last_error = f"HTTP {status}"
                if status == 429 or status >= 500:
                    retry_after = headers.get("Retry-After", "")
                    delay = float(retry_after) if retry_after.isdigit() else 2.0 * (attempt + 1)
                    if attempt < self.retries:
                        self._sleep(min(delay, 30.0))
                    continue
                raise ConnectorError(f"La fuente respondió con un error ({last_error}).")
            if attempt < self.retries:
                self._sleep(2.0 * (attempt + 1))
        raise ConnectorError(f"No se pudo consultar la fuente después de {self.retries + 1} intentos ({last_error}).")


# ----------------------------------------------------------------------------- HTML → text
class _TextExtractor(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n• ")
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK and tag != "li":
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str | None, max_chars: int = 60_000) -> str:
    if not value:
        return ""
    text = value
    if "&lt;" in text and "<" not in text:
        text = html.unescape(text)  # Greenhouse returns entity-escaped HTML
    parser = _TextExtractor()
    try:
        parser.feed(text[: max_chars * 3])
        parser.close()
        out = "".join(parser.parts)
    except Exception:  # malformed markup: fall back to stripping tags
        out = re.sub(r"<[^>]{0,2000}>", " ", text)
    out = html.unescape(out).replace("\u00a0", " ")
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n\s*\n\s*\n+", "\n\n", out)
    lines = [ln.strip() for ln in out.split("\n")]
    return "\n".join(lines).strip()[:max_chars]


# ----------------------------------------------------------------------------- locations
ISO2_TO_ISO3 = {"CA": "CAN", "MX": "MEX", "US": "USA", "GB": "GBR", "IE": "IRL", "DE": "DEU", "ES": "ESP",
                "FR": "FRA", "IT": "ITA", "PT": "PRT", "NL": "NLD", "PL": "POL", "IN": "IND", "BR": "BRA",
                "CO": "COL", "AR": "ARG", "CL": "CHL", "CR": "CRI", "PE": "PER", "UY": "URY", "JP": "JPN",
                "AU": "AUS", "SG": "SGP", "CN": "CHN", "AE": "ARE", "IL": "ISR", "ZA": "ZAF", "RO": "ROU",
                "CZ": "CZE", "SE": "SWE", "LU": "LUX", "PH": "PHL", "KR": "KOR", "NZ": "NZL", "EG": "EGY"}
REMOTE_RE = re.compile(r"(?i)\b(remote|remoto|anywhere|virtual|work from home|wfh|teletrabajo|home[- ]based)\b")
# Only unambiguous country NAMES are recognised in free text ("CA" in "San Francisco, CA" is California).
# Broad regions are NOT inferred from free text ("North America" often means US/Canada only): they stay unknown.
_FREE_TEXT_EXCLUDED = {"north america", "americas", "américa"}
_NAMES = sorted((k for k in COUNTRY_ALIASES if (len(k) >= 4 or k in ("usa", "mex")) and k not in _FREE_TEXT_EXCLUDED),
                key=len, reverse=True)
_COUNTRY_RE = re.compile(r"(?i)(?<![a-z])(" + "|".join(re.escape(k) for k in _NAMES) + r")(?![a-z])")
# Place names that CONTAIN a country name but are not that country (removed before matching).
_NOT_COUNTRY_RE = re.compile(
    r"(?i)\b(?:new\s+mexico|new\s+england|northern\s+ireland|panama\s+city|nuevo\s+m[eé]xico|"
    r"(?:mexico|peru|chile|lebanon|jordan|canada)\s*,\s*(?:[A-Z]{2}|indiana|illinois|missouri|maine|new\s+york|"
    r"kentucky|ohio|pennsylvania)\b|(?:indiana|georgia|jordan|mexico)\s+(?=beach|city|county)\w+)")
_US_STATE_TAIL = re.compile(r",\s*(?:A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|"
                            r"PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\s*$")


def countries_in(text: str) -> list[str]:
    cleaned = _NOT_COUNTRY_RE.sub(" ", text or "")
    out: list[str] = []
    for m in _COUNTRY_RE.finditer(cleaned):
        code = COUNTRY_ALIASES.get(m.group(1).lower())
        if code and code not in out:
            out.append(code)
    return out


def parse_free_location(text: str, structured_country: str = "") -> dict[str, Any]:
    """Free-text ATS location → job_country / work_mode / remote_eligible_countries (unknown stays empty).

    A structured country from the provider (Lever ``country``, Ashby address) always wins over free text.
    Texts ending in a US state code ("…, NM") are US locations even if they contain a country-like word.
    """
    text = (text or "").strip()
    countries = countries_in(text)
    if _US_STATE_TAIL.search(text):
        countries = ["US"]
    structured = (structured_country or "").upper()
    if len(structured) == 2:
        countries = [structured] + [c for c in countries if c == structured]
    remote = bool(REMOTE_RE.search(text))
    return {
        "location_text": text[:300],
        "job_country": next((c for c in countries if len(c) == 2), ""),
        "work_mode": "remote" if remote else "",
        # "Remote" alone does NOT mean worldwide: only explicitly named countries/regions are recorded.
        "remote_eligible_countries": countries if remote else [],
    }


def keyword_match(keywords: list[str], *texts: str) -> bool:
    if not keywords:
        return True
    hay = " ".join(t or "" for t in texts).lower()
    return any(k.lower() in hay for k in keywords if k.strip())
