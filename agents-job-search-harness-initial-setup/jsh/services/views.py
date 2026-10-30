"""Saved views: named, per-mode filters over job rows (e.g. "Canadá · Toronto · puntaje ≥ 60 · sin aplicar").

Filters are validated with pydantic and applied in memory to the rows produced by ``JobService.rows`` so they
always see exactly what the tables show (normalised salary, tags, liveness flag, application status…).
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator
from sqlalchemy import select

from ..i18n import t
from ..models import AuditEvent, SavedView, utcnow
from ..modes import ACTIVE_PIPELINE_STATUSES
from .jobs import fold

VIEWS = ("activos", "all", "discarded", "blocked")


class ViewFilters(BaseModel):
    view: Literal["", "activos", "all", "discarded", "blocked"] = ""
    q: str = Field("", max_length=300)
    countries: list[str] = Field(default_factory=list)
    location: str = Field("", max_length=120)
    work_modes: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    min_score: float | None = Field(None, ge=0, le=1000)
    min_skills: float | None = Field(None, ge=0, le=100)
    min_salary: float | None = Field(None, ge=0)  # annual, in the base currency (normalised)
    salary_unknown_ok: bool = True
    application: Literal["any", "none", "applied", "active"] = "any"
    decisions: list[str] = Field(default_factory=list)
    eligibility: list[str] = Field(default_factory=list)
    tags_any: list[str] = Field(default_factory=list)
    exclude_closed: bool = False
    posted_within_days: int | None = Field(None, ge=1, le=3650)

    model_config = {"extra": "ignore"}

    @field_validator("countries", "work_modes", "sources", "decisions", "eligibility", "tags_any", mode="before")
    @classmethod
    def _listify(cls, v: Any) -> list[str]:
        if v is None or v == "":
            return []
        if isinstance(v, str):
            v = v.split(",")
        return [str(x).strip() for x in v if str(x).strip()][:50]

    @field_validator("countries", mode="after")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [x.upper() for x in v]


def validate_filters(filters: dict[str, Any] | ViewFilters | None) -> ViewFilters:
    if isinstance(filters, ViewFilters):
        return filters
    try:
        return ViewFilters.model_validate(filters or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ValueError(f"Filtro inválido ({'.'.join(str(p) for p in first['loc'])}): {first['msg']}") from None


def matches(row: dict[str, Any], filters: dict[str, Any] | ViewFilters) -> bool:
    """True when the row passes every set filter (``view`` and ``q`` are applied by JobService.rows)."""
    f = validate_filters(filters)
    if f.countries and str(row.get("job_country") or "").upper() not in f.countries:
        return False
    if f.location:
        hay = fold(f"{row.get('location_text') or ''} {row.get('region') or ''}")
        if fold(f.location) not in hay:
            return False
    if f.work_modes and str(row.get("work_mode") or "") not in f.work_modes:
        return False
    if f.sources and str(row.get("source") or "") not in f.sources:
        return False
    if f.min_score is not None and (row.get("technical_score") or 0) < f.min_score:
        return False
    if f.min_skills is not None and (row.get("skills_match") or 0) < f.min_skills:
        return False
    if f.min_salary is not None:
        top = row.get("salary_annual_max") if row.get("salary_annual_max") is not None else row.get(
            "salary_annual_min")
        if top is None:
            if not f.salary_unknown_ok:
                return False
        elif top < f.min_salary:
            return False
    status = str(row.get("application_status") or "")
    if f.application == "none" and status not in ("", "discovered"):
        return False
    if f.application == "applied" and status in ("", "discovered"):
        return False
    if f.application == "active" and status not in ACTIVE_PIPELINE_STATUSES:
        return False
    if f.decisions and str(row.get("user_decision") or "new") not in f.decisions:
        return False
    if f.eligibility and str(row.get("eligibility_status") or "") not in f.eligibility:
        return False
    if f.tags_any:
        have = {fold(t.strip()) for t in str(row.get("tags") or "").split(",") if t.strip()}
        if not have & {fold(t) for t in f.tags_any}:
            return False
    if f.exclude_closed and (row.get("ad_status") == "expired" or row.get("liveness") == "likely_closed"):
        return False
    if f.posted_within_days is not None:
        posted = str(row.get("posted_date") or "")[:10]
        try:
            if (date.today() - date.fromisoformat(posted)).days > f.posted_within_days:
                return False
        except ValueError:
            return False
    return True


def summarize(filters: dict[str, Any] | ViewFilters) -> list[str]:
    """Short chips describing the active filters (in the interface language)."""
    f = validate_filters(filters)
    out: list[str] = []
    if f.view:
        out.append(t("vista: {}", t({"activos": "Activos", "all": "Todas", "blocked": "Bloqueadas", "discarded": "Descartadas"}[f.view])))
    if f.q:
        out.append(f'"{f.q}"')
    if f.countries:
        out.append("/".join(f.countries))
    if f.location:
        out.append(f"📍{f.location}")
    if f.work_modes:
        out.append("/".join(f.work_modes))
    if f.sources:
        out.append("/".join(f.sources))
    if f.min_score is not None:
        out.append(t("puntaje ≥ {:g}", f.min_score))
    if f.min_skills is not None:
        out.append(t("skills ≥ {:g}%", f.min_skills))
    if f.min_salary is not None:
        out.append(t("salario ≥ {:,.0f}", f.min_salary))
    if f.application != "any":
        out.append(t({"none": "sin aplicar", "applied": "aplicada", "active": "proceso activo"}[f.application]))
    if f.decisions:
        out.append("/".join(f.decisions))
    if f.eligibility:
        out.append("/".join(f.eligibility))
    if f.tags_any:
        out.append(" ".join(f"#{t}" for t in f.tags_any))
    if f.exclude_closed:
        out.append(t("sin cerradas"))
    if f.posted_within_days is not None:
        out.append(t("≤ {} días", f.posted_within_days))
    return out


class ViewService:
    def __init__(self, app):
        self.app = app
        self.db = app.db

    def list(self, mode_id: str) -> list[dict[str, Any]]:
        with self.db.session() as s:
            return [self._dict(v) for v in s.scalars(select(SavedView).where(SavedView.mode_id == mode_id)
                                                      .order_by(SavedView.position, SavedView.name))]

    def get(self, view_id: str) -> dict[str, Any]:
        with self.db.session() as s:
            v = s.get(SavedView, view_id)
            if v is None:
                raise KeyError(view_id)
            return self._dict(v)

    def find(self, mode_id: str, id_or_name: str) -> dict[str, Any] | None:
        key = fold(id_or_name or "")
        for v in self.list(mode_id):
            if v["id"] == id_or_name or fold(v["name"]) == key:
                return v
        return None

    def save(self, mode_id: str, name: str, filters: dict[str, Any], view_id: str | None = None,
             actor: str = "user") -> str:
        name = " ".join((name or "").split())[:120]
        if not name:
            raise ValueError("La vista necesita un nombre.")
        data = validate_filters(filters).model_dump(exclude_defaults=True)
        with self.db.session() as s:
            dup = s.scalar(select(SavedView).where(SavedView.mode_id == mode_id, SavedView.name == name))
            if dup is not None and dup.id != view_id:
                raise ValueError(f"Ya existe una vista llamada «{name}».")
            if view_id:
                v = s.get(SavedView, view_id)
                if v is None:
                    raise KeyError(view_id)
                v.name, v.filters, v.updated_at = name, data, utcnow()
            else:
                pos = len(list(s.scalars(select(SavedView.id).where(SavedView.mode_id == mode_id))))
                v = SavedView(mode_id=mode_id, name=name, filters=data, position=pos)
                s.add(v)
            s.flush()
            s.add(AuditEvent(entity_type="saved_view", entity_id=v.id, action="saved", mode_id=mode_id,
                             actor=actor[:60], details={"name": name, "filters": data}))
            s.commit()
            return v.id

    def delete(self, view_id: str, actor: str = "user") -> None:
        with self.db.session() as s:
            v = s.get(SavedView, view_id)
            if v is None:
                return
            s.add(AuditEvent(entity_type="saved_view", entity_id=v.id, action="deleted", mode_id=v.mode_id,
                             actor=actor[:60], details={"name": v.name}))
            s.delete(v)
            s.commit()
        for mode in ("CANADA_VISA", "MEXICO_REMOTE"):
            if self.selected(mode) == view_id:
                self.select(mode, "")

    def move(self, view_id: str, delta: int) -> None:
        with self.db.session() as s:
            v = s.get(SavedView, view_id)
            if v is None:
                return
            views = list(s.scalars(select(SavedView).where(SavedView.mode_id == v.mode_id)
                                   .order_by(SavedView.position, SavedView.name)))
            i = next(k for k, x in enumerate(views) if x.id == view_id)
            j = max(0, min(len(views) - 1, i + delta))
            views.insert(j, views.pop(i))
            for k, x in enumerate(views):
                x.position = k
            s.commit()

    # the UI remembers the selected view per mode
    def selected(self, mode_id: str) -> str:
        return str(self.db.get_setting(f"selected_view_{mode_id}", "") or "")

    def select(self, mode_id: str, view_id: str) -> None:
        self.db.set_setting(f"selected_view_{mode_id}", view_id or "")

    def rows(self, mode_id: str, view_id_or_name: str = "", search: str = "", view: str = "") -> list[dict]:
        """Rows of a saved view (``view``/``search`` arguments override/extend the stored ones)."""
        filters: dict[str, Any] = {}
        if view_id_or_name:
            found = self.find(mode_id, view_id_or_name)
            if found is None:
                raise KeyError(f"Vista no encontrada: {view_id_or_name}")
            filters = dict(found["filters"])
        f = validate_filters(filters)
        return self.app.jobs.rows(mode_id, view or f.view or "all", search, filters=f.model_dump())

    @staticmethod
    def _dict(v: SavedView) -> dict[str, Any]:
        return {"id": v.id, "mode_id": v.mode_id, "name": v.name, "filters": dict(v.filters or {}),
                "position": v.position, "summary": summarize(v.filters or {})}
