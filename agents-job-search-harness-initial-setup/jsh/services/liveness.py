"""Ad-liveness check: are saved job URLs still open? A FLAG for the user — it never changes ad_status or deletes.

Safety (the URLs come from untrusted job data):
- http/https only, standard ports; every hop's host must resolve ONLY to public (global) IP addresses — no
  localhost, private networks, link-local or metadata endpoints (SSRF guard). Redirects are followed manually
  (max 5), each one re-validated.
- GET with a clear User-Agent, no cookies or credentials, 15 s timeout, body read capped at 400 KB,
  at least 2 s between requests to the same host. Only the job URL is requested; nothing about the user is sent.

Classification:
- 404/410 → likely_closed · 200 with a "no longer available" phrase (EN/ES) → likely_closed
- redirected to a generic careers/search page or an ATS error page → likely_closed
- 200 otherwise → live · 401/403/429/5xx, network errors, blocked URLs → unknown (never flagged)
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
import threading
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from sqlalchemy import select

from ..connectors.base import USER_AGENT, html_to_text, ipv4_transport, ssl_context
from ..models import AuditEvent, Job, JobLiveness, utcnow
from .jobs import fold

log = logging.getLogger(__name__)

ENABLED_KEY, INTERVAL_KEY, BATCH_KEY, LAST_RUN_KEY = ("liveness_enabled", "liveness_interval_hours",
                                                      "liveness_batch", "liveness_last_run")
MAX_BYTES = 400 * 1024
MAX_REDIRECTS = 5
TIMEOUT = 15.0
HOST_INTERVAL = 2.0
CLOSED_PHRASES = [fold(p) for p in (
    "no longer available", "no longer accepting applications", "no longer accepting applicants",
    "this job has expired", "this job posting has expired", "job has been closed", "this position has been filled",
    "this position is no longer", "this job is closed", "the job you are looking for is no longer",
    "this requisition is no longer", "job posting is closed", "applications are closed", "position has been closed",
    "this role has been filled", "job not found",
    "ya no está disponible", "ya no esta disponible", "la vacante ha expirado", "esta vacante ha sido cerrada",
    "ya no acepta postulaciones", "ya no acepta solicitudes", "la oferta ha caducado", "oferta no disponible",
    "la posición ha sido cubierta", "vacante cerrada", "esta oferta ya no",
)]
GENERIC_PATHS = re.compile(r"^/?([a-z]{2}([-_][a-z]{2})?/)?(careers?|jobs?|search|opportunities|vacantes|empleos|"
                           r"job-search|search-jobs|careers/search|jobs/search|en|es|home|index\.html?)?/?$",
                           re.IGNORECASE)


class BlockedUrl(ValueError):
    """The URL is not allowed (scheme, port or a non-public address)."""


def _default_resolver(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


def check_url_allowed(url: str, resolver: Callable[[str], list[str]] = _default_resolver) -> None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise BlockedUrl("URL mal formada.") from None
    if parts.scheme not in ("http", "https"):
        raise BlockedUrl("Solo se permiten URLs http/https.")
    if parts.username or parts.password:
        raise BlockedUrl("URL con credenciales no permitida.")
    host = (parts.hostname or "").strip(".").lower()
    if not host:
        raise BlockedUrl("URL sin host.")
    if port not in (None, 80, 443):
        raise BlockedUrl("Puerto no estándar no permitido.")
    try:
        addrs = [host] if _is_ip(host) else resolver(host)
    except (OSError, UnicodeError, ValueError) as exc:
        raise BlockedUrl(f"No se pudo resolver el host ({exc.__class__.__name__}).") from None
    if not addrs:
        raise BlockedUrl("El host no tiene direcciones.")
    for a in addrs:
        _require_public(a)


def _require_public(address: str) -> None:
    try:
        ip = ipaddress.ip_address(str(address).split("%")[0])
    except ValueError:
        raise BlockedUrl("Dirección IP inválida.") from None
    if not ip.is_global or ip.is_multicast:
        raise BlockedUrl("El host apunta a una red privada/local: no se verifica.")


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def classify(original_url: str, final_url: str, status: int | None, text: str) -> tuple[str, str]:
    """(status, reason) — pure function."""
    if status is None:
        return "unknown", text or "sin respuesta"
    if status in (404, 410):
        return "likely_closed", f"HTTP {status} (no encontrada)"
    if status != 200:
        return "unknown", f"HTTP {status}"
    o, f = urlsplit(original_url), urlsplit(final_url)
    if final_url != original_url:
        q = parse_qs(f.query)
        if q.get("error") == ["true"] or "error=true" in f.query:
            return "likely_closed", "Redirigida a una página de error del portal"
        if GENERIC_PATHS.match(f.path or "/") and not GENERIC_PATHS.match(o.path or "/"):
            return "likely_closed", f"Redirigida a una página genérica ({f.netloc}{f.path or '/'})"
    folded = fold(text[:200_000])
    for phrase in CLOSED_PHRASES:
        if phrase in folded:
            return "likely_closed", f"La página dice «{phrase}»"
    return "live", "HTTP 200"


class LivenessService:
    def __init__(self, app, transport: httpx.BaseTransport | None = None,
                 resolver: Callable[[str], list[str]] = _default_resolver, sleep=time.sleep):
        self.app = app
        self.db = app.db
        self.transport = transport
        self.resolver = resolver
        self._sleep = sleep
        self._last_host: dict[str, float] = {}
        self._lock = threading.Lock()
        self.running = False

    # ------------------------------------------------------------------ settings
    def settings(self) -> dict[str, Any]:
        return {"enabled": bool(self.db.get_setting(ENABLED_KEY, False)),
                "interval_hours": int(self.db.get_setting(INTERVAL_KEY, 24) or 24),
                "batch": int(self.db.get_setting(BATCH_KEY, 25) or 25),
                "last_run": str(self.db.get_setting(LAST_RUN_KEY, "") or "")}

    def configure(self, enabled: bool | None = None, interval_hours: int | None = None,
                  batch: int | None = None) -> None:
        if enabled is not None:
            self.db.set_setting(ENABLED_KEY, bool(enabled))
        if interval_hours is not None:
            if not 1 <= int(interval_hours) <= 24 * 30:
                raise ValueError("Intervalo fuera de rango (1–720 horas).")
            self.db.set_setting(INTERVAL_KEY, int(interval_hours))
        if batch is not None:
            if not 1 <= int(batch) <= 200:
                raise ValueError("Lote fuera de rango (1–200).")
            self.db.set_setting(BATCH_KEY, int(batch))

    # ------------------------------------------------------------------ fetching
    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = self._last_host.get(host, 0.0) + HOST_INTERVAL - time.monotonic()
            self._last_host[host] = time.monotonic() + max(0.0, wait)
        if wait > 0:
            self._sleep(wait)

    def fetch(self, url: str) -> tuple[int | None, str, str]:
        """(http_status | None, final_url, text-or-error). Never raises for network problems."""
        current = url
        try:
            # trust_env=False: direct connections only, so the peer address below is the real server (not a proxy)
            with httpx.Client(timeout=TIMEOUT, verify=ssl_context(), follow_redirects=False,
                              transport=self.transport or ipv4_transport(), trust_env=False,
                              headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml",
                                       "Accept-Language": "en,es;q=0.8"}) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    check_url_allowed(current, self.resolver)
                    self._throttle(urlsplit(current).hostname or "")
                    with client.stream("GET", current) as resp:
                        # DNS-rebinding guard: the address actually connected to must also be public
                        stream = resp.extensions.get("network_stream")
                        peer = stream.get_extra_info("server_addr") if stream is not None else None
                        if peer:
                            _require_public(peer[0])
                        if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                            current = urljoin(current, resp.headers["location"])
                            continue
                        body = b""
                        for chunk in resp.iter_bytes():
                            body += chunk
                            if len(body) >= MAX_BYTES:
                                break
                        ctype = resp.headers.get("content-type", "")
                        raw = body[:MAX_BYTES].decode(resp.encoding or "utf-8", errors="replace")
                        text = html_to_text(raw, max_chars=200_000) if "html" in ctype or "<" in raw[:200] else raw
                        return resp.status_code, current, text
                return None, current, "Demasiadas redirecciones"
        except BlockedUrl as exc:
            return None, current, f"Bloqueada: {exc}"
        except (httpx.HTTPError, httpx.InvalidURL, ValueError, UnicodeError) as exc:
            return None, current, f"Error de red ({exc.__class__.__name__})"

    # ------------------------------------------------------------------ checks
    def check_job(self, job_id: str, actor: str = "liveness") -> dict[str, Any]:
        with self.db.session() as s:
            job = s.get(Job, job_id)
            if job is None:
                raise KeyError(job_id)
            url = job.canonical_url or job.apply_url or ""
        if not url.startswith(("http://", "https://")):
            status, reason, http, final = "unknown", "La vacante no tiene URL http(s)", None, ""
        else:
            http, final, text = self.fetch(url)
            status, reason = classify(url, final, http, text)
        return self._store(job_id, status, reason[:300], http, final[:1000], actor)

    def _store(self, job_id: str, status: str, reason: str, http: int | None, final: str,
               actor: str) -> dict[str, Any]:
        with self.db.session() as s:
            lv = s.get(JobLiveness, job_id)
            if lv is None:
                lv = JobLiveness(job_id=job_id, closed_streak=0, dismissed=False)
                s.add(lv)
            was_flagged = lv.status == "likely_closed" and not lv.dismissed
            lv.status, lv.reason, lv.http_status, lv.final_url, lv.checked_at = status, reason, http, final, utcnow()
            if status == "likely_closed":
                lv.closed_streak = (lv.closed_streak or 0) + 1
            elif status == "live":
                lv.closed_streak, lv.dismissed = 0, False
            if status == "likely_closed" and not lv.dismissed and not was_flagged:
                s.add(AuditEvent(entity_type="job", entity_id=job_id, action="ad_likely_closed", actor=actor[:60],
                                 details={"reason": reason, "http_status": http}))
            s.commit()
            return {"job_id": job_id, "status": status, "reason": reason, "http_status": http,
                    "final_url": final, "closed_streak": lv.closed_streak, "dismissed": lv.dismissed}

    def dismiss(self, job_id: str, actor: str = "user") -> None:
        """User: "it's still open" — hide the flag until a later check finds it live again then closed."""
        with self.db.session() as s:
            lv = s.get(JobLiveness, job_id)
            if lv is None:
                return
            lv.dismissed = True
            s.add(AuditEvent(entity_type="job", entity_id=job_id, action="liveness_dismissed", actor=actor[:60],
                             details={}))
            s.commit()

    def mark_expired(self, job_ids: list[str], actor: str = "user") -> int:
        """User confirmed: set ad_status=expired (re-evaluates; nothing is deleted)."""
        return sum(1 for jid in job_ids if self.app.jobs.set_ad_status(jid, "expired", actor=actor))

    def due_jobs(self, limit: int | None = None, interval_hours: int | None = None) -> list[str]:
        st = self.settings()
        limit = st["batch"] if limit is None else limit
        cutoff = utcnow() - timedelta(hours=st["interval_hours"] if interval_hours is None else interval_hours)
        with self.db.session() as s:
            jobs = s.execute(select(Job.id, Job.canonical_url, Job.apply_url, Job.ad_status)).all()
            checked = {lv.job_id: lv.checked_at for lv in s.scalars(select(JobLiveness))}
        due = [(checked.get(jid), jid) for jid, u, a, ad in jobs
               if ad != "expired" and (u or a or "").startswith(("http://", "https://"))
               and (checked.get(jid) is None or checked[jid] < cutoff)]
        due.sort(key=lambda x: (x[0] is not None, x[0] or utcnow()))
        return [jid for _, jid in due[:max(0, limit)]]

    def run_batch(self, limit: int | None = None, actor: str = "liveness",
                  progress: Callable[[int, int], None] | None = None,
                  interval_hours: int | None = None) -> dict[str, Any]:
        if self.running:
            return {"checked": 0, "likely_closed": 0, "live": 0, "unknown": 0, "busy": True}
        self.running = True
        try:
            ids = self.due_jobs(limit, interval_hours)
            counts = {"checked": 0, "likely_closed": 0, "live": 0, "unknown": 0}
            for i, jid in enumerate(ids):
                try:
                    res = self.check_job(jid, actor)
                except KeyError:
                    continue  # deleted meanwhile
                except Exception as exc:  # one odd URL must never block the queue
                    log.warning("Liveness check failed for %s: %s", jid, exc)
                    res = self._store(jid, "unknown", f"Error inesperado ({exc.__class__.__name__})", None, "",
                                      actor)
                counts["checked"] += 1
                counts[res["status"]] += 1
                if progress:
                    progress(i + 1, len(ids))
            self.db.set_setting(LAST_RUN_KEY, utcnow().isoformat(timespec="seconds"))
            return counts
        finally:
            self.running = False

    def status(self, job_id: str) -> dict[str, Any]:
        """Latest check of one job ({} when never checked). ``status`` is 'dismissed' when the user hid the flag."""
        with self.db.session() as s:
            lv = s.get(JobLiveness, job_id)
            if lv is None:
                return {}
            st = "dismissed" if lv.dismissed and lv.status == "likely_closed" else lv.status
            return {"status": st, "reason": lv.reason, "http_status": lv.http_status,
                    "checked_at": lv.checked_at.isoformat(timespec="minutes"), "closed_streak": lv.closed_streak}

    def flagged(self) -> list[dict[str, Any]]:
        with self.db.session() as s:
            rows = s.execute(select(JobLiveness, Job.title, Job.company_name, Job.canonical_url)
                             .join(Job, Job.id == JobLiveness.job_id)
                             .where(JobLiveness.status == "likely_closed", JobLiveness.dismissed.is_(False),
                                    Job.ad_status != "expired")).all()
        return [{"job_id": lv.job_id, "title": t, "company_name": c, "url": u, "reason": lv.reason,
                 "checked_at": lv.checked_at.isoformat(timespec="minutes"), "closed_streak": lv.closed_streak}
                for lv, t, c, u in rows]
