"""SQLAlchemy ORM models (SQLite is the source of truth; Excel is a projection).

Separation of concerns (spec §10):
- ``Job`` = the advertisement (shared across modes, never duplicated per mode).
- ``JobModeEvaluation`` = per-mode deterministic evaluation + current triage decision
  + mode-specific facts (e.g. CUSMA evidence for CANADA_VISA).
- ``UserJobDecision`` = append-only triage history (discard/restore/save...).
- ``Application`` / ``ApplicationEvent`` = per-mode application lifecycle.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .tolerant import (
    LenientBoolean, LenientDate, LenientDateTime, LenientFloat, LenientInteger, LenientJSON, LenientString,
    LenientText,
)


def new_id() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: LenientJSON, list[Any]: LenientJSON}


class Mode(Base):
    __tablename__ = "modes"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class SearchProfile(Base):
    __tablename__ = "search_profiles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    mode_id: Mapped[str] = mapped_column(ForeignKey("modes.id"))
    name: Mapped[str] = mapped_column(String(120))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Rule(Base):
    __tablename__ = "rules"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    mode_id: Mapped[str] = mapped_column(ForeignKey("modes.id"))
    profile_id: Mapped[str | None] = mapped_column(ForeignKey("search_profiles.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    scope: Mapped[str] = mapped_column(String(20), default="prefilter")
    field: Mapped[str] = mapped_column(String(80))
    operator: Mapped[str] = mapped_column(String(30))
    value: Mapped[Any] = mapped_column(LenientJSON, nullable=True)
    value_type: Mapped[str] = mapped_column(String(20), default="text")
    logic_group: Mapped[str] = mapped_column(String(80), default="")
    group_operator: Mapped[str] = mapped_column(String(3), default="AND")
    action: Mapped[str] = mapped_column(String(20), default="flag")
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    severity: Mapped[str] = mapped_column(String(20), default="soft")
    unknown_handling: Mapped[str] = mapped_column(String(10), default="flag")
    explanation: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class RuleVersion(Base):
    __tablename__ = "rule_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    rule_id: Mapped[str] = mapped_column(String(32), index=True)  # no FK: history survives deletion
    version: Mapped[int] = mapped_column(Integer)
    change: Mapped[str] = mapped_column(String(20))  # created|updated|deleted
    snapshot: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200), unique=True)
    website: Mapped[str] = mapped_column(String(500), default="")
    ats_provider: Mapped[str] = mapped_column(String(40), default="")
    ats_board_id: Mapped[str] = mapped_column(String(200), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    # v5: employer research / watchlist
    careers_url: Mapped[str] = mapped_column(String(500), default="", server_default="")
    watch: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    legitimacy: Mapped[str] = mapped_column(String(20), default="unknown", server_default="unknown")
    evidence: Mapped[str] = mapped_column(Text, default="", server_default="")  # sources for legitimacy/visa claims
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(200), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    profile_url: Mapped[str] = mapped_column(String(500), default="")
    evidence: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    phone: Mapped[str] = mapped_column(String(60), default="", server_default="")  # v5
    source: Mapped[str] = mapped_column(String(60), default="user", server_default="user")  # v5: user|agent:…|api:…
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # v5
    # v6: a real two-way conversation exists (not just an outgoing message)
    established: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ContactEvent(Base):
    """Interaction log with a contact (v5): email, call, message, meeting, interview, note."""
    __tablename__ = "contact_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    contact_id: Mapped[str] = mapped_column(ForeignKey("contacts.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    mode_id: Mapped[str] = mapped_column(String(40), default="")
    kind: Mapped[str] = mapped_column(String(20), default="note")
    happened_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(60), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(LenientString(32), primary_key=True, default=new_id)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    company_name: Mapped[str] = mapped_column(LenientString(200), default="")
    title: Mapped[str] = mapped_column(LenientString(300))
    job_country: Mapped[str] = mapped_column(LenientString(20), default="")
    region: Mapped[str] = mapped_column(LenientString(120), default="")
    location_text: Mapped[str] = mapped_column(LenientString(300), default="")
    remote_eligible_countries: Mapped[list[Any]] = mapped_column(LenientJSON, default=list)
    work_mode: Mapped[str] = mapped_column(LenientString(20), default="")  # remote|hybrid|onsite|""(unknown)
    seniority: Mapped[str] = mapped_column(LenientString(60), default="")
    employment_type: Mapped[str] = mapped_column(LenientString(60), default="")
    salary_min: Mapped[float | None] = mapped_column(LenientFloat, nullable=True)
    salary_max: Mapped[float | None] = mapped_column(LenientFloat, nullable=True)
    currency: Mapped[str] = mapped_column(LenientString(10), default="")
    salary_period: Mapped[str] = mapped_column(LenientString(20), default="")
    canonical_url: Mapped[str] = mapped_column(LenientString(1000), default="", index=True)
    apply_url: Mapped[str] = mapped_column(LenientString(1000), default="")
    posted_date: Mapped[date | None] = mapped_column(LenientDate, nullable=True)
    last_checked: Mapped[datetime | None] = mapped_column(LenientDateTime, nullable=True)
    ad_status: Mapped[str] = mapped_column(LenientString(20), default="unknown")  # active|expired|unknown
    sponsorship_statement: Mapped[str] = mapped_column(LenientText, default="")
    language: Mapped[str] = mapped_column(LenientString(20), default="")
    description: Mapped[str] = mapped_column(LenientText, default="")  # latest JD text (untrusted)
    notes: Mapped[str] = mapped_column(LenientText, default="")  # user-authored, never overwritten by imports
    mode_tags: Mapped[list[Any]] = mapped_column(LenientJSON, default=list)
    pinned: Mapped[bool] = mapped_column(LenientBoolean, default=False)
    extra: Mapped[dict[str, Any]] = mapped_column(LenientJSON, default=dict)  # unknown import columns, provenance
    row_version: Mapped[int] = mapped_column(LenientInteger, default=1)
    created_at: Mapped[datetime] = mapped_column(LenientDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(LenientDateTime, default=utcnow)

    sources: Mapped[list["JobSource"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    snapshots: Mapped[list["JobSnapshot"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobSource(Base):
    __tablename__ = "job_sources"
    __table_args__ = (UniqueConstraint("provider", "source_job_id", name="uq_provider_source_job"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    provider: Mapped[str] = mapped_column(String(60))
    source_job_id: Mapped[str | None] = mapped_column(String(300), nullable=True)
    url: Mapped[str] = mapped_column(String(1000), default="")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    job: Mapped[Job] = relationship(back_populates="sources")


class JobSnapshot(Base):
    __tablename__ = "job_snapshots"
    __table_args__ = (UniqueConstraint("job_id", "jd_hash", name="uq_job_snapshot_hash"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    jd_text: Mapped[str] = mapped_column(Text)
    jd_hash: Mapped[str] = mapped_column(String(64))
    source_url: Mapped[str] = mapped_column(String(1000), default="")
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    job: Mapped[Job] = relationship(back_populates="snapshots")


class JobModeEvaluation(Base):
    __tablename__ = "job_mode_evaluations"
    __table_args__ = (UniqueConstraint("job_id", "mode_id", name="uq_job_mode"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    mode_id: Mapped[str] = mapped_column(ForeignKey("modes.id"))
    eligibility_status: Mapped[str] = mapped_column(String(30), default="unevaluated")
    technical_score: Mapped[float] = mapped_column(Float, default=0.0)
    explanation: Mapped[list[Any]] = mapped_column(default=list)
    ruleset_version: Mapped[str] = mapped_column(String(64), default="")
    evaluated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    user_decision: Mapped[str] = mapped_column(String(20), default="new")  # new|saved|maybe|discarded
    discard_reason: Mapped[str] = mapped_column(String(60), default="")
    discard_note: Mapped[str] = mapped_column(Text, default="")
    override: Mapped[str] = mapped_column(String(20), default="")  # ""|include|exclude
    override_reason: Mapped[str] = mapped_column(Text, default="")
    mode_data: Mapped[dict[str, Any]] = mapped_column(default=dict)  # mode-specific facts
    # Phase 2: JD vs approved candidate skills (None = unknown: no candidate/skills or no skills found in JD)
    skills_match: Mapped[float | None] = mapped_column(Float, nullable=True)
    skills_detail: Mapped[dict[str, Any] | None] = mapped_column(LenientJSON, nullable=True)
    # v4: hash of the job facts this evaluation used; a mismatch means the job changed outside the app
    facts_hash: Mapped[str] = mapped_column(String(64), default="", server_default="")
    # v5: summary of the ACCEPTED AI review for this job+mode (None = none accepted); feeds ai_* rule fields
    ai_review: Mapped[dict[str, Any] | None] = mapped_column(LenientJSON, nullable=True)


class UserJobDecision(Base):
    __tablename__ = "user_job_decisions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    mode_id: Mapped[str] = mapped_column(ForeignKey("modes.id"))
    decision: Mapped[str] = mapped_column(String(20))
    previous_decision: Mapped[str] = mapped_column(String(20), default="")
    reason_code: Mapped[str] = mapped_column(String(60), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(60), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (UniqueConstraint("job_id", "mode_id", name="uq_application_job_mode"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    mode_id: Mapped[str] = mapped_column(ForeignKey("modes.id"))
    status: Mapped[str] = mapped_column(String(40), default="discovered")
    date_applied: Mapped[date | None] = mapped_column(Date, nullable=True)
    next_followup: Mapped[date | None] = mapped_column(Date, nullable=True)
    channel: Mapped[str] = mapped_column(String(60), default="")
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("contacts.id"), nullable=True)
    resume_version_id: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ApplicationEvent(Base):
    __tablename__ = "application_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    application_id: Mapped[str] = mapped_column(ForeignKey("applications.id"))
    from_status: Mapped[str] = mapped_column(String(40), default="")
    to_status: Mapped[str] = mapped_column(String(40))
    note: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(60), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(20))  # csv|xlsx
    file_name: Mapped[str] = mapped_column(String(500))
    mode_id: Mapped[str] = mapped_column(String(40), default="")
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    backup_file: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(32), index=True)
    action: Mapped[str] = mapped_column(String(60))
    mode_id: Mapped[str] = mapped_column(String(40), default="")
    actor: Mapped[str] = mapped_column(String(60), default="user")
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[Any] = mapped_column(LenientJSON, nullable=True)


class DeletedJob(Base):
    """Tombstone of a vacancy the USER deleted permanently (v6): connectors, agents and imports never re-create it.
    Only identifiers are kept (no description); adding it again by hand in the UI clears the tombstone."""
    __tablename__ = "deleted_jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    company_name: Mapped[str] = mapped_column(String(200), default="")
    canonical_url: Mapped[str] = mapped_column(String(1000), default="", index=True)
    source_keys: Mapped[list[Any]] = mapped_column(default=list)  # ["provider|source_job_id", …]
    deleted_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- v7: organisation & insights
class JobTag(Base):
    """Free-form USER tags on a vacancy (shared across modes). Unique per job, case-insensitive."""
    __tablename__ = "job_tags"
    __table_args__ = (UniqueConstraint("job_id", "normalized", name="uq_job_tag"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    tag: Mapped[str] = mapped_column(String(40))
    normalized: Mapped[str] = mapped_column(String(40), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SavedView(Base):
    """A named filter for a mode (e.g. 'Canadá · Toronto · puntaje > 60 · sin aplicar')."""
    __tablename__ = "saved_views"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    mode_id: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(120))
    filters: Mapped[dict[str, Any]] = mapped_column(default=dict)
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FxRate(Base):
    """1 unit of ``currency`` = ``rate`` units of ``base`` on ``as_of`` (source ecb|manual)."""
    __tablename__ = "fx_rates"
    __table_args__ = (UniqueConstraint("base", "currency", name="uq_fx_pair"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    base: Mapped[str] = mapped_column(String(3))
    currency: Mapped[str] = mapped_column(String(3))
    rate: Mapped[float] = mapped_column(Float)
    as_of: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class JobLiveness(Base):
    """Latest ad-liveness check of a vacancy's URL. A FLAG only: never changes ad_status or deletes anything."""
    __tablename__ = "job_liveness"
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # live|likely_closed|unknown
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_url: Mapped[str] = mapped_column(String(1000), default="")
    reason: Mapped[str] = mapped_column(String(300), default="")
    closed_streak: Mapped[int] = mapped_column(Integer, default=0)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)  # user said "it's still open"


class JobInbox(Base):
    """Write channel for EXTERNAL agents (schema v4).

    An agent only needs ``INSERT INTO job_inbox(payload) VALUES ('{json}')``; every other column has
    a SQL default. The app validates, de-duplicates, evaluates and audits each row, then records the
    outcome here (status imported|error). Rows are read with raw SQL so a malformed row cannot break
    the app."""
    __tablename__ = "job_inbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payload: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(80), default="agent", server_default="agent")
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending", index=True)
    result: Mapped[str] = mapped_column(String(20), default="", server_default="")  # created|updated|unchanged
    job_id: Mapped[str] = mapped_column(String(32), default="", server_default="")
    message: Mapped[str] = mapped_column(Text, default="", server_default="")  # error or warnings
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, server_default=func.current_timestamp())
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# --------------------------------------------------------------------------- Phase 2: candidate & résumé
# Everything extracted from a résumé is a PROPOSAL (status="proposed") until the user approves it.
# Only approved + active skills are used for JD matching and future tailoring.

class CandidateProfile(Base):
    """A candidate persona, e.g. 'Data Engineer' or 'Non-tech'. Each mode can use a different one."""
    __tablename__ = "candidate_profiles"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    version: Mapped[int] = mapped_column(Integer, default=1)  # bumps on every approved-profile change
    # v5: personal header for generated résumés (name, email, phone, location, links, headline). PERSONAL DATA:
    # never included in AI packages unless the user explicitly opts in.
    contact: Mapped[dict[str, Any]] = mapped_column(LenientJSON, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ResumeSource(Base):
    """An imported résumé. The original file is copied unchanged to resumes/originals/ (sha256 recorded)."""
    __tablename__ = "resume_sources"
    __table_args__ = (UniqueConstraint("candidate_id", "sha256", name="uq_resume_source_hash"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate_profiles.id"))
    file_name: Mapped[str] = mapped_column(String(300))
    kind: Mapped[str] = mapped_column(String(10))  # docx|pdf|txt|paste
    stored_path: Mapped[str] = mapped_column(String(500), default="")  # relative to the portable home
    sha256: Mapped[str] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text, default="")  # extracted plain text (for review/audit)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    summary: Mapped[dict[str, Any]] = mapped_column(default=dict)
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Skill(Base):
    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("candidate_id", "normalized", name="uq_skill_candidate_name"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate_profiles.id"))
    name: Mapped[str] = mapped_column(String(120))
    normalized: Mapped[str] = mapped_column(String(120))
    vocab_key: Mapped[str] = mapped_column(String(120), default="")  # built-in vocabulary entry, if any
    category: Mapped[str] = mapped_column(String(40), default="other")
    aliases: Mapped[list[Any]] = mapped_column(default=list)  # user-editable synonyms used for matching
    status: Mapped[str] = mapped_column(String(12), default="proposed")  # proposed|approved|rejected
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    tag: Mapped[str] = mapped_column(String(12), default="")  # ""|core|preferred
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    proficiency: Mapped[str] = mapped_column(String(40), default="")  # only when the user states it
    years: Mapped[float | None] = mapped_column(Float, nullable=True)  # only when the user states it
    confidence: Mapped[str] = mapped_column(String(10), default="")  # extraction confidence high|medium|low
    origin: Mapped[str] = mapped_column(String(20), default="resume")  # resume|manual|excel
    notes: Mapped[str] = mapped_column(Text, default="")  # e.g. résumé qualifier "(integration experience)"
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class SkillEvidence(Base):
    __tablename__ = "skill_evidence"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    skill_id: Mapped[str] = mapped_column(ForeignKey("skills.id"))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("resume_sources.id"), nullable=True)
    section: Mapped[str] = mapped_column(String(30), default="")  # experience|skills|summary|...|manual
    excerpt: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ProfileItem(Base):
    """Proposed/approved résumé facts: position, education, certification, language, summary."""
    __tablename__ = "profile_items"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate_profiles.id"))
    source_id: Mapped[str | None] = mapped_column(ForeignKey("resume_sources.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))
    dedupe_key: Mapped[str] = mapped_column(String(300), default="")
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    status: Mapped[str] = mapped_column(String(12), default="proposed")  # proposed|approved|rejected
    confidence: Mapped[str] = mapped_column(String(10), default="")
    excerpt: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)  # order in the source
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ResumeVersion(Base):
    """Approved snapshots: kind=master (Phase 2) or tailored (Phase 5, per job)."""
    __tablename__ = "resume_versions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate_profiles.id"))
    kind: Mapped[str] = mapped_column(String(12), default="master")
    version: Mapped[int] = mapped_column(Integer, default=1)
    name: Mapped[str] = mapped_column(String(200), default="")
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    content: Mapped[dict[str, Any]] = mapped_column(default=dict)
    provenance: Mapped[dict[str, Any]] = mapped_column(default=dict)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- Phase 3: discovery
class CompanySource(Base):
    """A saved job search on one source (watchlist entry), e.g. Amazon + 'data engineer' + CA."""
    __tablename__ = "company_sources"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(40))  # amazon_jobs|greenhouse|lever|ashby
    identifier: Mapped[str] = mapped_column(String(80), default="")  # ATS board token / site
    keywords: Mapped[list[Any]] = mapped_column(default=list)
    countries: Mapped[list[Any]] = mapped_column(default=list)  # ISO-2 codes
    mode_id: Mapped[str] = mapped_column(String(40), default="")  # mode tag for imported jobs ("" = none)
    max_results: Mapped[int] = mapped_column(Integer, default=100)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(20), default="")  # ok|partial|error
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ConnectorRun(Base):
    __tablename__ = "connector_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("company_sources.id", ondelete="SET NULL"),
                                                  nullable=True)
    provider: Mapped[str] = mapped_column(String(40))
    source_name: Mapped[str] = mapped_column(String(200), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|ok|partial|error
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    created: Mapped[int] = mapped_column(Integer, default=0)
    updated: Mapped[int] = mapped_column(Integer, default=0)
    unchanged: Mapped[int] = mapped_column(Integer, default=0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    total_available: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    job_ids: Mapped[list[Any]] = mapped_column(default=list)  # jobs created/updated by this run


# --------------------------------------------------------------------------- Phase 4/5: AI handoff & tailoring
# The app never calls a model. External agents (file packages, REST API, MCP) return structured results that are
# schema-validated, stored as RECEIVED/PROPOSED and only take effect after an explicit user action.

class PromptTemplate(Base):
    """Editable instructions embedded in AI packages (kind review|tailoring)."""
    __tablename__ = "prompt_templates"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))  # review|tailoring
    mode_id: Mapped[str] = mapped_column(String(40), default="")  # "" = all modes
    body: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AIPackage(Base):
    """An exported AI package (what was shared, for echo validation and audit)."""
    __tablename__ = "ai_packages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)  # package_id
    kind: Mapped[str] = mapped_column(String(20))  # review|tailoring
    mode_id: Mapped[str] = mapped_column(String(40), default="")
    job_ids: Mapped[list[Any]] = mapped_column(default=list)
    jd_hashes: Mapped[dict[str, Any]] = mapped_column(default=dict)  # job_id -> jd_hash at export time
    candidate_id: Mapped[str] = mapped_column(String(32), default="")
    profile_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ruleset_version: Mapped[str] = mapped_column(String(64), default="")
    template_key: Mapped[str] = mapped_column(String(60), default="")
    template_version: Mapped[int] = mapped_column(Integer, default=1)
    include_personal: Mapped[bool] = mapped_column(Boolean, default=False)
    extra: Mapped[dict[str, Any]] = mapped_column(default=dict)  # e.g. master_version_id for tailoring
    actor: Mapped[str] = mapped_column(String(60), default="user")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AIReview(Base):
    """A structured job review returned by an external AI (spec §7). status: received|accepted|rejected."""
    __tablename__ = "ai_reviews"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    mode_id: Mapped[str] = mapped_column(String(40))
    package_id: Mapped[str] = mapped_column(String(32), default="")
    jd_hash: Mapped[str] = mapped_column(String(64), default="")
    stale: Mapped[bool] = mapped_column(Boolean, default=False)  # the JD changed since the package was exported
    provider: Mapped[str] = mapped_column(String(80), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    source: Mapped[str] = mapped_column(String(20), default="file")  # file|api|mcp
    status: Mapped[str] = mapped_column(String(12), default="received")
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    actor: Mapped[str] = mapped_column(String(60), default="")
    decided_by: Mapped[str] = mapped_column(String(60), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TailoringRun(Base):
    """A résumé tailoring proposal for one job (deterministic or from an external AI). status: proposed|approved|
    discarded. Approval creates a ResumeVersion(kind=tailored) with full provenance."""
    __tablename__ = "tailoring_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    mode_id: Mapped[str] = mapped_column(String(40), default="")
    candidate_id: Mapped[str] = mapped_column(String(32), default="")
    master_version_id: Mapped[str] = mapped_column(String(32), default="")
    package_id: Mapped[str] = mapped_column(String(32), default="")
    source: Mapped[str] = mapped_column(String(20), default="deterministic")  # deterministic|file|api|mcp
    provider: Mapped[str] = mapped_column(String(80), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(12), default="proposed")
    proposal: Mapped[dict[str, Any]] = mapped_column(default=dict)  # validated changes + automatic flags
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    approved_change_ids: Mapped[list[Any]] = mapped_column(default=list)
    resume_version_id: Mapped[str] = mapped_column(String(32), default="")
    actor: Mapped[str] = mapped_column(String(60), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)